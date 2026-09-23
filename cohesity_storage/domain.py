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

# The two terminal statuses that mean "this run protected the data it was meant to". Named
# once because three separate questions rest on it: the age-since-last-success gauge, and -
# from v0.1.8 - which group is worth asking for object details.
SUCCESSFUL_RUN_STATUSES = frozenset({"Succeeded", "SucceededWithWarning"})

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
        """One *candidate* ``entityId`` for the cluster-level time-series schemas.

        This used to be asserted as *the* entity id, on the reasoning that Cohesity's community
        exporters pass a cluster id to every cluster-level time-series call. On a real customer
        cluster it is wrong: all five cluster schemas came back with empty ``dataPoints`` and
        **no error**, which is exactly what a wrong entityId looks like - a silent nothing, not
        a 404.

        So it is now only the second entry in
        :meth:`~cohesity_storage.client.CohesityClient.entity_id_candidates`, which probes each
        candidate until one returns data and then caches the winner. Kept as a property because
        it is still the id worth trying when there is nothing else to go on.
        """
        return self.cluster_id


# Where a cluster's own id is spelled. v1 /public/cluster calls it ``id``; /v2/clusters/status
# calls it ``clusterId``. Both responses are read for both spellings because neither is
# documented as the time-series entityId and the cost of trying one extra candidate is one
# request, once, on the first poll.
CLUSTER_ID_FIELDS = ("id", "clusterId")

# Second-tier ids carried by those same two responses. An incarnation id identifies a cluster
# across a rebuild and is a plausible entity key, but it is not the cluster's identity, so it is
# tried only after every primary id has come back empty.
CLUSTER_ALTERNATE_ID_FIELDS = ("clusterIncarnationId", "incarnationId")


def cluster_id_candidates(payload: Any, fields: tuple[str, ...] = CLUSTER_ID_FIELDS) -> list[str]:
    """Ids spelled by any of ``fields`` in a cluster-identity response, in that order.

    Deliberately forgiving: a payload that is not a dict, or that carries none of the fields,
    yields nothing rather than raising. This feeds a probe, and a source that has nothing to
    offer must cost the probe nothing.
    """
    if not isinstance(payload, dict):
        return []
    candidates = []
    for name in fields:
        value = payload.get(name)
        if value is None or isinstance(value, bool):
            continue
        text = str(value).strip()
        if text:
            candidates.append(text)
    return candidates


def ordered_unique(values: Any) -> list[str]:
    """The non-empty entries of ``values``, first occurrence kept, order preserved.

    The candidate list is usually mostly duplicates - v1 ``id`` and v2 ``clusterId`` are the
    same int64 on most clusters - and every duplicate would otherwise cost a wasted request per
    schema on the first poll.
    """
    seen: dict[str, None] = {}
    for value in values:
        text = _text(value).strip()
        if text and text not in seen:
            seen[text] = None
    return list(seen)


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


# Candidate spellings for the two usage numbers, most-trusted first.
#
# The first name in each tuple is what the published 6.8-7.4 DataUsageStats model calls the
# field, and it is what this extension read exclusively up to v0.1.3. On a real customer cluster
# neither was present: ``cohesity.storagedomain.resiliency.bytes`` arrived from the same
# ``stats`` object while ``.usage.logical`` and ``.usage.physical`` never did, which rules out a
# missing ``stats`` object and leaves only the field names. The response shape is not identical
# across the range, so rather than swap one guess for another, every plausible spelling is tried
# in order and the first one that carries a number wins.
#
# The real names on that cluster are readable from Grail: the diagnostic log event this version
# emits carries the sorted key names of the first storage domain's ``stats`` object. When they
# are known, put the right name first and this list can shrink.
STORAGE_DOMAIN_LOGICAL_FIELDS = (
    "totalLogicalUsageBytes",
    "logicalUsageBytes",
    # Bytes ingested before dedup and compression, which is what "logical" means for a domain.
    "dataInBytes",
    # Documented as possibly stale, so it is a last resort rather than a peer - but a usage
    # number an hour old is worth more than no usage number at all.
    "outdatedLogicalUsageBytes",
)
STORAGE_DOMAIN_PHYSICAL_FIELDS = (
    "localTotalPhysicalUsageBytes",
    "totalPhysicalUsageBytes",
    "physicalUsageBytes",
    "storageConsumedBytes",
    # Bytes actually written to the local tier. It equals localTotalPhysicalUsageBytes in the
    # recorded shape, which is what earns it a place - but it excludes resiliency overhead, so
    # it can read slightly low against the UI and goes last rather than first.
    "dataWrittenBytes",
)
# Unaliased: this one demonstrably arrives on a real cluster under exactly this name. Spelled as
# a tuple only so all three usage numbers are read the same way.
STORAGE_DOMAIN_RESILIENCY_FIELDS = ("localTierResiliencyImpactBytes",)


def first_number(stats: Any, names: tuple[str, ...]) -> int | None:
    """The first of ``names`` present in ``stats`` that reads as a number, or None.

    Present-but-null and present-but-unparseable both fall through to the next candidate rather
    than ending the search: a cluster that publishes a field with no value has told us nothing,
    and a later alias may still carry the number. Returning None rather than 0 is the rule the
    whole metric layer rests on - a reported zero usage reads as an outage.
    """
    if not isinstance(stats, dict):
        return None
    for name in names:
        value = _int(stats.get(name))
        if value is not None:
            return value
    return None


