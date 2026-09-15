"""Extension entry point: schedule the clusters, poll them, report the metrics.

The SDK calls :meth:`ExtensionImpl.query` once a minute. Each cluster carries its own poll
interval, so query decides which are due; cluster capacity moves slowly and every poll costs
Cohesity API calls.

What this file is *not* is the metric set. Ticket 07 built the client - every endpoint the v1
design needs, parsed into domain objects - and deliberately stopped short of naming metric keys,
units and dimensions, which is ticket 06's decision and is still open. So the poll below fetches
everything and reports only the two keys the scaffold already declared, logging a one-line
summary of the rest. Wiring those into `report_metric` is the seam, and it is a small one: the
data is already parsed and already namespaced.
"""

from __future__ import annotations

import time

from dynatrace_extension import Extension, Status, StatusValue

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
            storage = client.cluster_storage()
        except CohesityError as exception:
            self.logger.error(str(exception))
            self._report_failure(config)
            return
        except Exception:
            self.logger.exception(f"{config.name}: unexpected failure polling the cluster")
            self._report_failure(config)
            return

        dimensions = metrics.cluster_dimensions(status.cluster_id, status.name or config.name)
        self.report_metric(metrics.CLUSTER_COLLECTION_SUCCESS, 1, dimensions=dimensions)

        if storage.total_capacity_bytes is None:
            self.logger.warning(
                f"{config.name}: /v2/stats/cluster-storage carried no totalCapacityBytes. Every "
                f"field in that response is nullable; reporting zero would read as an outage."
            )
        else:
            self.report_metric(
                metrics.CLUSTER_TOTAL_CAPACITY_BYTES,
                storage.total_capacity_bytes,
                dimensions=dimensions,
            )

        self._collect_rest(config, client)

        for caveat in client.caveats():
            # Logged every poll, not once. A synthetic fixture is most dangerous to whoever
            # reads the logs six weeks from now without having been told.
            self.logger.warning(caveat)

    def _collect_rest(self, config: ClusterConfig, client: CohesityClient) -> None:
        """Fetch everything else and log what came back.

        SEAM (ticket 06). Each block below already holds parsed, namespaced data; what is
        missing is only the metric key, unit and dimension contract. Fetching it now rather than
        waiting keeps the API cost, the call shapes and the parsing honest - a seam nobody
        exercises is a seam that does not fit when the time comes.

        Note ``config.collect_nodes`` is not consulted anywhere: ticket 04's metric set has no
        node metrics and ticket 05 kept EXT_COHESITY_NODE out of v1, so the toggle currently
        switches nothing. It is left in the schema rather than removed because removing a
        configuration property is a migration; ticket 09 should decide which way it goes.
        """
        try:
            for call in CLUSTER_STATS_CALLS:
                series = client.cluster_time_series(call)
                empty = [name for name, metric in series.items() if metric.latest() is None]
                if len(empty) == len(series) and series:
                    # All-empty with no error is the signature of a wrong entityId, which is
                    # ticket 04's biggest open risk. It is silent unless something says this.
                    self.logger.warning(
                        f"{config.name}: {call['schemaName']} returned no data points for "
                        f"entityId {client.cluster_status().stats_entity_id}. The assumption "
                        f"that ClusterStatus.clusterId is the stats entity id may be wrong - "
                        f"this failure mode returns empty rather than an error."
                    )

            if config.collect_storage_domains:
                cluster_id = client.cluster_status().cluster_id
                domains = client.storage_domains()
                named = ", ".join(f"{d.name}={d.entity_id(cluster_id)}" for d in domains)
                self.logger.info(f"{config.name}: {len(domains)} storage domain(s): {named}")

            # Views have no collection toggle of their own. They are a cluster-level ranking of
            # file-services activity, not a per-storage-domain reading, so hanging them off the
            # storage-domain switch would be arbitrary. Ticket 09 owns the activation schema and
            # should decide whether they get their own switch; two calls is not worth one today.
            for metric_name in VIEW_METRICS:
                views = client.view_stats(metric_name)
                self.logger.info(f"{config.name}: {len(views)} view(s) ranked by {metric_name}")

            if config.collect_protection:
                groups = client.protection_groups()
                runs = client.new_protection_runs()
                self.logger.info(
                    f"{config.name}: {len(groups)} protection group(s); {len(runs)} newly "
                    f"finished run(s) this interval "
                    f"({', '.join(f'{run.protection_group_name}:{run.status}' for run in runs) or 'none'}); "
                    f"{client.counted_run_ids} run id(s) held against double counting"
                )
        except CohesityError as exception:
            # Not a total failure: capacity was already reported above, and collection_success
            # stays 1 because the cluster did answer. Losing one collection is worth saying so.
            self.logger.error(str(exception))

    def _report_failure(self, config: ClusterConfig) -> None:
        """Report the miss so an alert on collection_success fires instead of going silent.

        The cluster's own id is unavailable in exactly the cases that matter here, so the
        configured name stands in for it. That makes the failure series a different one from
        the success series, which is why it is only worth alerting on the absence of success,
        not on this value being 0.
        """
        dimensions = metrics.cluster_dimensions(config.name, config.name)
        self.report_metric(metrics.CLUSTER_COLLECTION_SUCCESS, 0, dimensions=dimensions)

    # -- scheduling --------------------------------------------------------

    def _client_for(self, config: ClusterConfig) -> CohesityClient:
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
        # initialize() is the documented hook, but guarding here keeps query() safe if the SDK
        # ever schedules a callback before it has run.
        if not hasattr(self, "_last_run"):
            self.initialize()


def main():
    ExtensionImpl(name=EXTENSION_NAME).run()


if __name__ == "__main__":
    main()
