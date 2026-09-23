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

import math
import re
from collections.abc import Mapping
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

# The bridge metric (ticket 16). A constant 1 whose only job is to carry identity: the pair
# (protection group, VM BIOS UUID) that the host-link workflow reads back out of Grail to build
# an OpenPipeline lookup table. Nothing charts it and nothing alerts on it.
#
# It sits under the protection-group prefix deliberately. That binds it to the group entity
# the rest of this extension already creates, so the line also keeps the group's node alive and
# correctly named - it does not mint anything new. The uuid rides as a dimension rather than as
# part of the key, because a metric key per VM would be a metric key explosion.
#
# **This is a bounded-scale mechanism, not a general one**, and the bound is the whole design:
# one series per protected object is exactly the cardinality ticket 04 ruled out of v1. One SQL
# group on the customer's own cluster holds 856 objects; a production Cohesity protects tens of
# thousands. So it is off unless asked for, VMware-only, and hard-capped per poll - see
# :data:`cohesity_storage.config.DEFAULT_HOST_LINK_OBJECTS` and the README.
PROTECTION_GROUP_PROTECTS = f"{PREFIX_PROTECTION_GROUP}.protects"

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
    PROTECTION_GROUP_PROTECTS,
)

# ---------------------------------------------------------------------------
# Dimension keys.
#
# Entity-identifying dimensions stay `cohesity.*`-prefixed so they cannot collide with a
# built-in field or another extension's. The four descriptive ones (`service`, `operation`,
# `status`, `result`) are deliberately bare: they are read as chart splits and detector `by:{}`
# clauses, they carry no identity, and the smartscape rules never look at them.
#
# The three protection-group flags are namespaced under the group's prefix rather than bare:
# a bare `active` or `paused` in Grail says nothing about *what* is active, and would collide
# with the first other extension to use the word. They are NOT spelled like Cohesity's
# `isPaused`: the metric ingestion protocol only accepts lowercase dimension keys, and a single
# uppercase letter makes the whole line invalid - that was every protection-group line up to
# v0.1.1. The ingest rejects them silently apart from an "invalid metric lines" count.
# ---------------------------------------------------------------------------

DIM_CLUSTER_ID = "cohesity.cluster.id"
DIM_CLUSTER_NAME = "cohesity.cluster.name"
DIM_STORAGE_DOMAIN_ID = "cohesity.storagedomain.id"
DIM_STORAGE_DOMAIN_NAME = "cohesity.storagedomain.name"
DIM_PROTECTION_GROUP_ID = "cohesity.protectiongroup.id"
DIM_PROTECTION_GROUP_NAME = "cohesity.protectiongroup.name"
DIM_VIEW_ID = "cohesity.view.id"
DIM_VIEW_NAME = "cohesity.view.name"
# The join key, carried only by the bridge metric. Already normalised to canonical
# lowercase 8-4-4-4-12 by domain.normalise_object_uuid - an unnormalised value here would
# match no host and there would be nothing to say so.
#
# From v0.2.0 this carries the VM's **BIOS/SMBIOS** uuid, which is the identifier a Dynatrace
# HOST publishes as host.additional_system_info["system.serial"]. Up to v0.1.9 it carried
# `object.uuid`, which is vCenter's instanceUuid: a well-formed uuid for the same VM that no
# HOST anywhere reports, so 200 series flowed and joined to nothing. The dimension NAME is
# unchanged on purpose - the workflow, the pipeline and the metric contract all still read
# `cohesity.object.uuid`, and only what fills it moved.
DIM_OBJECT_UUID = "cohesity.object.uuid"
# vCenter's own id for the same VM, carried alongside rather than instead. It joins nothing
# here - Dynatrace does not publish it on a HOST - but it is the right key for a vCenter-side
# join later, it costs one dimension rather than one series, and having both visible in Grail
# is what makes the two identifiers impossible to confuse again. Omitted when absent.
DIM_OBJECT_INSTANCE_UUID = "cohesity.object.instance_uuid"

DIM_SERVICE = "service"
DIM_OPERATION = "operation"
DIM_STATUS = "status"
DIM_RESULT = "result"
DIM_SLA_VIOLATED = f"{PREFIX_PROTECTION_GROUP}.sla_violated"
DIM_PAUSED = f"{PREFIX_PROTECTION_GROUP}.paused"
DIM_ACTIVE = f"{PREFIX_PROTECTION_GROUP}.active"

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
    it is the half of the contract that carries silence. The paused flag rides alongside so an
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