def storage_domain_stats_fields(payload: Any) -> tuple[str, ...]:
    """Sorted key names of the first storage domain's ``stats`` object. Names only, no values.

    The one fact needed to settle the aliasing above, and it cannot be read from the tenant any
    other way: extension logs are not reaching Grail, so this is carried out as a log event
    instead. Names carry no capacity figure, no id and nothing else worth withholding.
    """
    for raw in _sequence(payload, "storageDomains"):
        if not isinstance(raw, dict):
            continue
        stats = raw.get("stats")
        if isinstance(stats, dict):
            return tuple(sorted(str(name) for name in stats))
    return ()


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
                total_logical_usage_bytes=first_number(stats, STORAGE_DOMAIN_LOGICAL_FIELDS),
                local_total_physical_usage_bytes=first_number(stats, STORAGE_DOMAIN_PHYSICAL_FIELDS),
                local_tier_resiliency_impact_bytes=first_number(stats, STORAGE_DOMAIN_RESILIENCY_FIELDS),
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
                # Never bare _text(): a run with no status must say "unknown" rather than
                # producing a counted outcome that carries no status dimension at all.
                status=run_status(raw),
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


# The wrapper key each runs endpoint puts its array under, most specific first.
#
# ``runs/summary`` answers ``{"protectionRunsSummary": [...]}``; the two fallbacks this
# extension reaches for when that endpoint times out - ``/data-protect/protection-runs`` and
# ``/data-protect/protection-groups/{id}/runs`` - answer ``{"runs": [...]}``. Both are read
# here rather than in two parsers because the *fields* overlap almost entirely and the
# differences are all "where does this number live", which is what the lookups below absorb.
RUN_LIST_KEYS = ("protectionRunsSummary", "runs")

# Where a run's facts live in the list shape. Each run carries a per-target block - local
# backup, archival, replication - and the local backup one is the run as an operator means it.
RUN_BACKUP_KEYS = ("localBackupInfo", "localBackupRunInfo", "backupRunInfo")

# Time fields are spelled differently in the two shapes: runs/summary says ``startTimeUsecs``
# on the run, the run-list shape says ``runStartTimeUsecs`` inside the backup block. Only one
# of the two fallback endpoints appears in the 7.3.2 reference this extension was written
# from, so both spellings are accepted rather than guessed at - a wrong guess here would turn a
# working fallback into a silent empty list, which is the failure mode this whole change exists
# to stop repeating.
RUN_START_FIELDS = ("runStartTimeUsecs", "startTimeUsecs")
RUN_END_FIELDS = ("runEndTimeUsecs", "endTimeUsecs")
RUN_SUCCESS_OBJECT_FIELDS = ("successObjectsCount", "successfulObjectsCount")

# ---------------------------------------------------------------------------
# Run status, and the silent hole it used to leave.
#
# Measured on the customer tenant: `timeseries sum(cohesity.protectiongroup.run.outcome),
# by:{status}` answered `Succeeded 64` and `None 28`. Twenty-eight counted runs carried NO
# status dimension at all - the run was counted, and then could not be classified as anything.
# That is most of the metric's value gone, and it is invisible unless somebody happens to group
# by status: the totals look perfectly healthy.
#
# The mechanism was a two-place lookup (``localBackupInfo`` then the run root) meeting a run
# that has neither. A run whose only target is an archive or a replica has no local backup
# block, so its status lives in that target's own result instead. The lookup resolved to "",
# and `wire_dimensions` drops an empty value rather than sending `status=""` - correctly, but
# the result was a dimension that vanished.
#
# Two fixes, and the second one is the important one. Look in the other places a 7.3.2 run can
# carry a status; and when none of them has one, say ``unknown`` OUT LOUD rather than nothing.
# A dimension that is absent cannot be counted, alerted on, or noticed. A dimension that says
# "unknown" is all three.
# ---------------------------------------------------------------------------

#: What a run's status is when the cluster did not put one anywhere this module knows to look.
#: A real value, never an empty string: an absent dimension is a fact nobody can see.
RUN_STATUS_UNKNOWN = "unknown"

#: Per-target blocks that are shaped like ``localBackupInfo`` - a dict with its own ``status``.
RUN_ALTERNATE_BACKUP_KEYS = ("originalBackupInfo",)

#: Per-target blocks that hold a LIST of results, each with its own status. ``(block, list)``.
#: A run can have several archival targets; the first one that states a status wins, because
#: the question this metric answers is "did this run finish, and how", not "how did each of its
#: four copies finish" - that would be a different metric with a target dimension.
RUN_TARGET_RESULT_KEYS = (
    ("archivalInfo", "archivalTargetResults"),
    ("replicationInfo", "replicationTargetResults"),
    ("cloudSpinInfo", "cloudSpinTargetResults"),
)


