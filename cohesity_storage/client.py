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
  only safe way to count outcomes. It is also why the window is the *only* thing that can be
  made cheaper when the endpoint is too slow to answer - see :data:`RUNS_WINDOW_OVERLAP_SECONDS`
  and the two fallback paths beside it. The per-group fallback is the exception: it takes
  ``numRuns``, so it asks for a *count* and sends no window at all. See :data:`RUNS_PER_GROUP`.
* An endpoint answering HTTP 500 to its own documented parameters is not hypothetical: the
  customer cluster does it on two of them. See the parameter-variant block below, which is what
  the client does about it.
"""

from __future__ import annotations

import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from . import domain
from .config import DEFAULT_RUNS_FANOUT_GROUPS, ClusterConfig
from .errors import (
    CohesityApiError,
    CohesityAuthError,
    CohesityConnectError,
    CohesityEndpointError,
    CohesityError,
    CohesityFixtureError,
    error_facts,
)
from .transport import V1_PREFIX, Repeated, build_transport

# The one call that leaves the /v2 API. Cohesity's own community exporters read the cluster id
# from here and pass it as the entityId on every cluster-level time-series call, which makes it
# the best first guess available - but the endpoint is unpublished for 6.8-7.4 and an API key
# without the legacy privilege gets a 403, so nothing may depend on it answering.
V1_CLUSTER_PATH = "/public/cluster"

# Paths are relative to the /v2 prefix the transport adds.
CLUSTER_STATUS_PATH = "/clusters/status"
CLUSTER_STORAGE_PATH = "/stats/cluster-storage"
TIME_SERIES_STATS_PATH = "/stats/time-series-stats"
STORAGE_DOMAINS_PATH = "/storage-domains"
TOP_VIEWS_PATH = "/stats/top-views"
VIEWS_PATH = "/stats/views"
PROTECTION_GROUPS_PATH = "/data-protect/protection-groups"
PROTECTION_RUNS_PATH = "/data-protect/runs/summary"

# Cheaper stand-ins for runs/summary, in the order they are tried when it times out or errors.
# Neither is a free swap. The flat list endpoint is *not* in the 7.3.2 reference this extension
# was written from - §4d of the research records only the per-group one - so a 404 from it is an
# ordinary answer and not a fault; the per-group one is documented for every version in range
# but costs one request per group, which is why it goes last, is capped per poll and rotates
# across polls rather than re-asking the same head of the list forever.
PROTECTION_RUNS_LIST_PATH = "/data-protect/protection-runs"
PROTECTION_GROUP_RUNS_PATH = "/data-protect/protection-groups/{group_id}/runs"

#: Names for the three sources, used in diagnostics and as the cache key for the winner.
RUNS_SOURCE_SUMMARY = "runs/summary"
RUNS_SOURCE_LIST = "protection-runs"
RUNS_SOURCE_PER_GROUP = "protection-groups/{id}/runs"

# How far past the poll interval the runs window reaches back.
#
# runs/summary has no pagination and no job filter - only a time window - so the window is the
# only dial there is, and on the customer cluster (41 protection groups, real history) the
# 15-minute window v0.1.5 sent produced no response at all inside 120 seconds. The window is
# therefore the poll interval plus this overlap and nothing more.
#
# The tradeoff is sharp in both directions and neither side is safe to ignore. Narrower than
# the poll interval and a run that starts and finishes between two polls is never seen, which
# loses a failure silently - the worst outcome this extension has. Wider and the cluster has to
# assemble every run in the window before it sends a byte, which is precisely what times out.
# The overlap is what makes a late, slow or skipped poll survivable, and it is affordable only
# because the run.id ledger throws the duplicates it causes away.
RUNS_WINDOW_OVERLAP_SECONDS = 120

# Runs asked of each group on the per-group path, and the reason that path sends no window.
#
# v0.1.6 asked every group "did a run finish between <poll interval + 2 min> ago and now?" and
# on the customer cluster - 42 groups, runs completing constantly - the answer was "0 run(s) in
# the window" every time, for twenty minutes, while last_success.age showed groups finishing
# minutes earlier. A window that narrow is almost always empty for any *one* group, and a run
# that lands while that group is not the one being asked is missed forever: the window moves on
# and nothing looks back. So the per-group path asks a question that cannot come back empty by
# accident - "your last N runs" - and lets the run.id ledger throw away what it has already
# counted. Re-reading the same runs every poll is exactly what the ledger is for.
#
# N is the tradeoff between the two ways of being wrong. Too small and a group cycling faster
# than the poll interval (hourly log backups on a 5-minute poll is fine; a 1-minute RPO job is
# not) finishes more runs between polls than one response carries, and the extra ones are lost
# the same way the window lost them. Too large and every group's response carries history that
# the ledger will discard, on a fan-out that is already one request per group - the cost is
# response size and cluster work, multiplied by the group count. 3 covers a group completing
# three runs inside one poll interval, which is well past any schedule a backup job sanely has.
RUNS_PER_GROUP = 3

# The ticket 16 probe, and everything about it is sized to be unnoticeable.
#
# ``includeObjectDetails=true`` makes the per-group runs endpoint return ``objects[].object``,
# which is the only place in the data this extension already reads where a per-VM identifier
# appears. Whether that identifier is the VMware BIOS UUID decides whether a protection group
# can ever be joined to a Dynatrace HOST, and it cannot be read from the docs.
#
# ONE group, ONE run, ONCE per client lifetime. The answer is a property of the cluster's data
# model rather than of any poll, so asking again every five minutes would buy nothing - and
# this cluster is unhealthy enough that the run collection which finally works in v0.1.7 must
# not be made to pay for a question. A failure is recorded and dropped for the same reason.
OBJECT_DETAILS_RUNS = 1

#: Marker for the one-shot object-details fact, shared by the probe and its failure path so a
#: probe that failed cannot be retried by way of a different marker.
OBJECT_DETAILS_MARKER = "protectedObjects"

#: Marker for the "a run carried no status anywhere" fact. Once per client lifetime: the answer
#: is a property of this cluster's run shapes, and the same field list every five minutes would
#: be noise. The run COUNT in it is one poll's, which is enough to tell "one odd job" from
#: "a third of the estate".
UNKNOWN_STATUS_MARKER = "runStatusUnknown"

# The host-link fan-out (ticket 16), and the three things that bound it.
#
# The probe above asks one group once. This asks several groups every poll, which is a
# different and much more expensive thing, so each bound is here rather than implied.
#
# 1. VMware only. The v0.1.8 probe settled that SQL objects carry no ``uuid`` field at all
#    (the keys are childObjects, entityId, environment, id, name, objectType, osType,
#    protectionType, sourceId), so a kSQL group can only ever spend a request to learn
#    nothing. Of 59 groups on the customer's estate ~13 are VMware, so this alone cuts the
#    fan-out by three quarters before any cap applies.
# 2. A per-poll REQUEST cap, which is ``min(maxRunFanoutGroups, HOST_LINK_GROUPS_PER_POLL)``
#    and rotates through :func:`_rotate` exactly as the runs fan-out does. Reusing the
#    operator's existing "this cluster is struggling" dial is deliberate: turning that down
#    has to turn everything down, or it is not a dial, it is a decoration.
# 3. A per-poll OBJECT cap (``maxHostLinkObjects``), because request count and series count
#    are not the same bound. One VMware group with 856 objects is one request and 856 series.
HOST_LINK_GROUPS_PER_POLL = 5

#: Runs asked of each group for object details. One: the newest run's object list is the
#: current membership of the job, and older runs only re-describe VMs that are still there or
#: describe ones that have left.
HOST_LINK_RUNS = 1

#: Markers for the host-link facts. One per distinct OUTCOME rather than one for the whole
#: feature, so "VMware objects carry no uuid" is said once and loudly, and a cluster that later
#: starts publishing them says so too - while neither is repeated every five minutes forever.
HOST_LINK_MARKERS = {
    "linked": "hostLink:linked",
    "empty": "hostLink:empty",
    "none": "hostLink:none",
    "capped": "hostLink:capped",
    "error": "hostLink:error",
}

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


# ---------------------------------------------------------------------------
# Parameter variants
#
# An HTTP 500 is the cluster *mishandling* a request, not refusing it. 403 says "you may not",
# 404 says "not here", and both are answers; 500 says the documented parameter set reached code
# that could not cope with it. On the customer cluster time-series-stats answers 500 for all 20
# entityId candidates and top-views answers 500 for both metrics, which rules out the values and
# leaves the shape - some parameter, or some combination of them, is the trigger.
#
# So on a 500 the request is retried in a small ordered set of alternative shapes, the first
# that answers 200 is cached for the life of the client, and which one won is reported. Every
# variant below is either a *documented* form or the removal of an optional parameter, checked
# against the 7.3.2 reference in research/04: nothing here invents a parameter, and nothing
# changes what is being asked for. The one shape deliberately absent is dropping top-views'
# `metric`, because the endpoint then answers with its default (kNumBytesRead) and the parser
# would happily file it under the metric that was never asked for - a wrong number reported
# confidently, which is worse than a missing one.
#
# Ordered cheapest and most-likely first. A variant that does not apply to the request in hand
# returns no requests and costs nothing.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParamVariant:
    """One alternative shape for a request, and the requests it becomes.

    ``build`` returns a *list* because "one metric per request" is as much a shape as "drop
    this parameter" is; the caller merges what comes back. An empty list means the variant has
    nothing to say about these particular parameters and is skipped without spending budget.
    """

    name: str
    build: Callable[[dict[str, Any]], list[dict[str, Any]]]

    def requests(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        return self.build(dict(params))


# The window the short-window variant asks for. One minute is below every rollup interval the
# extension uses, so if the cluster is choking on the amount of data a window implies rather
# than on a parameter, this is the variant that shows it.
VARIANT_WINDOW_SECONDS = 60

# numTopViews for the reduced-ranking variant, against a default of 20.
VARIANT_NUM_TOP_VIEWS = 5

# How many extra requests one poll may spend probing, across every endpoint and every schema.
# The probe is normally a once-per-client cost - the winner is cached - but a cluster that
# answers 500 to everything would otherwise re-probe every endpoint every poll, and that turns
# one slow poll into a request storm against a cluster already in trouble. Twelve is roughly
# the size of one ordinary poll, so a probing poll is at most twice a normal one.
MAX_VARIANT_REQUESTS_PER_POLL = 12


def _without(params: dict[str, Any], *names: str) -> list[dict[str, Any]]:
    """The same request with some optional parameters removed, or nothing if none were sent."""
    if not any(name in params for name in names):
        return []
    return [{name: value for name, value in params.items() if name not in names}]


def _repeated_metric_names(params: dict[str, Any]) -> list[dict[str, Any]]:
    """``metricNames=a&metricNames=b`` - the form the spec says is wrong.

    First because it is the cheapest thing to be wrong about: the spec declares
    ``explode: false``, but a server that mis-parses the comma-joined value it asked for is
    exactly the kind of thing that answers 500 rather than 400.
    """
    names = params.get("metricNames")
    if not isinstance(names, (list, tuple)) or not names:
        return []
    return [{**params, "metricNames": Repeated(names)}]


def _one_metric_per_request(params: dict[str, Any]) -> list[dict[str, Any]]:
    """One request per metric name - last, because it is the only variant that costs several."""
    names = params.get("metricNames")
    if not isinstance(names, (list, tuple)) or len(names) < 2:
        return []
    return [{**params, "metricNames": (name,)} for name in names]


def _short_window(params: dict[str, Any]) -> list[dict[str, Any]]:
    if "startTimeMsecs" not in params:
        return []
    start = int(time.time() * 1000) - VARIANT_WINDOW_SECONDS * 1000
    return [{**params, "startTimeMsecs": start}]


def _metric_only(params: dict[str, Any]) -> list[dict[str, Any]]:
    """Just ``metric`` - the one parameter the endpoint requires, and nothing else."""
    metric = params.get("metric")
    return [{"metric": metric}] if metric else []


TIME_SERIES_VARIANTS = (
    ParamVariant("metricNames-repeated", _repeated_metric_names),
    ParamVariant("no-rollupIntervalSecs", lambda params: _without(params, "rollupIntervalSecs")),
    ParamVariant(
        "no-rollup", lambda params: _without(params, "rollupFunction", "rollupIntervalSecs")
    ),
    ParamVariant("short-window", _short_window),
    ParamVariant("one-metric-per-request", _one_metric_per_request),
)

TOP_VIEWS_VARIANTS = (
    ParamVariant("no-protocol", lambda params: _without(params, "protocol")),
    ParamVariant("no-lastHours", lambda params: _without(params, "lastHours")),
    ParamVariant(
        "fewer-numTopViews",
        lambda params: [{**params, "numTopViews": VARIANT_NUM_TOP_VIEWS}]
        if "numTopViews" in params
        else [],
    ),
    ParamVariant("metric-only", _metric_only),
)


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
        # Kept because ClusterStatus deliberately drops everything it does not model, and the
        # entityId probe wants every id the body happened to carry.
        self._cluster_status_payload: Any = None
        self._run_ledger = domain.RunLedger()
        # entityId probe state. The candidate list is built once (it costs two extra requests);
        # the winner is cached per schema so every poll after the first makes one call each.
        self._entity_id_candidates: tuple[str, ...] | None = None
        self._entity_id_cache: dict[str, str] = {}
        self._entity_ids_tried: dict[str, tuple[str, ...]] = {}
        # Parameter-shape state. The winning variant per path is cached for the life of the
        # client; the budget bounds how many extra requests one poll may spend looking for one.
        self._param_variants: dict[str, ParamVariant] = {}
        self._variant_budget = MAX_VARIANT_REQUESTS_PER_POLL
        self._variant_requests = 0
        # Which runs endpoint last worked, so a cluster that has already rejected runs/summary
        # is not asked again first on every poll for the rest of the extension's uptime.
        self._runs_source = ""
        # Where the per-group runs fan-out stopped last poll, and what it saw. The cursor is
        # what turns a cap into a rotation instead of a blind spot - see :meth:`_runs_from_groups`.
        self._runs_group_cursor = 0
        self._runs_fanout: dict[str, Any] | None = None
        # Whether the one-shot object-details probe has been spent. Separate from the
        # diagnostics marker set because it is set BEFORE the request rather than after the
        # fact is recorded - an unexpected exception must retire the probe too.
        self._object_details_probed = False
        # Where the host-link fan-out stopped last poll. Its own cursor rather than the runs
        # one: the two walk different lists (every group vs VMware groups only) and sharing a
        # position between them would make each skip what the other had just read.
        self._host_link_cursor = 0
        # Facts worth getting out of the ActiveGate, recorded once each and drained by the
        # caller. See :meth:`take_diagnostics`.
        self._diagnostics: list[dict[str, Any]] = []
        self._diagnosed: set[str] = set()

    # -- lifecycle ---------------------------------------------------------

    def begin_poll(self) -> None:
        """Open a poll's request budget. Called once per poll, before anything is collected.

        The budget is per poll rather than per client because a variant probe that found
        nothing has to be allowed to try again later - a 500 can be a cluster having a bad
        minute - while a cluster that answers 500 to everything must not be able to turn every
        poll into a fan-out. A client nobody calls this on still has the opening budget from
        __init__, spends it once and then stops probing, which is the safe direction to fail.
        """
        self._variant_budget = MAX_VARIANT_REQUESTS_PER_POLL
        self._variant_requests = 0

    @property
    def variant_requests(self) -> int:
        """Extra requests spent probing parameter shapes since :meth:`begin_poll`. For logs."""
        return self._variant_requests

    @property
    def runs_source(self) -> str:
        """Which runs endpoint last answered, or empty before any of them has been asked."""
        return self._runs_source

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
            self._cluster_status_payload = payload
            self._diagnose(
                "version",
                {
                    "kind": "cluster_version",
                    "version": status.software_version,
                    "nodeCount": status.node_count,
                    "source": self.describe(),
                },
            )
        return self._cluster_status

    @property
    def software_version(self) -> str:
        """The version if it has already been fetched - never a reason to go and fetch it.

        Diagnostics are reported on the way out of a poll, including the poll where
        ``/v2/clusters/status`` is the thing that failed. Reading the cached value means the
        act of describing a failure cannot itself fail.
        """
        status = self._cluster_status
        return (status.software_version if status else "") or ""

    def namespaced(self, object_id: str | int) -> str:
        """A cluster-scoped Cohesity id, namespaced so it stays unique across clusters."""
        return domain.namespace_id(self.cluster_status().cluster_id, object_id)

    def cluster_storage(self) -> domain.ClusterStorage:
        """Headline capacity. No parameters, no paging, seven scalars - the safest call there is."""
        return domain.parse_cluster_storage(self._transport.get(CLUSTER_STORAGE_PATH))

    # -- parameter variants ------------------------------------------------

    def _get_with_variants(
        self, path: str, params: dict[str, Any], variants: tuple[ParamVariant, ...]
    ) -> list[Any]:
        """GET ``path``, and on an HTTP 500 retry it in other shapes until one answers.

        Returns a list of payloads: one normally, several when the winning shape splits the
        request. Anything that is not a 500 is raised untouched - a 401, a 403 and a 404 each
        have their own meaning and their own fix, and retrying them in a different shape would
        bury all three under "the extension tried some things and gave up".
        """
        cached = self._param_variants.get(path)
        shapes = cached.requests(params) if cached is not None else []
        if shapes:
            return [self._transport.get(path, shape) for shape in shapes]

        # No cached shape, or one that does not apply to this particular request - the
        # one-metric-per-request shape against a schema that only asks for one metric, say.
        # Falling through rather than sending nothing: an empty payload list would read as a
        # schema with no data, which is the silent failure this whole module is built around.
        try:
            return [self._transport.get(path, params)]
        except CohesityError as exception:
            if not _is_server_error(exception):
                raise
            payloads = self._probe_param_variants(path, params, variants, exception)
            if payloads is None:
                # Nothing worked, so the caller sees the failure it would have seen anyway -
                # with the diagnostic recorded above saying what was tried.
                raise
            return payloads

    def _probe_param_variants(
        self,
        path: str,
        params: dict[str, Any],
        variants: tuple[ParamVariant, ...],
        original: BaseException,
    ) -> list[Any] | None:
        """Try each variant in turn; keep and cache the first that answers. None if none did."""
        attempts: list[str] = []
        spent = 0
        for variant in variants:
            shapes = variant.requests(params)
            if not shapes:
                # Does not apply to this request - a metric-splitting variant against a single
                # metric, say. Not recorded as an attempt, because nothing was attempted.
                continue
            if len(shapes) > self._variant_budget:
                attempts.append(f"{variant.name}=skipped, poll request budget spent")
                break
            payloads = []
            try:
                for shape in shapes:
                    self._variant_budget -= 1
                    self._variant_requests += 1
                    spent += 1
                    payloads.append(self._transport.get(path, shape))
            except CohesityError as exception:
                attempts.append(f"{variant.name}={_outcome(exception)}")
                continue
            attempts.append(f"{variant.name}=ok")
            self._param_variants[path] = variant
            self._diagnose(
                f"variant:{path}:resolved",
                {
                    "kind": "param_variant",
                    "path": path,
                    "variant": variant.name,
                    "attempts": list(attempts),
                    "requests": spent,
                    "resolved": True,
                },
            )
            return payloads

        self._diagnose(
            # A separate marker from the resolved one, so a probe that fails on one poll and
            # succeeds on a later one still reports the win. Both are once per client.
            f"variant:{path}:failed",
            {
                "kind": "param_variant",
                "path": path,
                "variant": "",
                "attempts": list(attempts),
                "requests": spent,
                "resolved": False,
                **error_facts(original, self._secrets()),
            },
        )
        return None

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

        series: dict[str, domain.TimeSeriesMetric] = {}
        for payload in self._get_with_variants(TIME_SERIES_STATS_PATH, params, TIME_SERIES_VARIANTS):
            for name, metric in domain.parse_time_series(payload).items():
                current = series.get(name)
                # Several payloads only happen under the one-metric-per-request variant, where
                # each answers a different metric, so a collision means the cluster echoed a
                # series nobody asked for. Prefer whichever copy carries points: an empty
                # series overwriting a populated one is the silent gap this extension keeps
                # meeting, and it is not worth risking to save a branch.
                if current is None or (metric.data_points and not current.data_points):
                    series[name] = metric
        return series

    def entity_id_candidates(self) -> tuple[str, ...]:
        """Every id worth trying as the ``entityId`` of a cluster-level schema, best first.

        v0.1.3 asserted one id - ``ClusterStatus.clusterId`` - and on a real customer cluster all
        five cluster-level metrics went missing because that assertion was wrong. The failure is
        silent (empty ``dataPoints``, HTTP 200), so nothing surfaced it. This replaces the
        assertion with an ordered list and lets :meth:`cluster_time_series` find out.

        Order is by how much each source is worth, not by convenience:

        1. the id from v1 ``GET /public/cluster`` - what Cohesity's own community exporters pass
           on every cluster-level time-series call, so it is the only candidate with field
           evidence behind it;
        2. ``clusterId`` from ``/v2/clusters/status`` - the v0.1.3 assumption, kept because on
           most clusters it is the same int64 and dedups away to nothing;
        3. the incarnation ids either response happened to carry;
        4. any ``entityId`` already returned by ``/v2/storage-domains?includeTimeSeriesSchema``
           - a *storage domain's* entity id, so a long shot for a cluster schema, but it is the
           only entityId the v2 API ever hands out and it costs one request to include.

        Built once and cached: the two discovery calls are worth making on the first poll and
        worth never making again.
        """
        if self._entity_id_candidates is not None:
            return self._entity_id_candidates

        status_payload = self._cluster_status_payload
        if status_payload is None:
            self.cluster_status()
            status_payload = self._cluster_status_payload
        v1_payload = self._v1_cluster()

        candidates = domain.ordered_unique(
            [
                *domain.cluster_id_candidates(v1_payload),
                *domain.cluster_id_candidates(status_payload, ("clusterId", "id")),
                *domain.cluster_id_candidates(v1_payload, domain.CLUSTER_ALTERNATE_ID_FIELDS),
                *domain.cluster_id_candidates(status_payload, domain.CLUSTER_ALTERNATE_ID_FIELDS),
                *self._schema_entity_ids(),
            ]
        )
        self._entity_id_candidates = tuple(candidates)
        return self._entity_id_candidates

    def _v1_cluster(self) -> Any:
        """The v1 ``/public/cluster`` body, or None if this cluster will not serve it.

        Unpublished for the whole 6.8-7.4 range and gated by a privilege the v2 key may not
        carry, so 403 and 404 are ordinary answers here rather than faults. Every Cohesity
        failure is swallowed for that reason: this call exists to *offer* a candidate, and a
        source with nothing to offer must not cost the poll.
        """
        try:
            return self._transport.get(V1_CLUSTER_PATH, prefix=V1_PREFIX)
        except CohesityError:
            return None

    def _schema_entity_ids(self) -> list[str]:
        """entityIds the storage-domain schema catalogue already knows about.

        Same rule as :meth:`_v1_cluster`: a discovery call that fails contributes nothing and
        breaks nothing. The catalogue is also the only place the v2 API states an entityId at
        all, which is why a storage domain's id is worth trying against a cluster schema.
        """
        try:
            domains = self.storage_domain_schemas()
        except CohesityError:
            return []
        return [ref.entity_id for domain_ in domains for ref in domain_.schemas if ref.entity_id]

    def entity_ids_tried(self, schema_name: str) -> tuple[str, ...]:
        """Which candidates were offered to this schema. For the warning that names them."""
        return self._entity_ids_tried.get(schema_name, ())

    def cluster_time_series(self, call: dict[str, Any]) -> dict[str, domain.TimeSeriesMetric]:
        """One entry of :data:`CLUSTER_STATS_CALLS`, against whichever entityId this schema answers to.

        On the first poll each candidate from :meth:`entity_id_candidates` is tried in order
        until one returns a data point, and the winner is then cached for the life of the
        client - so a probe that costs N requests once costs one request on every poll after.

        A schema no candidate satisfies returns the *last* empty result rather than an empty
        dict, so the caller still sees the requested series and can warn about them by name.
        Nothing is invented to fill the gap: emitting a zero for a metric the cluster declined
        to report would read as an idle cluster rather than as a broken lookup.
        """
        schema_name = call["schemaName"]
        rollup_function = call.get("rollupFunction")
        rollup_interval_secs = call.get("rollupIntervalSecs")

        cached = self._entity_id_cache.get(schema_name)
        if cached is not None:
            return self.time_series(
                schema_name,
                call["metricNames"],
                cached,
                rollup_function=rollup_function,
                rollup_interval_secs=rollup_interval_secs,
            )

        candidates = self.entity_id_candidates()
        self._entity_ids_tried[schema_name] = candidates
        marker = f"entityId:{schema_name}"
        # What each candidate actually did, in order. The state of the entityId investigation
        # IS "which ids were offered and what came back", so outcomes are recorded as they
        # happen rather than summarised at the end - an exception on the first candidate used
        # to throw all of it away, and that is the case the customer cluster hit.
        attempts: list[str] = []
        series: dict[str, domain.TimeSeriesMetric] = {}
        for candidate in candidates:
            try:
                series = self.time_series(
                    schema_name,
                    call["metricNames"],
                    candidate,
                    rollup_function=rollup_function,
                    rollup_interval_secs=rollup_interval_secs,
                )
            except Exception as exception:
                attempts.append(f"{candidate}={_outcome(exception)}")
                self._diagnose(
                    marker,
                    {
                        "kind": "entity_id_probe",
                        "schema": schema_name,
                        "entityId": "",
                        "candidates": list(candidates),
                        "attempts": list(attempts),
                        "resolved": False,
                        **error_facts(exception, self._secrets()),
                    },
                )
                # Re-raised, not swallowed: the section above still has to know the call
                # failed, and a probe that quietly reported nothing would be the old bug again.
                raise
            if any(metric.data_points for metric in series.values()):
                attempts.append(f"{candidate}=data")
                self._entity_id_cache[schema_name] = candidate
                self._diagnose(
                    marker,
                    {
                        "kind": "entity_id_probe",
                        "schema": schema_name,
                        "entityId": candidate,
                        "candidates": list(candidates),
                        "attempts": list(attempts),
                        "resolved": True,
                    },
                )
                return series
            attempts.append(f"{candidate}=empty")

        self._diagnose(
            marker,
            {
                "kind": "entity_id_probe",
                "schema": schema_name,
                "entityId": "",
                "candidates": list(candidates),
                "attempts": list(attempts),
                "resolved": False,
            },
        )
        return series

    # -- storage domains ---------------------------------------------------

    def storage_domains(self) -> list[domain.StorageDomain]:
        """Storage domains with their usage stats. Few enough to enumerate - typically under 20.

        Note the stats are not live: every ``*Bytes`` field in ``DataUsageStats`` has a paired
        ``*TimestampUsec``, and ``outdatedLogicalUsageBytes`` is documented as possibly stale.
        Worth remembering before believing a number that disagrees with the cluster UI.
        """
        payload = self._transport.get(STORAGE_DOMAINS_PATH, {"includeStats": True})
        fields = domain.storage_domain_stats_fields(payload)
        if fields:
            # Which names this cluster actually publishes - the fact that settles
            # STORAGE_DOMAIN_LOGICAL_FIELDS and STORAGE_DOMAIN_PHYSICAL_FIELDS. Names only.
            self._diagnose(
                "storageDomainStatsFields",
                {"kind": "storage_domain_stats_fields", "fields": list(fields)},
            )
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
            payloads = self._get_with_variants(path, params, TOP_VIEWS_VARIANTS)
        except CohesityEndpointError:
            if path == VIEWS_PATH:
                raise
            # A 404 is never probed for a parameter shape - it is the version fork, and the
            # deprecated path is the answer to it. The variants still apply to the fallback:
            # a 7.2 cluster can mishandle a parameter just as a 7.3.2 one can.
            payloads = self._get_with_variants(VIEWS_PATH, params, TOP_VIEWS_VARIANTS)
        views: list[domain.ViewStats] = []
        for payload in payloads:
            views.extend(domain.parse_views_stats(payload, metric))
        return views

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

    def probe_protected_objects(self, groups: list[domain.ProtectionGroup] | None = None) -> None:
        """Ask one protection group, once, what identifiers its protected objects carry.

        This is a question, not a collection, and it is written so that it can never become
        anything else. It never raises, it never runs twice, and it costs exactly one request
        for the life of the client. Run collection is the thing that finally works in v0.1.7;
        an enrichment experiment that could break it would not be worth the answer.

        What comes back goes out as a fact on the diagnostics channel: the key names under
        ``objects[].object``, whether a VMware-specific sub-object is there and what it calls
        its own keys, the protection group's environment, and the shape verdict for up to three
        uuids. That is enough to settle ticket 16 either way - a ``uuid-8-4-4-4-12`` on a
        kVMware group means the join to ``host.additional_system_info["system.serial"]`` is
        real; a ``numeric-id`` means it never was and nothing further should be built on it.
        """
        if self._object_details_probed:
            return
        # Set before anything can fail, so "once per client lifetime" holds just as firmly for
        # a probe that raises as for one that answers.
        self._object_details_probed = True
        if self.is_replaying:
            # Replay resolves a file per request path, and the per-group runs path is keyed by
            # a group id that exists only in one particular fixture set. A missing-fixture
            # ERROR on every replayed poll would be a worse answer than no answer. The fake
            # cluster the e2e harness runs against speaks real HTTP and does exercise this.
            return
        try:
            fact = self._protected_objects_fact(groups)
        except Exception as exception:  # noqa: BLE001 - a question must never cost an answer
            fact = {
                "kind": "protected_objects",
                "environment": "",
                "objects": 0,
                **error_facts(exception, self._secrets()),
            }
        self._diagnose(OBJECT_DETAILS_MARKER, fact)

    def _protected_objects_fact(
        self, groups: list[domain.ProtectionGroup] | None
    ) -> dict[str, Any]:
        """The one request and what it said, as a fact. Raises; the caller turns that into one."""
        if groups is None:
            groups = self.protection_groups()
        group = _object_details_group(groups)
        if group is None:
            return {"kind": "protected_objects", "environment": "", "objects": 0}
        path = PROTECTION_GROUP_RUNS_PATH.format(
            group_id=urllib.parse.quote(str(group.id), safe="")
        )
        payload = self._transport.get(
            path, {"numRuns": OBJECT_DETAILS_RUNS, "includeObjectDetails": True}
        )
        shape = domain.parse_protected_object_shape(payload, environment=group.environment)
        return {
            "kind": "protected_objects",
            "environment": shape.environment,
            "objects": shape.objects_seen,
            "fields": list(shape.object_fields),
            "vmwareKey": shape.vmware_key,
            "vmwareFields": list(shape.vmware_fields),
            # Values, deliberately - see ProtectedObjectShape. Names, addresses and everything
            # else the object carries stay behind as field NAMES and never as values.
            "uuids": [
                {"value": value, "verdict": verdict} for value, verdict in shape.uuid_samples
            ],
        }

    def protected_object_links(
        self, groups: list[domain.ProtectionGroup] | None = None
    ) -> list[domain.ProtectedObjectLink]:
        """(protection group, VM BIOS UUID) pairs for the VMware groups this poll reached.

        Ticket 16's collection. It is the only thing this extension does whose cost scales with
        the size of the *protected estate* rather than with the number of Cohesity objects being
        monitored, so it is off unless asked for and bounded three ways -
        see :data:`HOST_LINK_GROUPS_PER_POLL` for which bounds and why each one is needed.

        **It never raises.** A cluster that refuses object details must not cost the protection
        metrics v0.1.7 finally got working; every failure becomes a diagnostic and an empty
        list. That is also why the caller runs it last.

        Returns an empty list - having said so on the diagnostics channel - when the toggle is
        off, when there are no VMware groups, when nothing answered, or when the objects that
        came back carried no usable BIOS UUID. That last case is the one the ticket cares about
        most and it is reported at ERROR: it means the join is impossible on this cluster, and
        no fallback join on object names is attempted, because a name join draws confident
        wrong edges rather than no edges.
        """
        if not self._config.collect_host_link:
            return []
        if self.is_replaying:
            # Same reason the probe skips replay: a per-group runs path resolves to a fixture
            # key built from the group id, and only one fixture set could ever carry it. The
            # e2e harness runs against a fake cluster over real HTTP and does exercise this.
            return []
        try:
            return self._host_link_fanout(groups)
        except Exception as exception:  # noqa: BLE001 - an enrichment must never cost a metric
            self._diagnose(
                HOST_LINK_MARKERS["error"],
                {"kind": "host_link", **error_facts(exception, self._secrets())},
            )
            return []

    def _host_link_fanout(
        self, groups: list[domain.ProtectionGroup] | None
    ) -> list[domain.ProtectedObjectLink]:
        """The bounded fan-out and its counters. Raises; the caller turns that into a fact."""
        if groups is None:
            groups = self.protection_groups()
        # Deleted groups are excluded for the same reason the probe excludes them: their
        # objects describe an estate that is gone, and an edge to a host from a job that no
        # longer exists is worse than no edge.
        vmware = [
            group
            for group in _runs_fanout_order(groups)
            if domain.is_vmware_environment(group.environment) and not group.is_deleted
        ]
        cap_objects = self._config.max_host_link_objects
        if not vmware:
            self._diagnose(
                HOST_LINK_MARKERS["none"],
                {"kind": "host_link", "groups_vmware": 0, "cap": cap_objects},
            )
            return []

        # min() rather than a constant of its own: an operator who turns maxRunFanoutGroups
        # down because the cluster is struggling means "spend fewer requests on it", and a
        # second fan-out that ignored that would make the dial a lie.
        budget = min(self._config.max_run_fanout_groups, HOST_LINK_GROUPS_PER_POLL)
        selected, cursor = _rotate(vmware, self._host_link_cursor, budget)
        self._host_link_cursor = cursor

        links: list[domain.ProtectedObjectLink] = []
        verdicts: set[str] = set()
        seen = 0
        errored = 0
        capped = False
        for group in selected:
            try:
                parsed = self._host_link_for_group(group)
            except CohesityError:
                # One job the key cannot see must not cost the others, exactly as in the runs
                # fan-out. Counted so the diagnostic can tell "nothing answered" from "nothing
                # to say".
                errored += 1
                continue
            seen += parsed.objects_seen
            verdicts.update(parsed.verdicts)
            for link in parsed.links:
                if len(links) >= cap_objects:
                    capped = True
                    break
                links.append(link)
            if capped:
                break

        self._diagnose(
            HOST_LINK_MARKERS["capped"]
            if capped
            else HOST_LINK_MARKERS["linked"]
            if links
            else HOST_LINK_MARKERS["empty"],
            {
                "kind": "host_link",
                "groups_vmware": len(vmware),
                "groups_queried": len(selected),
                "groups_errored": errored,
                "objects_seen": seen,
                "objects_linked": len(links),
                "capped": capped,
                "cap": cap_objects,
                "verdicts": sorted(verdicts),
            },
        )
        return links

    def _host_link_for_group(self, group: domain.ProtectionGroup) -> domain.ProtectedObjectLinks:
        """One group's newest run, asked for its object details and reduced to uuid pairs."""
        path = PROTECTION_GROUP_RUNS_PATH.format(
            group_id=urllib.parse.quote(str(group.id), safe="")
        )
        payload = self._transport.get(
            path, {"numRuns": HOST_LINK_RUNS, "includeObjectDetails": True}
        )
        return domain.parse_protected_object_links(
            payload,
            group_id=str(group.id),
            group_name=group.name,
            # Namespaced HERE, because this is the layer that knows which cluster answered.
            # It must be the same spelling the smartscape rules already use for this group or
            # the edge resolves to a second, empty copy of the entity.
            namespaced_group_id=domain.namespace_id(self.cluster_status().cluster_id, group.id),
        )

    def protection_runs(
        self,
        *,
        start_time_usecs: int | None = None,
        end_time_usecs: int | None = None,
        window_seconds: int | None = None,
        groups: list[domain.ProtectionGroup] | None = None,
    ) -> list[domain.ProtectionRun]:
        """Every run the cluster reports in the window, terminal or not, deduplicated or not.

        The window is the poll interval plus :data:`RUNS_WINDOW_OVERLAP_SECONDS` and both ends
        are sent explicitly. Up to v0.1.5 it was three times the interval with no end bound,
        which on the customer cluster - 41 groups with real history - produced no response at
        all inside 120 seconds; runs/summary has no pagination and no job filter, so the window
        is the only thing there is to make smaller. The overlap is deliberate and is exactly
        why :meth:`new_protection_runs` exists - raw output from here double counts by design.

        ``groups`` is only read by the per-group fallback, and only if it is reached. Passing
        the groups the caller already fetched keeps that fallback from paying for them twice.
        """
        if window_seconds is None:
            window_seconds = self._config.interval_minutes * 60 + RUNS_WINDOW_OVERLAP_SECONDS
        now_usecs = int(time.time() * 1_000_000)
        start = (
            start_time_usecs
            if start_time_usecs is not None
            else now_usecs - window_seconds * 1_000_000
        )
        # Sent rather than left to default. The endpoint documents endTimeUsecs as "now" when
        # absent, which is the same instant - but an explicit bound is what makes the window
        # the extension asked for the window the cluster builds, and it is the only other dial
        # on an endpoint that has two.
        end = end_time_usecs if end_time_usecs is not None else now_usecs
        return self._runs_from_any_source(start, end, groups)

    def _runs_from_any_source(
        self, start: int, end: int, groups: list[domain.ProtectionGroup] | None
    ) -> list[domain.ProtectionRun]:
        """runs/summary, then the flat list, then per group - whichever answers first.

        Losing the five run metrics because one endpoint is slow is a bad trade when two other
        endpoints report the same runs. The winner is remembered, so a cluster that has already
        refused runs/summary is not made to time out on it again every poll - but the full
        order is still walked from the winner on, because an endpoint that worked once can
        stop working and the next one down is still better than nothing.
        """
        attempts: list[str] = []
        failure: BaseException | None = None
        for name, fetch in self._runs_sources():
            try:
                runs = fetch(start, end, groups)
            except CohesityError as exception:
                failure = failure or exception
                attempts.append(f"{name}={_outcome(exception)}")
                continue
            attempts.append(f"{name}=ok")
            self._runs_source = name
            self._diagnose(
                f"runs_source:{name}",
                {
                    "kind": "runs_source",
                    "source": name,
                    "attempts": list(attempts),
                    "runs": len(runs),
                },
            )
            return runs

        self._diagnose(
            "runs_source:none",
            {
                "kind": "runs_source",
                "source": "",
                "attempts": list(attempts),
                "runs": 0,
                **(error_facts(failure, self._secrets()) if failure else {}),
            },
        )
        if failure is not None:
            raise failure
        return []

    def _runs_sources(self) -> list[tuple[str, Any]]:
        """The three sources, with whichever last worked moved to the front."""
        sources = [
            (RUNS_SOURCE_SUMMARY, self._runs_from_summary),
            (RUNS_SOURCE_LIST, self._runs_from_list),
            (RUNS_SOURCE_PER_GROUP, self._runs_from_groups),
        ]
        index = [name for name, _ in sources].index(self._runs_source) if self._runs_source else 0
        return sources[index:] + sources[:index]

    def _runs_from_summary(self, start: int, end: int, _groups) -> list[domain.ProtectionRun]:
        params = {"startTimeUsecs": start, "endTimeUsecs": end}
        payload = self._transport.get(PROTECTION_RUNS_PATH, params)
        return self._note_unknown_status(payload, domain.parse_protection_runs(payload))

    def _runs_from_list(self, start: int, end: int, _groups) -> list[domain.ProtectionRun]:
        params = {"startTimeUsecs": start, "endTimeUsecs": end}
        payload = self._transport.get(PROTECTION_RUNS_LIST_PATH, params)
        return self._note_unknown_status(payload, domain.parse_run_list(payload))

    def _note_unknown_status(
        self, payload: Any, runs: list[domain.ProtectionRun]
    ) -> list[domain.ProtectionRun]:
        """Report, once, that some run's status could not be found - and where to look instead.

        Measured on the customer tenant: 28 of 92 counted runs carried no status dimension at
        all, because their status is in neither ``localBackupInfo`` nor the run root. Those runs
        now say ``unknown``, which makes the hole countable - but ``unknown`` on its own does
        not say where the status really is, and guessing from a schema is how the hole got
        there.

        So this carries out the KEY NAMES the cluster actually sent for one such run, one level
        deep. Exactly the trick that found the storage-domain stats field names. Names only: no
        job name, no object name, no number, nothing that would matter if the log stream were
        read more widely.

        Returns ``runs`` so it can wrap a parse in one line at each of the three call sites.
        """
        unknown = [run for run in runs if run.status == domain.RUN_STATUS_UNKNOWN]
        if not unknown:
            return runs
        self._diagnose(
            UNKNOWN_STATUS_MARKER,
            {
                "kind": "run_status_unknown",
                "runs": len(unknown),
                "fields": list(domain.run_field_names(payload, unknown[0].id)),
            },
        )
        return runs

    def _runs_from_groups(
        self, _start: int, _end: int, groups: list[domain.ProtectionGroup] | None
    ) -> list[domain.ProtectionRun]:
        """One request per group, rotating, asking each for its last runs. Priced like a last resort.

        **No time window here**, and that is the v0.1.7 fix. The window is what the other two
        sources are built around because they have no other filter, but this endpoint does: it
        takes ``numRuns`` (the count parameter documented for ``GetProtectionGroupRuns`` on
        every version in range). Asking one group "did you finish a run in the last seven
        minutes?" is almost always answered "no" even on a cluster where runs finish constantly,
        and anything that finished while a different group was being asked is gone for good.
        Asking "what are your last 3 runs?" cannot miss that way, and the ledger in
        :meth:`new_protection_runs` makes re-reading the same runs every poll free.

        The cap rotates rather than truncating. Most-recently-finished still orders the list -
        a group that just finished is the one most likely to be carrying something new - but
        the poll starts where the last one stopped, so a cap smaller than the group count
        delays a group rather than excluding it. 42 groups at 20 per poll is every group seen
        every second or third poll; the old fixed top-10 never looked at the other 32 at all.

        A group that fails individually is skipped, counted and left behind - on a cluster
        answering HTTP 500 from two other endpoints, one job the key cannot see must not cost
        the rest. Every group failing is a different thing and is raised, so the caller does not
        read "no runs" off an endpoint that never answered.
        """
        if groups is None:
            groups = self.protection_groups()
        ordered = _runs_fanout_order(groups)
        if not ordered:
            self._runs_fanout = None
            return []
        selected, cursor = _rotate(ordered, self._runs_group_cursor, self._config.max_run_fanout_groups)
        self._runs_group_cursor = cursor
        runs: list[domain.ProtectionRun] = []
        failure: BaseException | None = None
        errored = 0
        for group in selected:
            path = PROTECTION_GROUP_RUNS_PATH.format(
                # Cohesity group ids are colon-joined int64s, which are legal in a path
                # segment - quoted anyway, because the id is cluster data and this is the one
                # place in the extension where cluster data becomes a URL.
                group_id=urllib.parse.quote(str(group.id), safe="")
            )
            try:
                payload = self._transport.get(path, {"numRuns": RUNS_PER_GROUP})
            except CohesityError as exception:
                failure = failure or exception
                errored += 1
                continue
            runs.extend(
                self._note_unknown_status(
                    payload,
                    domain.parse_run_list(payload, group_id=group.id, group_name=group.name),
                )
            )
        # Held, not reported, until the ledger has had its say: "how many were new" is the half
        # of this fact that separates "nothing ran" from "we are not looking in the right
        # place", and only new_protection_runs knows it.
        self._runs_fanout = {
            "kind": "runs_fanout",
            "groups_total": len(ordered),
            "groups_queried": len(selected),
            "groups_errored": errored,
            "runs_per_group": RUNS_PER_GROUP,
            "requests": len(selected),
            "runs_seen": len(runs),
            "runs_new": 0,
        }
        if errored == len(selected) and failure is not None:
            # Published before the raise: the poll where every group refused is the one whose
            # counters are worth the most, and the exception means nobody downstream will get
            # round to calling _report_fanout for it.
            self._report_fanout(0)
            raise failure
        return runs

    def _report_fanout(self, new_runs: int) -> None:
        """Publish the held fan-out counters, now that the dedup count is known.

        Deliberately *not* routed through :meth:`_diagnose`: every other fact is a property of
        the cluster and is worth saying once, but these are per-poll counters and a single
        sample of them answers nothing. Twenty minutes of "0 run(s)" with no way to tell
        whether the fan-out was even asking the right groups is exactly what cost this round,
        and one record per poll per cluster is a rounding error against the metric ingest the
        same poll produces.

        Only ever one pending: a caller that never drains diagnostics keeps the newest fact
        rather than an ever-growing list of them.
        """
        fanout = self._runs_fanout
        self._runs_fanout = None
        if fanout is None:
            return
        fanout["runs_new"] = new_runs
        self._diagnostics = [
            fact for fact in self._diagnostics if fact.get("kind") != "runs_fanout"
        ]
        self._diagnostics.append(fanout)

    def new_protection_runs(self, **kwargs) -> list[domain.ProtectionRun]:
        """Runs that have finished and have not been counted before.

        This is the method the metric layer should use. Calling :meth:`protection_runs` directly
        and counting what it returns inflates every failure by the window-to-interval ratio, and
        the symptom looks like a Cohesity problem rather than an extension bug, so it will be
        believed.

        The ledger is keyed on ``run.id``, which is why switching between the three runs
        sources mid-life is safe: all three report the same run under the same id, so a run
        already counted from runs/summary is not counted again from the per-group endpoint.

        It is also what makes the per-group path's windowless "last N runs" question safe: that
        question returns the same runs poll after poll on purpose, and every one of them past
        the first is discarded here.
        """
        fresh = domain.new_terminal_runs(self.protection_runs(**kwargs), self._run_ledger)
        self._report_fanout(len(fresh))
        return fresh

    @property
    def runs_fanout(self) -> dict[str, Any] | None:
        """The per-group fan-out's counters for the poll just finished, if it ran. For logs."""
        for fact in reversed(self._diagnostics):
            if fact.get("kind") == "runs_fanout":
                return fact
        return None

    @property
    def counted_run_ids(self) -> int:
        """How many run ids the dedup ledger is holding. For logging and for tests."""
        return len(self._run_ledger)

    # -- diagnostics -------------------------------------------------------

    def _diagnose(self, marker: str, fact: dict[str, Any]) -> None:
        """Record one fact about this cluster, the first time it is learned and never again.

        Facts, not log records: the caller turns these into log events, so this module keeps its
        promise of no Dynatrace import. The marker is what makes it once per client lifetime
        rather than once per poll - the same three or four facts re-sent every five minutes
        would be noise that costs ingest and teaches nobody anything new.
        """
        if marker in self._diagnosed:
            return
        self._diagnosed.add(marker)
        self._diagnostics.append(fact)

    def note_failure(self, label: str, exception: BaseException) -> None:
        """Record a collection failure as a fact, so it leaves the ActiveGate.

        The whole reason this exists: ``_section`` catches a failing section into
        ``self.logger``, and on the tenant this was debugged against those log lines do not
        reach Grail. Five metrics were missing and the error explaining why was written where
        nobody can read it. A fact goes out over log ingest, which does arrive.

        Once per label per client, like every other fact. The same 403 comes back every five
        minutes forever; reporting it every five minutes forever teaches nobody anything after
        the first one.
        """
        self._diagnose(
            f"failure:{label}",
            {
                "kind": "section_failure",
                "section": label,
                **error_facts(exception, self._secrets()),
            },
        )

    def _secrets(self) -> tuple[str, ...]:
        """Values this process holds that must never reach a log record, removed by identity.

        The vault id is here as well as the key. ``auth_source`` names it in every 401 message
        on purpose - a rejected credential and an unresolved one have different fixes - but a
        log record has a wider audience than the ActiveGate's own logs, so it comes back out
        on the way to Grail.
        """
        return tuple(
            value for value in (self._config.api_key, self._config.credential_vault_id) if value
        )

    def take_diagnostics(self) -> list[dict[str, Any]]:
        """Facts learned since the last call, and clears them.

        Drained rather than read so a caller that reports them cannot report them twice, and so
        a caller that never asks cannot make the list grow - only a handful of markers exist, so
        an undrained client holds a handful of dicts and no more.
        """
        pending, self._diagnostics = self._diagnostics, []
        return pending

    # -- fastcheck ---------------------------------------------------------

    def probe(self) -> domain.ClusterStatus:
        """One cheap round trip that proves reachability, TLS and the API key at once.

        ``/v2/clusters/status`` is the right probe: it needs only CLUSTER_VIEW, so it separates
        "the key does not work" from "the key lacks the five privileges time-series-stats wants",
        and it returns the version the rest of the poll is shaped by.
        """
        return self.cluster_status(refresh=True)


