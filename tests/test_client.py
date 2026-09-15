"""The client: what it asks for, what it does with the answer, and how failures read.

Two kinds of test here. Against a recording transport, to assert the *requests* - the version
fork, the comma-joined metricNames, the dedup across polls. And against the shipped fixtures, to
assert the whole path from a recorded body to parsed domain objects, which is the same path the
extension takes in replay mode.
"""

from __future__ import annotations

import json
import ssl
import time
import urllib.error
from pathlib import Path

import pytest

from cohesity_storage import client as client_module
from cohesity_storage import domain
from cohesity_storage.client import CohesityClient
from cohesity_storage.config import ClusterConfig
from cohesity_storage.errors import (
    CohesityApiError,
    CohesityAuthError,
    CohesityConnectError,
    CohesityEndpointError,
    CohesityFixtureError,
)
from cohesity_storage.fixtures import FixtureStore
from cohesity_storage.transport import FixtureTransport, HttpTransport, encode_params

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FIXTURE_CLUSTER_ID = "1234567890123456"


def config(**overrides) -> ClusterConfig:
    values = {"name": "cohesity-prod", "host": "10.20.30.40", "api_key": "demo-key"}
    values.update(overrides)
    return ClusterConfig(**values)


class RecordingTransport:
    """Answers from a dict of canned bodies and remembers exactly what was asked for."""

    def __init__(self, bodies: dict | None = None):
        self.bodies = bodies or {}
        self.calls: list[tuple[str, dict]] = []
        self.raise_for: dict = {}

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        if path in self.raise_for:
            raise self.raise_for[path]
        if path not in self.bodies:
            msg = f"no canned body for {path}"
            raise AssertionError(msg)
        return self.bodies[path]

    def close(self):
        pass

    def describe(self):
        return "recording"

    def paths(self):
        return [path for path, _ in self.calls]


def status_body(software_version="7.4.1_u2_release"):
    return {"clusterId": int(FIXTURE_CLUSTER_ID), "name": "prod", "softwareVersion": software_version}


def client_with(transport, **config_overrides) -> CohesityClient:
    return CohesityClient(config(**config_overrides), transport=transport)


class TestVersionFork:
    """/v2/stats/top-views does not exist before 7.3; /v2/stats/views is deprecated from 7.3."""

    def build(self, software_version):
        transport = RecordingTransport(
            {
                client_module.CLUSTER_STATUS_PATH: status_body(software_version),
                client_module.TOP_VIEWS_PATH: {"viewsStats": []},
                client_module.VIEWS_PATH: {"viewsStats": []},
            }
        )
        return transport, client_with(transport)

    def test_a_74_cluster_gets_top_views(self):
        transport, client = self.build("7.4.1_u2_release-20250104")

        client.view_stats("kNumBytesRead")

        assert client_module.TOP_VIEWS_PATH in transport.paths()
        assert client_module.VIEWS_PATH not in transport.paths()

    def test_a_73_cluster_gets_top_views(self):
        # The boundary itself, because 7.3 is where top-views appears and views is deprecated.
        _, client = self.build("7.3.0_release")

        assert client.views_path() == client_module.TOP_VIEWS_PATH

    def test_a_72_cluster_gets_the_deprecated_path(self):
        transport, client = self.build("7.2.1_u1_release")

        client.view_stats("kNumBytesRead")

        assert client_module.VIEWS_PATH in transport.paths()
        assert client_module.TOP_VIEWS_PATH not in transport.paths()

    def test_a_68_cluster_gets_the_deprecated_path(self):
        _, client = self.build("6.8.1_u6_release")

        assert client.views_path() == client_module.VIEWS_PATH

    def test_an_unreadable_version_falls_back_to_the_path_that_exists_everywhere(self):
        # Guessing toward the deprecated path costs a deprecation warning; guessing toward the
        # new one loses the metric on every cluster below 7.3.
        _, client = self.build("release-unknown")

        assert client.views_path() == client_module.VIEWS_PATH

    def test_a_404_on_top_views_falls_back_rather_than_losing_the_metric(self):
        transport, client = self.build("7.4.0_release")
        transport.raise_for[client_module.TOP_VIEWS_PATH] = CohesityEndpointError("404")

        client.view_stats("kNumBytesRead")

        assert transport.paths().count(client_module.VIEWS_PATH) == 1

    def test_a_404_on_the_deprecated_path_is_not_retried(self):
        transport, client = self.build("6.8.0_release")
        transport.raise_for[client_module.VIEWS_PATH] = CohesityEndpointError("404")

        with pytest.raises(CohesityEndpointError):
            client.view_stats("kNumBytesRead")