def run_status(raw: dict, backup: dict | None = None) -> str:
    """The run's status from wherever this cluster put it, or :data:`RUN_STATUS_UNKNOWN`.

    Ordered most-authoritative first. The local backup block is the run as an operator means
    it; the run root is where ``runs/summary`` puts it; the rest are the per-target blocks that
    a run with no local copy has instead.

    Never returns an empty string. That is the whole point - see the comment above.
    """
    backup = backup if isinstance(backup, dict) else {}
    direct = _pick_text(("status",), backup, raw)
    if direct:
        return direct
    for key in RUN_ALTERNATE_BACKUP_KEYS:
        block = raw.get(key)
        if isinstance(block, dict):
            status = _text(block.get("status"))
            if status:
                return status
    for outer, inner in RUN_TARGET_RESULT_KEYS:
        block = raw.get(outer)
        if not isinstance(block, dict):
            continue
        for result in _as_list(block.get(inner)):
            if isinstance(result, dict):
                status = _text(result.get("status"))
                if status:
                    return status
    return RUN_STATUS_UNKNOWN


def run_field_names(payload: Any, run_id: str = "") -> tuple[str, ...]:
    """Key NAMES present on one run, one level deep. Names only - never a value.

    This is the storage-domain trick again: when the extension cannot find a field, it reports
    what the cluster *did* send so the real location can be read off Grail rather than guessed
    at from a schema that may not describe this version. ``blockName.keyName`` for dict
    sub-objects, because a status hiding one level down is exactly the case that produced this.

    ``run_id`` picks the run; empty takes the first entry. Job names, object names and every
    number stay behind.
    """
    for raw in _run_entries(payload):
        if run_id and _text(raw.get("id")) != run_id:
            continue
        names: set[str] = set()
        for name, value in raw.items():
            names.add(str(name))
            if isinstance(value, dict):
                names.update(f"{name}.{inner}" for inner in value)
        return tuple(sorted(names))
    return ()


def parse_run_list(payload: Any, *, group_id: str = "", group_name: str = "") -> list[ProtectionRun]:
    """Read either runs shape into the same :class:`ProtectionRun` records.

    ``group_id``/``group_name`` stand in for the per-group endpoint, whose response does not
    repeat the job it was asked about - the job is in the URL. They are only ever a fallback:
    a value the cluster stated wins over one the caller assumed.
    """
    runs: list[ProtectionRun] = []
    for raw in _run_entries(payload):
        run_id = _text(raw.get("id"))
        if not run_id:
            # Same rule as the summary parser: a run that cannot be deduplicated would be
            # counted once per overlapping window, so it is dropped rather than guessed at.
            continue
        backup = _run_backup(raw)
        stats = backup.get("localSnapshotStats")
        stats = stats if isinstance(stats, dict) else {}
        runs.append(
            ProtectionRun(
                id=run_id,
                protection_group_id=_text(raw.get("protectionGroupId")) or group_id,
                protection_group_name=_text(raw.get("protectionGroupName")) or group_name,
                status=run_status(raw, backup),
                start_time_usecs=_pick_int(RUN_START_FIELDS, backup, raw),
                end_time_usecs=_pick_int(RUN_END_FIELDS, backup, raw),
                bytes_written=_pick_int(("bytesWritten",), stats, backup, raw),
                logical_size_bytes=_pick_int(("logicalSizeBytes",), stats, backup, raw),
                is_sla_violated=_pick_bool(("isSlaViolated",), backup, raw),
                success_objects_count=_pick_int(RUN_SUCCESS_OBJECT_FIELDS, backup, raw),
                total_objects_count=_pick_int(("totalObjectsCount",), backup, raw),
                is_full_run=_pick_bool(("isFullRun",), backup, raw),
                environment=_pick_text(("environment",), raw, backup),
            )
        )
    return runs


def _run_entries(payload: Any) -> list[dict]:
    for key in RUN_LIST_KEYS:
        entries = [item for item in _sequence(payload, key) if isinstance(item, dict)]
        if entries:
            return entries
    return []


def _run_backup(raw: dict) -> dict:
    for key in RUN_BACKUP_KEYS:
        block = raw.get(key)
        if isinstance(block, dict):
            return block
    return {}


# ---------------------------------------------------------------------------
# Protected objects: is Cohesity's per-object ``uuid`` the VMware BIOS UUID?
#
# Ticket 16 wants an edge from a Cohesity protection group to the Dynatrace HOST it backs up.
# The Dynatrace side is settled - a VMware host publishes its BIOS UUID as
# ``host.additional_system_info["system.serial"]`` and it normalises to 8-4-4-4-12 hex. The
# Cohesity side is not: ``objects[].object.uuid`` is documented as an identifier and nothing
# says whether it is the hypervisor's UUID or a Cohesity-internal one. If it is internal the
# join is impossible and the enrichment layer stops here, which is worth knowing before
# anything is built on top of it.
#
# It cannot be answered from the published schema and it cannot be answered from outside the
# customer's network, so it is answered the way the storage-domain field names were: read it
# off the cluster once and carry the SHAPE of the answer out over the diagnostics channel.
# Key names, a verdict, and the uuid values themselves - which are VM identifiers from the
# customer's own estate landing in the customer's own tenant. Never an object name, never an
# address, never anything that would matter if the log stream were read more widely.
# ---------------------------------------------------------------------------

#: How many objects' uuids one probe carries out. Three is enough to tell a shape from a
#: coincidence and small enough that the record stays a record rather than an inventory.
MAX_UUID_SAMPLES = 3

