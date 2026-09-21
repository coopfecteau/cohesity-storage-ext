"""Drift mode: the fake cluster's numbers move, so a real ActiveGate does not chart flat lines.

The e2e harness (``e2e/``) runs the fake server with ``--drift``. What matters there is that
counters actually increment and gauges actually move - and, just as much, that drift is opt-in,
deterministic for a given clock, and never touches the fixture files or their provenance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cohesity_storage.client import CohesityClient
from cohesity_storage.config import ClusterConfig
from cohesity_storage.fixtures import PROVENANCE_SYNTHETIC, FixtureStore
from tests.cohesity_fake_cluster import (
    DRIFT_CAPACITY_CYCLE_SECONDS,
    DRIFT_RUN_PERIOD_SECONDS,
    FakeCohesityCluster,
    drift,
    resolve,
    self_signed_certificate,
)
from tools.local_cohesity_server import parse_args

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
HEADERS = {"apikey": "demo"}
# A fixed instant at the start of a capacity cycle and on a run-period boundary, so the
# arithmetic below is easy to follow.
T0 = float(DRIFT_CAPACITY_CYCLE_SECONDS * 20_000)


def store() -> FixtureStore:
    return FixtureStore(FIXTURE_DIR)


def served(path: str, now: float | None, params: dict | None = None):
    return resolve(store(), path, params or {}, HEADERS, drift_now=now).body


class TestOptIn:
    def test_without_drift_the_recorded_body_is_served_unchanged(self):
        body = served("/v2/stats/cluster-storage", None)

        assert body == store().load("v2_stats_cluster-storage").body

    def test_the_wrapper_flag_defaults_off_and_turns_on(self):
        assert parse_args([]).drift is False
        assert parse_args(["--drift"]).drift is True

    def test_the_fixture_and_its_provenance_are_never_mutated(self):
        fixtures = store()
        before = fixtures.load("v2_data-protect_runs_summary")

        resolve(fixtures, "/v2/data-protect/runs/summary", {}, HEADERS, drift_now=T0 + 3600)
        after = fixtures.load("v2_data-protect_runs_summary")

        assert after.body == before.body
        assert after.provenance == PROVENANCE_SYNTHETIC

    def test_the_same_instant_always_gives_the_same_numbers(self):
        path = "/v2/stats/time-series-stats"
        params = {"schemaName": "kBridgeClusterLogicalStats"}

        assert served(path, T0 + 90, params) == served(path, T0 + 90, params)


class TestCapacity:
    def test_used_capacity_creeps_up_through_the_cycle(self):
        early = served("/v2/stats/cluster-storage", T0 + 60)
        late = served("/v2/stats/cluster-storage", T0 + DRIFT_CAPACITY_CYCLE_SECONDS / 2)

        assert late["localUsageBytes"] > early["localUsageBytes"]

    def test_used_plus_available_still_equals_total(self):
        body = served("/v2/stats/cluster-storage", T0 + DRIFT_CAPACITY_CYCLE_SECONDS * 0.9)

        assert body["localUsageBytes"] + body["localAvailableBytes"] == body["totalCapacityBytes"]
        assert body["localUsageBytes"] <= body["totalCapacityBytes"]

    def test_storage_domain_usage_moves_too(self):
        early = served("/v2/storage-domains", T0 + 60, {"includeStats": "true"})
        late = served("/v2/storage-domains", T0 + DRIFT_CAPACITY_CYCLE_SECONDS / 2, {"includeStats": "true"})

        first_early = early["storageDomains"][0]["stats"]["totalLogicalUsageBytes"]
        first_late = late["storageDomains"][0]["stats"]["totalLogicalUsageBytes"]
        assert first_late > first_early


class TestNoise:
    def test_iops_change_from_one_minute_to_the_next(self):
        path = "/v2/stats/time-series-stats"
        params = {"schemaName": "kBridgeClusterLogicalStats"}

        values = {
            served(path, T0 + minute * 60, params)["timeSeriesStats"][0]["dataPoints"][-1]["int64Value"]
            for minute in range(5)
        }

        assert len(values) > 1

    def test_integers_stay_integers_and_percentages_stay_percentages(self):
        body = served("/v2/stats/time-series-stats", T0 + 600, {"schemaName": "kSentryClusterStats"})

        for series in body["timeSeriesStats"]:
            for point in series["dataPoints"]:
                assert point["int64Value"] is None
                # The fixture carries a deliberate null point; drift must leave it null.
                assert point["doubleValue"] is None or 0 <= point["doubleValue"] <= 100
        iops = served("/v2/stats/time-series-stats", T0 + 600, {"schemaName": "kBridgeClusterLogicalStats"})
        assert isinstance(iops["timeSeriesStats"][0]["dataPoints"][0]["int64Value"], int)

    def test_view_throughput_moves_and_lasthours_does_not(self):
        path = "/v2/stats/top-views"
        params = {"metric": "kNumBytesRead"}
        recorded = store().load("v2_stats_top-views__kNumBytesRead").body

        body = served(path, T0 + 60, params)

        window = body["viewsStats"][0]["stats"][0]["valueInLastHours"][0]
        original = recorded["viewsStats"][0]["stats"][0]["valueInLastHours"][0]
        assert window["lastHours"] == original["lastHours"]
        assert window["value"] != original["value"]


class TestRuns:
    PATH = "/v2/data-protect/runs/summary"

    def run_ids(self, now: float) -> set[str]:
        return {run["id"] for run in served(self.PATH, now)["protectionRunsSummary"]}

    def test_the_recorded_runs_are_still_there(self):
        body = store().load("v2_data-protect_runs_summary").body
        recorded = {run["id"] for run in body["protectionRunsSummary"]}

        assert recorded <= self.run_ids(T0 + 60)

    def test_a_later_poll_sees_run_ids_an_earlier_one_did_not(self):
        earlier = self.run_ids(T0 + 60)
        later = self.run_ids(T0 + 60 + DRIFT_RUN_PERIOD_SECONDS)

        assert later - earlier

    def test_every_generated_run_has_finished_and_is_terminal(self):
        now = T0 + 1234
        runs = served(self.PATH, now)["protectionRunsSummary"]

        generated = [run for run in runs if "-drift-" in run["id"]]
        assert generated
        for run in generated:
            assert run["status"] in ("Succeeded", "Failed")
            assert run["startTimeUsecs"] < run["endTimeUsecs"] <= now * 1_000_000

    def test_a_non_dict_body_passes_through(self):
        assert drift(self.PATH, ["unexpected"], T0) == ["unexpected"]

    def test_run_counters_increment_through_the_real_client(self, monkeypatch):
        """The property the e2e harness relies on: new runs keep arriving past the first poll."""
        clock = [T0 + 60]
        client = CohesityClient(ClusterConfig(name="fake", host="127.0.0.1", port=1, api_key="demo"))
        # The clock is pinned by calling resolve() directly, so the dedup ledger is exercised
        # at two known instants rather than at whatever the wall clock says.
        monkeypatch.setattr(
            client._transport,
            "get",
            lambda path, params=None: (
                resolve(store(), "/v2" + path, params or {}, HEADERS, drift_now=clock[0]).body
            ),
        )

        first = client.new_protection_runs()
        again = client.new_protection_runs()
        clock[0] += DRIFT_RUN_PERIOD_SECONDS
        later = client.new_protection_runs()

        assert first
        assert again == []
        assert later
        assert all("-drift-" in run.id for run in later)


class TestOverASocket:
    def test_a_drifting_server_serves_generated_runs(self, tmp_path):
        pytest.importorskip("cryptography")
        cluster = FakeCohesityCluster(
            store=store(), certfile=self_signed_certificate(tmp_path), anchor=None, drift=True
        )
        cluster.start()
        try:
            client = CohesityClient(
                ClusterConfig(
                    name="fake", host=cluster.host, port=cluster.port, api_key="demo", verify_tls=False
                )
            )
            runs = client.protection_runs()
        finally:
            cluster.stop()

        assert any("-drift-" in run.id for run in runs)