def protected_object_samples(
    cluster_id: str | int,
    cluster_name: str,
    links: list[domain.ProtectedObjectLink],
) -> list[Sample]:
    """The bridge metric: a constant 1 per (protection group, VM BIOS UUID) pair.

    The value is meaningless on purpose - nothing charts it. What matters is the dimension
    set, because the host-link workflow reads exactly these three fields back out of Grail
    (``cohesity.cluster.id``, ``cohesity.protectiongroup.id``, ``cohesity.object.uuid``) and
    turns them into the OpenPipeline lookup table that draws the
    ``EXT_COHESITY_PROTECTION_GROUP --protects--> HOST`` edge. The group name rides along so a
    human reading the table in Grail can tell which job a row is about.

    **A partial line is never emitted.** A link missing either id or the uuid is dropped
    outright rather than sent with a gap: the workflow would pair it with the wrong group, and
    a wrong edge is worse than a missing one - it says a VM is backed up by a job that does not
    touch it.

    The uuid is already canonical by construction (:func:`domain.normalise_object_uuid` is the
    only way a :class:`~.domain.ProtectedObjectLink` can be built), so nothing is normalised
    here. Re-normalising would be a second copy of the rule to drift out of step.
    """
    samples = []
    for link in links:
        if not (link.protection_group_id and link.uuid):
            continue
        dimensions = cluster_dimensions(cluster_id, cluster_name)
        # NOT run through entity_id(): the id on a ProtectedObjectLink is already namespaced by
        # the client, which is the only layer that knows which cluster it read. Namespacing it
        # twice would produce `{cluster}_{cluster}_{group}` and match no entity at all.
        dimensions[DIM_PROTECTION_GROUP_ID] = link.protection_group_id
        if link.protection_group_name:
            dimensions[DIM_PROTECTION_GROUP_NAME] = link.protection_group_name
        dimensions[DIM_OBJECT_UUID] = link.uuid
        if link.instance_uuid:
            # Never a substitute for the line above and never a reason to emit one: a link with
            # only an instanceUuid was already dropped by the `not link.uuid` guard, because an
            # identifier that cannot match is worse than none - it looks like it works.
            dimensions[DIM_OBJECT_INSTANCE_UUID] = link.instance_uuid
        samples.append(Sample(PROTECTION_GROUP_PROTECTS, 1, dimensions))
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
            # `or RUN_STATUS_UNKNOWN`, and it is not belt and braces. An empty status is
            # dropped by wire_dimensions, which produced 28 counted runs on the customer
            # tenant carrying NO status dimension at all - a run counted and then
            # unclassifiable, invisible unless somebody grouped by status. The domain parser
            # now resolves "unknown" itself; this is the chokepoint that makes it true for
            # every path into this function, including a ProtectionRun built by hand.
            status=run.status or domain.RUN_STATUS_UNKNOWN,
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
# Wire format
#
# The SDK builds each line as ``f'{key}="{value}"'`` and escapes nothing, so whatever a
# Cohesity admin typed into a name reaches the ingest verbatim. A quote ends the value early, a
# backslash escapes the next character, and a newline ends the line - each one turns a valid
# metric into an "invalid metric lines" count with no hint of which line or why. Names are the
# only free text here, and the fixtures never contain any of these characters, which is why
# every local run was clean. So every line is made safe here, at one chokepoint, rather than
# trusted to whoever builds the dimensions.
# ---------------------------------------------------------------------------

#: Dynatrace truncates a dimension value past 255 characters. Cutting at 250 ourselves keeps
#: the cut on our side of the escape - see :func:`wire_dimensions`.
DIMENSION_VALUE_MAX_CHARS = 250

# Any run of whitespace or control characters. Line breaks are the fatal ones, but a tab or a
# stray control byte in a chart legend is noise nobody meant to type either.
_UNPRINTABLE_RUN = re.compile(r"[\s\0-\037\177]+")


def clean_dimension_value(value: Any) -> str:
    """The text a dimension value should carry, before escaping. Empty means "leave it out"."""
    if value is None:
        return ""
    text = _UNPRINTABLE_RUN.sub(" ", str(value)).strip()
    # Truncated BEFORE escaping, so the cut can never land between a backslash and the
    # character it escapes - a dangling backslash would escape the closing quote.
    return text[:DIMENSION_VALUE_MAX_CHARS].rstrip()


def escape_dimension_value(text: str) -> str:
    # Backslash first, or the backslashes added for quotes would be doubled again.
    return text.replace("\\", "\\\\").replace('"', '\\"')


def wire_dimensions(dimensions: Mapping[str, Any] | None) -> dict[str, str]:
    """Dimensions as they must reach ``report_metric``: cleaned, truncated and escaped.

    An empty value is dropped rather than sent as ``""``: an empty dimension is a series split
    on nothing, and "absent" is already how the rest of this module says "the cluster did not
    say".
    """
    wired: dict[str, str] = {}
    for key, value in (dimensions or {}).items():
        text = clean_dimension_value(value)
        if text:
            wired[key] = escape_dimension_value(text)
    return wired


def wire_value(value: Any) -> float | int | None:
    """A value the protocol can carry, or None. NaN and infinity have no line-protocol spelling.

    Python prints them as ``nan`` and ``inf``, which the SDK would pass straight through into
    an invalid line.
    """
    number = _number(value)
    if isinstance(number, float) and not math.isfinite(number):
        return None
    return number


# ---------------------------------------------------------------------------
# Diagnostics
#
# Log records, not metrics, and the only ones the extension emits. They exist because the
# extension's own logs are not reaching Grail on the tenant this was debugged against, which
# left exactly the facts needed to explain two silent metric gaps unreadable. Log ingest is a
# different path and it does arrive, so the handful of facts worth having are carried out that
# way instead.
#
# Deliberately narrow. Names of things, never values of things: no API key, no response body, no
# capacity figure, nothing that would matter if the log stream were read by someone who should
# not see this cluster's data.
# ---------------------------------------------------------------------------

#: Marks every record this extension writes, so one Grail filter finds all of them.
LOG_SOURCE_DIAGNOSTICS = "cohesity_storage.diagnostics"