#: Sub-object names that say "this object came from VMware". ``vCenterSummary`` is the one the
#: 7.3/7.4 reference documents; matched as a substring because the block is spelled differently
#: across the 6.8-7.4 range, and the question here is whether *any* VMware-specific block
#: exists rather than whether this cluster spells it the way the docs do.
VMWARE_SUMMARY_HINTS = ("vcenter", "vmware", "esxi")

#: The verdicts :func:`uuid_shape` can return. Each is a different answer to ticket 16.
#: ``numeric-id`` is the one that kills the join: a decimal int64, or a colon-joined pair of
#: them, is Cohesity's own id scheme and no amount of normalisation turns it into a BIOS UUID.
UUID_VERDICT_MISSING = "missing"
UUID_VERDICT_NUMERIC = "numeric-id"
UUID_VERDICT_CANONICAL = "uuid-8-4-4-4-12"
UUID_VERDICT_HEX32 = "uuid-32-hex-undashed"
UUID_VERDICT_NORMALISES = "uuid-after-normalisation"
UUID_VERDICT_OTHER = "not-a-uuid"

_UUID_CANONICAL = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_UUID_HEX32 = re.compile(r"[0-9a-f]{32}")
# Cohesity's own id shape: an int64, or several of them joined by colons.
_COHESITY_NUMERIC_ID = re.compile(r"[0-9]+(?::[0-9]+)*")
_NON_HEX = re.compile(r"[^0-9a-f]")


def uuid_shape(value: Any) -> str:
    """Which of :data:`UUID_VERDICT_MISSING` and its siblings this value looks like.

    The decimal check comes FIRST and that ordering is the whole point. A run of 32 decimal
    digits is also 32 valid hex digits, so a purely structural hex test would report Cohesity's
    own int64 id as a UUID and answer ticket 16 exactly backwards. Anything that is only digits
    and colons is therefore called what it is instead.

    A value that reaches 8-4-4-4-12 only after separators are stripped gets its own verdict
    rather than being folded into the clean one: whoever builds the lookup table needs to know
    that normalisation was required, because that is code they will have to write.
    """
    text = _text(value).strip().lower()
    if not text:
        return UUID_VERDICT_MISSING
    if _COHESITY_NUMERIC_ID.fullmatch(text):
        return UUID_VERDICT_NUMERIC
    if _UUID_CANONICAL.fullmatch(text):
        return UUID_VERDICT_CANONICAL
    if _UUID_HEX32.fullmatch(text):
        return UUID_VERDICT_HEX32
    if _UUID_HEX32.fullmatch(_NON_HEX.sub("", text)):
        return UUID_VERDICT_NORMALISES
    return UUID_VERDICT_OTHER


@dataclass(frozen=True)
class ProtectedObjectShape:
    """What ``objects[].object`` looks like on one cluster. Names of things, not values of them.

    The one exception is :attr:`uuid_samples`, which carries values on purpose - a verdict
    alone cannot be checked by whoever reads it, and a UUID that turns out to match a host is
    the answer to the ticket rather than a leak.
    """

    environment: str = ""
    objects_seen: int = 0
    object_fields: tuple[str, ...] = ()
    vmware_key: str = ""
    vmware_fields: tuple[str, ...] = ()
    #: ``(uuid or "", verdict)`` for at most :data:`MAX_UUID_SAMPLES` objects.
    uuid_samples: tuple[tuple[str, str], ...] = ()


def parse_protected_object_shape(payload: Any, *, environment: str = "") -> ProtectedObjectShape:
    """Read one ``includeObjectDetails=true`` runs response into a reportable shape.

    Field names are a UNION across every object in the response rather than the first object's
    keys: Cohesity omits null fields, so one object without a ``vCenterSummary`` would
    otherwise read as a cluster that has no such block at all - the wrong answer to the only
    question being asked.

    ``environment`` is what the protection group called itself, and it wins over the run's own.
    Learning the objects are kSQL rather than kVMware settles the ticket faster than any uuid
    does, and the group is the more reliable place to read it from.
    """
    fields: set[str] = set()
    vmware_key = ""
    vmware_fields: tuple[str, ...] = ()
    samples: list[tuple[str, str]] = []
    seen = 0
    run_environment = ""
    for raw in _run_entries(payload):
        run_environment = run_environment or _text(raw.get("environment"))
        for entry in _as_list(raw.get("objects")):
            if not isinstance(entry, dict):
                continue
            obj = entry.get("object")
            if not isinstance(obj, dict):
                continue
            seen += 1
            fields.update(str(name) for name in obj)
            if not vmware_key:
                vmware_key, vmware_fields = _vmware_summary(obj)
            if len(samples) < MAX_UUID_SAMPLES:
                samples.append((_text(obj.get("uuid")).strip(), uuid_shape(obj.get("uuid"))))
    return ProtectedObjectShape(
        environment=environment or run_environment,
        objects_seen=seen,
        object_fields=tuple(sorted(fields)),
        vmware_key=vmware_key,
        vmware_fields=vmware_fields,
        uuid_samples=tuple(samples),
    )


