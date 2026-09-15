"""The Cohesity half of the extension: one method per endpoint the v1 metric set needs.

Every method returns parsed domain objects (:mod:`.domain`), never raw JSON and never a
Dynatrace metric. Deciding metric keys, units and the dimension contract is ticket 06's job and
it is deliberately not done here - the metric-reporting layer is the seam above this module.
Kept free of Dynatrace imports so it can be exercised without an EEC or a tenant.

Where the responses come from is a configuration choice, not a code path: setting ``fixtureDir``
swaps the transport for one that replays recorded JSON and nothing above this line changes.
See :mod:`.transport` and :mod:`.fixtures`.

Endpoint facts that look obvious and are wrong, all settled by ticket 04:

* The API is ``/v2/...``. The published reference sets are named ``v1-cluster-<version>``, which
  is portal versioning; every page inside them declares V2. The real v1 ``/public/*`` family is
  unpublished for the whole 6.8-7.4 range, so nothing here depends on it.
* ``metricNames`` is ``explode: false`` - comma-joined into ONE query parameter. The transport
  enforces that; do not hand-build a query string here.
* ``entityIdList`` **sums** across entities instead of returning a series each. There is no
  batch-by-entity mode, which is why per-view and per-job numbers come from the top-views and
  data-protect endpoints rather than from time-series-stats.
* ``/v2/stats/top-views`` does not exist before 7.3 and ``/v2/stats/views`` is deprecated from
  7.3. Parameters and response are identical, so it is a path swap - and it is the only version
  fork in the whole v1 endpoint set.
* ``/v2/data-protect/runs/summary`` has no pagination and no job filter, only a time window, so
  overlapping polls re-return the same run. :meth:`CohesityClient.new_protection_runs` is the
  only safe way to count outcomes.
"""

from __future__ import annotations

import time
from typing import Any

from . import domain
from .config import ClusterConfig
from .errors import (
    CohesityApiError,
    CohesityAuthError,
    CohesityConnectError,
    CohesityEndpointError,
    CohesityError,
    CohesityFixtureError,
)
from .transport import build_transport

# Paths are relative to the /v2 prefix the transport adds.
CLUSTER_STATUS_PATH = "/clusters/status"
CLUSTER_STORAGE_PATH = "/stats/cluster-storage"
TIME_SERIES_STATS_PATH = "/stats/time-series-stats"
STORAGE_DOMAINS_PATH = "/storage-domains"
TOP_VIEWS_PATH = "/stats/top-views"
VIEWS_PATH = "/stats/views"
PROTECTION_GROUPS_PATH = "/data-protect/protection-groups"
PROTECTION_RUNS_PATH = "/data-protect/runs/summary"

# The 7.3 boundary. Below it top-views does not exist; from it, views is deprecated.
TOP_VIEWS_MIN_VERSION = (7, 3)

# Views are polled as a ranking, not an inventory: "what is hammering the cluster" is a top-N
# question, and a sampled population would make a terrible entity set. 20 rather than the
# endpoint's default of 100 keeps the series count bounded at 40.
DEFAULT_NUM_TOP_VIEWS = 20

# The cluster-level time-series calls, batched by schema so each schema costs one request.
# Schema names, metric names and rollups are Cohesity's own choices, read out of their
# community exporters (ticket 04 s1d) - average for gauges, max for byte counters. This is
# endpoint data, not a metric contract: what these become in Grail is ticket 06's decision.
CLUSTER_STATS_CALLS = (
    {
        "schemaName": "kSentryClusterStats",
        "metricNames": ("kCpuUsagePct", "kMemoryUsagePct"),
        "rollupFunction": "kAverage",
        "rollupIntervalSecs": 180,
    },
    {
        "schemaName": "kBridgeClusterLogicalStats",
        "metricNames": ("kReadIos", "kWriteIos", "kReadLatencyUsecs", "kWriteLatencyUsecs"),
        "rollupFunction": "kAverage",
        "rollupIntervalSecs": 180,
    },
    {
        "schemaName": "kBridgeClusterStats",
        "metricNames": ("kMorphedGarbageBytes",),
        "rollupFunction": "kAverage",
        "rollupIntervalSecs": 720,
    },
)

