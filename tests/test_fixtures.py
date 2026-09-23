"""Fixture naming and, more importantly, fixture provenance.

The provenance marker is the whole reason this file exists. A hand-written fixture makes the
extension look finished while encoding a guess as a fact, so the tests treat "declares where it
came from" as a property of the repository, not as a nice-to-have.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cohesity_storage.errors import CohesityFixtureError
from cohesity_storage.fixtures import (
    PROVENANCE_CAPTURED,
    PROVENANCE_SYNTHETIC,
    PROVENANCE_UNKNOWN,
    FixtureStore,
    fixture_key,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"

# Every endpoint the v1 metric set needs, plus both sides of the 7.3 views fork.
REQUIRED_FIXTURES = {
    "v2_clusters_status",
    "v2_stats_cluster-storage",
    "v2_stats_time-series-stats__kSentryClusterStats",
    "v2_stats_time-series-stats__kBridgeClusterLogicalStats",
    "v2_stats_time-series-stats__kBridgeClusterStats",
    "v2_storage-domains__stats",
    "v2_storage-domains__timeseriesschema",
    "v2_stats_top-views__kNumBytesRead",
    "v2_stats_top-views__kNumBytesWritten",
    "v2_stats_views__kNumBytesRead",
    "v2_stats_views__kNumBytesWritten",
    "v2_data-protect_protection-groups",
    "v2_data-protect_runs_summary",
    # The fallback shape, for the two endpoints tried when runs/summary times out.
    "v2_data-protect_protection-runs",
    # Not a metric endpoint. Alerts are collected as log records, so nothing in the metric
    # set reaches this fixture - but replay mode has to be able to serve it, or the alert
    # collection is the one section that cannot be exercised without a cluster.
    "v2_alerts",
}


class TestFixtureKeys:
    def test_a_path_becomes_a_file_stem(self):
        assert fixture_key("/v2/clusters/status") == "v2_clusters_status"

    def test_a_selector_parameter_becomes_a_suffix(self):
        # Three different schemas are fetched from the same path; they cannot share a file.
        assert (
            fixture_key("/v2/stats/time-series-stats", {"schemaName": "kSentryClusterStats"})
            == "v2_stats_time-series-stats__kSentryClusterStats"
        )

    def test_a_shape_flag_becomes_a_suffix(self):
        assert fixture_key("/v2/storage-domains", {"includeStats": "true"}) == "v2_storage-domains__stats"
        assert (
            fixture_key("/v2/storage-domains", {"includeTimeSeriesSchema": True})
            == "v2_storage-domains__timeseriesschema"
        )

    def test_a_time_window_does_not_change_the_key(self):
        # A fixture is a shape, not a moment. Keying on startTimeUsecs would make every poll
        # ask for a file that has never existed.
        first = fixture_key("/v2/data-protect/runs/summary", {"startTimeUsecs": 1})
        second = fixture_key("/v2/data-protect/runs/summary", {"startTimeUsecs": 2})

        assert first == second == "v2_data-protect_runs_summary"

    def test_a_false_flag_is_not_a_suffix(self):
        assert fixture_key("/v2/storage-domains", {"includeStats": False}) == "v2_storage-domains"


class TestProvenance:
    def test_the_envelope_is_read(self, tmp_path):
        (tmp_path / "thing.json").write_text(
            json.dumps(
                {
                    "_fixture": {
                        "provenance": "captured",
                        "capturedAt": "2026-10-01T09:00:00Z",
                        "clusterVersion": "7.4",
                    },
                    "body": {"ok": True},
                }
            ),
            encoding="utf-8",
        )

        fixture = FixtureStore(tmp_path).load("thing")

        assert fixture.is_captured is True
        assert fixture.body == {"ok": True}
        assert fixture.caveat() == ""

    def test_a_synthetic_fixture_says_so_loudly(self, tmp_path):
        (tmp_path / "thing.json").write_text(
            json.dumps({"_fixture": {"provenance": "synthetic"}, "body": {}}), encoding="utf-8"
        )

        caveat = FixtureStore(tmp_path).load("thing").caveat()

        assert "SYNTHETIC" in caveat
        assert "never seen on a cluster" in caveat

    def test_a_bare_body_loads_but_counts_as_unknown_not_captured(self, tmp_path):
        # Dropping a raw capture in should work; silently promoting it to trusted should not.
        (tmp_path / "thing.json").write_text(json.dumps({"clusterId": 1}), encoding="utf-8")

        fixture = FixtureStore(tmp_path).load("thing")

        assert fixture.provenance == PROVENANCE_UNKNOWN
        assert fixture.is_captured is False
        assert "declares no provenance" in fixture.caveat()

    def test_an_invented_provenance_value_is_not_trusted(self, tmp_path):
        (tmp_path / "thing.json").write_text(
            json.dumps({"_fixture": {"provenance": "definitely-real"}, "body": {}}), encoding="utf-8"
        )

        assert FixtureStore(tmp_path).load("thing").provenance == PROVENANCE_UNKNOWN

    def test_provenance_is_summarised_for_a_runbook(self, tmp_path):
        for name, provenance in (("a", "captured"), ("b", "synthetic"), ("c", "synthetic")):
            (tmp_path / f"{name}.json").write_text(
                json.dumps({"_fixture": {"provenance": provenance}, "body": {}}), encoding="utf-8"
            )

        summary = FixtureStore(tmp_path).provenance_summary()

        assert summary[PROVENANCE_CAPTURED] == ["a"]
        assert summary[PROVENANCE_SYNTHETIC] == ["b", "c"]


class TestFixtureErrors:
    def test_a_missing_directory_explains_replay_mode(self, tmp_path):
        with pytest.raises(CohesityFixtureError) as raised:
            FixtureStore(tmp_path / "nope").load("anything")

        assert "fixtureDir" in str(raised.value)

    def test_a_missing_file_lists_what_is_there(self, tmp_path):
        (tmp_path / "present.json").write_text("{}", encoding="utf-8")

        with pytest.raises(CohesityFixtureError) as raised:
            FixtureStore(tmp_path).load("absent")

        assert "present" in str(raised.value)

    def test_unparseable_json_is_not_reported_as_a_cluster_problem(self, tmp_path):
        (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")

        with pytest.raises(CohesityFixtureError) as raised:
            FixtureStore(tmp_path).load("broken")

        assert "could not be read as JSON" in str(raised.value)


class TestShippedFixtures:
    def store(self):
        return FixtureStore(FIXTURE_DIR)

    def test_every_endpoint_the_metric_set_needs_has_a_fixture(self):
        assert set(self.store().fixture_keys()) == REQUIRED_FIXTURES

    def test_every_shipped_fixture_declares_its_provenance(self):
        # The one property that must never regress: no fixture may arrive unmarked, because an
        # unmarked fixture is indistinguishable from a fact once it is in the repository.
        for key in self.store().fixture_keys():
            assert self.store().load(key).provenance != PROVENANCE_UNKNOWN, key

    def test_every_shipped_fixture_is_currently_synthetic_and_says_where_it_came_from(self):
        # When a real capture lands this test is what will fail, which is the right prompt to
        # update the inventory in the ticket rather than let the change pass unnoticed.
        for key in self.store().fixture_keys():
            fixture = self.store().load(key)

            assert fixture.provenance == PROVENANCE_SYNTHETIC, key
            assert fixture.source.startswith("https://developers.cohesity.com/"), key
            assert fixture.description, key

    def test_the_two_views_fixtures_are_byte_identical_bodies(self):
        # Params and response are identical across the 7.3 fork; if these ever diverge, the
        # "one-line path swap" claim in ticket 04 has stopped being true.
        store = self.store()
        for metric in ("kNumBytesRead", "kNumBytesWritten"):
            new_path = store.load(f"v2_stats_top-views__{metric}").body
            old_path = store.load(f"v2_stats_views__{metric}").body

            assert new_path == old_path