SEVERITY_INFO = "INFO"
SEVERITY_WARN = "WARN"
SEVERITY_ERROR = "ERROR"

#: How many field names one record will carry. A storage domain's stats object holds a few
#: dozen; a cluster that published hundreds would be a bug, not a reason to write a huge record.
MAX_DIAGNOSTIC_FIELDS = 60


def diagnostic_log_events(
    cluster_id: str | int,
    cluster_name: str,
    software_version: str,
    facts: list[Mapping[str, Any]],
) -> list[dict]:
    """Turn the facts a client learned into log records ready for ``report_log_events``.

    Built here rather than in the client for the same reason samples are: the client deals in
    what the cluster said, this module deals in what Dynatrace is told. Unknown fact kinds are
    dropped rather than guessed at, so a future fact cannot produce a record nobody can read.
    """
    common = {
        "log.source": LOG_SOURCE_DIAGNOSTICS,
        DIM_CLUSTER_ID: str(cluster_id),
        DIM_CLUSTER_NAME: cluster_name,
        "cohesity.cluster.version": software_version or "unreported",
    }
    events = []
    for fact in facts:
        event = _diagnostic_event(fact)
        if event is not None:
            events.append({**common, **event})
    return events


def _diagnostic_event(fact: Mapping[str, Any]) -> dict | None:
    kind = str(fact.get("kind") or "")
    if kind == "cluster_version":
        return {
            "severity": SEVERITY_INFO,
            "content": (
                f"Cohesity extension started against software version "
                f"{fact.get('version') or 'unreported'} "
                f"({fact.get('nodeCount', 0)} node(s), read from {fact.get('source', 'unknown')})"
            ),
            "cohesity.diagnostic": kind,
        }
    if kind == "entity_id_probe":
        schema = str(fact.get("schema") or "")
        candidates = [str(item) for item in fact.get("candidates") or []]
        # Per-candidate outcomes: "1234=empty, 5678=data". Absent on a fact written before the
        # probe recorded them, which is why the candidate list is still carried separately.
        attempts = [str(item) for item in fact.get("attempts") or []]
        common = {
            "cohesity.diagnostic": kind,
            "cohesity.schema": schema,
            "cohesity.entity_id_candidates": ", ".join(candidates),
            "cohesity.entity_id_attempts": ", ".join(attempts),
        }
        if fact.get("resolved"):
            return {
                "severity": SEVERITY_INFO,
                "content": (
                    f"entityId probe: {schema} returns data for entityId "
                    f"{fact.get('entityId')} (tried {len(candidates)} candidate(s): "
                    f"{', '.join(candidates) or 'none'})"
                ),
                **common,
                "cohesity.entity_id": str(fact.get("entityId") or ""),
            }
        if fact.get("error"):
            # The probe did not merely come back empty - a call raised. This is the record that
            # did not exist before 0.1.5, and the one that explains a schema producing nothing
            # with no empty-response warning to go with it.
            return {
                "severity": SEVERITY_ERROR,
                "content": (
                    f"entityId probe: {schema} could not be probed - the request failed at "
                    f"{_failure_phrase(fact)}. Tried {len(candidates)} candidate(s) "
                    f"({', '.join(candidates) or 'none'}), outcomes: "
                    f"{', '.join(attempts) or 'none recorded'}"
                ),
                **common,
                **_failure_attributes(fact),
                "cohesity.entity_id": "",
            }
        return {
            "severity": SEVERITY_WARN,
            "content": (
                f"entityId probe: {schema} returned no data points for any candidate "
                f"({', '.join(candidates) or 'none offered'}), so its metrics are not reported. "
                f"The right entityId for this schema is still unknown"
            ),
            **common,
            "cohesity.entity_id": "",
        }
    if kind == "section_failure":
        section = str(fact.get("section") or "unnamed section")
        return {
            "severity": SEVERITY_ERROR,
            "content": (
                f"{section} collection failed at {_failure_phrase(fact)}, so its metrics were "
                f"not reported this interval"
            ),
            "cohesity.diagnostic": kind,
            "cohesity.section": section,
            **_failure_attributes(fact),
        }
    if kind == "param_variant":
        path = str(fact.get("path") or "")
        attempts = [str(item) for item in fact.get("attempts") or []][:MAX_DIAGNOSTIC_FIELDS]
        requests = int(fact.get("requests") or 0)
        common = {
            "cohesity.diagnostic": kind,
            "cohesity.path": path,
            "cohesity.variant_attempts": ", ".join(attempts),
            "cohesity.variant_requests": str(requests),
        }
        if fact.get("resolved"):
            variant = str(fact.get("variant") or "")
            return {
                "severity": SEVERITY_WARN,
                "content": (
                    f"{path} refused its documented parameters with HTTP 500, but answers the "
                    f"shape '{variant}'. That shape is now used for this endpoint for the life "
                    f"of the extension process; found in {requests} extra request(s), tried: "
                    f"{', '.join(attempts) or 'none'}"
                ),
                **common,
                "cohesity.variant": variant,
            }
        return {
            "severity": SEVERITY_ERROR,
            "content": (
                f"{path} answered HTTP 500 to its documented parameters and to every "
                f"alternative shape tried ({', '.join(attempts) or 'none'}), so its metrics "
                f"are not reported. The failure was {_failure_phrase(fact)}"
            ),
            **common,
            **_failure_attributes(fact),
            "cohesity.variant": "",
        }
    if kind == "runs_source":
        source = str(fact.get("source") or "")
        attempts = [str(item) for item in fact.get("attempts") or []][:MAX_DIAGNOSTIC_FIELDS]
        common = {
            "cohesity.diagnostic": kind,
            "cohesity.runs_source": source,
            "cohesity.runs_attempts": ", ".join(attempts),
        }
        if not source:
            tried = ", ".join(attempts) or "none attempted"
            return {
                "severity": SEVERITY_ERROR,
                "content": (
                    f"no protection-runs endpoint answered ({tried}), so no run outcome, "
                    f"duration or byte count is reported. The first failure was "
                    f"{_failure_phrase(fact)}"
                ),
                **common,
                **_failure_attributes(fact),
            }
        # Which of the three paths served the runs is the whole question ticket 18 opened
        # with, and it is not answerable from the metrics: they carry the same keys whichever
        # endpoint produced them. INFO when runs/summary won, WARN otherwise - a fallback
        # working is good news that still means the documented endpoint is broken.
        first = attempts[0].split("=")[0] if attempts else source
        return {
            "severity": SEVERITY_INFO if first == source else SEVERITY_WARN,
            "content": (
                f"protection runs are being read from {source} "
                f"({fact.get('runs', 0)} run(s) in the window; tried: "
                f"{', '.join(attempts) or 'none'})"
            ),
            **common,
        }
    if kind == "runs_fanout":
        queried = int(fact.get("groups_queried") or 0)
        total = int(fact.get("groups_total") or 0)
        errored = int(fact.get("groups_errored") or 0)
        seen = int(fact.get("runs_seen") or 0)
        fresh = int(fact.get("runs_new") or 0)
        per_group = int(fact.get("runs_per_group") or 0)
        # The one diagnostic that is written every poll rather than once, because a single
        # sample of it answers nothing. Three numbers separate the three stories that all
        # looked identical from the metrics in v0.1.6: groups queried says whether the fan-out
        # was looking at all, runs seen says whether the cluster had anything to show, and runs
        # new says whether what it showed had already been counted. "0 seen of 20 queried" is a
        # quiet cluster; "14 seen, 0 new" is a healthy steady state; "0 queried" is a bug.
        return {
            "severity": SEVERITY_WARN if errored else SEVERITY_INFO,
            "content": (
                f"per-group run fan-out asked {queried} of {total} protection group(s) for "
                f"their last {per_group} run(s) each ({queried} request(s), rotating so every "
                f"group is reached within a few polls): {seen} run(s) returned, {fresh} new "
                f"after dedup, {errored} group(s) did not answer"
            ),
            "cohesity.diagnostic": kind,
            "cohesity.runs_groups_queried": str(queried),
            "cohesity.runs_groups_total": str(total),
            "cohesity.runs_groups_errored": str(errored),
            "cohesity.runs_per_group": str(per_group),
            "cohesity.runs_seen": str(seen),
            "cohesity.runs_new": str(fresh),
        }
    if kind == "protected_objects":
        # Ticket 16's one question, asked once and answered here. Everything in this record is
        # a NAME except the uuids, which are values on purpose: a verdict nobody can check is
        # not evidence, and these are the customer's own VM identifiers arriving in the
        # customer's own tenant. No object name, no address, no hostname.
        fields = [str(item) for item in fact.get("fields") or []][:MAX_DIAGNOSTIC_FIELDS]
        vmware_key = str(fact.get("vmwareKey") or "")
        vmware_fields = [
            str(item) for item in fact.get("vmwareFields") or []
        ][:MAX_DIAGNOSTIC_FIELDS]
        samples = [item for item in fact.get("uuids") or [] if isinstance(item, Mapping)]
        values = [str(item.get("value") or "") for item in samples]
        verdicts = [str(item.get("verdict") or "") for item in samples]
        environment = str(fact.get("environment") or "unreported")
        objects = int(fact.get("objects") or 0)
        common = {
            "cohesity.diagnostic": kind,
            "cohesity.object_environment": environment,
            "cohesity.object_count": str(objects),
            "cohesity.object_fields": ", ".join(fields),
            "cohesity.object_vmware_key": vmware_key,
            "cohesity.object_vmware_fields": ", ".join(vmware_fields),
            "cohesity.object_uuids": ", ".join(value for value in values if value),
            "cohesity.object_uuid_verdicts": ", ".join(verdicts),
        }
        if fact.get("error"):
            # WARN, not ERROR. Nothing is missing from the metric set because of this - the
            # probe is an experiment, and saying otherwise would train someone to ignore the
            # ERRORs that do mean a metric is gone.
            return {
                "severity": SEVERITY_WARN,
                "content": (
                    f"protected-object probe: includeObjectDetails could not be read - the "
                    f"request failed at {_failure_phrase(fact)}. Protection-run collection is "
                    f"unaffected; the protection-group to HOST join stays unproven"
                ),
                **common,
                **_failure_attributes(fact),
            }
        if not objects:
            return {
                "severity": SEVERITY_WARN,
                "content": (
                    f"protected-object probe: the {environment} protection group asked with "
                    f"includeObjectDetails returned no objects[].object at all, so this "
                    f"cluster publishes no per-object identifier here and the "
                    f"protection-group to HOST join cannot be built from run data"
                ),
                **common,
            }
        vmware = (
            f"a VMware-specific '{vmware_key}' sub-object is present, with key(s): "
            f"{', '.join(vmware_fields) or 'none'}"
            if vmware_key
            else "NO VMware-specific sub-object is present"
        )
        pairs = ", ".join(
            f"{value or 'absent'} -> {verdict}"
            for value, verdict in zip(values, verdicts, strict=False)
        )
        return {
            "severity": SEVERITY_INFO,
            "content": (
                f"protected-object probe on a {environment} protection group: {objects} "
                f"object(s); objects[].object key(s) ({len(fields)}): "
                f"{', '.join(fields) or 'none'}; {vmware}; uuid shape of up to "
                f"{len(samples)} object(s): {pairs or 'none'}"
            ),
            **common,
        }
    if kind.startswith("alert_"):
        return _alert_event(kind, fact)
    if kind == "run_status_unknown":
        return _run_status_unknown_event(fact)
    if kind == "host_link":
        return _host_link_event(fact)
    if kind == "storage_domain_stats_fields":
        fields = [str(item) for item in fact.get("fields") or []][:MAX_DIAGNOSTIC_FIELDS]
        return {
            "severity": SEVERITY_INFO,
            "content": (
                f"storage domain stats fields ({len(fields)}): {', '.join(fields) or 'none'}"
            ),
            "cohesity.diagnostic": kind,
            "cohesity.stats_fields": ", ".join(fields),
        }
    return None