def _outcome(exception: BaseException) -> str:
    """One candidate's failure, short enough to sit in a comma-joined list.

    The HTTP status is what tells a 403 (the key lacks a privilege) from a 404 (the schema is
    not on this version) from a timeout, and those are three different people's problems.
    """
    status = getattr(exception, "status", None)
    name = type(exception).__name__
    return f"{name} HTTP {status}" if status else name


def _is_server_error(exception: BaseException) -> bool:
    """True for the one failure a different parameter shape could plausibly fix.

    5xx only. A 429 is the cluster saying "slow down", which no reshaping helps; a timeout
    carries no status at all and is the runs fallback's problem, not this one's.
    """
    status = getattr(exception, "status", None)
    return isinstance(status, int) and status >= 500


def _runs_fanout_order(
    groups: list[domain.ProtectionGroup],
) -> list[domain.ProtectionGroup]:
    """Every askable group, most recently finished first.

    The order still matters even though the cap no longer truncates the list permanently: a
    group that finished a minute ago is the one most likely to be carrying a run nobody has
    counted, so it should be at the front of the rotation rather than somewhere in it. Groups
    whose last run time is unknown go last rather than being dropped: unknown is not the same
    as old, and one of them may be a job nobody has ever seen finish.
    """
    return sorted(
        (group for group in groups if group.id),
        key=lambda group: (
            group.last_run_end_time_usecs is None,
            -(group.last_run_end_time_usecs or 0),
        ),
    )


