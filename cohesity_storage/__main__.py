"""Extension entry point: schedule the clusters, poll them, report the metrics.

The SDK calls :meth:`ExtensionImpl.query` once a minute. Each cluster carries its own poll
interval, so query decides which are due; cluster capacity moves slowly and every poll costs
Cohesity API calls.

What this file is *not* is the metric contract. Keys, units and dimensions live in
:mod:`.metrics`, which turns parsed domain objects into :class:`~.metrics.Sample` records with
no SDK import anywhere in the path. This module only decides *when* to call and hands the
result to ``report_metric``. That split is why the whole mapping can be tested against the
shipped fixtures without an EEC or a tenant.

Collection is switched in exactly one place: the per-cluster toggles in the activation schema.
The feature sets in extension.yaml mirror them one for one and are not a second, independent
gate - two switches that can disagree produce "metric missing, both switches look fine", which
is a support call nobody can answer.
"""

from __future__ import annotations

import time

from dynatrace_extension import Extension, MetricType, Status, StatusValue

from . import metrics
from .client import CLUSTER_STATS_CALLS, VIEW_METRICS, CohesityClient
from .config import ClusterConfig, load_clusters
from .errors import CohesityError

EXTENSION_NAME = "cohesity_storage"

# query() ticks every 60s. Without a tolerance a cluster on a 5-minute interval would drift to
# 6 minutes whenever a tick lands a fraction of a second early.
INTERVAL_TOLERANCE_SECONDS = 5