def _alert_event(kind: str, fact: Mapping[str, Any]) -> dict | None:
    """The alert-collection facts. Extracted from :func:`_diagnostic_event` for its
    complexity budget, exactly as the run-status and host-link records were.
    """
    if kind == "alert_source":
        # Which of the two alert paths this cluster serves. Not answerable from the records:
        # they carry identical fields whichever endpoint produced them.
        return {
            "severity": SEVERITY_INFO,
            "content": (
                f"cluster alerts are being read from {fact.get('source') or 'unknown'} "
                f"({fact.get('path') or 'no path recorded'})"
            ),
            "cohesity.diagnostic": kind,
            "cohesity.alert_source": str(fact.get("source") or ""),
        }
    if kind == "alert_source_failed":
        # WARN, not ERROR: the chain has another candidate to try, and the path that failed is
        # frequently one this cluster was never going to serve. The section failing outright
        # arrives separately as a section_failure.
        return {
            "severity": SEVERITY_WARN,
            "content": (
                f"the alert path {fact.get('source') or 'unknown'} did not answer "
                f"({fact.get('error') or 'unknown error'}), so the next candidate was tried. "
                f"{fact.get('detail') or ''}"
            ).strip(),
            "cohesity.diagnostic": kind,
            "cohesity.alert_source": str(fact.get("source") or ""),
        }
    if kind == "alert_floor_ignored":
        # WARN, not INFO. The operator still gets exactly the alerts they asked for, so nothing
        # is wrong with the output - but the budget was spent on alerts that were then thrown
        # away, which means the cap can still be hiding severe ones and raising it is the only
        # lever left.
        dropped = int(fact.get("dropped") or 0)
        returned = int(fact.get("returned") or 0)
        return {
            "severity": SEVERITY_WARN,
            "content": (
                f"the cluster ignored the alertSeverityList request filter: it returned "
                f"{returned} alert(s) of which {dropped} were below the "
                f"{fact.get('floor') or 'configured'} floor and were dropped locally. The "
                f"alerts are correct, but the per-poll budget of "
                f"{fact.get('max_alerts') or 'unknown'} was spent reading alerts nobody asked "
                f"for, so raise it if severe alerts look truncated"
            ),
            "cohesity.diagnostic": kind,
            "cohesity.alert_floor": str(fact.get("floor") or ""),
        }
    if kind == "alert_shape":
        return _alert_shape_event(fact)
    return None


