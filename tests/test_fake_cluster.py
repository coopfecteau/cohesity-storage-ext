"""The fake cluster: the loop `dt-sdk run` exercises when there is no Cohesity to talk to.

Most of it is tested through :func:`resolve`, which is pure. One test binds a real socket and
speaks TLS to it with the extension's own transport, because the point of the fake server is to
prove the parts that a fixture cannot: sockets, certificates, headers and status codes.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cohesity_storage.client import CohesityClient
from cohesity_storage.config import ClusterConfig
from cohesity_storage.errors import CohesityAuthError, CohesityEndpointError
from cohesity_storage.fixtures import FixtureStore
from tests.cohesity_fake_cluster import (
    HOSTILE_NAME_SUFFIX,
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