def _vmware_summary(obj: dict) -> tuple[str, tuple[str, ...]]:
    """The VMware-specific sub-object's name and its key names, or two empties.

    Keys are walked in sorted order rather than insertion order so that two objects carrying
    the same blocks cannot produce two different answers depending on how the cluster happened
    to serialise them.
    """
    for name in sorted(str(key) for key in obj):
        value = obj.get(name)
        if isinstance(value, dict) and any(hint in name.lower() for hint in VMWARE_SUMMARY_HINTS):
            return name, tuple(sorted(str(inner) for inner in value))
    return "", ()


# ---------------------------------------------------------------------------
# The host link (ticket 16): one canonical spelling of a VM's BIOS UUID.
#
# Two systems name the same VM and neither spells it the way the other does. Cohesity's
# ``objects[].object.uuid`` is 8-4-4-4-12 hex. Dynatrace publishes the same identifier on a
# HOST as ``host.additional_system_info["system.serial"]``, shaped
# ``VMware-00 11 22 33 44 55 66 77-88 99 aa bb cc dd ee ff``. The join is only a join if both
# sides are reduced to one spelling, and that reduction has to be the SAME code - two
# normalisers that disagree by one character produce zero matches and look exactly like an
# estate with no overlap.
#
# ``dynatrace/sync-host-link-task.js`` carries a JavaScript twin of this function, because the
# workflow that builds the lookup table runs in Dynatrace and cannot import Python. A test
# holds the two files to the same set of worked examples.
# ---------------------------------------------------------------------------

#: How many hex digits a UUID has once every separator is gone.
_UUID_HEX_DIGITS = 32

#: Where the dashes go when the 32 digits are regrouped: 8-4-4-4-12.
_UUID_GROUPS = (8, 4, 4, 4, 12)


def normalise_object_uuid(value: Any) -> str:
    """``value`` as a canonical lowercase 8-4-4-4-12 UUID, or ``""`` if it is not one.

    Handles both spellings this extension has to join:

    - Cohesity's ``00112233-4455-6677-8899-aabbccddeeff``, which is already canonical;
    - Dynatrace's ``VMware-00 11 22 33 44 55 66 77-88 99 aa bb cc dd ee ff``, which is not.

    **The vendor prefix cannot be stripped by "remove everything that is not a hex digit".**
    ``vmware`` contains ``a`` and ``e``, which are hex digits, so a blind strip yields 34
    digits and the value is silently rejected. So a leading ``<prefix>-`` is dropped first,
    and only when that prefix contains a character that is *not* a hex digit - which is what
    tells ``VMware-`` apart from the first group of a UUID that happens to be all letters,
    like ``abcdefab-1234-...``.

    Returns ``""`` rather than raising for anything unusable, because the caller's job is to
    skip that object rather than to fail the poll.

    The decimal guard comes first for the same reason it does in :func:`uuid_shape`: a run of
    32 decimal digits is also 32 valid hex digits, so Cohesity's own int64 id would otherwise
    normalise into a perfectly well-formed UUID that matches no host on earth.
    """
    text = _text(value).strip().lower()
    if not text or _COHESITY_NUMERIC_ID.fullmatch(text):
        return ""
    head, dash, tail = text.partition("-")
    if dash and _NON_HEX.search(head):
        text = tail
    digits = _NON_HEX.sub("", text)
    if len(digits) != _UUID_HEX_DIGITS:
        return ""
    parts = []
    cursor = 0
    for size in _UUID_GROUPS:
        parts.append(digits[cursor : cursor + size])
        cursor += size
    return "-".join(parts)


# ---------------------------------------------------------------------------
# WHICH of a VMware VM's two uuids. This is the whole of the v0.2.0 fix.
#
# A vSphere VM carries two 8-4-4-4-12 identifiers and they are not interchangeable:
#
#   SMBIOS/BIOS UUID  what the guest's own firmware reports. VMware mints it starting ``42``
#                     (or ``564d`` on a VM converted from Workstation/Server), and it is what
#                     Dynatrace publishes on a HOST as
#                     ``host.additional_system_info["system.serial"]``.
#   instanceUuid      vCenter's own key for the VM. vCenter mints it starting ``50``, and
#                     Dynatrace never publishes it on a HOST at all.
#
# ``objects[].object.uuid`` is the SECOND one. Measured on the customer's cluster: Cohesity
# answered three 50...-prefixed uuids while the three monitored hosts reported
# 42...- and 564d...-prefixed serials. Two hundred bridge-metric series flowed and matched
# exactly nothing, and nothing anywhere said why, because both sides were well-formed uuids of
# the right shape. That is the failure this block exists to make impossible to repeat.
#
# So the BIOS uuid is read from an ORDERED list of candidate field names - the same
# alias-tolerance the storage-domain stats use, and for the same reason: the field is named
# differently across 6.8-7.4 and the reference cannot be trusted to have the current spelling.
# ``object.uuid`` is NOT in the list and must never be added. Falling back to it would restore
# exactly the bug being fixed, and an identifier that cannot match is worse than no identifier
# at all: it looks like the feature works.
# ---------------------------------------------------------------------------