class TestRequestShapes:
    def test_metric_names_are_comma_joined_into_one_parameter(self):
        # explode: false. Repeating the parameter makes the cluster read only the last value,
        # and the failure is silent - a series that is simply never populated.
        assert encode_params({"metricNames": ("kReadIos", "kWriteIos")}) == "metricNames=kReadIos%2CkWriteIos"

    def test_booleans_are_serialised_as_json_not_python(self):
        assert encode_params({"includeStats": True}) == "includeStats=true"

    def test_none_values_are_dropped_rather_than_sent_as_the_string_none(self):
        assert encode_params({"endTimeMsecs": None, "startTimeMsecs": 5}) == "startTimeMsecs=5"

    def test_the_time_series_call_carries_schema_entity_and_a_window(self):
        transport = RecordingTransport(
            {
                client_module.CLUSTER_STATUS_PATH: status_body(),
                client_module.TIME_SERIES_STATS_PATH: {"timeSeriesStats": []},
            }
        )
        client = client_with(transport)

        client.cluster_time_series(client_module.CLUSTER_STATS_CALLS[0])

        _, params = transport.calls[-1]
        assert params["schemaName"] == "kSentryClusterStats"
        assert params["entityId"] == FIXTURE_CLUSTER_ID
        assert params["rollupFunction"] == "kAverage"
        assert params["startTimeMsecs"] > 0
        # The three parameters missing from the 6.8 and 7.2 specs are never sent.
        assert "prorateDataPoints" not in params
        assert "includeGrowthChange" not in params
        assert "entityIdList" not in params

    def test_protection_groups_exclude_deleted_jobs_by_default(self):
        # A deleted group still holds snapshots but will never run again, so its
        # age-since-last-success rises forever and alerts on a job nobody can fix.
        transport = RecordingTransport({client_module.PROTECTION_GROUPS_PATH: {"protectionGroups": []}})

        client_with(transport).protection_groups()

        _, params = transport.calls[-1]
        assert params["isDeleted"] is False
        assert params["includeLastRunInfo"] is True

    def test_the_run_window_is_wider_than_the_poll_interval(self):
        transport = RecordingTransport({client_module.PROTECTION_RUNS_PATH: {"protectionRunsSummary": []}})
        client = client_with(transport, interval_minutes=10)

        client.protection_runs()

        _, params = transport.calls[-1]
        window_seconds = (int(time.time() * 1_000_000) - params["startTimeUsecs"]) / 1_000_000
        assert window_seconds >= 10 * 60 * 3 - 5

    def test_the_cluster_status_call_is_made_once_and_cached(self):
        transport = RecordingTransport({client_module.CLUSTER_STATUS_PATH: status_body()})
        client = client_with(transport)

        client.cluster_status()
        client.cluster_status()

        assert transport.paths().count(client_module.CLUSTER_STATUS_PATH) == 1

    def test_a_status_without_a_cluster_id_is_refused(self):
        # An unnamespaced id collides silently the day a second cluster is added.
        transport = RecordingTransport({client_module.CLUSTER_STATUS_PATH: {"name": "prod"}})

        with pytest.raises(CohesityApiError) as raised:
            client_with(transport).cluster_status()

        assert "clusterId" in str(raised.value)


class TestNamespacing:
    def test_object_ids_are_namespaced_against_the_cluster_the_client_talks_to(self):
        transport = RecordingTransport({client_module.CLUSTER_STATUS_PATH: status_body()})

        assert client_with(transport).namespaced("1001") == f"{FIXTURE_CLUSTER_ID}_1001"

    def test_two_clusters_do_not_produce_the_same_entity_id(self):
        first = client_with(RecordingTransport({client_module.CLUSTER_STATUS_PATH: {"clusterId": 1001}}))
        second = client_with(RecordingTransport({client_module.CLUSTER_STATUS_PATH: {"clusterId": 1002}}))

        assert first.namespaced(4) != second.namespaced(4)


