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
    FakeCohesityCluster,
    resolve,
    self_signed_certificate,
    shift_times,
)

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