#: Where a VMware protected object carries its BIOS UUID, most-trusted first. ``biosUuid`` is
#: the spelling the 7.3.2 reference uses on the VMware object params; the rest are the
#: spellings the same field takes elsewhere in the Cohesity and vSphere surface (vSphere's own
#: API calls the SMBIOS uuid ``hardwareUuid`` on some views, and ``config.uuid`` elsewhere).
#: Nothing here can be an instanceUuid, which is what makes the list safe to extend.
BIOS_UUID_FIELDS = (
    "biosUuid",
    "biosUUID",
    "vmBiosUuid",
    "smbiosUuid",
    "smBiosUuid",
    "hardwareUuid",
    "biosUuidHex",
)

#: vCenter's own id for the same VM, most-trusted first. Carried as a SEPARATE dimension, never
#: as a fallback for the one above. ``uuid`` is last and is here only because the customer's
#: cluster demonstrably puts the instanceUuid there - which is the bug this release fixes.
INSTANCE_UUID_FIELDS = ("instanceUuid", "instanceUUID", "uuid")

#: Sub-objects of ``objects[].object`` that a VMware-specific field may hide in, searched after
#: the object root. ``vCenterSummary`` is the block the 7.3.2 reference documents and the one
#: the v0.1.8 probe actually saw on the customer's cluster.
VMWARE_OBJECT_BLOCKS = (
    "vCenterSummary",
    "vmwareParams",
    "vmWareParams",
    "vmwareObjectParams",
    "esxiParams",
)

#: A canonical uuid starting with this byte came from vCenter, not from the VM's firmware. It is
#: a heuristic, not a proof - one real BIOS uuid in 256 starts ``50`` by chance - so it is
#: COUNTED and reported rather than used to drop a value. A ratio tells the two apart: 200 of
#: 200 means the field name is wrong, 1 of 200 means a coincidence.
INSTANCE_UUID_BYTE = "50"


def looks_like_instance_uuid(uuid: str) -> bool:
    """Whether a canonical uuid has vCenter's ``50`` signature rather than VMware firmware's.

    Called on the value that was ALREADY chosen from :data:`BIOS_UUID_FIELDS`, so a true answer
    means the field this cluster calls a BIOS uuid is carrying an instanceUuid - the v0.1.9 bug
    wearing a different field name. See :data:`INSTANCE_UUID_BYTE` for why this counts rather
    than rejects.
    """
    return uuid.startswith(INSTANCE_UUID_BYTE)


def _candidate_blocks(obj: dict) -> list[tuple[str, dict]]:
    """The object root, then each VMware-specific sub-object it actually carries.

    A list of ``(label prefix, mapping)`` so a found field can be reported as ``block.field``
    rather than as a bare name that says nothing about where to look for it next time.
    """
    blocks: list[tuple[str, dict]] = [("", obj)]
    blocks.extend(
        (name, obj[name]) for name in VMWARE_OBJECT_BLOCKS if isinstance(obj.get(name), dict)
    )
    return blocks


def _uuid_from(obj: dict, names: tuple[str, ...]) -> tuple[str, str]:
    """The first of ``names`` anywhere on ``obj`` that normalises, and where it was found.

    Name priority beats location priority - every block is searched for ``biosUuid`` before any
    block is searched for ``hardwareUuid`` - because the names are ordered by how much they are
    trusted to mean "BIOS uuid" and the blocks are not ordered by anything. The returned label
    is ``field`` or ``block.field``, which is a NAME and goes out on the diagnostics channel;
    the value stays behind.

    A present-but-unusable candidate falls through to the next name rather than ending the
    search, exactly as :func:`first_number` does: a field published with null in it has told us
    nothing, and a later alias may still carry the identifier.
    """
    for name in names:
        for prefix, block in _candidate_blocks(obj):
            uuid = normalise_object_uuid(block.get(name))
            if uuid:
                return uuid, f"{prefix}.{name}" if prefix else name
    return "", ""


def bios_uuid_verdict(obj: Any) -> str:
    """The :func:`uuid_shape` of the first BIOS candidate this object carries at all.

    Called only when :func:`bios_uuid` found nothing, and it is the difference between the two
    answers that matter: :data:`UUID_VERDICT_MISSING` means no candidate field exists on the
    object, so the field NAME is what needs fixing; anything else means a candidate exists and
    the value in it is the wrong shape, which is a different conversation entirely.
    """
    if not isinstance(obj, dict):
        return UUID_VERDICT_MISSING
    for name in BIOS_UUID_FIELDS:
        for _, block in _candidate_blocks(obj):
            if block.get(name) is not None:
                return uuid_shape(block.get(name))
    return UUID_VERDICT_MISSING


def bios_uuid(obj: Any) -> tuple[str, str]:
    """A VM's BIOS UUID as a canonical uuid, and the field name it came from.

    Two empties when no candidate carries one. That is the deliberate outcome: the caller
    emits nothing for this object rather than reaching for ``object.uuid``, and the host-link
    diagnostic says how many objects that happened to - which is what tells a wrong field name
    ("200 objects, 0 BIOS uuids") apart from an unprotected estate.
    """
    return _uuid_from(obj, BIOS_UUID_FIELDS) if isinstance(obj, dict) else ("", "")