class TestRunDeduplicationThroughTheClient:
    def runs_body(self):
        return {
            "protectionRunsSummary": [
                {"id": "r-1", "protectionGroupId": "g-1", "protectionGroupName": "n", "status": "Failed"},
                {"id": "r-2", "protectionGroupId": "g-1", "protectionGroupName": "n", "status": "Succeeded"},
                {"id": "r-3", "protectionGroupId": "g-2", "protectionGroupName": "m", "status": "Running"},
            ]
        }

    def test_three_overlapping_polls_count_each_failure_once(self):
        transport = RecordingTransport({client_module.PROTECTION_RUNS_PATH: self.runs_body()})
        client = client_with(transport)

        counted = [client.new_protection_runs() for _ in range(3)]

        assert [run.id for run in counted[0]] == ["r-1", "r-2"]
        assert counted[1] == []
        assert counted[2] == []

    def test_the_raw_call_still_returns_everything(self):
        # The dedup belongs to new_protection_runs; protection_runs stays a faithful read.
        transport = RecordingTransport({client_module.PROTECTION_RUNS_PATH: self.runs_body()})
        client = client_with(transport)

        assert len(client.protection_runs()) == 3
        assert len(client.protection_runs()) == 3

    def test_the_ledger_survives_across_polls_but_not_across_clients(self):
        transport = RecordingTransport({client_module.PROTECTION_RUNS_PATH: self.runs_body()})
        first = client_with(transport)
        first.new_protection_runs()

        assert first.counted_run_ids == 2
        assert len(client_with(transport).new_protection_runs()) == 2