def _alert_shape_event(fact: Mapping[str, Any]) -> dict:
    """What an alert response looks like - names and counts, never content.

    The reason this record exists is the question nobody could answer before alerts were
    collected: does this cluster's alert prose carry names out of the protected estate? The
    honest way to answer it is to say how many descriptions exist and let a human look at a
    few, not to paste one into a log record that then has to be treated as sensitive itself.

    Unmapped severities are named because they are the one thing that silently degrades: an
    unrecognised severity still produces a record, at WARN, and without this nobody would know
    the map needed a new entry.
    """
    kind = "alert_shape"
    keys = [str(item) for item in fact.get("keys") or []][:MAX_DIAGNOSTIC_FIELDS]
    severities = [str(item) for item in fact.get("severities") or []]
    unmapped = [str(item) for item in fact.get("unmapped") or []][:MAX_DIAGNOSTIC_FIELDS]
    count = int(fact.get("count") or 0)
    described = int(fact.get("with_description") or 0)
    return {
        # WARN only when a severity did not map, because that is the only part of this record
        # that asks somebody to change something.
        "severity": SEVERITY_WARN if unmapped else SEVERITY_INFO,
        "content": (
            f"alert response shape: {count} alert(s), {described} carrying a description. "
            f"Severities seen: {', '.join(severities) or 'none'}"
            + (
                f". UNMAPPED severities, add them to domain._ALERT_SEVERITIES: "
                f"{', '.join(unmapped)}"
                if unmapped
                else ""
            )
            + f". Key name(s) on the first alert ({len(keys)}): {', '.join(keys) or 'none'}"
        ),
        "cohesity.diagnostic": kind,
        "cohesity.alert_keys": ", ".join(keys),
        "cohesity.alert_severities": ", ".join(severities),
        "cohesity.alert_unmapped_severities": ", ".join(unmapped),
    }