def instance_uuid(obj: Any) -> str:
    """vCenter's instanceUuid for the same VM, or ``""``.

    Not a join key for anything shipped here - a Dynatrace HOST does not publish it. It rides
    as its own dimension because it is the key a future vCenter-side join needs, it costs one
    dimension rather than one series, and it is the value that makes the v0.1.9 mistake
    legible in Grail: an operator can see the ``50`` id and the ``42`` id side by side.
    """
    return _uuid_from(obj, INSTANCE_UUID_FIELDS)[0] if isinstance(obj, dict) else ""


def candidate_uuid_fields(obj: Any) -> tuple[str, ...]:
    """Which candidate field names are actually PRESENT on this object, sorted, as labels.

    Names only, no values - the same trick that settled the storage-domain field names. Both
    candidate lists are reported, because "biosUuid is absent and instanceUuid is present" and
    "neither is present" are different answers needing different next steps.
    """
    if not isinstance(obj, dict):
        return ()
    blocks = _candidate_blocks(obj)
    found = {
        f"{prefix}.{name}" if prefix else name
        for name in BIOS_UUID_FIELDS + INSTANCE_UUID_FIELDS
        for prefix, block in blocks
        if block.get(name) is not None
    }
    return tuple(sorted(found))


#: Cohesity's ``environment`` values that mean "this object came out of vSphere". Matched
#: case-insensitively as a substring so ``kVMware``, ``kVMwareVCenter`` and whatever 7.5 calls
#: it all count - the alternative is a whitelist that goes stale on the next Cohesity release
#: and reports the estate as unjoinable.
VMWARE_ENVIRONMENT_HINT = "vmware"


def is_vmware_environment(environment: Any) -> bool:
    """Whether a protection group's ``environment`` says its objects come from vSphere.

    This is the whole of the VMware-only selection, and it is deliberately the *only* filter:
    the probe shipped in v0.1.8 established that SQL objects carry no ``uuid`` field at all,
    so asking a kSQL group for object details spends a request to learn nothing. Anything that
    is not recognisably VMware is left alone rather than tried hopefully.
    """
    return VMWARE_ENVIRONMENT_HINT in _text(environment).lower()


@dataclass(frozen=True)
class ProtectedObjectLink:
    """One protected VM, reduced to the pair that can be joined to a Dynatrace HOST.

    Nothing else about the object travels: not its name, not its address, not its Cohesity id.

    :attr:`uuid` is the VM's **BIOS** uuid, already normalised, so a link that exists is one
    that can be emitted. :attr:`instance_uuid` is vCenter's id for the same VM and joins
    nothing on the Dynatrace side; it rides along because it is the key a vCenter-side join
    would need, and because seeing both at once is what makes the v0.1.9 confusion legible.
    """

    protection_group_id: str
    protection_group_name: str
    uuid: str
    instance_uuid: str = ""


@dataclass(frozen=True)
class ProtectedObjectLinks:
    """The links one group yielded, and enough counted detail to explain an empty result.

    ``verdicts`` is what makes "VMware objects carry no uuid" sayable out loud. Without it an
    estate whose objects are all ``numeric-id`` looks identical to one where the request
    failed, and the ticket's instruction is that this case must be loud rather than silent.

    ``bios_field`` and ``candidate_fields`` are the v0.2.0 addition and they answer the
    question v0.1.9 could not: WHICH identifier is being emitted. The first is the candidate
    that won, the second is every candidate the objects actually carried. "0 links, candidates
    present: uuid" is a field-name answer; "200 links from biosUuid" is not.
    """

    links: tuple[ProtectedObjectLink, ...] = ()
    objects_seen: int = 0
    #: Sorted unique :func:`uuid_shape` verdicts of the objects that produced NO link.
    verdicts: tuple[str, ...] = ()
    #: The :data:`BIOS_UUID_FIELDS` label that produced the links, e.g. ``vCenterSummary.biosUuid``.
    bios_field: str = ""
    #: Every candidate field name present on the objects, whether or not it was chosen.
    candidate_fields: tuple[str, ...] = ()
    #: How many emitted uuids carry vCenter's ``50`` signature - see :func:`looks_like_instance_uuid`.
    instance_shaped: int = 0


