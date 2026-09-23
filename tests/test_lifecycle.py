"""The ExtensionImpl lifecycle as the SDK actually drives it.

Everything else in the suite tests the client and the metric mapping in isolation. This file
exists because that left the class wiring them to the SDK untested, and it broke on a real
ActiveGate: the SDK runs fastcheck() WITHOUT ever calling initialize(), so any state created
only in initialize() does not exist yet when fastcheck() probes the cluster.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from dynatrace_extension import Extension, StatusValue

from cohesity_storage import metrics
from cohesity_storage.__main__ import EXTENSION_NAME, ExtensionImpl
from cohesity_storage.client import MAX_VARIANT_REQUESTS_PER_POLL, RUNS_WINDOW_OVERLAP_SECONDS
from cohesity_storage.config import load_clusters
from cohesity_storage.fixtures import FixtureStore
from tests.cohesity_fake_cluster import FakeCohesityCluster, self_signed_certificate

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


class FakeActivationConfig:
    def __init__(self, endpoints):
        self._endpoints = endpoints

    def get(self, key, default=None):
        return {"endpoints": self._endpoints}.get(key, default)


@pytest.fixture
def fresh_extension():
    # Extension is a process-wide singleton, so an instance left over from another test would
    # already carry initialize()'s state and hide exactly the bug this file guards against.
    Extension._instance = None
    extension = ExtensionImpl(name=EXTENSION_NAME)
    for attribute in ("_clients", "_last_run"):
        if hasattr(extension, attribute):
            delattr(extension, attribute)
    yield extension
    Extension._instance = None


def replay_endpoint() -> dict:
    return {
        "name": "replay",
        "host": "10.20.30.40",
        "apiKey": "replay-key",
        "fixtureDir": str(FIXTURES),
    }


def test_fastcheck_runs_before_initialize(fresh_extension):
    assert not hasattr(fresh_extension, "_clients")
    fresh_extension.activation_config = FakeActivationConfig([replay_endpoint()])

    status = fresh_extension.fastcheck()

    assert status.status == StatusValue.OK, status.message


def test_fastcheck_leaves_state_that_query_can_reuse(fresh_extension):
    fresh_extension.activation_config = FakeActivationConfig([replay_endpoint()])

    fresh_extension.fastcheck()

    assert len(fresh_extension._clients) == 1


def polling_extension(extension, monkeypatch, endpoint: dict | None = None) -> tuple[list, list]:
    """Arm an extension for a real poll, capturing what it would have sent.

    ``report_metric`` and ``report_log_events`` are the two places the SDK reaches the EEC, so
    stubbing exactly those runs every line of query() without one.
    """
    extension.activation_config = FakeActivationConfig([endpoint or replay_endpoint()])
    samples: list = []
    events: list = []
    monkeypatch.setattr(extension, "report_metric", lambda *args, **_: samples.append(args))
    monkeypatch.setattr(extension, "report_log_events", lambda batch, **_: events.append(batch))
    return samples, events


def test_diagnostics_are_reported_once_per_client_not_once_per_poll(fresh_extension, monkeypatch):
    # The facts do not change between polls, so re-sending them every five minutes would be
    # ingest spent on telling the same person the same thing forever.
    samples, events = polling_extension(fresh_extension, monkeypatch)
    config = load_clusters(fresh_extension.activation_config)[0][0]

    fresh_extension._poll(config)
    fresh_extension._poll(config)
    fresh_extension._poll(config)

    assert len(events) == 1
    assert samples, "the poll should still have reported metrics"


def test_the_one_batch_answers_both_questions_the_customer_cluster_raised(
    fresh_extension, monkeypatch
):
    _, events = polling_extension(fresh_extension, monkeypatch)
    config = load_clusters(fresh_extension.activation_config)[0][0]

    fresh_extension._poll(config)

    kinds = [event["cohesity.diagnostic"] for event in events[0]]
    assert "cluster_version" in kinds
    assert "storage_domain_stats_fields" in kinds
    # One per cluster-level schema, so a schema that resolved and one that did not are told
    # apart by name rather than by a single summary line.
    assert kinds.count("entity_id_probe") == 3


def test_the_diagnostic_batch_never_carries_the_api_key(fresh_extension, monkeypatch):
    _, events = polling_extension(fresh_extension, monkeypatch)
    config = load_clusters(fresh_extension.activation_config)[0][0]

    fresh_extension._poll(config)

    assert "replay-key" not in json.dumps(events)


class TestFailureDiagnostics:
    """A section that fails has to say so somewhere a human can read.

    ``_section`` has always caught the failure into ``self.logger``, and on the tenant this was
    built against those log lines never reach Grail. The result on a real customer cluster was
    six missing metrics with no error anywhere - the collection_success metric said the cluster
    answered, and it did; the section that failed was invisible. So every swallowed failure now
    also leaves as a diagnostic log event, on the channel that demonstrably arrives.
    """

    #: Distinctive on purpose: a key spelled "demo" is a substring of the fixture cluster's
    #: own name, so a test asserting it never appears would be asserting the wrong thing.
    API_KEY = "s3cr3tCohesityApiKeyValue"

    @pytest.fixture
    def cluster(self, tmp_path):
        # Only a certificate needs cryptography, and it is a development dependency - the
        # extension itself never imports it.
        pytest.importorskip("cryptography")
        certfile = self_signed_certificate(tmp_path)
        server = FakeCohesityCluster(
            store=FixtureStore(FIXTURES), certfile=certfile, anchor=None, api_key=self.API_KEY
        )
        server.start()
        yield server
        server.stop()

    def endpoint(self, cluster) -> dict:
        return {
            "name": "fake-cluster",
            "host": cluster.host,
            "port": cluster.port,
            "apiKey": self.API_KEY,
            # Same choice an operator faces on day one against a real Cohesity appliance.
            "verifyTls": False,
        }

    def poll(self, extension, cluster, monkeypatch, *, times: int = 1) -> list[list[dict]]:
        """Poll the fake cluster, and hand back the diagnostic batches it would have sent."""
        _, batches = polling_extension(extension, monkeypatch, endpoint=self.endpoint(cluster))
        config = load_clusters(extension.activation_config)[0][0]
        for _ in range(times):
            extension._poll(config)
        return batches

    def records(self, batches: list[list[dict]], kind: str) -> list[dict]:
        return [
            event
            for batch in batches
            for event in batch
            if event["cohesity.diagnostic"] == kind
        ]

    #: The path the v0.1.8 protected-object probe asks, for the group it picks out of the
    #: shipped inventory: the most recently succeeded, unpaused, undeleted one.
    OBJECT_DETAILS_PATH = "/v2/data-protect/protection-groups/g-9001/runs"

    def test_the_protected_object_probe_asks_once_across_three_polls(
        self, fresh_extension, cluster, monkeypatch
    ):
        # Ticket 16's question costs one request for the life of the client. Re-asking it every
        # five minutes would buy nothing - the answer is a property of the data model - and
        # this is a cluster already slow enough that v0.1.7 had to stop asking it for windows.
        batches = self.poll(fresh_extension, cluster, monkeypatch, times=3)

        assert len(self.records(batches, "protected_objects")) == 1

    def test_the_probe_reports_the_uuid_shape_and_the_vmware_block(
        self, fresh_extension, cluster, monkeypatch
    ):
        batches = self.poll(fresh_extension, cluster, monkeypatch)

        event = self.records(batches, "protected_objects")[0]
        assert event["severity"] == metrics.SEVERITY_INFO
        assert event["cohesity.object_environment"] == "kVMware"
        assert event["cohesity.object_vmware_key"] == "vCenterSummary"
        assert "uuid" in event["cohesity.object_fields"]
        assert event["cohesity.object_uuid_verdicts"].startswith("uuid-8-4-4-4-12")
        assert "00112233-4455-6677-8899-aabbccddeeff" in event["cohesity.object_uuids"]

    def test_the_probe_never_carries_an_object_name(
        self, fresh_extension, cluster, monkeypatch
    ):
        # The fixture's object names are obvious placeholders precisely so this assertion is
        # asserting something. "name" is allowed to appear as a field NAME and never as a value.
        batches = self.poll(fresh_extension, cluster, monkeypatch)

        assert "PLACEHOLDER" not in json.dumps(batches)
        assert "name" in self.records(batches, "protected_objects")[0]["cohesity.object_fields"]

    def test_a_probe_the_key_may_not_make_does_not_cost_the_run_metrics(
        self, fresh_extension, cluster, monkeypatch
    ):
        # The safety property, end to end. Protection-run collection is what v0.1.7 finally
        # got working; an enrichment experiment that could take it down is not worth an answer.
        cluster.fail_paths = {self.OBJECT_DETAILS_PATH: 403}
        samples, batches = polling_extension(
            fresh_extension, monkeypatch, endpoint=self.endpoint(cluster)
        )
        config = load_clusters(fresh_extension.activation_config)[0][0]

        fresh_extension._poll(config)

        event = self.records(batches, "protected_objects")[0]
        assert event["severity"] == metrics.SEVERITY_WARN
        assert event["cohesity.http_status"] == "403"
        assert metrics.PROTECTION_GROUP_RUN_OUTCOME in [sample[0] for sample in samples]
        # The probe is the only thing that failed - the protection section itself did not.
        assert not [
            failure
            for failure in self.records(batches, "section_failure")
            if failure["cohesity.section"] == "protection"
        ]

    def test_a_section_refused_with_a_403_produces_one_naming_diagnostic(
        self, fresh_extension, cluster, monkeypatch
    ):
        # The customer shape: one endpoint the API key's owner has no privilege for, 200
        # everywhere else. collection_success stays 1, so this record is the only signal.
        cluster.fail_paths = {"/v2/storage-domains": 403}

        batches = self.poll(fresh_extension, cluster, monkeypatch)

        failures = self.records(batches, "section_failure")
        assert len(failures) == 1
        event = failures[0]
        assert event["severity"] == metrics.SEVERITY_ERROR
        assert event["cohesity.section"] == "storage domains"
        assert event["cohesity.error"] == "CohesityAuthError"
        assert event["cohesity.http_status"] == "403"
        assert event["cohesity.path"] == "/storage-domains"
        # Names, so the call is identifiable; never values, so nothing can leak through one.
        assert event["cohesity.query_params"] == "includeStats"
        assert "storage domains" in event["content"]
        assert "403" in event["content"]

    def test_an_exception_that_is_not_a_cohesity_error_is_reported_too(
        self, fresh_extension, cluster, monkeypatch
    ):
        # The case the old code lost most completely: self.logger.exception() and nothing else.
        # A parser that trips over an unexpected body raises this, not a CohesityError.
        def boom(*_args, **_kwargs):
            msg = "storage domain stats were not what this build expects"
            raise ValueError(msg)

        monkeypatch.setattr(metrics, "storage_domain_samples", boom)

        batches = self.poll(fresh_extension, cluster, monkeypatch)

        failures = self.records(batches, "section_failure")
        assert len(failures) == 1
        assert failures[0]["cohesity.error"] == "ValueError"
        # No status and no path, because there is no request behind it - and saying so beats
        # inventing one.
        assert failures[0]["cohesity.http_status"] == ""
        assert failures[0]["cohesity.path"] == ""
        assert "not what this build expects" in failures[0]["cohesity.detail"]

    def test_a_probe_that_raises_on_its_first_candidate_still_reports(
        self, fresh_extension, cluster, monkeypatch
    ):
        # time-series-stats needs five privileges of its own, so a key that passes every other
        # call can still be refused here. Before 0.1.5 the exception left the probe with
        # nothing recorded at all, which is exactly what the customer cluster showed.
        cluster.fail_paths = {"/v2/stats/time-series-stats": 403}

        batches = self.poll(fresh_extension, cluster, monkeypatch)

        probes = self.records(batches, "entity_id_probe")
        assert len(probes) == 1
        probe = probes[0]
        assert probe["severity"] == metrics.SEVERITY_ERROR
        assert probe["cohesity.schema"] == "kSentryClusterStats"
        assert probe["cohesity.http_status"] == "403"
        # The candidate list survived, and so did what the one attempt made of it.
        assert probe["cohesity.entity_id_candidates"]
        assert "HTTP 403" in probe["cohesity.entity_id_attempts"]
        # And the section that contains it says so separately, by label.
        assert self.records(batches, "section_failure")[0]["cohesity.section"] == (
            "cluster time series"
        )

    def test_a_failure_is_reported_once_per_client_not_once_per_poll(
        self, fresh_extension, cluster, monkeypatch
    ):
        # The same 403 comes back every five minutes forever. Saying so every five minutes
        # forever is ingest spent telling the same person the same thing.
        cluster.fail_paths = {"/v2/storage-domains": 403}

        batches = self.poll(fresh_extension, cluster, monkeypatch, times=3)

        assert len(batches) == 1
        assert len(self.records(batches, "section_failure")) == 1

    def test_a_failure_recorded_in_a_section_is_drained_in_that_same_poll(
        self, fresh_extension, cluster, monkeypatch
    ):
        # The drain runs after the sections, which is the only ordering that lets a failure
        # from this poll leave in this poll. A drain before them would delay every failure by
        # one interval, and the failure on the final poll before a restart would never leave.
        cluster.fail_paths = {"/v2/storage-domains": 403}

        batches = self.poll(fresh_extension, cluster, monkeypatch)

        assert len(batches) == 1
        assert self.records(batches, "section_failure")

    def test_an_unreachable_cluster_still_explains_itself(
        self, fresh_extension, cluster, monkeypatch
    ):
        # /v2/clusters/status failing ends the poll before any section runs, and that early
        # return used to skip the diagnostics drain entirely - so the one poll whose
        # explanation matters most was the one that sent nothing.
        cluster.fail_paths = {"/v2/clusters/status": 403}

        batches = self.poll(fresh_extension, cluster, monkeypatch)

        failures = self.records(batches, "section_failure")
        assert len(failures) == 1
        assert failures[0]["cohesity.section"] == "cluster status"
        assert failures[0]["cohesity.path"] == "/clusters/status"
        # The version is unknown here by definition; the record says so rather than reading
        # as a cluster that reported an empty version.
        assert failures[0]["cohesity.cluster.version"] == "unreported"

    def test_no_failure_record_carries_the_api_key(
        self, fresh_extension, cluster, monkeypatch
    ):
        cluster.fail_paths = {"/v2/stats/time-series-stats": 403}

        batches = self.poll(fresh_extension, cluster, monkeypatch)

        assert self.API_KEY not in json.dumps(batches)

    # -- the three endpoints that fail on the customer cluster -------------

    def test_an_endpoint_that_500s_on_one_parameter_is_probed_and_recovered(
        self, fresh_extension, cluster, monkeypatch
    ):
        # The customer shape for time-series-stats: HTTP 500 on every entityId that was tried,
        # which rules out the values and leaves the request shape.
        cluster.reject_params = {"/v2/stats/time-series-stats": ("rollupIntervalSecs",)}

        batches = self.poll(fresh_extension, cluster, monkeypatch)

        variants = self.records(batches, "param_variant")
        assert len(variants) == 1
        assert variants[0]["cohesity.variant"] == "no-rollupIntervalSecs"
        assert variants[0]["cohesity.path"] == "/stats/time-series-stats"
        # The section itself did not fail, because the probe found a shape that works.
        assert not self.records(batches, "section_failure")

    def test_a_cluster_that_wants_repeated_metric_names_is_found_out(
        self, fresh_extension, cluster, monkeypatch
    ):
        # The spec says explode:false, so the extension comma-joins. A server that mis-parses
        # its own declared form answers 500, and that is the first shape worth retrying.
        cluster.require_repeated = {"/v2/stats/time-series-stats": ("metricNames",)}

        batches = self.poll(fresh_extension, cluster, monkeypatch)

        assert self.records(batches, "param_variant")[0]["cohesity.variant"] == (
            "metricNames-repeated"
        )

    def test_top_views_keeps_its_metric_through_every_shape_tried(
        self, fresh_extension, cluster, monkeypatch
    ):
        cluster.reject_params = {"/v2/stats/top-views": ("protocol",)}

        self.poll(fresh_extension, cluster, monkeypatch)

        top_views = [request for request in cluster.requests if "top-views" in request]
        assert top_views
        # Dropping `metric` would be answered 200 with the endpoint's default series, filed
        # under whichever metric was asked for. Every request names its own metric.
        assert all("metric=" in request for request in top_views)

    def test_an_endpoint_that_500s_on_everything_says_so_and_stops(
        self, fresh_extension, cluster, monkeypatch
    ):
        cluster.reject_params = {"/v2/stats/top-views": ("metric",)}

        batches = self.poll(fresh_extension, cluster, monkeypatch, times=2)

        variants = self.records(batches, "param_variant")
        assert len(variants) == 1
        assert variants[0]["severity"] == metrics.SEVERITY_ERROR
        assert variants[0]["cohesity.variant"] == ""
        assert variants[0]["cohesity.http_status"] == "500"

    def test_a_probing_poll_cannot_become_a_request_storm(
        self, fresh_extension, cluster, monkeypatch
    ):
        # Two endpoints refusing everything, three schemas and twenty entityId candidates
        # between them. Without a per-poll budget this is where one slow poll turns into
        # hundreds of requests against a cluster that is already in trouble.
        cluster.reject_params = {
            "/v2/stats/time-series-stats": ("schemaName",),
            "/v2/stats/top-views": ("metric",),
        }

        self.poll(fresh_extension, cluster, monkeypatch)

        probed = [
            request
            for request in cluster.requests
            if "time-series-stats" in request or "top-views" in request
        ]
        # The base calls the poll would make anyway, plus at most one poll's variant budget.
        assert len(probed) <= 20 + 3 + 2 + MAX_VARIANT_REQUESTS_PER_POLL

    def test_a_runs_endpoint_that_refuses_falls_back_rather_than_losing_five_metrics(
        self, fresh_extension, cluster, monkeypatch
    ):
        # Over 24 hours on the customer cluster the three run metrics summed to zero while
        # last_success.age showed jobs finishing minutes earlier. Losing them because one
        # endpoint is slow is a bad trade when two others report the same runs.
        cluster.fail_paths = {"/v2/data-protect/runs/summary": 500}

        batches = self.poll(fresh_extension, cluster, monkeypatch)

        sources = self.records(batches, "runs_source")
        assert len(sources) == 1
        assert sources[0]["severity"] == metrics.SEVERITY_WARN
        assert sources[0]["cohesity.runs_source"]
        assert sources[0]["cohesity.runs_attempts"].startswith("runs/summary=")

    def test_the_runs_window_asked_for_is_the_interval_plus_an_overlap(
        self, fresh_extension, cluster, monkeypatch
    ):
        self.poll(fresh_extension, cluster, monkeypatch)

        runs = [request for request in cluster.requests if "runs/summary" in request]
        assert len(runs) == 1
        query = parse_qs(urlparse(runs[0]).query)
        window = (int(query["endTimeUsecs"][0]) - int(query["startTimeUsecs"][0])) / 1_000_000
        assert window == pytest.approx(5 * 60 + RUNS_WINDOW_OVERLAP_SECONDS, abs=2)