class ExtensionImpl(Extension):
    def initialize(self):
        self._last_run: dict[str, float] = {}
        # Clients are kept per cluster because they carry state that must survive a poll: the
        # cached cluster identity and version, and the ledger of run ids already counted.
        # Rebuilding one per interval would re-count every protection run, every interval.
        self._clients: dict[str, CohesityClient] = {}

    def fastcheck(self) -> Status:
        """Refuse a broken configuration, and prove the cluster answers, before any interval runs.

        Two stages, in this order on purpose. Configuration problems are checked first and
        without touching the network, so a mistyped port never presents as a timeout. Only then
        does it make one real round trip per cluster - unreachable host, untrusted certificate
        and rejected API key each produce their own message, and finding that out here beats an
        extension that installs cleanly and then reports nothing forever.
        """
        configs, errors = load_clusters(self.activation_config)
        if errors:
            return Status(StatusValue.GENERIC_ERROR, "; ".join(errors))
        if not configs:
            return Status(StatusValue.GENERIC_ERROR, "No Cohesity clusters are configured")

        problems = []
        for config in configs:
            problems.extend(self._probe(config))
        if problems:
            return Status(StatusValue.GENERIC_ERROR, "; ".join(problems))
        return Status(StatusValue.OK)

    def query(self):
        configs, errors = load_clusters(self.activation_config)
        for error in errors:
            self.logger.error(f"Skipping cluster: {error}")

        for config in self._due_clusters(configs):
            self._poll(config)

    def on_shutdown(self):
        for client in getattr(self, "_clients", {}).values():
            client.close()

    # -- fastcheck ---------------------------------------------------------

    def _probe(self, config: ClusterConfig) -> list[str]:
        """One round trip against a cluster. Returns its problems, or nothing."""
        client = self._client_for(config)
        try:
            status = client.probe()
        except CohesityError as exception:
            # The message already names the cluster, says what to do and keeps the raw error.
            return [str(exception)]
        except Exception as exception:  # noqa: BLE001 - one cluster must not break the check
            return [f"{config.name}: unexpected failure contacting the cluster ({exception})"]

        self.logger.info(
            f"{config.name}: {client.describe()} answered as cluster {status.cluster_id} "
            f"('{status.name}') running {status.software_version or 'an unreported version'}; "
            f"view stats will be read from {client.views_path()}; "
            f"collecting {', '.join(config.enabled_collections) or 'cluster metrics only'}; "
            # Which auth mode actually took effect, stated once at fastcheck. In vault mode this
            # is also the only place the injected field name is ever visible - it is not in the
            # schema and it is not in the UI, so a log line is where it has to be observed.
            f"the API key was read from {config.auth_source}"
        )
        for caveat in client.caveats():
            self.logger.warning(caveat)
        return []

    # -- polling -----------------------------------------------------------

    def _poll(self, config: ClusterConfig) -> None:
        client = self._client_for(config)
        try:
            status = client.cluster_status()
        except CohesityError as exception:
            self.logger.error(str(exception))
            self._report_failure(config)
            return
        except Exception:
            self.logger.exception(f"{config.name}: unexpected failure polling the cluster")
            self._report_failure(config)
            return

        cluster_id = status.cluster_id
        cluster_name = status.name or config.name
        self._emit(
            metrics.CLUSTER_COLLECTION_SUCCESS,
            1,
            metrics.cluster_dimensions(cluster_id, cluster_name),
        )

        # Each section is independent: a storage domain call that 403s must not cost the
        # capacity numbers that already arrived, and one silent collection is worth a log line
        # rather than a lost poll. collection_success stays 1 because the cluster did answer.
        #
        # The first two have no toggle because they are not optional - they carry the cluster
        # identity every other entity is namespaced against. The last three are switched by the
        # activation toggles, which are the only collection switch there is.
        sections = [
            ("cluster capacity", self._report_cluster_storage, True),
            ("cluster time series", self._report_cluster_series, True),
            ("storage domains", self._report_storage_domains, config.collect_storage_domains),
            ("views", self._report_views, config.collect_views),
            ("protection", self._report_protection, config.collect_protection),
        ]
        for label, collect, enabled in sections:
            if enabled:
                self._section(config, label, collect, client, cluster_id, cluster_name)

        for caveat in client.caveats():
            # Logged every poll, not once. A synthetic fixture is most dangerous to whoever
            # reads the logs six weeks from now without having been told.
            self.logger.warning(caveat)

    def _section(self, config: ClusterConfig, label: str, collect, *args) -> None:
        try:
            collect(*args)
        except CohesityError as exception:
            self.logger.error(f"{config.name}: {label} collection failed - {exception}")
        except Exception:
            self.logger.exception(f"{config.name}: unexpected failure collecting {label}")

    def _report(self, samples: list[metrics.Sample]) -> None:
        for sample in samples:
            self._emit(sample.key, sample.value, sample.dimensions, delta=sample.delta)

    def _emit(self, key: str, value, dimensions: dict[str, str], *, delta: bool = False) -> None:
        """The only call to ``report_metric`` in the extension. Every line leaves through here.

        The SDK escapes nothing and checks little, and the ingest's only feedback on a bad line is
        an "invalid metric lines" count with no key attached. So the line is made valid here -
        names escaped and flattened, NaN and infinity dropped - rather than at each call site,
        where one forgotten path is enough to bring the count back.
        """
        number = metrics.wire_value(value)
        if number is None:
            self._warn_once(
                ("value", key),
                f"{key}: skipped a sample whose value {value!r} is not a finite number - the "
                f"line protocol has no spelling for it. Logged once per key",
            )
            return
        try:
            self.report_metric(
                key,
                number,
                dimensions=metrics.wire_dimensions(dimensions),
                # Run outcomes are events being counted, not a state being read. A gauge would
                # keep asserting the last run's status until the next one, and would make two
                # failures in one interval indistinguishable from one.
                metric_type=MetricType.DELTA if delta else MetricType.GAUGE,
            )
        except ValueError as exception:
            # The SDK's own limits (50 dimensions, 2000 characters per line). Escaping can double
            # a long name, so a handful of long names on one line can reach the length limit.
            # One oversized line must not cost the rest of the section.
            self._warn_once(("line", key), f"{key}: a sample was not reported - {exception}")

    def _warn_once(self, marker: tuple[str, str], message: str) -> None:
        # Once per key for the life of the process, not per poll: the same bad field comes back
        # every interval, and a warning repeated every five minutes is one nobody reads.
        warned = self.__dict__.setdefault("_warned", set())
        if marker not in warned:
            warned.add(marker)
            self.logger.warning(message)

    def _report_cluster_storage(self, client: CohesityClient, cluster_id: str, cluster_name: str) -> None:
        storage = client.cluster_storage()
        samples = metrics.cluster_storage_samples(cluster_id, cluster_name, storage)
        if not samples:
            # Every field in this response is nullable, so an all-null body is possible and
            # says nothing on its own. Reporting zeros instead would read as an outage.
            self.logger.warning(
                f"{cluster_name}: /v2/stats/cluster-storage carried no usable numbers - every "
                f"field in that response is nullable, so no capacity metric was reported"
            )
        self._report(samples)

    def _report_cluster_series(self, client: CohesityClient, cluster_id: str, cluster_name: str) -> None:
        for call in CLUSTER_STATS_CALLS:
            series = client.cluster_time_series(call)
            samples = metrics.cluster_time_series_samples(
                cluster_id, cluster_name, call["schemaName"], series
            )
            if series and not samples:
                # All-empty with no error is the signature of a wrong entityId, which is ticket
                # 04's biggest open risk. It is silent unless something says this.
                self.logger.warning(
                    f"{cluster_name}: {call['schemaName']} returned no data points for entityId "
                    f"{client.cluster_status().stats_entity_id}. The assumption that "
                    f"ClusterStatus.clusterId is the stats entity id may be wrong - this failure "
                    f"mode returns empty rather than an error"
                )
            self._report(samples)

    def _report_storage_domains(self, client: CohesityClient, cluster_id: str, cluster_name: str) -> None:
        domains = client.storage_domains()
        self._report(metrics.storage_domain_samples(cluster_id, cluster_name, domains))
        self.logger.info(f"{cluster_name}: reported {len(domains)} storage domain(s)")

    def _report_views(self, client: CohesityClient, cluster_id: str, cluster_name: str) -> None:
        for view_metric in VIEW_METRICS:
            views = client.view_stats(view_metric)
            self._report(metrics.view_samples(cluster_id, cluster_name, view_metric, views))

    def _report_protection(self, client: CohesityClient, cluster_id: str, cluster_name: str) -> None:
        """Groups first, then runs - the runs endpoint does not return the storage domain.

        Fetching groups is what supplies ``cohesity.storagedomain.id`` on run metrics, and that
        dimension is the whole ``writes_to`` edge. It also supplies the paused and active flags,
        which the runs summary omits.
        """
        groups = client.protection_groups()
        now_usecs = int(time.time() * 1_000_000)
        self._report(metrics.protection_group_samples(cluster_id, cluster_name, groups, now_usecs))

        # Deduplicated on run.id by the client's ledger. Counting what protection_runs()
        # returns directly would inflate every failure by the window-to-interval ratio.
        runs = client.new_protection_runs()
        self._report(metrics.protection_run_samples(cluster_id, cluster_name, runs, groups))
        self.logger.info(
            f"{cluster_name}: {len(groups)} protection group(s); {len(runs)} newly finished "
            f"run(s) this interval "
            f"({', '.join(f'{run.protection_group_name}:{run.status}' for run in runs) or 'none'}); "
            f"{client.counted_run_ids} run id(s) held against double counting"
        )

    def _report_failure(self, config: ClusterConfig) -> None:
        """Report the miss so an alert on collection_success fires instead of going silent.

        The cluster's own id is unavailable in exactly the cases that matter here, so the
        configured name stands in for it. That makes the failure series a different one from
        the success series, which is why it is only worth alerting on the absence of success,
        not on this value being 0.
        """
        dimensions = metrics.cluster_dimensions(config.name, config.name)
        self._emit(metrics.CLUSTER_COLLECTION_SUCCESS, 0, dimensions)

    # -- scheduling --------------------------------------------------------

    def _client_for(self, config: ClusterConfig) -> CohesityClient:
        self._ensure_state()
        client = self._clients.get(config.key)
        if client is None:
            client = CohesityClient(config)
            self._clients[config.key] = client
        return client

    def _due_clusters(self, configs: list[ClusterConfig]) -> list[ClusterConfig]:
        """Pick the clusters whose interval has elapsed, and forget ones that were removed."""
        self._ensure_state()
        now = time.monotonic()
        live_keys = {config.key for config in configs}
        for stale in [key for key in self._last_run if key not in live_keys]:
            del self._last_run[stale]
        for stale in [key for key in self._clients if key not in live_keys]:
            self._clients.pop(stale).close()

        due = []
        for config in configs:
            last = self._last_run.get(config.key)
            interval = config.interval_minutes * 60 - INTERVAL_TOLERANCE_SECONDS
            if last is None or (now - last) >= interval:
                # Stamped before the poll, not after, so a slow cluster does not immediately
                # queue another poll behind the one that ran long.
                self._last_run[config.key] = now
                due.append(config)
        return due

    def _ensure_state(self) -> None:
        # The SDK does not call initialize() in fastcheck mode - only on a normal run - yet
        # fastcheck() still needs a client to probe the cluster. So every path that touches
        # per-instance state goes through here rather than trusting initialize() to have run.
        # Missing this surfaced on a real ActiveGate as "'ExtensionImpl' object has no
        # attribute '_clients'" during monitoring-configuration assignment.
        if not hasattr(self, "_clients"):
            self.initialize()


def main():
    ExtensionImpl(name=EXTENSION_NAME).run()


if __name__ == "__main__":
    main()