# The two generic view metrics worth polling out of an enum of roughly 110.
VIEW_METRICS = ("kNumBytesRead", "kNumBytesWritten")


def views_stats_path(version: tuple[int, ...]) -> str:
    """Which views-stats endpoint this cluster version serves.

    An unknown version picks the *deprecated* path on purpose. ``/v2/stats/views`` exists on
    every version in range and merely carries a deprecation flag from 7.3; ``top-views`` returns
    404 below 7.3. Guessing wrong toward the deprecated path costs nothing, guessing wrong
    toward the new one loses the metric entirely.
    """
    if version and version >= TOP_VIEWS_MIN_VERSION:
        return TOP_VIEWS_PATH
    return VIEWS_PATH


class CohesityClient:
    """One cluster's worth of Cohesity API, parsed.

    Holds two pieces of state across polls, both of which have to live somewhere and neither of
    which belongs to a single call: the cluster's identity and version (fetched once, it decides
    id namespacing and the views path) and the ledger of run ids already counted.
    """

    def __init__(self, config: ClusterConfig, transport: Any = None):
        self._config = config
        self._transport = transport if transport is not None else build_transport(config)
        self._cluster_status: domain.ClusterStatus | None = None
        self._run_ledger = domain.RunLedger()

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._transport.close()

    def describe(self) -> str:
        """Where this client is actually reading from - for logs, and for fastcheck."""
        return self._transport.describe()

    @property
    def is_replaying(self) -> bool:
        return bool(self._config.fixture_dir)

    def caveats(self) -> list[str]:
        """Warnings about the provenance of what was just served. Empty against a real cluster."""
        caveats = getattr(self._transport, "caveats", None)
        return caveats() if callable(caveats) else []

    # -- cluster -----------------------------------------------------------

    def cluster_status(self, *, refresh: bool = False) -> domain.ClusterStatus:
        """Cluster identity and software version, cached for the life of the client.

        Cached because it is a prerequisite of almost everything else - it supplies the id every
        other entity is namespaced against and the version that selects the views path - and
        because neither of those changes without a cluster upgrade. ``refresh=True`` after an
        upgrade, or on a fastcheck.
        """
        if self._cluster_status is None or refresh:
            payload = self._transport.get(CLUSTER_STATUS_PATH)
            status = domain.parse_cluster_status(payload)
            if not status.cluster_id:
                # Without the cluster id nothing can be namespaced, and an unnamespaced id will
                # collide silently the day a second cluster is added. Refuse rather than
                # report something that looks fine now and is wrong later.
                msg = (
                    f"{self._config.name}: /v2/clusters/status returned no clusterId, so no "
                    f"metric can be attributed to this cluster. Confirm the API key's owner "
                    f"holds CLUSTER_VIEW"
                )
                raise CohesityApiError(msg)
            self._cluster_status = status
        return self._cluster_status

    def namespaced(self, object_id: str | int) -> str:
        """A cluster-scoped Cohesity id, namespaced so it stays unique across clusters."""
        return domain.namespace_id(self.cluster_status().cluster_id, object_id)

    def cluster_storage(self) -> domain.ClusterStorage:
        """Headline capacity. No parameters, no paging, seven scalars - the safest call there is."""
        return domain.parse_cluster_storage(self._transport.get(CLUSTER_STORAGE_PATH))

    # -- time series -------------------------------------------------------

    def time_series(
        self,
        schema_name: str,
        metric_names: tuple[str, ...] | list[str],
        entity_id: str,
        *,
        start_time_msecs: int | None = None,
        end_time_msecs: int | None = None,
        rollup_function: str | None = None,
        rollup_interval_secs: int | None = None,
        window_seconds: int = 300,
    ) -> dict[str, domain.TimeSeriesMetric]:
        """Raw time series for one schema, keyed by metric name.

        The window defaults to the last five minutes and callers take the latest non-null point:
        one reading per poll is what a Dynatrace metric wants, and asking for a longer window
        only moves work onto the cluster.

        Three optional parameters documented in 7.1 and 7.3+ (``prorateDataPoints``,
        ``includeGrowthChange``, ``entityIdList``) are deliberately never sent - they are absent
        from the 6.8 and 7.2 specs and buy nothing here.
        """
        now_msecs = int(time.time() * 1000)
        params: dict[str, Any] = {
            "schemaName": schema_name,
            # Comma-joined into one parameter by the transport. Repeating it makes the cluster
            # read only the last value, and the failure is silent.
            "metricNames": tuple(metric_names),
            "entityId": entity_id,
            "startTimeMsecs": start_time_msecs
            if start_time_msecs is not None
            else now_msecs - window_seconds * 1000,
        }
        if end_time_msecs is not None:
            params["endTimeMsecs"] = end_time_msecs
        if rollup_function:
            params["rollupFunction"] = rollup_function
        if rollup_interval_secs:
            params["rollupIntervalSecs"] = rollup_interval_secs
        return domain.parse_time_series(self._transport.get(TIME_SERIES_STATS_PATH, params))

    def cluster_time_series(self, call: dict[str, Any]) -> dict[str, domain.TimeSeriesMetric]:
        """One entry of :data:`CLUSTER_STATS_CALLS`, against the cluster's own entity id.

        See :attr:`domain.ClusterStatus.stats_entity_id` for the assumption this rests on. If it
        is wrong, every series here comes back with an empty ``dataPoints`` list and no error -
        so a caller that gets nothing from all three calls should suspect the entity id first.
        """
        return self.time_series(
            call["schemaName"],
            call["metricNames"],
            self.cluster_status().stats_entity_id,
            rollup_function=call.get("rollupFunction"),
            rollup_interval_secs=call.get("rollupIntervalSecs"),
        )

    # -- storage domains ---------------------------------------------------

    def storage_domains(self) -> list[domain.StorageDomain]:
        """Storage domains with their usage stats. Few enough to enumerate - typically under 20.

        Note the stats are not live: every ``*Bytes`` field in ``DataUsageStats`` has a paired
        ``*TimestampUsec``, and ``outdatedLogicalUsageBytes`` is documented as possibly stale.
        Worth remembering before believing a number that disagrees with the cluster UI.
        """
        payload = self._transport.get(STORAGE_DOMAINS_PATH, {"includeStats": True})
        return domain.parse_storage_domains(payload)

    def storage_domain_schemas(self) -> list[domain.StorageDomain]:
        """Storage domains carrying their ``(schemaName, metricName, entityId)`` triples.

        The only supported v2 way to discover what time-series-stats will accept. The v1
        ``entitiesSchema`` catalogue would cover every entity type, but it is unpublished for
        6.8-7.4 and ticket 04 recommends capturing it offline rather than calling it at runtime.
        """
        payload = self._transport.get(STORAGE_DOMAINS_PATH, {"includeTimeSeriesSchema": True})
        return domain.parse_storage_domains(payload)

    # -- views -------------------------------------------------------------

    def views_path(self) -> str:
        """The views-stats path this cluster serves, decided once from its software version."""
        return views_stats_path(self.cluster_status().version)

    def view_stats(
        self,
        metric: str,
        *,
        num_top_views: int = DEFAULT_NUM_TOP_VIEWS,
        last_hours: int = 1,
        protocol: str = "kAny",
    ) -> list[domain.ViewStats]:
        """Top-N views by one metric. One metric per call - the parameter has no array form.

        Falls back to the deprecated path if the new one 404s, which covers a version string the
        extension could not parse. The fallback is not the primary mechanism: the version fork
        is decided from ``softwareVersion``, and this only catches the case where that failed.
        """
        params = {
            "metric": metric,
            "numTopViews": num_top_views,
            "lastHours": last_hours,
            "protocol": protocol,
        }
        path = self.views_path()
        try:
            payload = self._transport.get(path, params)
        except CohesityEndpointError:
            if path == VIEWS_PATH:
                raise
            payload = self._transport.get(VIEWS_PATH, params)
        return domain.parse_views_stats(payload, metric)

    # -- protection --------------------------------------------------------

    def protection_groups(self, *, include_deleted: bool = False) -> list[domain.ProtectionGroup]:
        """Protection groups with their last-run summary: the job inventory and the RPO source.

        Deleted groups are excluded by default and that matters more than it looks. A deleted
        group still holds snapshots but will never run again, so its age-since-last-success
        rises forever and alerts permanently on a job nobody can fix.

        ``useCachedData`` reads a replica that lags around 15 seconds, which is free accuracy to
        give up on a five-minute poll and takes the load off the cluster's primary.
        """
        params: dict[str, Any] = {
            "includeLastRunInfo": True,
            "isDeleted": include_deleted,
            "useCachedData": True,
        }
        return domain.parse_protection_groups(self._transport.get(PROTECTION_GROUPS_PATH, params))

    def protection_runs(
        self,
        *,
        start_time_usecs: int | None = None,
        end_time_usecs: int | None = None,
        window_seconds: int | None = None,
    ) -> list[domain.ProtectionRun]:
        """Every run the cluster reports in the window, terminal or not, deduplicated or not.

        The window defaults to three times the configured poll interval so a slow or skipped
        poll cannot drop a run. That overlap is deliberate and is exactly why
        :meth:`new_protection_runs` exists - raw output from here double counts by design.
        """
        if window_seconds is None:
            window_seconds = max(900, self._config.interval_minutes * 60 * 3)
        now_usecs = int(time.time() * 1_000_000)
        params: dict[str, Any] = {
            "startTimeUsecs": start_time_usecs
            if start_time_usecs is not None
            else now_usecs - window_seconds * 1_000_000,
        }
        if end_time_usecs is not None:
            params["endTimeUsecs"] = end_time_usecs
        return domain.parse_protection_runs(self._transport.get(PROTECTION_RUNS_PATH, params))

    def new_protection_runs(self, **kwargs) -> list[domain.ProtectionRun]:
        """Runs that have finished and have not been counted before.

        This is the method the metric layer should use. Calling :meth:`protection_runs` directly
        and counting what it returns inflates every failure by the window-to-interval ratio -
        three, at the default settings - and the symptom looks like a Cohesity problem rather
        than an extension bug, so it will be believed.
        """
        return domain.new_terminal_runs(self.protection_runs(**kwargs), self._run_ledger)

    @property
    def counted_run_ids(self) -> int:
        """How many run ids the dedup ledger is holding. For logging and for tests."""
        return len(self._run_ledger)

    # -- fastcheck ---------------------------------------------------------

    def probe(self) -> domain.ClusterStatus:
        """One cheap round trip that proves reachability, TLS and the API key at once.

        ``/v2/clusters/status`` is the right probe: it needs only CLUSTER_VIEW, so it separates
        "the key does not work" from "the key lacks the five privileges time-series-stats wants",
        and it returns the version the rest of the poll is shaped by.
        """
        return self.cluster_status(refresh=True)


__all__ = [
    "CLUSTER_STATS_CALLS",
    "VIEW_METRICS",
    "CohesityApiError",
    "CohesityAuthError",
    "CohesityClient",
    "CohesityConnectError",
    "CohesityEndpointError",
    "CohesityError",
    "CohesityFixtureError",
    "views_stats_path",
]