def parse_protected_object_links(
    payload: Any,
    *,
    group_id: str = "",
    group_name: str = "",
    namespaced_group_id: str = "",
) -> ProtectedObjectLinks:
    """Read an ``includeObjectDetails=true`` runs response into joinable (group, uuid) pairs.

    ``group_id`` filters the runs, because the per-group endpoint is not the only shape this
    has to survive - the flat run list returns every group's runs and pairing another group's
    VM with this group would draw a false edge. Empty means "take every run in the body".

    ``namespaced_group_id`` is what ends up on the metric: the id the smartscape rules already
    use, ``{clusterId}_{groupId}``. It is passed in rather than computed here because this
    module deliberately knows nothing about which cluster it is reading.

    Duplicates are collapsed. One VM appearing in three of a group's last runs is one link,
    and emitting it three times would be three identical metric lines per poll.

    The uuid taken is the **BIOS** one, read through :func:`bios_uuid`. Up to v0.1.9 it was
    ``object.uuid``, which is vCenter's instanceUuid and matches no Dynatrace HOST; see
    :data:`BIOS_UUID_FIELDS`. An object with no BIOS candidate yields no link at all rather
    than falling back to the identifier that cannot match.
    """
    links: dict[str, ProtectedObjectLink] = {}
    rejected: set[str] = set()
    candidates: set[str] = set()
    chosen_field = ""
    instance_shaped = 0
    seen = 0
    for raw in _run_entries(payload):
        if group_id and _text(raw.get("protectionGroupId")) not in ("", group_id):
            continue
        for entry in _as_list(raw.get("objects")):
            if not isinstance(entry, dict):
                continue
            obj = entry.get("object")
            if not isinstance(obj, dict):
                continue
            seen += 1
            # Field NAMES, gathered for every object whether or not it yields a link: this is
            # the list that says "the cluster publishes instanceUuid and nothing else", which
            # is the only thing that distinguishes a wrong field name from an empty estate.
            candidates.update(candidate_uuid_fields(obj))
            uuid, field = bios_uuid(obj)
            if not uuid:
                # The shape verdict, not the value: this set leaves the ActiveGate in a
                # diagnostic, and an unusable identifier is still the customer's data.
                rejected.add(bios_uuid_verdict(obj))
                continue
            chosen_field = chosen_field or field
            if looks_like_instance_uuid(uuid):
                # Counted, not dropped. One real BIOS uuid in 256 starts 50 by chance, so
                # rejecting on the byte would silently lose real hosts - the very failure
                # mode this release exists to end. The RATIO is what carries the meaning.
                instance_shaped += 1
            links[uuid] = ProtectedObjectLink(
                protection_group_id=namespaced_group_id or group_id,
                protection_group_name=group_name,
                uuid=uuid,
                instance_uuid=instance_uuid(obj),
            )
    return ProtectedObjectLinks(
        links=tuple(links[uuid] for uuid in sorted(links)),
        objects_seen=seen,
        verdicts=tuple(sorted(rejected)),
        bios_field=chosen_field,
        candidate_fields=tuple(sorted(candidates)),
        instance_shaped=instance_shaped,
    )


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
        if self.last_run_status not in SUCCESSFUL_RUN_STATUSES:
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


def _pick_int(names: tuple[str, ...], *sources: Any) -> int | None:
    """The first of ``names`` that reads as a number, searched across ``sources`` in order.

    Sources rather than one dict because the same fact sits in different places in the two
    runs shapes. ``or`` would not do: zero bytes written is a real and meaningful answer.
    """
    for source in sources:
        if not isinstance(source, dict):
            continue
        for name in names:
            value = _int(source.get(name))
            if value is not None:
                return value
    return None


def _pick_bool(names: tuple[str, ...], *sources: Any) -> bool | None:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for name in names:
            if source.get(name) is not None:
                return _bool(source.get(name))
    return None


def _pick_text(names: tuple[str, ...], *sources: Any) -> str:
    for source in sources:
        if not isinstance(source, dict):
            continue
        for name in names:
            value = _text(source.get(name))
            if value:
                return value
    return ""


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
    "BIOS_UUID_FIELDS",
    "CLUSTER_ALTERNATE_ID_FIELDS",
    "CLUSTER_ID_FIELDS",
    "INSTANCE_UUID_BYTE",
    "INSTANCE_UUID_FIELDS",
    "NON_TERMINAL_RUN_STATUSES",
    "MAX_UUID_SAMPLES",
    "RUN_ALTERNATE_BACKUP_KEYS",
    "RUN_LIST_KEYS",
    "RUN_STATUS_UNKNOWN",
    "RUN_TARGET_RESULT_KEYS",
    "STORAGE_DOMAIN_LOGICAL_FIELDS",
    "STORAGE_DOMAIN_PHYSICAL_FIELDS",
    "STORAGE_DOMAIN_RESILIENCY_FIELDS",
    "SUCCESSFUL_RUN_STATUSES",
    "UUID_VERDICT_CANONICAL",
    "UUID_VERDICT_HEX32",
    "UUID_VERDICT_MISSING",
    "UUID_VERDICT_NORMALISES",
    "UUID_VERDICT_NUMERIC",
    "UUID_VERDICT_OTHER",
    "VMWARE_ENVIRONMENT_HINT",
    "VMWARE_OBJECT_BLOCKS",
    "VMWARE_SUMMARY_HINTS",
    "ClusterStatus",
    "ClusterStorage",
    "DataPoint",
    "ProtectedObjectLink",
    "ProtectedObjectLinks",
    "ProtectedObjectShape",
    "ProtectionGroup",
    "ProtectionRun",
    "RunLedger",
    "SchemaRef",
    "StorageDomain",
    "TimeSeriesMetric",
    "ViewStats",
    "bios_uuid",
    "bios_uuid_verdict",
    "candidate_uuid_fields",
    "cluster_id_candidates",
    "first_number",
    "instance_uuid",
    "is_vmware_environment",
    "looks_like_instance_uuid",
    "namespace_id",
    "new_terminal_runs",
    "normalise_object_uuid",
    "ordered_unique",
    "parse_cluster_status",
    "parse_cluster_storage",
    "parse_data_point",
    "parse_protected_object_links",
    "parse_protected_object_shape",
    "parse_protection_groups",
    "parse_protection_runs",
    "parse_run_list",
    "parse_storage_domains",
    "parse_time_series",
    "parse_version",
    "parse_views_stats",
    "run_field_names",
    "run_status",
    "storage_domain_stats_fields",
    "uuid_shape",
]