def _run_status_unknown_event(fact: Mapping[str, Any]) -> dict:
    """Extracted from :func:`_diagnostic_event`, which is at its complexity budget."""
    kind = "run_status_unknown"
    # The record that turns "status: none" in a chart into an actionable field name. The
    # run was counted - it is not lost - but it cannot be classified until somebody reads
    # this list and sees which block this cluster puts the status in.
    fields = [str(item) for item in fact.get("fields") or []][:MAX_DIAGNOSTIC_FIELDS]
    runs = int(fact.get("runs") or 0)
    return {
        "severity": SEVERITY_WARN,
        "content": (
            f"{runs} protection run(s) this poll carried no status in localBackupInfo, at "
            f"the run root, or in any archival/replication/cloudSpin target result, so "
            f'they are counted as status="unknown" rather than losing the dimension '
            f"entirely. Key name(s) one such run DID carry ({len(fields)}): "
            f"{', '.join(fields) or 'none'} - whichever of those holds the status belongs "
            f"in domain.run_status"
        ),
        "cohesity.diagnostic": kind,
        "cohesity.run_status_unknown_runs": str(runs),
        "cohesity.run_fields": ", ".join(fields),
    }


def _host_link_event(fact: Mapping[str, Any]) -> dict:
    """Extracted from :func:`_diagnostic_event`, which is at its complexity budget."""
    kind = "host_link"
    # Ticket 16's collection, reporting on itself. This is the record that has to be
    # readable by someone who never read the ticket, because four different outcomes all
    # look like "no edges appeared in Smartscape" from the Dynatrace side: no VMware groups
    # exist, the requests failed, the objects carry no usable uuid, or the per-poll cap
    # truncated. Only the third means the join is impossible; only the fourth means the
    # data is partial. Saying which is the whole point.
    groups_vmware = int(fact.get("groups_vmware") or 0)
    queried = int(fact.get("groups_queried") or 0)
    errored = int(fact.get("groups_errored") or 0)
    objects = int(fact.get("objects_seen") or 0)
    linked = int(fact.get("objects_linked") or 0)
    capped = bool(fact.get("capped"))
    cap = int(fact.get("cap") or 0)
    verdicts = [str(item) for item in fact.get("verdicts") or []][:MAX_DIAGNOSTIC_FIELDS]
    # The v0.2.0 fields, and the reason this record exists at all. `objects_bios` is counted
    # before the per-poll cap, so it says how many objects CAN be joined rather than how many
    # were sent: "200 objects, 200 BIOS uuids, no edges" is a coverage question about the
    # monitored estate, while "200 objects, 0 BIOS uuids" is a field-name question about this
    # cluster's API. Those two were indistinguishable in v0.1.9 and both read as success.
    bios = int(fact.get("objects_bios") or 0)
    bios_field = str(fact.get("bios_field") or "")
    candidates = [str(item) for item in fact.get("candidates") or []][:MAX_DIAGNOSTIC_FIELDS]
    instance_shaped = int(fact.get("instance_shaped") or 0)
    common = {
        "cohesity.diagnostic": kind,
        "cohesity.host_link_groups_vmware": str(groups_vmware),
        "cohesity.host_link_groups_queried": str(queried),
        "cohesity.host_link_groups_errored": str(errored),
        "cohesity.host_link_objects_seen": str(objects),
        "cohesity.host_link_objects_linked": str(linked),
        "cohesity.host_link_objects_bios": str(bios),
        "cohesity.host_link_bios_field": bios_field,
        "cohesity.host_link_uuid_candidates": ", ".join(candidates),
        "cohesity.host_link_instance_shaped": str(instance_shaped),
        "cohesity.host_link_capped": "true" if capped else "false",
        "cohesity.host_link_cap": str(cap),
        "cohesity.host_link_verdicts": ", ".join(verdicts),
    }
    if fact.get("suspect"):
        # The shape sanity check, reported once and on its own marker. A VMware BIOS uuid
        # starts 42 or 564d; a vCenter instanceUuid starts 50. If the field this cluster calls
        # a BIOS uuid is handing back 50s, it is the v0.1.9 bug wearing a different field name,
        # and the metric will join to nothing while looking perfectly healthy. The RATIO is the
        # evidence: all of them means the field name is wrong, one of them means coincidence.
        return {
            "severity": SEVERITY_ERROR if instance_shaped == bios else SEVERITY_WARN,
            "content": (
                f"host link: {instance_shaped} of {bios} uuid(s) taken from "
                f"'{bios_field or 'none'}' start with '{domain.INSTANCE_UUID_BYTE}', which is "
                f"vCenter's instanceUuid signature - a VMware BIOS UUID starts 42 or 564d. If "
                f"that is ALL of them the candidate field name is wrong and these will join to "
                f"no Dynatrace HOST while looking healthy; if it is one or two it is "
                f"coincidence. Candidate field(s) seen on these objects: "
                f"{', '.join(candidates) or 'none'}"
            ),
            **common,
        }
    if fact.get("error"):
        return {
            "severity": SEVERITY_WARN,
            "content": (
                f"host link: object details could not be read - the request failed at "
                f"{_failure_phrase(fact)}. Every other protection metric is unaffected; "
                f"no protection-group to HOST edge is published this interval"
            ),
            **common,
            **_failure_attributes(fact),
        }
    if not groups_vmware:
        return {
            "severity": SEVERITY_INFO,
            "content": (
                "host link: this cluster has no VMware protection group, so there is "
                "nothing to join to a Dynatrace HOST. Only VMware objects publish a BIOS "
                "UUID - SQL and physical objects carry no uuid field at all - so no other "
                "environment is even asked"
            ),
            **common,
        }
    if objects and not linked:
        # The loud one, and the reason ERROR rather than WARN. Everything upstream worked:
        # VMware groups exist, they were asked, they answered, and they returned objects -
        # and not one of those objects carried a BIOS UUID under any name this extension
        # knows. `candidates` is what makes that actionable: it lists the uuid-ish fields the
        # objects DID carry, so the right name can be added to domain.BIOS_UUID_FIELDS
        # instead of the join being written off. No fallback to object.uuid and no fallback
        # join on names: both draw confident wrong edges.
        return {
            "severity": SEVERITY_ERROR,
            "content": (
                f"host link: {objects} VMware object(s) from {queried} protection group(s) "
                f"carried NO BIOS UUID under any known field name (shapes seen: "
                f"{', '.join(verdicts) or 'none'}), so no bridge metric was emitted. This is "
                f"a FIELD NAME answer, not a coverage one. uuid-ish field(s) these objects "
                f"do carry: {', '.join(candidates) or 'none'} - if one of those is the BIOS "
                f"uuid, add it to domain.BIOS_UUID_FIELDS. object.uuid is NOT used as a "
                f"fallback: it is vCenter's instanceUuid and matches no Dynatrace HOST"
            ),
            **common,
        }
    if capped:
        # Partial data that cannot be silent. Half a lookup table looks exactly like half
        # the estate being unmonitored, and nothing else in the product would say otherwise.
        return {
            "severity": SEVERITY_WARN,
            "content": (
                f"host link: emitted {linked} of {objects} VMware object(s) and stopped at "
                f"the per-poll cap of {cap}. The mapping published this interval is "
                f"PARTIAL - hosts beyond the cap get no edge. Raise 'maxHostLinkObjects' "
                f"if the cardinality is acceptable, or accept that this cluster's protected "
                f"estate is larger than a metric-carried mapping should cover"
            ),
            **common,
        }
    # The healthy case, and it has to say enough that a zero-match join can still be diagnosed
    # from it alone. Naming the field that won and the candidates that were available is the
    # same trick that settled the storage-domain field names: the next person does not have to
    # guess what the cluster publishes, because the cluster already said.
    return {
        "severity": SEVERITY_WARN if errored else SEVERITY_INFO,
        "content": (
            f"host link: asked {queried} of {groups_vmware} VMware protection group(s) for "
            f"object details ({errored} did not answer); {objects} object(s) seen, {bios} "
            f"carried a BIOS UUID (read from '{bios_field or 'none'}'; candidates present: "
            f"{', '.join(candidates) or 'none'}), {linked} published as a (protection group, "
            f"BIOS UUID) pair. If no HOST edge appears with {bios} of {objects} objects "
            f"joinable, the answer is COVERAGE - those VMs are not OneAgent-monitored - not "
            f"the field name. Objects with no BIOS UUID: {', '.join(verdicts) or 'none'}"
        ),
        **common,
    }