class TestErrorMapping:
    """Auth, TLS and "not on this version" have to read differently. They fix different things."""

    def transport(self, **overrides):
        return HttpTransport(config(**overrides))

    def raising(self, monkeypatch, exception):
        def fail(*_args, **_kwargs):
            raise exception

        monkeypatch.setattr("urllib.request.urlopen", fail)

    def http_error(self, code, reason="Forbidden"):
        return urllib.error.HTTPError("https://host/v2/x", code, reason, {}, None)

    def test_a_401_names_the_api_key_and_the_page_that_lists_it(self, monkeypatch):
        self.raising(monkeypatch, self.http_error(401, "Unauthorized"))

        with pytest.raises(CohesityAuthError) as raised:
            self.transport().get("/clusters/status")

        message = str(raised.value)
        assert "API Keys" in message
        assert "HTTP 401" in message
        assert "cohesity-prod" in message

    def test_a_401_in_vault_mode_rules_the_vault_out_rather_than_in(self, monkeypatch):
        # An unresolved vault entry is refused in config.py, so any 401 that reaches here was
        # produced by a credential that *did* resolve. Saying so is the whole point of the two
        # error messages: one sends a Dynatrace admin to the vault, this one sends a Cohesity
        # admin to the key, and a support call turns entirely on which of the two it is.
        self.raising(monkeypatch, self.http_error(401, "Unauthorized"))
        vaulted = self.transport(
            use_credential_vault=True,
            credential_vault_id="CREDENTIALS_VAULT-0123456789ABCDEF",
            api_key_field="token",
        )

        with pytest.raises(CohesityAuthError) as raised:
            vaulted.get("/clusters/status")

        message = str(raised.value)
        assert "not a credential vault problem" in message
        assert "CREDENTIALS_VAULT-0123456789ABCDEF" in message
        assert "token" in message

    def test_a_403_names_the_privileges_time_series_stats_needs(self, monkeypatch):
        # A valid key with an under-privileged owner is the likeliest 403, and the five
        # privileges on time-series-stats drive the whole credential ask in ticket 01.
        self.raising(monkeypatch, self.http_error(403))

        with pytest.raises(CohesityAuthError) as raised:
            self.transport().get("/stats/time-series-stats")

        assert "PROTECTION_VIEW" in str(raised.value)

    def test_a_404_reads_as_a_version_problem_not_a_fault(self, monkeypatch):
        self.raising(monkeypatch, self.http_error(404, "Not Found"))

        with pytest.raises(CohesityEndpointError) as raised:
            self.transport().get("/stats/top-views")

        message = str(raised.value)
        assert "7.3" in message
        assert "softwareVersion" in message

    def test_an_untrusted_certificate_says_how_to_trust_it(self, monkeypatch):
        reason = ssl.SSLCertVerificationError("self signed certificate")
        self.raising(monkeypatch, urllib.error.URLError(reason))

        with pytest.raises(CohesityConnectError) as raised:
            self.transport().get("/clusters/status")

        message = str(raised.value)
        assert "CA certificate file path" in message
        assert "self-signed" in message

    def test_a_handshake_failure_is_not_blamed_on_the_credential(self, monkeypatch):
        self.raising(monkeypatch, ssl.SSLError("WRONG_VERSION_NUMBER"))

        with pytest.raises(CohesityConnectError) as raised:
            self.transport().get("/clusters/status")

        message = str(raised.value)
        assert "not a credential one" in message
        assert "API key" not in message

    def test_an_unreachable_host_names_the_route_and_the_port(self, monkeypatch):
        self.raising(monkeypatch, urllib.error.URLError(ConnectionRefusedError(111, "refused")))

        with pytest.raises(CohesityConnectError) as raised:
            self.transport(port=8443).get("/clusters/status")

        assert "port 8443" in str(raised.value)

    def test_a_timeout_points_at_the_timeout_setting(self, monkeypatch):
        self.raising(monkeypatch, TimeoutError("timed out"))

        with pytest.raises(CohesityConnectError) as raised:
            self.transport(request_timeout_seconds=7).get("/clusters/status")

        assert "7s" in str(raised.value)

    def test_html_from_a_proxy_is_not_reported_as_a_cluster_fault(self, monkeypatch):
        class Fake:
            def read(self):
                return b"<html>login</html>"

            def __enter__(self):
                return self

            def __exit__(self, *_exc):
                return False

        monkeypatch.setattr("urllib.request.urlopen", lambda *_a, **_k: Fake())

        with pytest.raises(CohesityApiError) as raised:
            self.transport().get("/clusters/status")

        assert "proxy or load balancer" in str(raised.value)

    def test_a_missing_ca_file_is_refused_before_any_request(self):
        with pytest.raises(CohesityConnectError) as raised:
            HttpTransport(config(verify_tls=True, ca_cert_path="/nope/ca.pem"))

        assert "on the ActiveGate" in str(raised.value)


