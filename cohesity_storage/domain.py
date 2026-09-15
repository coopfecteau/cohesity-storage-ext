"""Cohesity REST responses turned into the handful of facts the extension actually uses.

Pure data and pure functions - no HTTP, no Dynatrace, no clock except one passed in. Every
parsing rule here can therefore be exercised against a recorded body, which matters more than
usual on this extension: none of these shapes has been seen on a real cluster yet. They are
read out of the published 6.8-7.4 schemas (ticket 04) and will be corrected against a capture.

Three rules that cost someone a day each if rediscovered:

*`dataPoints[]` entries carry no `value`.* They carry `int64Value` / `doubleValue` /
`stringValue`, all nullable, selected by the sibling `type` enum on the metric. `dp["value"]`
raises KeyError on the very first call.

*Run outcomes must be deduplicated on `run.id`.* `/v2/data-protect/runs/summary` has no
pagination and no job filter, only a time window, so an overlapping poll window re-returns the
same completed run. Without :class:`RunLedger` every failure is counted once per overlap.

*Ids are cluster-scoped int64s.* The same id means different things on two clusters, so every
object id is namespaced against the cluster it came from before it leaves the extension.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass
from typing import Any

# A run in one of these statuses has not finished. It will be returned again by a later poll
# with its real outcome, so counting it now would attribute the wrong status to the run.
# Anything *not* listed is treated as terminal on purpose: Cohesity's status enum has grown
# across versions, and counting an unrecognised outcome once (dimensioned by its raw string)
# beats silently dropping it forever. Either way the ledger guarantees one count per run.
NON_TERMINAL_RUN_STATUSES = frozenset(
    {"Accepted", "Running", "Canceling", "Finalizing", "OnHold", "Paused", "LegalHold"}
)

# How many run ids the ledger remembers. Sized for ~500 jobs on a 15-minute schedule with a
# 15-minute window, which is four windows of headroom. INFERRED from ticket 04, not measured.
RUN_LEDGER_CAPACITY = 2000

# Leading numeric components of a Cohesity softwareVersion such as "7.3.1_u2_release-20250104".
_VERSION_PREFIX = re.compile(r"^(\d+(?:\.\d+)*)")


def namespace_id(cluster_id: str | int, object_id: str | int) -> str:
    """Namespace a cluster-scoped Cohesity id so it stays unique across clusters.

    v1 monitors one cluster, but the ids are int64s scoped to a cluster and collide the moment
    a second one is added. Doing this from day one costs nothing; retrofitting it orphans every
    entity that was already minted.
    """
    return f"{cluster_id}_{object_id}"


def parse_version(software_version: str | None) -> tuple[int, ...]:
    """Leading numeric part of a Cohesity version string, for comparison.

    Returns an empty tuple when nothing numeric can be read, which callers must treat as
    "version unknown" rather than "version zero".
    """
    if not software_version:
        return ()
    match = _VERSION_PREFIX.match(str(software_version).strip())
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


# ---------------------------------------------------------------------------
# Time series
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DataPoint:
    """One sample. ``value`` is None when the cluster published the point but not a number."""

    timestamp_msecs: int | None
    value: float | int | str | None


@dataclass(frozen=True)
class TimeSeriesMetric:
    metric_name: str
    value_type: str
    data_points: tuple[DataPoint, ...] = ()

    def latest(self) -> DataPoint | None:
        """The most recent point that actually carried a value.

        Points are returned oldest-first and the tail is the most likely to be null (the
        cluster has flushed the bucket but not yet filled it), so scan backwards.
        """
        for point in reversed(self.data_points):
            if point.value is not None:
                return point
        return None

    def latest_value(self) -> float | int | str | None:
        point = self.latest()
        return None if point is None else point.value


def parse_data_point(raw: Any, value_type: str = "") -> DataPoint:
    """Read one ``DataPoint``, honouring the sibling ``type`` enum.

    Every field in the published model is nullable, including ``timestampMsecs``. The declared
    type is preferred - a metric declared kInt64 whose doubleValue is also populated should be
    read as the int - but an unset or unrecognised type falls through to whichever field
    carries a value, because the type enum is the field most likely to drift between versions
    and it is not worth losing a reading over.
    """
    if not isinstance(raw, dict):
        return DataPoint(timestamp_msecs=None, value=None)

    preferred = {
        "kInt64": "int64Value",
        "kDouble": "doubleValue",
        "kString": "stringValue",
    }.get(value_type)

    value = None
    if preferred is not None:
        value = raw.get(preferred)
    if value is None:
        for name in ("int64Value", "doubleValue", "stringValue"):
            candidate = raw.get(name)
            if candidate is not None:
                value = candidate
                break

    return DataPoint(timestamp_msecs=_int(raw.get("timestampMsecs")), value=value)


def parse_time_series(payload: Any) -> dict[str, TimeSeriesMetric]:
    """``TimeSeriesStats`` keyed by metric name.

    A metric that was requested but has no data comes back as an entry with no data points, not
    as a missing key and not as an error - which is exactly what a wrong ``entityId`` looks
    like. Callers that find every series empty should suspect the id, not the cluster.
    """
    metrics: dict[str, TimeSeriesMetric] = {}
    for raw in _sequence(payload, "timeSeriesStats"):
        if not isinstance(raw, dict):
            continue
        metric_name = _text(raw.get("metricName"))
        if not metric_name:
            continue
        value_type = _text(raw.get("type"))
        points = tuple(parse_data_point(point, value_type) for point in _as_list(raw.get("dataPoints")))
        metrics[metric_name] = TimeSeriesMetric(
            metric_name=metric_name, value_type=value_type, data_points=points
        )
    return metrics


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterStatus:
    """Identity and version of the cluster. The root everything else is namespaced against."""

    cluster_id: str
    name: str
    software_version: str
    node_count: int = 0

    @property
    def version(self) -> tuple[int, ...]:
        return parse_version(self.software_version)

    @property
    def stats_entity_id(self) -> str:
        """The ``entityId`` to pass to ``/v2/stats/time-series-stats`` for cluster schemas.

        ASSUMPTION, and the single biggest open risk in ticket 04. Cohesity's own community
        exporters use the cluster id from *v1* ``/public/cluster`` as the entityId on every
        cluster-level time-series call. Nothing in v2 documents an entityId for cluster schemas,
        so this assumes ``ClusterStatus.clusterId`` is that same int64.

        If it is not, the affected series come back as empty ``dataPoints`` with **no error** -
        a silent nothing, not a 404. Verify on the first real cluster by calling
        time-series-stats with this id and checking that any point comes back at all.
        """
        return self.cluster_id


def parse_cluster_status(payload: Any) -> ClusterStatus:
    payload = payload if isinstance(payload, dict) else {}
    nodes = _as_list(payload.get("nodeStatuses"))
    return ClusterStatus(
        cluster_id=_text(payload.get("clusterId") or payload.get("id")),
        name=_text(payload.get("name") or payload.get("clusterName")),
        software_version=_text(payload.get("softwareVersion")),
        node_count=len(nodes),
    )


@dataclass(frozen=True)
class ClusterStorage:
    """The seven scalars from the parameterless ``/v2/stats/cluster-storage``.

    All seven are nullable in the published model, so every one of these may be None and the
    metric layer must skip rather than substitute - a reported zero capacity would look like an
    outage rather than a missing field.
    """

    total_capacity_bytes: int | None = None
    local_usage_bytes: int | None = None
    local_available_bytes: int | None = None
    data_protection_logical_usage_bytes: int | None = None
    data_protection_physical_usage_bytes: int | None = None
    file_services_logical_usage_bytes: int | None = None
    file_services_physical_usage_bytes: int | None = None

    @property
    def used_pct(self) -> float | None:
        """Used share of raw capacity, or None if either side is missing or capacity is zero."""
        if not self.total_capacity_bytes or self.local_usage_bytes is None:
            return None
        return self.local_usage_bytes / self.total_capacity_bytes * 100.0


def parse_cluster_storage(payload: Any) -> ClusterStorage:
    payload = payload if isinstance(payload, dict) else {}
    return ClusterStorage(
        total_capacity_bytes=_int(payload.get("totalCapacityBytes")),
        local_usage_bytes=_int(payload.get("localUsageBytes")),
        local_available_bytes=_int(payload.get("localAvailableBytes")),
        data_protection_logical_usage_bytes=_int(payload.get("dataProtectionLogicalUsageBytes")),
        data_protection_physical_usage_bytes=_int(payload.get("dataProtectionPhysicalUsageBytes")),
        file_services_logical_usage_bytes=_int(payload.get("fileServicesLogicalUsageBytes")),
        file_services_physical_usage_bytes=_int(payload.get("fileServicesPhysicalUsageBytes")),
    )


# ---------------------------------------------------------------------------
# Storage domains
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaRef:
    """A ``(schemaName, metricName, entityId)`` triple - everything time-series-stats demands.

    ``includeTimeSeriesSchema=true`` on ``/v2/storage-domains`` is the only *supported, v2,
    documented* way to obtain one of these. The v1 ``entitiesSchema`` catalogue is unpublished
    for the whole 6.8-7.4 range, so this is the discovery path that survives.
    """

    schema_name: str
    metric_name: str
    entity_id: str


@dataclass(frozen=True)
class StorageDomain:
    id: str
    name: str
    total_logical_usage_bytes: int | None = None
    local_total_physical_usage_bytes: int | None = None
    local_tier_resiliency_impact_bytes: int | None = None
    schemas: tuple[SchemaRef, ...] = ()

    def entity_id(self, cluster_id: str | int) -> str:
        return namespace_id(cluster_id, self.id)


def parse_storage_domains(payload: Any) -> list[StorageDomain]:
    domains: list[StorageDomain] = []
    for raw in _sequence(payload, "storageDomains"):
        if not isinstance(raw, dict):
            continue
        stats = raw.get("stats") if isinstance(raw.get("stats"), dict) else {}
        schemas = tuple(
            SchemaRef(
                schema_name=_text(entry.get("schemaName")),
                metric_name=_text(entry.get("metricName")),
                entity_id=_text(entry.get("entityId")),
            )
            for entry in _as_list(raw.get("schemas"))
            if isinstance(entry, dict)
        )
        domains.append(
            StorageDomain(
                id=_text(raw.get("id")),
                name=_text(raw.get("name")),
                total_logical_usage_bytes=_int(stats.get("totalLogicalUsageBytes")),
                local_total_physical_usage_bytes=_int(stats.get("localTotalPhysicalUsageBytes")),
                local_tier_resiliency_impact_bytes=_int(stats.get("localTierResiliencyImpactBytes")),
                schemas=schemas,
            )
        )
    return domains


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewStats:
    """One view's value for the single metric the call asked for.

    Views are dimensions rather than entities (ticket 05): the poll is a top-N ranking, and a
    sampled population makes a bad entity set - a view that drops out of the top 20 would look
    like it had been deleted.
    """

    view_id: str
    view_name: str
    protocols: tuple[str, ...] = ()
    value: int | None = None


def parse_views_stats(payload: Any, metric: str) -> list[ViewStats]:
    """Read ``ViewsStats``, picking out the one metric that was requested.

    ``/v2/stats/top-views`` and the deprecated ``/v2/stats/views`` return byte-identical bodies,
    which is what makes the 7.3 fork a path swap rather than two code paths.
    """
    views: list[ViewStats] = []
    for raw in _sequence(payload, "viewsStats"):
        if not isinstance(raw, dict):
            continue
        views.append(
            ViewStats(
                view_id=_text(raw.get("viewId")),
                view_name=_text(raw.get("viewName")),
                protocols=tuple(_text(item) for item in _as_list(raw.get("protocols"))),
                value=_view_metric_value(raw, metric),
            )
        )
    return views


def _view_metric_value(raw: dict, metric: str) -> int | None:
    for stat in _as_list(raw.get("stats")):
        if not isinstance(stat, dict):
            continue
        if metric and _text(stat.get("metric")) != metric:
            continue
        # valueInLastHours is a list because the endpoint can answer several windows; we ask
        # for one, so the first entry is it.
        for window in _as_list(stat.get("valueInLastHours")):
            if isinstance(window, dict):
                return _int(window.get("value"))
    return None


# ---------------------------------------------------------------------------
# Protection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProtectionRun:
    """One execution of a protection group, as returned by ``/v2/data-protect/runs/summary``."""

    id: str
    protection_group_id: str
    protection_group_name: str
    status: str
    start_time_usecs: int | None = None
    end_time_usecs: int | None = None
    bytes_written: int | None = None
    logical_size_bytes: int | None = None
    is_sla_violated: bool | None = None
    success_objects_count: int | None = None
    total_objects_count: int | None = None
    is_full_run: bool | None = None
    environment: str = ""

    @property
    def is_terminal(self) -> bool:
        return self.status not in NON_TERMINAL_RUN_STATUSES

    @property
    def duration_msecs(self) -> float | None:
        if self.start_time_usecs is None or self.end_time_usecs is None:
            return None
        if self.end_time_usecs < self.start_time_usecs:
            # Clock skew between nodes, or a run whose end was recorded before its start.
            # Reporting a negative duration would be worse than reporting nothing.
            return None
        return (self.end_time_usecs - self.start_time_usecs) / 1000.0


def parse_protection_runs(payload: Any) -> list[ProtectionRun]:
    runs: list[ProtectionRun] = []
    for raw in _sequence(payload, "protectionRunsSummary"):
        if not isinstance(raw, dict):
            continue
        run_id = _text(raw.get("id"))
        if not run_id:
            # Without an id the run cannot be deduplicated, and a run that cannot be
            # deduplicated will be counted once per overlapping window. Drop it.
            continue
        runs.append(
            ProtectionRun(
                id=run_id,
                protection_group_id=_text(raw.get("protectionGroupId")),
                protection_group_name=_text(raw.get("protectionGroupName")),
                status=_text(raw.get("status")),
                start_time_usecs=_int(raw.get("startTimeUsecs")),
                end_time_usecs=_int(raw.get("endTimeUsecs")),
                bytes_written=_int(raw.get("bytesWritten")),
                logical_size_bytes=_int(raw.get("logicalSizeBytes")),
                is_sla_violated=_bool(raw.get("isSlaViolated")),
                success_objects_count=_int(raw.get("successObjectsCount")),
                total_objects_count=_int(raw.get("totalObjectsCount")),
                is_full_run=_bool(raw.get("isFullRun")),
                environment=_text(raw.get("environment")),
            )
        )
    return runs


@dataclass(frozen=True)
class ProtectionGroup:
    """A backup job, plus the summary of its last run when ``includeLastRunInfo=true``."""

    id: str
    name: str
    environment: str = ""
    policy_id: str = ""
    storage_domain_id: str = ""
    is_active: bool | None = None
    is_deleted: bool | None = None
    is_paused: bool | None = None
    num_protected_objects: int | None = None
    last_run_status: str = ""
    last_run_end_time_usecs: int | None = None
    last_run_is_sla_violated: bool | None = None

    def entity_id(self, cluster_id: str | int) -> str:
        return namespace_id(cluster_id, self.id)

    def last_success_age_msecs(self, now_usecs: int) -> float | None:
        """How long since this job last finished successfully, in milliseconds.

        None when the last run did not succeed or carried no end time: the age of a *success*
        is undefined until there has been one, and emitting zero there would read as "just
        backed up" for a job that has never run.

        This is the gauge that carries the failure a counter cannot see - the run that never
        started. It is well defined at every instant, which is why gauge semantics are right
        here and wrong for run outcomes.
        """
        if self.last_run_status not in ("Succeeded", "SucceededWithWarning"):
            return None
        if self.last_run_end_time_usecs is None:
            return None
        return max(0.0, (now_usecs - self.last_run_end_time_usecs) / 1000.0)


def parse_protection_groups(payload: Any) -> list[ProtectionGroup]:
    groups: list[ProtectionGroup] = []
    for raw in _sequence(payload, "protectionGroups"):
        if not isinstance(raw, dict):
            continue
        backup = _last_backup_info(raw)
        groups.append(
            ProtectionGroup(
                id=_text(raw.get("id")),
                name=_text(raw.get("name")),
                environment=_text(raw.get("environment")),
                policy_id=_text(raw.get("policyId")),
                storage_domain_id=_text(raw.get("storageDomainId")),
                is_active=_bool(raw.get("isActive")),
                is_deleted=_bool(raw.get("isDeleted")),
                is_paused=_bool(raw.get("isPaused")),
                num_protected_objects=_int(raw.get("numProtectedObjects")),
                last_run_status=_text(backup.get("status")),
                last_run_end_time_usecs=_int(backup.get("endTimeUsecs")),
                last_run_is_sla_violated=_bool(backup.get("isSlaViolated")),
            )
        )
    return groups


def _last_backup_info(raw: dict) -> dict:
    last_run = raw.get("lastRun")
    if not isinstance(last_run, dict):
        return {}
    backup = last_run.get("localBackupInfo")
    return backup if isinstance(backup, dict) else {}


class RunLedger:
    """Remembers which run ids have already been counted, so a re-read does not double count.

    ``/v2/data-protect/runs/summary`` takes a time window and nothing else - no pagination, no
    job filter - so the window has to be wider than the poll interval to avoid dropping runs,
    which guarantees that completed runs are returned several times. Bounded, because the
    extension runs for months and a set that only grows is a leak with a slow fuse.
    """

    def __init__(self, capacity: int = RUN_LEDGER_CAPACITY):
        self._capacity = max(1, capacity)
        self._seen: OrderedDict[str, None] = OrderedDict()

    def __len__(self) -> int:
        return len(self._seen)

    def claim(self, run_id: str) -> bool:
        """True the first time a run id is offered, False every time after.

        Re-offering a known id refreshes its recency, so a long-running job that keeps showing
        up in the window is not evicted and then counted a second time.
        """
        if run_id in self._seen:
            self._seen.move_to_end(run_id)
            return False
        self._seen[run_id] = None
        while len(self._seen) > self._capacity:
            self._seen.popitem(last=False)
        return True


def new_terminal_runs(runs: list[ProtectionRun], ledger: RunLedger) -> list[ProtectionRun]:
    """The runs worth reporting: finished, and not seen before.

    Non-terminal runs are skipped *without being recorded* - a run first seen as ``Running`` and
    later as ``Failed`` must count once, as ``Failed``. Recording it while in flight would lose
    the outcome entirely.
    """
    fresh = []
    for run in runs:
        if not run.is_terminal:
            continue
        if ledger.claim(run.id):
            fresh.append(run)
    return fresh


# ---------------------------------------------------------------------------
# Coercion helpers
#
# Every numeric field in the published models is nullable, and JSON numbers arrive as int,
# float or string depending on the cluster's serialiser. These never raise: a field that cannot
# be read becomes None so the metric layer skips one series instead of losing the whole poll.
# ---------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _sequence(payload: Any, key: str) -> list:
    """Items from ``{key: [...]}``, tolerating a bare array.

    Some Cohesity collection endpoints answer with the wrapper object and some with the array
    itself, and which is which differs between the v1 and v2 generations of the same call.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return _as_list(payload.get(key))
    return []


# Re-exported so the metric layer can namespace ids without importing the whole domain module.
__all__ = [
    "NON_TERMINAL_RUN_STATUSES",
    "ClusterStatus",
    "ClusterStorage",
    "DataPoint",
    "ProtectionGroup",
    "ProtectionRun",
    "RunLedger",
    "SchemaRef",
    "StorageDomain",
    "TimeSeriesMetric",
    "ViewStats",
    "namespace_id",
    "new_terminal_runs",
    "parse_cluster_status",
    "parse_cluster_storage",
    "parse_data_point",
    "parse_protection_groups",
    "parse_protection_runs",
    "parse_storage_domains",
    "parse_time_series",
    "parse_version",
    "parse_views_stats",
]
