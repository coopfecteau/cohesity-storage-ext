"""Metric keys, dimensions, and the payload-to-sample mapping.

This module is the whole Dynatrace-facing contract settled by tickets 05 and 06. It holds no
HTTP, no SDK and no clock, so every rule below can be exercised against a recorded body - which
is what :mod:`tests.test_reporting` does, end to end, without an EEC or a tenant.

Three rules drive everything here.

*Prefix is topology.* A metric binds to an entity by its key prefix, and the OpenPipeline
smartscape rules in ``extension/openpipeline/metrics.pipeline.json`` match on exactly these
prefixes. So the prefixes are configuration, not naming: ``cohesity.storagedomain`` has no
underscore because the entity rule says it has no underscore, and changing one without the
other silently detaches every metric from its entity.

*Ids, never names.* Entity identity comes from the cluster's own ids. Cohesity ids are
cluster-scoped int64s and will collide the moment a second cluster is added, so every id that
leaves this module is namespaced ``{clusterId}_{objectId}`` even though v1 monitors a single
cluster. Storage domains and protection groups can both be renamed; names ride along as
dimensions and are never part of an identity.

*A missing number is not zero.* Every numeric field in Cohesity's published models is nullable,
and several are documented as possibly stale. A sample is emitted only when a real number came
back - reporting 0 for an absent capacity reads as an outage, which is worse than a gap.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from . import domain
from .domain import namespace_id

# ---------------------------------------------------------------------------
# Prefixes. One per entity type; each is matched by a smartscape node rule.
# ---------------------------------------------------------------------------

PREFIX_CLUSTER = "cohesity.cluster"
PREFIX_STORAGE_DOMAIN = "cohesity.storagedomain"
PREFIX_PROTECTION_GROUP = "cohesity.protectiongroup"

# ---------------------------------------------------------------------------
# Metric keys - 20 plus one self-monitoring key, exactly ticket 06's list.
# ---------------------------------------------------------------------------

CLUSTER_CAPACITY_TOTAL = f"{PREFIX_CLUSTER}.capacity.total"
CLUSTER_CAPACITY_USED = f"{PREFIX_CLUSTER}.capacity.used"
CLUSTER_CAPACITY_AVAILABLE = f"{PREFIX_CLUSTER}.capacity.available"
CLUSTER_USAGE_LOGICAL = f"{PREFIX_CLUSTER}.usage.logical"
CLUSTER_USAGE_PHYSICAL = f"{PREFIX_CLUSTER}.usage.physical"
CLUSTER_CPU_USAGE = f"{PREFIX_CLUSTER}.cpu.usage"
CLUSTER_MEMORY_USAGE = f"{PREFIX_CLUSTER}.memory.usage"
CLUSTER_IO_IOPS = f"{PREFIX_CLUSTER}.io.iops"
CLUSTER_IO_LATENCY = f"{PREFIX_CLUSTER}.io.latency"
CLUSTER_GARBAGE_BYTES = f"{PREFIX_CLUSTER}.garbage.bytes"
# View is dimensions rather than an entity (ticket 05), so this measurement hangs off the
# cluster. Under a `cohesity.view` prefix it would match no entity rule and float unattached.
CLUSTER_VIEW_THROUGHPUT = f"{PREFIX_CLUSTER}.view.throughput"
CLUSTER_COLLECTION_SUCCESS = f"{PREFIX_CLUSTER}.collection_success"

STORAGE_DOMAIN_USAGE_LOGICAL = f"{PREFIX_STORAGE_DOMAIN}.usage.logical"
STORAGE_DOMAIN_USAGE_PHYSICAL = f"{PREFIX_STORAGE_DOMAIN}.usage.physical"
STORAGE_DOMAIN_RESILIENCY_BYTES = f"{PREFIX_STORAGE_DOMAIN}.resiliency.bytes"

PROTECTION_GROUP_RUN_OUTCOME = f"{PREFIX_PROTECTION_GROUP}.run.outcome"
PROTECTION_GROUP_RUN_DURATION = f"{PREFIX_PROTECTION_GROUP}.run.duration"
PROTECTION_GROUP_RUN_BYTES_WRITTEN = f"{PREFIX_PROTECTION_GROUP}.run.bytes_written"
PROTECTION_GROUP_RUN_BYTES_LOGICAL = f"{PREFIX_PROTECTION_GROUP}.run.bytes_logical"
PROTECTION_GROUP_RUN_OBJECTS = f"{PREFIX_PROTECTION_GROUP}.run.objects"
PROTECTION_GROUP_LAST_SUCCESS_AGE = f"{PREFIX_PROTECTION_GROUP}.last_success.age"

#: Every key the extension can emit. Kept as data so a test can assert that each one is also
#: declared in extension.yaml - an undeclared key is dropped by the EEC without a word.
ALL_METRIC_KEYS = (
    CLUSTER_CAPACITY_TOTAL,
    CLUSTER_CAPACITY_USED,
    CLUSTER_CAPACITY_AVAILABLE,
    CLUSTER_USAGE_LOGICAL,
    CLUSTER_USAGE_PHYSICAL,
    CLUSTER_CPU_USAGE,
    CLUSTER_MEMORY_USAGE,
    CLUSTER_IO_IOPS,
    CLUSTER_IO_LATENCY,
    CLUSTER_GARBAGE_BYTES,
    CLUSTER_VIEW_THROUGHPUT,
    CLUSTER_COLLECTION_SUCCESS,
    STORAGE_DOMAIN_USAGE_LOGICAL,
    STORAGE_DOMAIN_USAGE_PHYSICAL,
    STORAGE_DOMAIN_RESILIENCY_BYTES,
    PROTECTION_GROUP_RUN_OUTCOME,
    PROTECTION_GROUP_RUN_DURATION,
    PROTECTION_GROUP_RUN_BYTES_WRITTEN,
    PROTECTION_GROUP_RUN_BYTES_LOGICAL,
    PROTECTION_GROUP_RUN_OBJECTS,
    PROTECTION_GROUP_LAST_SUCCESS_AGE,
)

# ---------------------------------------------------------------------------
# Dimension keys.
#
# Entity-identifying dimensions stay `cohesity.*`-prefixed so they cannot collide with a
# built-in field or another extension's. The four descriptive ones (`service`, `operation`,
# `status`, `result`) are deliberately bare: they are read as chart splits and detector `by:{}`
# clauses, they carry no identity, and the smartscape rules never look at them.
#
# `isSlaViolated`, `isPaused` and `isActive` keep Cohesity's own camelCase spelling so that a
# value seen in Grail can be matched against the API response it came from without a lookup.
# ---------------------------------------------------------------------------

DIM_CLUSTER_ID = "cohesity.cluster.id"
DIM_CLUSTER_NAME = "cohesity.cluster.name"
DIM_STORAGE_DOMAIN_ID = "cohesity.storagedomain.id"
DIM_STORAGE_DOMAIN_NAME = "cohesity.storagedomain.name"
DIM_PROTECTION_GROUP_ID = "cohesity.protectiongroup.id"
DIM_PROTECTION_GROUP_NAME = "cohesity.protectiongroup.name"
DIM_VIEW_ID = "cohesity.view.id"
DIM_VIEW_NAME = "cohesity.view.name"

DIM_SERVICE = "service"
DIM_OPERATION = "operation"
DIM_STATUS = "status"
DIM_RESULT = "result"
DIM_SLA_VIOLATED = "isSlaViolated"
DIM_PAUSED = "isPaused"
DIM_ACTIVE = "isActive"

SERVICE_DATAPROTECT = "dataprotect"
SERVICE_FILESERVICES = "fileservices"
OPERATION_READ = "read"
OPERATION_WRITE = "write"
RESULT_SUCCESS = "success"
RESULT_TOTAL = "total"

# Which Cohesity time-series metric becomes which key, and what extra dimension it carries.
# Keyed by (schemaName, metricName) because metric names are only unique within a schema.
#
# Latency is NOT converted to milliseconds. Flash-backed Cohesity routinely serves
# sub-millisecond, so dividing by 1000 rounds a fast cluster to 0 and throws away exactly the
# resolution the metric exists for. The unit in extension.yaml is MicroSecond to match.
CLUSTER_TIME_SERIES_MAP: dict[tuple[str, str], tuple[str, dict[str, str]]] = {
    ("kSentryClusterStats", "kCpuUsagePct"): (CLUSTER_CPU_USAGE, {}),
    ("kSentryClusterStats", "kMemoryUsagePct"): (CLUSTER_MEMORY_USAGE, {}),
    ("kBridgeClusterLogicalStats", "kReadIos"): (CLUSTER_IO_IOPS, {DIM_OPERATION: OPERATION_READ}),
    ("kBridgeClusterLogicalStats", "kWriteIos"): (CLUSTER_IO_IOPS, {DIM_OPERATION: OPERATION_WRITE}),
    ("kBridgeClusterLogicalStats", "kReadLatencyUsecs"): (
        CLUSTER_IO_LATENCY,
        {DIM_OPERATION: OPERATION_READ},
    ),
    ("kBridgeClusterLogicalStats", "kWriteLatencyUsecs"): (
        CLUSTER_IO_LATENCY,
        {DIM_OPERATION: OPERATION_WRITE},
    ),
    ("kBridgeClusterStats", "kMorphedGarbageBytes"): (CLUSTER_GARBAGE_BYTES, {}),
}

# The two view metrics polled, and the direction each one measures.
VIEW_METRIC_OPERATIONS = {
    "kNumBytesRead": OPERATION_READ,
    "kNumBytesWritten": OPERATION_WRITE,
}


@dataclass(frozen=True)
class Sample:
    """One metric ready to hand to ``report_metric``.

    Returning these rather than calling the SDK is what lets the whole mapping be tested
    against the shipped fixtures without an EEC. ``delta`` picks ``MetricType.DELTA``; it is
    true only for run outcomes, which are events being counted rather than a state being read.
    """

    key: str
    value: float | int
    dimensions: dict[str, str] = field(default_factory=dict)
    delta: bool = False


def entity_id(cluster_id: str | int, object_id: str | int) -> str:
    """Namespace a cluster-scoped Cohesity id so it stays unique across clusters.

    One implementation, in :func:`cohesity_storage.domain.namespace_id`, because the client
    namespaces ids too and two spellings of this rule would eventually disagree - at which point
    half the metrics would hang off a second copy of every entity.
    """
    return namespace_id(cluster_id, object_id)


# ---------------------------------------------------------------------------
# Dimension builders
# ---------------------------------------------------------------------------


def cluster_dimensions(cluster_id: str | int, cluster_name: str) -> dict[str, str]:
    """Dimensions carried by every metric, whatever entity it belongs to.

    The cluster id is not namespaced: it *is* the namespace.
    """
    return {
        DIM_CLUSTER_ID: str(cluster_id),
        DIM_CLUSTER_NAME: cluster_name,
    }


def storage_domain_dimensions(
    cluster_id: str | int,
    cluster_name: str,
    storage_domain_id: str | int,
    storage_domain_name: str = "",
) -> dict[str, str]:
    dimensions = cluster_dimensions(cluster_id, cluster_name)
    dimensions[DIM_STORAGE_DOMAIN_ID] = entity_id(cluster_id, storage_domain_id)
    if storage_domain_name:
        dimensions[DIM_STORAGE_DOMAIN_NAME] = storage_domain_name
    return dimensions


def protection_group_dimensions(
    cluster_id: str | int,
    cluster_name: str,
    protection_group_id: str | int,
    protection_group_name: str = "",
    *,
    storage_domain_id: str | int | None = None,
    status: str = "",
    is_sla_violated: bool | None = None,
    is_paused: bool | None = None,
    is_active: bool | None = None,
) -> dict[str, str]:
    """Dimensions for a protection-group metric, including the one the topology needs.

    ``storage_domain_id`` is what draws the ``writes_to`` edge, and it is optional here on
    purpose: a protection group whose storage domain is unknown must still exist as an entity,
    just without the edge. The smartscape rule mirrors that - the group's own id is required,
    the domain's is not.
    """
    dimensions = cluster_dimensions(cluster_id, cluster_name)
    dimensions[DIM_PROTECTION_GROUP_ID] = entity_id(cluster_id, protection_group_id)
    if protection_group_name:
        dimensions[DIM_PROTECTION_GROUP_NAME] = protection_group_name
    if storage_domain_id not in (None, ""):
        dimensions[DIM_STORAGE_DOMAIN_ID] = entity_id(cluster_id, storage_domain_id)
    if status:
        dimensions[DIM_STATUS] = status
    _set_flag(dimensions, DIM_SLA_VIOLATED, is_sla_violated)
    _set_flag(dimensions, DIM_PAUSED, is_paused)
    _set_flag(dimensions, DIM_ACTIVE, is_active)
    return dimensions


def view_dimensions(
    cluster_id: str | int, cluster_name: str, view: domain.ViewStats, operation: str
) -> dict[str, str]:
    dimensions = cluster_dimensions(cluster_id, cluster_name)
    # Namespaced like every other Cohesity id even though View is not an entity: the same
    # int64 means a different view on a second cluster, entity or not.
    dimensions[DIM_VIEW_ID] = entity_id(cluster_id, view.view_id)
    if view.view_name:
        dimensions[DIM_VIEW_NAME] = view.view_name
    dimensions[DIM_OPERATION] = operation
    return dimensions


# ---------------------------------------------------------------------------
# Payload -> samples
# ---------------------------------------------------------------------------


def cluster_storage_samples(
    cluster_id: str | int, cluster_name: str, storage: domain.ClusterStorage
) -> list[Sample]:
    """The seven headline capacity scalars, as three capacity keys and two usage keys.

    ``usage.logical`` and ``usage.physical`` are one key each split by ``service`` rather than
    four separate keys: data protection and file services are the same measurement of two
    workloads, and a dimension lets a chart sum them without a second query.
    """
    base = cluster_dimensions(cluster_id, cluster_name)
    pairs: list[tuple[str, Any, dict[str, str]]] = [
        (CLUSTER_CAPACITY_TOTAL, storage.total_capacity_bytes, {}),
        (CLUSTER_CAPACITY_USED, storage.local_usage_bytes, {}),
        (CLUSTER_CAPACITY_AVAILABLE, storage.local_available_bytes, {}),
        (
            CLUSTER_USAGE_LOGICAL,
            storage.data_protection_logical_usage_bytes,
            {DIM_SERVICE: SERVICE_DATAPROTECT},
        ),
        (
            CLUSTER_USAGE_LOGICAL,
            storage.file_services_logical_usage_bytes,
            {DIM_SERVICE: SERVICE_FILESERVICES},
        ),
        (
            CLUSTER_USAGE_PHYSICAL,
            storage.data_protection_physical_usage_bytes,
            {DIM_SERVICE: SERVICE_DATAPROTECT},
        ),
        (
            CLUSTER_USAGE_PHYSICAL,
            storage.file_services_physical_usage_bytes,
            {DIM_SERVICE: SERVICE_FILESERVICES},
        ),
    ]
    return [
        Sample(key, number, {**base, **extra})
        for key, value, extra in pairs
        if (number := _number(value)) is not None
    ]


def cluster_time_series_samples(
    cluster_id: str | int,
    cluster_name: str,
    schema_name: str,
    series: dict[str, domain.TimeSeriesMetric],
) -> list[Sample]:
    """The latest point of each requested series, mapped onto its key.

    A series with no non-null point produces nothing rather than a zero. All-empty across every
    schema is the signature of a wrong ``entityId``; the caller warns about that, because from
    here it is indistinguishable from an idle cluster.
    """
    base = cluster_dimensions(cluster_id, cluster_name)
    samples = []
    for metric_name, metric in series.items():
        mapping = CLUSTER_TIME_SERIES_MAP.get((schema_name, metric_name))
        if mapping is None:
            continue
        key, extra = mapping
        value = _number(metric.latest_value())
        if value is None:
            continue
        samples.append(Sample(key, value, {**base, **extra}))
    return samples


def view_samples(
    cluster_id: str | int,
    cluster_name: str,
    view_metric: str,
    views: list[domain.ViewStats],
) -> list[Sample]:
    """Top-N view throughput, as a cluster metric split by view.

    This does not fragment the cluster entity. Identity comes from ``idComponents``, which for
    the cluster is the cluster id alone; the view dimensions are extra fields the identity rule
    never reads.
    """
    operation = VIEW_METRIC_OPERATIONS.get(view_metric)
    if operation is None:
        return []
    samples = []
    for view in views:
        value = _number(view.value)
        if value is None or not view.view_id:
            continue
        samples.append(
            Sample(
                CLUSTER_VIEW_THROUGHPUT,
                value,
                view_dimensions(cluster_id, cluster_name, view, operation),
            )
        )
    return samples


def storage_domain_samples(
    cluster_id: str | int, cluster_name: str, domains: list[domain.StorageDomain]
) -> list[Sample]:
    samples = []
    for storage_domain in domains:
        if not storage_domain.id:
            # Without an id there is nothing to namespace and no entity to bind to. A metric
            # with a name but no id would mint an entity that a rename orphans.
            continue
        dimensions = storage_domain_dimensions(
            cluster_id, cluster_name, storage_domain.id, storage_domain.name
        )
        for key, value in (
            (STORAGE_DOMAIN_USAGE_LOGICAL, storage_domain.total_logical_usage_bytes),
            (STORAGE_DOMAIN_USAGE_PHYSICAL, storage_domain.local_total_physical_usage_bytes),
            (STORAGE_DOMAIN_RESILIENCY_BYTES, storage_domain.local_tier_resiliency_impact_bytes),
        ):
            number = _number(value)
            if number is not None:
                samples.append(Sample(key, number, dict(dimensions)))
    return samples


def protection_group_samples(
    cluster_id: str | int,
    cluster_name: str,
    groups: list[domain.ProtectionGroup],
    now_usecs: int,
) -> list[Sample]:
    """``last_success.age`` per group - the gauge that sees the run which never happened.

    A counter cannot report an absence, so a job with a broken schedule emits nothing at all and
    looks identical to a healthy quiet one. This gauge is defined at every instant, which is why
    it is the half of the contract that carries silence. ``isPaused`` rides alongside so an
    alert can tell a deliberate pause from a broken job.
    """
    samples = []
    for group in groups:
        if not group.id:
            continue
        age = group.last_success_age_msecs(now_usecs)
        if age is None:
            # Undefined until there has been a success. Emitting 0 would read as "just backed
            # up" for a job that has never completed - the exact inversion of the truth.
            continue
        samples.append(
            Sample(
                PROTECTION_GROUP_LAST_SUCCESS_AGE,
                age,
                protection_group_dimensions(
                    cluster_id,
                    cluster_name,
                    group.id,
                    group.name,
                    storage_domain_id=group.storage_domain_id or None,
                    status=group.last_run_status,
                    is_sla_violated=group.last_run_is_sla_violated,
                    is_paused=group.is_paused,
                    is_active=group.is_active,
                ),
            )
        )
    return samples


def protection_run_samples(
    cluster_id: str | int,
    cluster_name: str,
    runs: list[domain.ProtectionRun],
    groups: list[domain.ProtectionGroup] | None = None,
) -> list[Sample]:
    """Per-run measurements for runs that finished since the last poll.

    ``run.outcome`` is a **delta counter of 1 per newly-completed run**, dimensioned by the
    terminal status. Modelling outcomes as a 0/1 gauge instead would make two failures in one
    interval indistinguishable from one, and would keep asserting the last value long after the
    run ended. Callers must pass only deduplicated terminal runs
    (:meth:`CohesityClient.new_protection_runs`) - raw output double counts by design, because
    the poll window deliberately overlaps.

    ``groups`` supplies the storage domain id and the paused/active flags, which the runs
    endpoint does not return. It is optional: a run whose group is unknown still produces
    metrics, just without the edge dimension.
    """
    by_id = {group.id: group for group in (groups or []) if group.id}
    samples = []
    for run in runs:
        group = by_id.get(run.protection_group_id)
        group_id = run.protection_group_id or (group.id if group else "")
        if not group_id:
            continue
        dimensions = protection_group_dimensions(
            cluster_id,
            cluster_name,
            group_id,
            run.protection_group_name or (group.name if group else ""),
            storage_domain_id=(group.storage_domain_id or None) if group else None,
            status=run.status,
            is_sla_violated=run.is_sla_violated,
            is_paused=group.is_paused if group else None,
            is_active=group.is_active if group else None,
        )
        samples.append(Sample(PROTECTION_GROUP_RUN_OUTCOME, 1, dict(dimensions), delta=True))

        for key, value in (
            (PROTECTION_GROUP_RUN_DURATION, run.duration_msecs),
            (PROTECTION_GROUP_RUN_BYTES_WRITTEN, run.bytes_written),
            (PROTECTION_GROUP_RUN_BYTES_LOGICAL, run.logical_size_bytes),
        ):
            number = _number(value)
            if number is not None:
                samples.append(Sample(key, number, dict(dimensions)))

        for result, value in (
            (RESULT_SUCCESS, run.success_objects_count),
            (RESULT_TOTAL, run.total_objects_count),
        ):
            number = _number(value)
            if number is not None:
                samples.append(
                    Sample(
                        PROTECTION_GROUP_RUN_OBJECTS,
                        number,
                        {**dimensions, DIM_RESULT: result},
                    )
                )
    return samples


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_flag(dimensions: dict[str, str], key: str, value: bool | None) -> None:
    """Add a boolean dimension, or leave it out entirely when the cluster did not say.

    Absent rather than "false": a missing flag and a flag that is off are different facts, and
    collapsing them would let an alert on ``isPaused=="false"`` silently cover jobs whose state
    was never reported.
    """
    if value is not None:
        dimensions[key] = "true" if value else "false"


def _number(value: Any) -> float | int | None:
    """A metric value, or None if the field carried something that is not a number.

    ``dataPoints`` can carry a ``stringValue``, and booleans are ints in Python; both would be
    accepted by ``report_metric`` and produce a series nobody can read.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value
    return None


__all__ = [
    "ALL_METRIC_KEYS",
    "CLUSTER_TIME_SERIES_MAP",
    "PREFIX_CLUSTER",
    "PREFIX_PROTECTION_GROUP",
    "PREFIX_STORAGE_DOMAIN",
    "VIEW_METRIC_OPERATIONS",
    "Sample",
    "cluster_dimensions",
    "cluster_storage_samples",
    "cluster_time_series_samples",
    "entity_id",
    "protection_group_dimensions",
    "protection_group_samples",
    "protection_run_samples",
    "storage_domain_dimensions",
    "storage_domain_samples",
    "view_dimensions",
    "view_samples",
]