def _object_details_group(
    groups: list[domain.ProtectionGroup],
) -> domain.ProtectionGroup | None:
    """The one group worth spending the object-details request on, or None.

    A group whose last run SUCCEEDED is the only safe sample. A failed run can carry no objects
    at all, and a run that has never happened certainly does not - either would answer "this
    cluster publishes no object identifiers" when what actually happened is that nothing was
    asked. Paused comes next, then recency, because the most recent success is the one whose
    objects are most likely to still exist to be joined against.

    Deleted groups are excluded outright rather than ranked last: their objects describe an
    estate that is gone, and a uuid from one proves nothing about the estate that is here.
    """
    ranked = sorted(
        (group for group in groups if group.id and not group.is_deleted),
        key=lambda group: (
            group.last_run_status not in domain.SUCCESSFUL_RUN_STATUSES,
            bool(group.is_paused),
            group.last_run_end_time_usecs is None,
            -(group.last_run_end_time_usecs or 0),
        ),
    )
    return ranked[0] if ranked else None


def _rotate(
    ordered: list[domain.ProtectionGroup], cursor: int, cap: int
) -> tuple[list[domain.ProtectionGroup], int]:
    """``cap`` groups starting at ``cursor``, wrapping, plus where the next poll should start.

    This is the whole of the rotation. A cap applied by truncation makes the groups past it
    invisible forever, which is how v0.1.6 could be working perfectly and still report nothing:
    it asked the same top 10 of 42 every five minutes. Continuing from where the last poll
    stopped turns the cap into a rate rather than a filter - every group is reached within
    ``ceil(total / cap)`` polls, and no group can be starved by another that keeps finishing
    and jumping to the front of the ordering.

    The cursor is a position in a list that is re-sorted every poll, so it is an approximation
    rather than a promise - but it is an approximation that drifts *forward*, which is the
    harmless direction: the worst case is a group revisited a poll early, and the ledger
    already makes revisiting free.
    """
    total = len(ordered)
    count = min(max(cap, 1), total)
    start = cursor % total
    selected = [ordered[(start + offset) % total] for offset in range(count)]
    return selected, (start + count) % total


__all__ = [
    "CLUSTER_STATS_CALLS",
    "DEFAULT_RUNS_FANOUT_GROUPS",
    "HOST_LINK_GROUPS_PER_POLL",
    "HOST_LINK_MARKERS",
    "HOST_LINK_RUNS",
    "MAX_VARIANT_REQUESTS_PER_POLL",
    "OBJECT_DETAILS_MARKER",
    "OBJECT_DETAILS_RUNS",
    "RUNS_PER_GROUP",
    "RUNS_WINDOW_OVERLAP_SECONDS",
    "TIME_SERIES_VARIANTS",
    "TOP_VIEWS_VARIANTS",
    "UNKNOWN_STATUS_MARKER",
    "V1_CLUSTER_PATH",
    "VIEW_METRICS",
    "ParamVariant",
    "CohesityApiError",
    "CohesityAuthError",
    "CohesityClient",
    "CohesityConnectError",
    "CohesityEndpointError",
    "CohesityError",
    "CohesityFixtureError",
    "views_stats_path",
]