class TestReplayAgainstShippedFixtures:
    """Replay mode, exercised end to end against the fixtures the repository ships."""

    def client(self, **overrides) -> CohesityClient:
        return CohesityClient(config(fixture_dir=str(FIXTURE_DIR), **overrides))

    def test_replay_is_chosen_by_configuration_alone(self):
        assert isinstance(self.client()._transport, FixtureTransport)
        assert self.client().is_replaying is True

    def test_cluster_status_comes_back_parsed(self):
        status = self.client().cluster_status()

        assert status.cluster_id == FIXTURE_CLUSTER_ID
        assert status.version >= (7, 3)
        assert status.node_count == 3

    def test_cluster_storage_carries_all_seven_scalars(self):
        storage = self.client().cluster_storage()

        assert storage.total_capacity_bytes > 0
        assert 0 < storage.used_pct < 100

    def test_every_cluster_stats_call_has_a_fixture_and_parses(self):
        client = self.client()
        for call in client_module.CLUSTER_STATS_CALLS:
            series = client.cluster_time_series(call)

            assert set(series) == set(call["metricNames"])
            for metric in series.values():
                assert metric.latest_value() is not None

    def test_the_null_tail_point_in_the_memory_fixture_is_stepped_over(self):
        series = self.client().cluster_time_series(client_module.CLUSTER_STATS_CALLS[0])

        assert series["kMemoryUsagePct"].data_points[-1].value is None
        assert series["kMemoryUsagePct"].latest_value() == 63.7

    def test_storage_domains_parse_and_namespace(self):
        client = self.client()
        domains = client.storage_domains()

        assert len(domains) == 3
        assert domains[0].entity_id(client.cluster_status().cluster_id).startswith(f"{FIXTURE_CLUSTER_ID}_")
        assert domains[0].local_tier_resiliency_impact_bytes > 0

    def test_the_time_series_schema_discovery_call_yields_triples(self):
        domains = self.client().storage_domain_schemas()
        schemas = domains[0].schemas

        assert schemas
        assert all(ref.schema_name and ref.metric_name and ref.entity_id for ref in schemas)

    def test_both_view_metrics_have_fixtures_on_both_sides_of_the_fork(self):
        client = self.client()
        for metric in client_module.VIEW_METRICS:
            views = client.view_stats(metric)

            assert len(views) == 3
            assert all(view.value is not None for view in views)

    def test_protection_groups_and_runs_parse(self):
        client = self.client()
        groups = client.protection_groups()
        runs = client.new_protection_runs()

        assert {group.name for group in groups} >= {"Nightly-VMware-Tier1", "SQL-Hourly-Logs"}
        # Five runs in the fixture, one of them Running - it must not be counted.
        assert len(runs) == 4
        assert "Running" not in {run.status for run in runs}

    def test_every_shipped_fixture_is_reachable_through_a_client_method(self):
        # A fixture nobody can reach is a fixture that will rot unnoticed.
        client = self.client()
        client.cluster_status()
        client.cluster_storage()
        for call in client_module.CLUSTER_STATS_CALLS:
            client.cluster_time_series(call)
        client.storage_domains()
        client.storage_domain_schemas()
        for metric in client_module.VIEW_METRICS:
            client.view_stats(metric)
        client.protection_groups()
        client.protection_runs()

        served = {fixture.key for fixture in client._transport.served}
        shipped = set(FixtureStore(FIXTURE_DIR).fixture_keys())
        # The pre-7.3 views fixtures are only reachable on an older cluster, by design.
        assert shipped - served == {"v2_stats_views__kNumBytesRead", "v2_stats_views__kNumBytesWritten"}

    def test_replay_warns_that_the_numbers_are_invented(self):
        client = self.client()
        client.cluster_storage()

        caveats = client.caveats()
        assert caveats
        assert "SYNTHETIC" in caveats[0]

    def test_a_missing_fixture_says_how_to_record_one(self):
        client = CohesityClient(config(fixture_dir=str(FIXTURE_DIR / "nonexistent")))

        with pytest.raises(CohesityFixtureError) as raised:
            client.cluster_status()

        assert "does not exist" in str(raised.value)

    def test_a_pre_73_cluster_replays_the_deprecated_views_fixture(self, tmp_path):
        # Proves the fork end to end: swap the version, and a different file answers.
        for key in FixtureStore(FIXTURE_DIR).fixture_keys():
            (tmp_path / f"{key}.json").write_bytes((FIXTURE_DIR / f"{key}.json").read_bytes())
        status_path = tmp_path / "v2_clusters_status.json"
        document = json.loads(status_path.read_text(encoding="utf-8"))
        document["body"]["softwareVersion"] = "7.2.1_u1_release"
        status_path.write_text(json.dumps(document), encoding="utf-8")

        client = CohesityClient(config(fixture_dir=str(tmp_path)))
        client.view_stats("kNumBytesRead")

        assert {fixture.key for fixture in client._transport.served} >= {"v2_stats_views__kNumBytesRead"}


class TestDomainObjectsNotRawJson:
    def test_the_client_returns_parsed_objects_so_the_metric_layer_stays_a_seam(self):
        # Ticket 06 owns metric keys, units and dimensions. The client must not pre-empt that,
        # and it must not hand raw JSON upward either.
        client = CohesityClient(config(fixture_dir=str(FIXTURE_DIR)))

        assert isinstance(client.cluster_status(), domain.ClusterStatus)
        assert isinstance(client.cluster_storage(), domain.ClusterStorage)
        assert all(isinstance(item, domain.StorageDomain) for item in client.storage_domains())
        assert all(isinstance(item, domain.ProtectionRun) for item in client.protection_runs())
        assert all(isinstance(item, domain.ProtectionGroup) for item in client.protection_groups())
        assert all(isinstance(item, domain.ViewStats) for item in client.view_stats("kNumBytesRead"))