def _failure_phrase(fact: Mapping[str, Any]) -> str:
    """One sentence naming where a failure happened and what the cluster called it.

    The path and the parameter NAMES, never a parameter value. Which call was refused is the
    whole question; what was passed to it is how a key or an id ends up in a log stream.
    """
    status = fact.get("status")
    params = [str(item) for item in fact.get("params") or []][:MAX_DIAGNOSTIC_FIELDS]
    where = str(fact.get("path") or "") or "no single endpoint"
    if params:
        where = f"{where} (query: {', '.join(params)})"
    kind = str(fact.get("error") or "Exception")
    if status:
        kind = f"{kind} HTTP {status}"
    detail = str(fact.get("detail") or "")
    phrase = f"{where} - {kind}"
    return f"{phrase}: {detail}" if detail else phrase


def _failure_attributes(fact: Mapping[str, Any]) -> dict:
    """The same failure as fields, so Grail can group by status or by path without parsing."""
    status = fact.get("status")
    params = [str(item) for item in fact.get("params") or []][:MAX_DIAGNOSTIC_FIELDS]
    return {
        "cohesity.error": str(fact.get("error") or "Exception"),
        "cohesity.http_status": str(status) if status else "",
        "cohesity.path": str(fact.get("path") or ""),
        "cohesity.query_params": ", ".join(params),
        # Already redacted and truncated by errors.error_facts; never a response body.
        "cohesity.detail": str(fact.get("detail") or ""),
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_flag(dimensions: dict[str, str], key: str, value: bool | None) -> None:
    """Add a boolean dimension, or leave it out entirely when the cluster did not say.

    Absent rather than "false": a missing flag and a flag that is off are different facts, and
    collapsing them would let an alert on ``paused=="false"`` silently cover jobs whose state
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



LOG_SOURCE_ALERTS = "cohesity_storage.alerts"

#: How long an alert's prose may be before it is truncated. Cohesity help text runs to several
#: paragraphs and the whole of it on every alert is ingest volume nobody reads. The number is
#: generous enough to keep the first sentence, which is the one that says what broke.
ALERT_TEXT_LIMIT = 1024

#: Cluster severity -> the level the log pipeline filters on. "unknown" deliberately lands on
#: WARN rather than INFO: a severity the extension could not read is a thing to look at, and
#: burying it at INFO is how it would stay unlooked-at.
_ALERT_LEVELS = {
    domain.ALERT_SEVERITY_CRITICAL: SEVERITY_ERROR,
    domain.ALERT_SEVERITY_WARNING: SEVERITY_WARN,
    domain.ALERT_SEVERITY_INFO: SEVERITY_INFO,
    domain.ALERT_SEVERITY_UNKNOWN: SEVERITY_WARN,
}


def alert_log_events(
    cluster_id: str | int,
    cluster_name: str,
    alerts: list[domain.Alert],
    *,
    include_description: bool = True,
) -> list[dict]:
    """Turn cluster alerts into log records ready for ``report_log_events``.

    Built here rather than in the client for the same reason samples are: the client deals in
    what the cluster said, this module deals in what Dynatrace is told.

    ``include_description`` is the redaction switch. The alert prose is the one field that can
    carry a name from the protected estate - a datastore, a share, occasionally a VM - and
    whether that should cross into Dynatrace is a decision for whoever owns the data, not a
    default this extension gets to pick. Everything else here describes the cluster itself.
    """
    common = {
        "log.source": LOG_SOURCE_ALERTS,
        DIM_CLUSTER_ID: str(cluster_id),
        DIM_CLUSTER_NAME: cluster_name,
    }
    events = []
    for alert in alerts:
        event = {
            **common,
            "severity": _ALERT_LEVELS.get(alert.severity, SEVERITY_WARN),
            "content": _alert_content(alert, include_description=include_description),
            "cohesity.alert.id": alert.id,
            "cohesity.alert.name": _alert_text(alert.name),
            "cohesity.alert.severity": alert.severity,
            "cohesity.alert.category": alert.category,
            "cohesity.alert.state": alert.state,
        }
        if alert.severity_source:
            # Only when the mapping failed. This is the field that turns "why is everything
            # unknown" into an answer without another deploy.
            event["cohesity.alert.severity_source"] = _alert_text(alert.severity_source)
        if include_description and alert.description:
            event["cohesity.alert.description"] = _alert_text(alert.description)
        # The cluster's own clock, carried as an attribute rather than used as the record's
        # timestamp. Ingest rejects anything more than an hour old, and an alert that has been
        # open for a week is both perfectly valid and far outside that window - using it would
        # make exactly the oldest, most serious alerts vanish without a word. The record is
        # timestamped when it was observed; these say when it actually happened.
        if alert.latest_timestamp_usecs is not None:
            event["cohesity.alert.latest_timestamp_usecs"] = str(alert.latest_timestamp_usecs)
        if alert.first_timestamp_usecs is not None:
            event["cohesity.alert.first_timestamp_usecs"] = str(alert.first_timestamp_usecs)
        events.append(event)
    return events


def _alert_content(alert: domain.Alert, *, include_description: bool) -> str:
    """The one-line sentence a human reads in the log viewer."""
    head = f"Cohesity {alert.severity} alert: {alert.name}"
    if include_description and alert.description:
        return _alert_text(f"{head} - {alert.description}")
    return _alert_text(head)


def _alert_text(value: str) -> str:
    """Collapse whitespace and truncate, the same discipline the dimensions get.

    Alert prose is free-form cluster output: it arrives with newlines, tabs and runs of spaces,
    and a multi-line log record is one that reads badly everywhere it is shown.
    """
    collapsed = " ".join(str(value).split())
    if len(collapsed) <= ALERT_TEXT_LIMIT:
        return collapsed
    return collapsed[: ALERT_TEXT_LIMIT - 1] + "…"

__all__ = [
    "ALERT_TEXT_LIMIT",
    "ALL_METRIC_KEYS",
    "CLUSTER_TIME_SERIES_MAP",
    "DIMENSION_VALUE_MAX_CHARS",
    "LOG_SOURCE_ALERTS",
    "LOG_SOURCE_DIAGNOSTICS",
    "MAX_DIAGNOSTIC_FIELDS",
    "PREFIX_CLUSTER",
    "PREFIX_PROTECTION_GROUP",
    "PREFIX_STORAGE_DOMAIN",
    "PROTECTION_GROUP_PROTECTS",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_WARN",
    "Sample",
    "VIEW_METRIC_OPERATIONS",
    "alert_log_events",
    "clean_dimension_value",
    "cluster_dimensions",
    "cluster_storage_samples",
    "cluster_time_series_samples",
    "diagnostic_log_events",
    "entity_id",
    "escape_dimension_value",
    "protected_object_samples",
    "protection_group_dimensions",
    "protection_group_samples",
    "protection_run_samples",
    "storage_domain_dimensions",
    "storage_domain_samples",
    "view_dimensions",
    "view_samples",
    "wire_dimensions",
    "wire_value",
]
