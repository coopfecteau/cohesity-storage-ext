"""The fake cluster: the loop `dt-sdk run` exercises when there is no Cohesity to talk to.

Most of it is tested through :func:`resolve`, which is pure. One test binds a real socket and
speaks TLS to it with the extension's own transport, because the point of the fake server is to
prove the parts that a fixture cannot: sockets, certificates, headers and status codes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cohesity_storage import metrics
from cohesity_storage.client import CLUSTER_STATS_CALLS, CohesityClient
from cohesity_storage.config import ClusterConfig
from cohesity_storage.domain import (
    STORAGE_DOMAIN_LOGICAL_FIELDS,
    STORAGE_DOMAIN_PHYSICAL_FIELDS,
)
from cohesity_storage.errors import CohesityAuthError, CohesityEndpointError
from cohesity_storage.fixtures import FixtureStore
from tests.cohesity_fake_cluster import (
    HOSTILE_NAME_SUFFIX,
    V1_CLUSTER_PATH,
    FakeCohesityCluster,
    resolve,
    self_signed_certificate,
    shift_times,
)
from tools.local_cohesity_server import parse_args

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
GOOD_HEADERS = {"apikey": "demo"}


def store() -> FixtureStore:
    return FixtureStore(FIXTURE_DIR)


class TestAuthentication:
    def test_a_request_without_the_api_key_header_gets_401(self):
        answer = resolve(store(), "/v2/stats/cluster-storage", {}, {})

        assert answer.status == 401

    def test_an_empty_api_key_header_gets_401(self):
        answer = resolve(store(), "/v2/stats/cluster-storage", {}, {"apikey": "   "})

        assert answer.status == 401

    def test_any_non_empty_key_is_accepted_when_none_is_configured(self):
        answer = resolve(store(), "/v2/stats/cluster-storage", {}, GOOD_HEADERS)

        assert answer.status == 200
        assert answer.body["totalCapacityBytes"] > 0

    def test_a_configured_key_must_match(self):
        assert resolve(store(), "/v2/clusters/status", {}, GOOD_HEADERS, api_key="other").status == 401
        assert resolve(store(), "/v2/clusters/status", {}, GOOD_HEADERS, api_key="demo").status == 200


class TestVersionBehaviour:
    def test_a_pre_73_cluster_404s_top_views_exactly_as_a_real_one_does(self):
        answer = resolve(
            store(), "/v2/stats/top-views", {"metric": "kNumBytesRead"}, GOOD_HEADERS,
            software_version="7.2.1_u1_release",
        )

        assert answer.status == 404
        assert "7.3" in answer.body["message"]

    def test_a_pre_73_cluster_still_serves_the_deprecated_path(self):
        answer = resolve(
            store(), "/v2/stats/views", {"metric": "kNumBytesRead"}, GOOD_HEADERS,
            software_version="7.2.1_u1_release",
        )

        assert answer.status == 200

    def test_the_reported_version_can_be_overridden(self):
        answer = resolve(store(), "/v2/clusters/status", {}, GOOD_HEADERS, software_version="6.8.1_u6")

        assert answer.body["softwareVersion"] == "6.8.1_u6"

    def test_an_unrecorded_path_is_a_404_not_a_crash(self):
        answer = resolve(store(), "/v2/nothing/here", {}, GOOD_HEADERS)

        assert answer.status == 404


class TestTimeShifting:
    def test_instant_fields_move_and_measurements_do_not(self):
        payload = {
            "startTimeUsecs": 1_000_000,
            "timestampMsecs": 1_000,
            "bytesWritten": 4096,
            "totalObjectsCount": 18,
        }

        shifted = shift_times(payload, 10)

        assert shifted["startTimeUsecs"] == 11_000_000
        assert shifted["timestampMsecs"] == 11_000
        # Shifting a size or a count would invent data rather than re-date it.
        assert shifted["bytesWritten"] == 4096
        assert shifted["totalObjectsCount"] == 18

    def test_nulls_and_nesting_survive(self):
        payload = {"runs": [{"endTimeUsecs": None, "startTimeUsecs": 5}]}

        shifted = shift_times(payload, 1)

        assert shifted["runs"][0]["endTimeUsecs"] is None
        assert shifted["runs"][0]["startTimeUsecs"] == 1_000_005

    def test_a_run_from_the_fixture_reads_as_recent_when_anchored(self):
        answer = resolve(
            store(), "/v2/data-protect/runs/summary", {}, GOOD_HEADERS, shift_seconds=86_400
        )
        original = store().load("v2_data-protect_runs_summary").body

        served = answer.body["protectionRunsSummary"][0]["startTimeUsecs"]
        recorded = original["protectionRunsSummary"][0]["startTimeUsecs"]
        assert served - recorded == 86_400 * 1_000_000


class TestWrongEntityIdAndRenamedFields:
    """The two customer-cluster shapes v0.1.3 could not see, modelled so they can be seen.

    Both are silent failures. A wrong entityId is HTTP 200 with the series present and empty;
    a field spelled differently is simply absent from an otherwise complete ``stats`` object.
    Neither produces an error, which is why both survived a green test suite and a real e2e run.
    """

    def series(self, answer):
        return answer.body["timeSeriesStats"]

    def test_the_right_entity_id_gets_the_recorded_points(self):
        answer = resolve(
            store(),
            "/v2/stats/time-series-stats",
            {"schemaName": "kSentryClusterStats", "entityId": "42"},
            GOOD_HEADERS,
            stats_entity_id="42",
        )

        assert answer.status == 200
        assert any(entry["dataPoints"] for entry in self.series(answer))

    def test_a_wrong_entity_id_gets_200_and_empty_data_points_not_an_error(self):
        answer = resolve(
            store(),
            "/v2/stats/time-series-stats",
            {"schemaName": "kSentryClusterStats", "entityId": "not-the-one"},
            GOOD_HEADERS,
            stats_entity_id="42",
        )

        assert answer.status == 200
        assert [entry["metricName"] for entry in self.series(answer)]
        assert all(entry["dataPoints"] == [] for entry in self.series(answer))

    def test_the_knob_is_off_by_default_so_every_other_test_is_unaffected(self):
        answer = resolve(
            store(),
            "/v2/stats/time-series-stats",
            {"schemaName": "kSentryClusterStats", "entityId": "anything"},
            GOOD_HEADERS,
        )

        assert any(entry["dataPoints"] for entry in self.series(answer))

    def test_v1_public_cluster_is_refused_by_default(self):
        # Unpublished for 6.8-7.4, so "this cluster will not serve it" is the honest default.
        answer = resolve(store(), V1_CLUSTER_PATH, {}, GOOD_HEADERS)

        assert answer.status == 404

    def test_v1_public_cluster_can_refuse_with_a_403(self):
        answer = resolve(store(), V1_CLUSTER_PATH, {}, GOOD_HEADERS, v1_cluster_status=403)

        assert answer.status == 403

    def test_v1_public_cluster_serves_an_id_when_the_cluster_is_told_to(self):
        answer = resolve(store(), V1_CLUSTER_PATH, {}, GOOD_HEADERS, v1_cluster_id="777")

        assert (answer.status, answer.body["id"]) == (200, "777")

    def test_v1_public_cluster_still_needs_the_api_key(self):
        assert resolve(store(), V1_CLUSTER_PATH, {}, {}, v1_cluster_id="777").status == 401

    def test_a_renamed_stats_field_is_gone_under_its_recorded_name(self):
        answer = resolve(
            store(),
            "/v2/storage-domains",
            {"includeStats": "true"},
            GOOD_HEADERS,
            storage_domain_stats_aliases={"totalLogicalUsageBytes": "logicalUsageBytes"},
        )

        stats = answer.body["storageDomains"][0]["stats"]
        assert "totalLogicalUsageBytes" not in stats
        assert stats["logicalUsageBytes"] > 0

    def test_renaming_leaves_the_fields_it_was_not_asked_about_alone(self):
        answer = resolve(
            store(),
            "/v2/storage-domains",
            {"includeStats": "true"},
            GOOD_HEADERS,
            storage_domain_stats_aliases={"totalLogicalUsageBytes": "logicalUsageBytes"},
        )

        assert answer.body["storageDomains"][0]["stats"]["localTierResiliencyImpactBytes"] > 0


class TestOverASocket:
    """One end-to-end pass: real TLS, real headers, real status codes, the real transport."""

    @pytest.fixture
    def cluster(self, tmp_path):
        # Only this class needs a certificate, and only a certificate needs cryptography. It is
        # a development dependency; the extension itself never imports it.
        pytest.importorskip("cryptography")
        certfile = self_signed_certificate(tmp_path)
        server = FakeCohesityCluster(store=store(), certfile=certfile, anchor=None)
        server.start()
        yield server
        server.stop()

    def config(self, cluster, **overrides):
        values = {
            "name": "fake-cluster",
            "host": cluster.host,
            "port": cluster.port,
            "api_key": "demo",
            # A real Cohesity also ships a self-signed certificate, so this is the same choice
            # an operator faces on day one: trust its CA, or turn verification off.
            "verify_tls": False,
        }
        values.update(overrides)
        return ClusterConfig(**values)

    def test_the_whole_client_works_against_it(self, cluster):
        client = CohesityClient(self.config(cluster))

        status = client.cluster_status()
        storage = client.cluster_storage()
        runs = client.new_protection_runs()

        assert status.cluster_id == "1234567890123456"
        assert storage.total_capacity_bytes > 0
        assert len(runs) == 4
        assert "/v2/clusters/status" in cluster.requests[0]

    def test_the_host_link_produces_bridge_metrics_over_a_real_socket(self, cluster):
        """Ticket 16's whole chain, once, over TLS - the path replay mode cannot cover.

        The per-group runs endpoint is keyed by a group id, so replay has no fixture for it and
        `protected_object_links` skips replay outright. This is therefore the only place the
        fan-out, the `includeObjectDetails` request, the uuid normalisation and the metric's
        dimensions are exercised together against something that speaks HTTP.
        """
        client = CohesityClient(self.config(cluster, collect_host_link=True))

        links = client.protected_object_links()
        samples = metrics.protected_object_samples("1234567890123456", "prod", links)

        # Two VMware objects on the one kVMware group that has a run carrying objects.
        assert {link.uuid for link in links} == {
            "00112233-4455-6677-8899-aabbccddeeff",
            "421f9a3b-88c0-4f11-9d2e-6b7a10cc45ef",
        }
        assert {sample.key for sample in samples} == {metrics.PROTECTION_GROUP_PROTECTS}
        for sample in samples:
            wired = metrics.wire_dimensions(sample.dimensions)
            # Every field the sync workflow joins on has to survive the wire format.
            assert wired[metrics.DIM_CLUSTER_ID] == "1234567890123456"
            assert wired[metrics.DIM_PROTECTION_GROUP_ID].startswith("1234567890123456_")
            assert wired[metrics.DIM_OBJECT_UUID] in {link.uuid for link in links}
        # The kSQL group is never asked - it has no uuid to give.
        assert not [request for request in cluster.requests if "g-9002/runs" in request]

    def test_the_host_link_asks_nothing_when_it_is_switched_off(self, cluster):
        client = CohesityClient(self.config(cluster))

        assert client.protected_object_links() == []
        assert not [request for request in cluster.requests if "includeObjectDetails" in request]

    def test_the_entity_id_probe_finds_the_id_only_the_v1_endpoint_knows(self, cluster):
        # The customer shape exactly: /v2/clusters/status carries an id the stats API does not
        # answer to, and v1 /public/cluster carries the one it does.
        cluster.v1_cluster_id = "9988776655"
        cluster.stats_entity_id = "9988776655"
        client = CohesityClient(self.config(cluster))

        series = client.cluster_time_series(CLUSTER_STATS_CALLS[0])

        assert series["kCpuUsagePct"].latest_value() is not None
        assert client.entity_id_candidates()[0] == "9988776655"
        assert any(V1_CLUSTER_PATH in request for request in cluster.requests)

    def test_a_403_from_the_v1_endpoint_costs_a_candidate_not_the_poll(self, cluster):
        cluster.v1_cluster_status = 403
        cluster.stats_entity_id = "1234567890123456"
        client = CohesityClient(self.config(cluster))

        series = client.cluster_time_series(CLUSTER_STATS_CALLS[0])

        assert series["kCpuUsagePct"].latest_value() is not None
        assert "9988776655" not in client.entity_id_candidates()

    def test_no_candidate_answering_reports_nothing_rather_than_a_zero(self, cluster):
        cluster.stats_entity_id = "an-id-nothing-here-will-offer"
        client = CohesityClient(self.config(cluster))

        series = client.cluster_time_series(CLUSTER_STATS_CALLS[0])

        assert all(metric.latest_value() is None for metric in series.values())
        assert metrics.cluster_time_series_samples("1", "p", "kSentryClusterStats", series) == []

    def usage(self, cluster) -> dict:
        return {
            storage_domain.id: (
                storage_domain.total_logical_usage_bytes,
                storage_domain.local_total_physical_usage_bytes,
            )
            for storage_domain in CohesityClient(self.config(cluster)).storage_domains()
        }

    def test_usage_fields_spelled_differently_read_the_same_numbers(self, cluster):
        baseline = self.usage(cluster)
        cluster.storage_domain_stats_aliases = {
            "totalLogicalUsageBytes": "logicalUsageBytes",
            "localTotalPhysicalUsageBytes": "totalPhysicalUsageBytes",
        }

        assert self.usage(cluster) == baseline
        assert all(numbers[0] and numbers[1] for numbers in baseline.values())

    def test_no_candidate_name_present_produces_no_sample_not_a_zero(self, cluster):
        # Renaming every candidate away is the only honest way to say "this cluster spells them
        # in some way nobody has anticipated", which is the state the customer cluster was in.
        cluster.storage_domain_stats_aliases = {
            name: f"unheardOf{name}"
            for name in STORAGE_DOMAIN_LOGICAL_FIELDS + STORAGE_DOMAIN_PHYSICAL_FIELDS
        }
        client = CohesityClient(self.config(cluster))

        samples = metrics.storage_domain_samples("1", "prod", client.storage_domains())

        keys = {sample.key for sample in samples}
        assert metrics.STORAGE_DOMAIN_USAGE_LOGICAL not in keys
        assert metrics.STORAGE_DOMAIN_USAGE_PHYSICAL not in keys
        # The one that demonstrably arrives on a real cluster is untouched by the aliasing.
        assert metrics.STORAGE_DOMAIN_RESILIENCY_BYTES in keys

    def test_a_missing_api_key_surfaces_as_the_auth_message(self, cluster):
        cluster.api_key = "the-real-key"
        client = CohesityClient(self.config(cluster, api_key="the-wrong-key"))

        with pytest.raises(CohesityAuthError) as raised:
            client.cluster_status()

        assert "API Keys" in str(raised.value)

    def test_a_pre_73_cluster_makes_the_client_fall_back_over_the_wire(self, cluster):
        cluster.software_version = "7.2.1_u1_release"
        client = CohesityClient(self.config(cluster))

        views = client.view_stats("kNumBytesRead")

        assert len(views) == 3
        assert any("/v2/stats/views" in request for request in cluster.requests)
        assert not any("/v2/stats/top-views" in request for request in cluster.requests)

    def test_a_top_views_404_is_surfaced_as_a_version_problem(self, cluster):
        # Forced: the client is told 7.4 but the server behaves as 7.2, which is what a wrong
        # softwareVersion reading would produce.
        cluster.software_version = None
        client = CohesityClient(self.config(cluster))
        client.cluster_status()
        cluster.software_version = "7.2.0_release"

        with pytest.raises(CohesityEndpointError):
            client._transport.get("/stats/top-views", {"metric": "kNumBytesRead"})


class TestHostileNames:
    """``--hostile-names``: every name carries what the line protocol treats specially."""

    NAME_FIELDS = [
        ("/v2/clusters/status", None, "name"),
        ("/v2/storage-domains", "storageDomains", "name"),
        ("/v2/stats/top-views", "viewsStats", "viewName"),
        ("/v2/data-protect/protection-groups", "protectionGroups", "name"),
        ("/v2/data-protect/runs/summary", "protectionRunsSummary", "protectionGroupName"),
    ]

    def params(self, path: str) -> dict[str, str]:
        # The fixture key for these two depends on the query, so ask exactly as the client does.
        if path == "/v2/stats/top-views":
            return {"metric": "kNumBytesRead"}
        if path == "/v2/storage-domains":
            return {"includeStats": "true"}
        return {}

    def names(self, body, list_field, name_field) -> list[str]:
        items = [body] if list_field is None else body[list_field]
        return [item[name_field] for item in items]

    def test_the_suffix_carries_every_special_character(self):
        for character in ('"', "\\", "\n", "\t"):
            assert character in HOSTILE_NAME_SUFFIX
        assert not HOSTILE_NAME_SUFFIX.isascii()

    @pytest.mark.parametrize(("path", "list_field", "name_field"), NAME_FIELDS)
    def test_every_name_the_extension_reports_is_rewritten(self, path, list_field, name_field):
        params = self.params(path)
        plain = resolve(store(), path, params, GOOD_HEADERS)
        hostile = resolve(store(), path, params, GOOD_HEADERS, hostile=True)

        assert plain.status == hostile.status == 200
        before = self.names(plain.body, list_field, name_field)
        after = self.names(hostile.body, list_field, name_field)
        assert before
        assert after == [name + HOSTILE_NAME_SUFFIX for name in before]

    def test_off_by_default_and_ids_are_never_touched(self):
        plain = resolve(store(), "/v2/data-protect/protection-groups", {}, GOOD_HEADERS)
        hostile = resolve(store(), "/v2/data-protect/protection-groups", {}, GOOD_HEADERS, hostile=True)

        assert HOSTILE_NAME_SUFFIX not in str(plain.body)
        assert [group["id"] for group in plain.body["protectionGroups"]] == [
            group["id"] for group in hostile.body["protectionGroups"]
        ]

    def test_the_wrapper_flag_defaults_off_and_turns_on(self):
        assert parse_args([]).hostile_names is False
        assert parse_args(["--hostile-names"]).hostile_names is True
