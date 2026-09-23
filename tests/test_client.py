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
from cohesity_storage import domain, metrics
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
from cohesity_storage.transport import (
    FixtureTransport,
    HttpTransport,
    Repeated,
    encode_params,
)

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
FIXTURE_CLUSTER_ID = "1234567890123456"


def config(**overrides) -> ClusterConfig:
    values = {"name": "cohesity-prod", "host": "10.20.30.40", "api_key": "demo-key"}
    values.update(overrides)
    return ClusterConfig(**values)


class RecordingTransport:
    """Answers from a dict of canned bodies and remembers exactly what was asked for.

    A body may be a callable ``(path, params) -> body``, which is what the entityId probe tests
    need: time-series-stats has to answer differently per ``entityId`` rather than per path.
    """

    #: The two discovery calls the client makes before any cluster-level time series. Both are
    #: allowed to fail on a real cluster, so the default here is "this source offers nothing" -
    #: a test that cares supplies its own body.
    def _discovery_bodies(self) -> dict:
        return {
            client_module.V1_CLUSTER_PATH: {},
            client_module.STORAGE_DOMAINS_PATH: {"storageDomains": []},
        }

    def __init__(self, bodies: dict | None = None):
        self.bodies = self._discovery_bodies()
        self.bodies.update(bodies or {})
        self.calls: list[tuple[str, dict]] = []
        self.raise_for: dict = {}

    def get(self, path, params=None, *, prefix=None):  # noqa: ARG002 - the client passes it
        self.calls.append((path, dict(params or {})))
        if path in self.raise_for:
            raise self.raise_for[path]
        if path not in self.bodies:
            msg = f"no canned body for {path}"
            raise AssertionError(msg)
        body = self.bodies[path]
        return body(path, dict(params or {})) if callable(body) else body

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

    def test_the_run_window_is_the_poll_interval_plus_one_overlap(self):
        # It used to be three times the interval. On a cluster with 41 groups and real history
        # that window never answered inside 120s, and runs/summary has no pagination and no job
        # filter, so the window is the only thing there is to make smaller.
        transport = RecordingTransport({client_module.PROTECTION_RUNS_PATH: {"protectionRunsSummary": []}})
        client = client_with(transport, interval_minutes=10)

        client.protection_runs()

        _, params = transport.calls[-1]
        window_seconds = (params["endTimeUsecs"] - params["startTimeUsecs"]) / 1_000_000
        assert window_seconds == pytest.approx(
            10 * 60 + client_module.RUNS_WINDOW_OVERLAP_SECONDS, abs=1
        )

    def test_the_run_window_still_overlaps_the_previous_poll(self):
        # Narrower than the interval would drop a run that started and finished between two
        # polls, which is the one failure this extension must never report as a success.
        transport = RecordingTransport({client_module.PROTECTION_RUNS_PATH: {"protectionRunsSummary": []}})
        client = client_with(transport, interval_minutes=5)

        client.protection_runs()

        _, params = transport.calls[-1]
        window_seconds = (params["endTimeUsecs"] - params["startTimeUsecs"]) / 1_000_000
        assert window_seconds > 5 * 60

    def test_both_ends_of_the_run_window_are_sent(self):
        # endTimeUsecs defaults to "now" on the cluster, so this changes no numbers - it makes
        # the window the extension asked for the window the cluster builds.
        transport = RecordingTransport({client_module.PROTECTION_RUNS_PATH: {"protectionRunsSummary": []}})

        client_with(transport).protection_runs()

        _, params = transport.calls[-1]
        assert params["startTimeUsecs"] < params["endTimeUsecs"]
        assert params["endTimeUsecs"] <= int(time.time() * 1_000_000)

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


V1_ENTITY_ID = "9988776655443322"
SCHEMA_ENTITY_ID = "sd-entity-4"

# Two metrics rather than one, because the probe's "did anything come back" test is over the
# whole schema and a schema is never a single series.
SENTRY_METRICS = ("kCpuUsagePct", "kMemoryUsagePct")


def time_series_body(points: list[dict]) -> dict:
    return {
        "timeSeriesStats": [
            {"metricName": name, "type": "kDouble", "dataPoints": list(points)}
            for name in SENTRY_METRICS
        ]
    }


def answering(entity_id: str):
    """A time-series responder that carries data for one entityId and empties for every other.

    This is the failure that cost the extension five metrics on a real cluster: a wrong entityId
    is answered HTTP 200 with the requested series present and their dataPoints empty. There is
    no error and no 404 - looking at whether a point came back is the only way to tell.
    """

    def respond(_path, params):
        answered = str(params.get("entityId")) == entity_id
        return time_series_body([{"timestampMsecs": 1, "doubleValue": 12.5}] if answered else [])

    return respond


def schema_catalogue(entity_id: str) -> dict:
    return {
        "storageDomains": [
            {
                "id": "7",
                "name": "DefaultStorageDomain",
                "schemas": [
                    {
                        "schemaName": "kBridgeClusterStats",
                        "metricName": "kMorphedGarbageBytes",
                        "entityId": entity_id,
                    }
                ],
            }
        ]
    }


class TestEntityIdProbe:
    """v0.1.3 asserted one entityId and lost five metrics when the assertion was wrong.

    The assertion is replaced by a probe: try each candidate until one returns a data point,
    then cache the winner. Every test here is about a shape the probe has to survive, because
    the failure it guards against is silent - empty dataPoints, HTTP 200, no error.
    """

    def build(self, *, answers: str, bodies: dict | None = None):
        transport = RecordingTransport(
            {
                client_module.CLUSTER_STATUS_PATH: status_body(),
                client_module.TIME_SERIES_STATS_PATH: answering(answers),
                **(bodies or {}),
            }
        )
        return transport, client_with(transport)

    def entity_ids(self, transport: RecordingTransport) -> list[str]:
        return [
            params["entityId"]
            for path, params in transport.calls
            if path == client_module.TIME_SERIES_STATS_PATH
        ]

    def test_the_v1_id_is_tried_first_and_wins_in_one_call(self):
        # Cohesity's own community exporters pass this id on every cluster-level call, so it is
        # the only candidate with field evidence behind it.
        transport, client = self.build(
            answers=V1_ENTITY_ID, bodies={client_module.V1_CLUSTER_PATH: {"id": V1_ENTITY_ID}}
        )

        series = client.cluster_time_series(client_module.CLUSTER_STATS_CALLS[0])

        assert series["kCpuUsagePct"].latest_value() == 12.5
        assert self.entity_ids(transport) == [V1_ENTITY_ID]

    def test_a_later_candidate_wins_when_the_earlier_ones_come_back_empty(self):
        transport, client = self.build(
            answers=SCHEMA_ENTITY_ID,
            bodies={
                client_module.V1_CLUSTER_PATH: {"id": V1_ENTITY_ID},
                client_module.STORAGE_DOMAINS_PATH: schema_catalogue(SCHEMA_ENTITY_ID),
            },
        )

        series = client.cluster_time_series(client_module.CLUSTER_STATS_CALLS[0])

        assert series["kCpuUsagePct"].latest_value() == 12.5
        # Every earlier candidate was tried, in order, and the winner is last.
        assert self.entity_ids(transport) == [V1_ENTITY_ID, FIXTURE_CLUSTER_ID, SCHEMA_ENTITY_ID]

    def test_the_v2_cluster_id_still_wins_when_it_is_the_right_one(self):
        # The v0.1.3 assumption is not removed, only demoted - on most clusters it is correct.
        transport, client = self.build(answers=FIXTURE_CLUSTER_ID)

        client.cluster_time_series(client_module.CLUSTER_STATS_CALLS[0])

        assert self.entity_ids(transport) == [FIXTURE_CLUSTER_ID]

    def test_every_candidate_failing_yields_no_data_and_names_what_was_tried(self):
        schema = client_module.CLUSTER_STATS_CALLS[0]["schemaName"]
        transport, client = self.build(
            answers="nothing-answers-to-this",
            bodies={
                client_module.V1_CLUSTER_PATH: {"id": V1_ENTITY_ID},
                client_module.STORAGE_DOMAINS_PATH: schema_catalogue(SCHEMA_ENTITY_ID),
            },
        )

        series = client.cluster_time_series(client_module.CLUSTER_STATS_CALLS[0])

        # The requested series come back, all empty - so the caller can still warn by name.
        assert set(series) == set(SENTRY_METRICS)
        assert all(metric.latest_value() is None for metric in series.values())
        assert client.entity_ids_tried(schema) == (
            V1_ENTITY_ID,
            FIXTURE_CLUSTER_ID,
            SCHEMA_ENTITY_ID,
        )
        assert self.entity_ids(transport) == list(client.entity_ids_tried(schema))

    def test_nothing_is_invented_to_fill_the_gap(self):
        _, client = self.build(answers="nothing-answers-to-this")

        series = client.cluster_time_series(client_module.CLUSTER_STATS_CALLS[0])

        assert metrics.cluster_time_series_samples("1", "prod", "kSentryClusterStats", series) == []

    def test_the_winner_is_cached_so_a_later_poll_makes_one_call_per_schema(self):
        transport, client = self.build(
            answers=SCHEMA_ENTITY_ID,
            bodies={
                client_module.V1_CLUSTER_PATH: {"id": V1_ENTITY_ID},
                client_module.STORAGE_DOMAINS_PATH: schema_catalogue(SCHEMA_ENTITY_ID),
            },
        )
        for call in client_module.CLUSTER_STATS_CALLS:
            client.cluster_time_series(call)
        first_poll = len(transport.calls)

        for call in client_module.CLUSTER_STATS_CALLS:
            client.cluster_time_series(call)

        second_poll = transport.calls[first_poll:]
        assert len(second_poll) == len(client_module.CLUSTER_STATS_CALLS)
        assert {params["entityId"] for _, params in second_poll} == {SCHEMA_ENTITY_ID}

    def test_the_discovery_calls_are_made_once_for_the_life_of_the_client(self):
        # Two extra requests on the first poll is a fair price; two per poll forever is not.
        transport, client = self.build(
            answers=FIXTURE_CLUSTER_ID, bodies={client_module.V1_CLUSTER_PATH: {"id": V1_ENTITY_ID}}
        )

        for _ in range(3):
            for call in client_module.CLUSTER_STATS_CALLS:
                client.cluster_time_series(call)

        assert transport.paths().count(client_module.V1_CLUSTER_PATH) == 1
        assert transport.paths().count(client_module.STORAGE_DOMAINS_PATH) == 1

    def test_a_403_on_the_v1_endpoint_is_one_fewer_candidate_not_a_failed_poll(self):
        # That endpoint is unpublished for 6.8-7.4 and the v2 key may not carry its privilege.
        transport, client = self.build(answers=FIXTURE_CLUSTER_ID)
        transport.raise_for[client_module.V1_CLUSTER_PATH] = CohesityAuthError("403 refused")

        series = client.cluster_time_series(client_module.CLUSTER_STATS_CALLS[0])

        assert series["kCpuUsagePct"].latest_value() == 12.5
        assert client.entity_id_candidates() == (FIXTURE_CLUSTER_ID,)

    def test_a_404_on_the_v1_endpoint_is_survived_the_same_way(self):
        transport, client = self.build(answers=FIXTURE_CLUSTER_ID)
        transport.raise_for[client_module.V1_CLUSTER_PATH] = CohesityEndpointError("404 absent")

        assert client.entity_id_candidates() == (FIXTURE_CLUSTER_ID,)

    def test_a_failing_schema_catalogue_is_survived_the_same_way(self):
        transport, client = self.build(answers=FIXTURE_CLUSTER_ID)
        transport.raise_for[client_module.STORAGE_DOMAINS_PATH] = CohesityAuthError("403 refused")

        assert client.entity_id_candidates() == (FIXTURE_CLUSTER_ID,)

    def test_incarnation_ids_are_offered_after_every_primary_id(self):
        transport = RecordingTransport(
            {
                client_module.CLUSTER_STATUS_PATH: {**status_body(), "clusterIncarnationId": 5555},
                client_module.V1_CLUSTER_PATH: {"id": V1_ENTITY_ID, "incarnationId": 4444},
                client_module.TIME_SERIES_STATS_PATH: answering("5555"),
            }
        )

        assert client_with(transport).entity_id_candidates() == (
            V1_ENTITY_ID,
            FIXTURE_CLUSTER_ID,
            "4444",
            "5555",
        )

    def test_duplicate_candidates_never_cost_a_second_request(self):
        # v1 id and v2 clusterId are the same int64 on most clusters.
        transport, client = self.build(
            answers="nothing-answers-to-this",
            bodies={client_module.V1_CLUSTER_PATH: {"id": FIXTURE_CLUSTER_ID}},
        )

        client.cluster_time_series(client_module.CLUSTER_STATS_CALLS[0])

        assert self.entity_ids(transport) == [FIXTURE_CLUSTER_ID]


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


def api_error(status: int, message: str = "cluster side") -> CohesityApiError:
    """A failure shaped the way the transport annotates one, status and all."""
    error = CohesityApiError(message)
    error.status = status
    return error


def read_timeout() -> CohesityConnectError:
    """What a 120-second read timeout arrives as: no status, because nothing answered."""
    return CohesityConnectError("cohesity-prod: /x did not answer within 120s")


def group(group_id: str, *, last_run_end_time_usecs: int | None = None) -> domain.ProtectionGroup:
    return domain.ProtectionGroup(
        id=group_id, name=f"job-{group_id}", last_run_end_time_usecs=last_run_end_time_usecs
    )


def group_runs_path(group_id: str) -> str:
    return client_module.PROTECTION_GROUP_RUNS_PATH.format(group_id=group_id)


class TestRunsFallback:
    """runs/summary times out on the customer cluster, and that must not cost five metrics.

    The endpoint has no pagination and no job filter, so when the window alone is not enough
    the only remaining move is a different endpoint. Two exist; both are tried before the run
    metrics are given up on.
    """

    def summary_body(self):
        return {
            "protectionRunsSummary": [
                {"id": "r-1", "protectionGroupId": "g-1", "protectionGroupName": "n", "status": "Succeeded"},
            ]
        }

    def list_body(self):
        # The run-list shape: facts under localBackupInfo, times spelled runStartTimeUsecs.
        return {
            "runs": [
                {
                    "id": "r-1",
                    "protectionGroupId": "g-1",
                    "protectionGroupName": "n",
                    "environment": "kVMware",
                    "localBackupInfo": {
                        "status": "Succeeded",
                        "runStartTimeUsecs": 1_000_000,
                        "runEndTimeUsecs": 4_000_000,
                        "isSlaViolated": False,
                        "localSnapshotStats": {"bytesWritten": 42, "logicalSizeBytes": 99},
                    },
                }
            ]
        }

    def test_a_timeout_on_runs_summary_falls_back_to_the_flat_list(self):
        transport = RecordingTransport({client_module.PROTECTION_RUNS_LIST_PATH: self.list_body()})
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()
        client = client_with(transport)

        runs = client.protection_runs()

        assert [run.id for run in runs] == ["r-1"]
        assert runs[0].status == "Succeeded"
        assert runs[0].bytes_written == 42
        assert client.runs_source == client_module.RUNS_SOURCE_LIST

    def test_a_list_endpoint_that_does_not_exist_falls_back_to_per_group(self):
        # The flat list is not in the 7.3.2 reference this extension was written from, so a 404
        # from it is an ordinary answer - it must not end the chain.
        transport = RecordingTransport({group_runs_path("g-1"): self.list_body()})
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()
        transport.raise_for[client_module.PROTECTION_RUNS_LIST_PATH] = CohesityEndpointError("404")
        client = client_with(transport)

        runs = client.protection_runs(groups=[group("g-1")])

        assert [run.id for run in runs] == ["r-1"]
        assert client.runs_source == client_module.RUNS_SOURCE_PER_GROUP

    def fan_out_transport(self, groups):
        """A transport where only the per-group path answers, as on the customer cluster."""
        bodies = {group_runs_path(item.id): {"runs": []} for item in groups}
        transport = RecordingTransport(bodies)
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()
        transport.raise_for[client_module.PROTECTION_RUNS_LIST_PATH] = CohesityEndpointError("404")
        return transport

    def test_the_per_group_fan_out_is_capped(self):
        groups = [group(f"g-{index}", last_run_end_time_usecs=index) for index in range(40)]
        transport = self.fan_out_transport(groups)

        client_with(transport).protection_runs(groups=groups)

        per_group = [path for path in transport.paths() if path.endswith("/runs")]
        assert len(per_group) == client_module.DEFAULT_RUNS_FANOUT_GROUPS

    def test_the_cap_is_configurable(self):
        groups = [group(f"g-{index}", last_run_end_time_usecs=index) for index in range(40)]
        transport = self.fan_out_transport(groups)

        client_with(transport, max_run_fanout_groups=5).protection_runs(groups=groups)

        assert len([path for path in transport.paths() if path.endswith("/runs")]) == 5

    def test_the_fan_out_asks_for_a_count_of_runs_and_no_time_window(self):
        # The v0.1.7 fix. A window a poll interval wide is almost always empty for any ONE
        # group, and a run that lands while another group is being asked is missed forever.
        # "Your last N runs" cannot come back empty by accident; the ledger discards the
        # repeats that question produces.
        groups = [group("g-1", last_run_end_time_usecs=1)]
        transport = self.fan_out_transport(groups)

        client_with(transport).protection_runs(groups=groups)

        params = [item for path, item in transport.calls if path == group_runs_path("g-1")][0]
        assert params == {"numRuns": client_module.RUNS_PER_GROUP}

    def test_the_fan_out_rotates_so_every_group_is_reached(self):
        # A cap applied by truncation makes the groups past it invisible forever - which is how
        # v0.1.6 could be working and still report nothing, asking the same top 10 of 42 every
        # five minutes. The cap is a per-poll request budget, not a filter.
        groups = [group(f"g-{index}", last_run_end_time_usecs=index) for index in range(42)]
        transport = self.fan_out_transport(groups)
        client = client_with(transport, max_run_fanout_groups=20)

        for _ in range(3):
            client.protection_runs(groups=groups)

        asked = {path for path in transport.paths() if path.endswith("/runs")}
        assert asked == {group_runs_path(item.id) for item in groups}

    def test_a_poll_continues_where_the_previous_one_stopped(self):
        groups = [group(f"g-{index}", last_run_end_time_usecs=index) for index in range(40)]
        transport = self.fan_out_transport(groups)
        client = client_with(transport, max_run_fanout_groups=10)

        client.protection_runs(groups=groups)
        first = [path for path in transport.paths() if path.endswith("/runs")]
        transport.calls.clear()
        client.protection_runs(groups=groups)
        second = [path for path in transport.paths() if path.endswith("/runs")]

        assert not set(first) & set(second)
        # Most-recently-finished still orders the list, so the first poll takes the head of it.
        assert first[0] == group_runs_path("g-39")
        assert second[0] == group_runs_path("g-29")

    def test_the_fan_out_reports_what_it_asked_and_what_it_found(self):
        # The only way to tell "no runs happened" from "we are not looking in the right place",
        # which is the distinction that cost v0.1.6 twenty minutes of silence.
        groups = [group(f"g-{index}", last_run_end_time_usecs=index) for index in range(5)]
        transport = self.fan_out_transport(groups)
        transport.bodies[group_runs_path("g-4")] = self.list_body()
        transport.raise_for[group_runs_path("g-3")] = CohesityAuthError("403")
        client = client_with(transport, max_run_fanout_groups=3)

        client.new_protection_runs(groups=groups)

        fact = [item for item in client.take_diagnostics() if item["kind"] == "runs_fanout"][0]
        assert fact["groups_total"] == 5
        assert fact["groups_queried"] == 3
        assert fact["groups_errored"] == 1
        assert fact["runs_seen"] == 1
        assert fact["runs_new"] == 1

    def test_runs_already_counted_are_reported_as_seen_but_not_new(self):
        # The steady state of the windowless question: the same runs come back every poll and
        # the ledger throws them away. Seen-but-not-new is a healthy cluster, not a fault.
        groups = [group("g-1", last_run_end_time_usecs=1)]
        transport = self.fan_out_transport(groups)
        transport.bodies[group_runs_path("g-1")] = self.list_body()
        client = client_with(transport)

        client.new_protection_runs(groups=groups)
        client.take_diagnostics()
        assert client.new_protection_runs(groups=groups) == []

        fact = [item for item in client.take_diagnostics() if item["kind"] == "runs_fanout"][0]
        assert (fact["runs_seen"], fact["runs_new"]) == (1, 0)

    def test_the_fan_out_diagnostic_does_not_pile_up_undrained(self):
        groups = [group("g-1", last_run_end_time_usecs=1)]
        client = client_with(self.fan_out_transport(groups))

        for _ in range(4):
            client.new_protection_runs(groups=groups)

        facts = [item for item in client.take_diagnostics() if item["kind"] == "runs_fanout"]
        assert len(facts) == 1

    def test_the_fan_out_asks_the_groups_that_finished_most_recently(self):
        # A job that last succeeded days ago is the least likely to be carrying a run nobody
        # has counted, so it belongs at the back of the rotation rather than the front.
        groups = [group(f"g-{index}", last_run_end_time_usecs=index) for index in range(40)]
        bodies = {group_runs_path(item.id): {"runs": []} for item in groups}
        transport = RecordingTransport(bodies)
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()
        transport.raise_for[client_module.PROTECTION_RUNS_LIST_PATH] = CohesityEndpointError("404")

        client_with(transport).protection_runs(groups=groups)

        assert group_runs_path("g-39") in transport.paths()
        assert group_runs_path("g-0") not in transport.paths()

    def test_a_group_with_no_known_last_run_is_asked_last_rather_than_dropped(self):
        # Unknown is not the same as old: it may be a job nobody has seen finish yet.
        groups = [group("g-new"), *(group(f"g-{index}", last_run_end_time_usecs=index) for index in range(9))]
        bodies = {group_runs_path(item.id): {"runs": []} for item in groups}
        transport = RecordingTransport(bodies)
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()
        transport.raise_for[client_module.PROTECTION_RUNS_LIST_PATH] = CohesityEndpointError("404")

        client_with(transport).protection_runs(groups=groups)

        assert transport.paths()[-1] == group_runs_path("g-new")

    def test_one_unreadable_group_does_not_cost_the_others(self):
        groups = [group("g-1"), group("g-2")]
        transport = RecordingTransport(
            {group_runs_path("g-1"): {"runs": []}, group_runs_path("g-2"): self.list_body()}
        )
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()
        transport.raise_for[client_module.PROTECTION_RUNS_LIST_PATH] = CohesityEndpointError("404")
        transport.raise_for[group_runs_path("g-1")] = CohesityAuthError("403")

        runs = client_with(transport).protection_runs(groups=groups)

        assert [run.id for run in runs] == ["r-1"]

    def test_the_winning_source_is_asked_first_next_time(self):
        transport = RecordingTransport({client_module.PROTECTION_RUNS_LIST_PATH: self.list_body()})
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()
        client = client_with(transport)

        client.protection_runs()
        transport.calls.clear()
        client.protection_runs()

        # The endpoint that has already proved it cannot answer inside the timeout is not made
        # to prove it again on every poll for the rest of the extension's uptime.
        assert transport.paths() == [client_module.PROTECTION_RUNS_LIST_PATH]

    def test_a_run_already_counted_is_not_counted_again_from_another_source(self):
        # The ledger is keyed on run.id and all three endpoints report the same id, which is
        # what makes switching sources mid-life safe.
        transport = RecordingTransport(
            {
                client_module.PROTECTION_RUNS_PATH: self.summary_body(),
                client_module.PROTECTION_RUNS_LIST_PATH: self.list_body(),
            }
        )
        client = client_with(transport)
        assert [run.id for run in client.new_protection_runs()] == ["r-1"]

        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()

        assert client.new_protection_runs() == []

    def test_every_source_failing_raises_rather_than_reporting_no_runs(self):
        # "No runs finished" and "nothing answered" are different facts, and reporting the
        # first when the second is true is how 24 hours of zero looked like a quiet cluster.
        transport = RecordingTransport()
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()
        transport.raise_for[client_module.PROTECTION_RUNS_LIST_PATH] = CohesityEndpointError("404")
        transport.raise_for[group_runs_path("g-1")] = api_error(500)

        with pytest.raises(CohesityConnectError):
            client_with(transport).protection_runs(groups=[group("g-1")])

    def test_which_source_won_leaves_as_a_diagnostic(self):
        transport = RecordingTransport({client_module.PROTECTION_RUNS_LIST_PATH: self.list_body()})
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = read_timeout()
        client = client_with(transport)

        client.protection_runs()

        fact = [item for item in client.take_diagnostics() if item["kind"] == "runs_source"][0]
        assert fact["source"] == client_module.RUNS_SOURCE_LIST
        assert fact["attempts"][0].startswith(f"{client_module.RUNS_SOURCE_SUMMARY}=")
        assert fact["attempts"][-1] == f"{client_module.RUNS_SOURCE_LIST}=ok"


class TestParameterVariants:
    """Two endpoints answer HTTP 500 on the customer cluster, for every value that was tried.

    A 500 is the cluster mishandling the request rather than refusing it, so the shape is the
    remaining suspect. The client retries a small ordered set of shapes, keeps the first that
    answers, and says which one that was.
    """

    def series_body(self, metric_names):
        return {
            "timeSeriesStats": [
                {
                    "metricName": name,
                    "type": "kDouble",
                    "dataPoints": [{"timestampMsecs": 1, "doubleValue": 1.5}],
                }
                for name in metric_names
            ]
        }

    def refusing(self, unless, body):
        """A body callable that 500s until ``unless(params)`` is satisfied."""

        def answer(_path, params):
            if not unless(params):
                raise api_error(500)
            return body(params)

        return answer

    def time_series(self, client):
        return client.time_series(
            "kBridgeClusterLogicalStats",
            ("kReadIos", "kWriteIos"),
            "1234",
            rollup_function="kAverage",
            rollup_interval_secs=180,
        )

    def test_a_500_is_retried_in_another_shape_until_one_answers(self):
        transport = RecordingTransport(
            {
                client_module.TIME_SERIES_STATS_PATH: self.refusing(
                    lambda params: "rollupIntervalSecs" not in params,
                    lambda params: self.series_body(params["metricNames"]),
                )
            }
        )
        client = client_with(transport)

        series = self.time_series(client)

        assert sorted(series) == ["kReadIos", "kWriteIos"]
        assert "rollupIntervalSecs" not in transport.calls[-1][1]

    def test_the_winning_shape_is_used_from_then_on_without_probing_again(self):
        transport = RecordingTransport(
            {
                client_module.TIME_SERIES_STATS_PATH: self.refusing(
                    lambda params: "rollupIntervalSecs" not in params,
                    lambda params: self.series_body(params["metricNames"]),
                )
            }
        )
        client = client_with(transport)
        self.time_series(client)
        transport.calls.clear()

        self.time_series(client)

        assert len(transport.calls) == 1

    def test_the_repeated_metric_names_shape_is_tried_first(self):
        # Cheapest thing to be wrong about: the spec says explode:false, but a server that
        # mis-parses the comma-joined value it asked for answers 500 rather than 400.
        transport = RecordingTransport(
            {
                client_module.TIME_SERIES_STATS_PATH: self.refusing(
                    lambda params: isinstance(params["metricNames"], Repeated),
                    lambda params: self.series_body(params["metricNames"]),
                )
            }
        )
        client = client_with(transport)

        assert sorted(self.time_series(client)) == ["kReadIos", "kWriteIos"]
        assert len(transport.calls) == 2

    def test_one_metric_per_request_merges_into_one_series_set(self):
        transport = RecordingTransport(
            {
                client_module.TIME_SERIES_STATS_PATH: self.refusing(
                    lambda params: len(params["metricNames"]) == 1,
                    lambda params: self.series_body(params["metricNames"]),
                )
            }
        )
        client = client_with(transport)

        series = self.time_series(client)

        assert sorted(series) == ["kReadIos", "kWriteIos"]
        assert all(metric.data_points for metric in series.values())

    def test_a_cached_shape_that_does_not_fit_the_next_call_is_not_sent_as_nothing(self):
        # The cache is per path, and the three cluster schemas share one. A shape that splits
        # by metric cannot apply to the schema that asks for a single metric, and returning no
        # requests at all there would read as a schema with no data - the exact silent gap.
        transport = RecordingTransport(
            {
                client_module.TIME_SERIES_STATS_PATH: self.refusing(
                    lambda params: len(params["metricNames"]) == 1,
                    lambda params: self.series_body(params["metricNames"]),
                )
            }
        )
        client = client_with(transport)
        self.time_series(client)

        single = client.time_series("kBridgeClusterStats", ("kMorphedGarbageBytes",), "1234")

        assert sorted(single) == ["kMorphedGarbageBytes"]

    def test_a_403_is_never_probed_because_a_shape_cannot_fix_a_privilege(self):
        transport = RecordingTransport()
        transport.raise_for[client_module.TIME_SERIES_STATS_PATH] = CohesityAuthError("403")
        client = client_with(transport)

        with pytest.raises(CohesityAuthError):
            self.time_series(client)

        assert len(transport.calls) == 1

    def test_a_404_on_top_views_still_falls_back_by_path_not_by_shape(self):
        transport = RecordingTransport(
            {
                client_module.CLUSTER_STATUS_PATH: status_body("7.4.0_release"),
                client_module.VIEWS_PATH: {"viewsStats": []},
            }
        )
        transport.raise_for[client_module.TOP_VIEWS_PATH] = CohesityEndpointError("404")

        client_with(transport).view_stats("kNumBytesRead")

        assert transport.paths().count(client_module.TOP_VIEWS_PATH) == 1

    def test_a_top_views_500_drops_optional_parameters_before_giving_up(self):
        transport = RecordingTransport(
            {
                client_module.CLUSTER_STATUS_PATH: status_body("7.3.2_release"),
                client_module.TOP_VIEWS_PATH: self.refusing(
                    lambda params: "protocol" not in params,
                    lambda _params: {"viewsStats": []},
                ),
            }
        )

        client_with(transport).view_stats("kNumBytesRead")

        assert "protocol" not in transport.calls[-1][1]

    def test_no_top_views_variant_ever_drops_the_metric(self):
        # The endpoint defaults to kNumBytesRead, so a request that never named the metric
        # would answer 200 with the wrong series - a confident wrong number, which is worse
        # than a missing one.
        for variant in client_module.TOP_VIEWS_VARIANTS:
            for shape in variant.requests({"metric": "kNumBytesWritten", "numTopViews": 20,
                                           "lastHours": 1, "protocol": "kAny"}):
                assert shape["metric"] == "kNumBytesWritten"

    def test_a_probing_poll_is_bounded(self):
        transport = RecordingTransport(
            {client_module.TIME_SERIES_STATS_PATH: self.refusing(lambda _params: False, dict)}
        )
        client = client_with(transport)
        client.begin_poll()

        for _ in range(3):
            with pytest.raises(CohesityApiError):
                self.time_series(client)

        assert client.variant_requests <= client_module.MAX_VARIANT_REQUESTS_PER_POLL

    def test_the_budget_reopens_on_the_next_poll(self):
        transport = RecordingTransport(
            {client_module.TIME_SERIES_STATS_PATH: self.refusing(lambda _params: False, dict)}
        )
        client = client_with(transport)
        client.begin_poll()
        with pytest.raises(CohesityApiError):
            self.time_series(client)
        spent = client.variant_requests

        client.begin_poll()

        assert client.variant_requests == 0
        assert spent > 0

    def test_every_shape_failing_re_raises_the_original_failure(self):
        transport = RecordingTransport(
            {client_module.TIME_SERIES_STATS_PATH: self.refusing(lambda _params: False, dict)}
        )
        client = client_with(transport)

        with pytest.raises(CohesityApiError):
            self.time_series(client)

        fact = [item for item in client.take_diagnostics() if item["kind"] == "param_variant"][0]
        assert fact["resolved"] is False
        assert fact["status"] == 500
        assert [attempt.split("=")[0] for attempt in fact["attempts"]][:2] == [
            "metricNames-repeated",
            "no-rollupIntervalSecs",
        ]

    def test_the_winning_shape_is_named_in_a_diagnostic(self):
        transport = RecordingTransport(
            {
                client_module.TIME_SERIES_STATS_PATH: self.refusing(
                    lambda params: "rollupIntervalSecs" not in params,
                    lambda params: self.series_body(params["metricNames"]),
                )
            }
        )
        client = client_with(transport)

        self.time_series(client)

        fact = [item for item in client.take_diagnostics() if item["kind"] == "param_variant"][0]
        assert fact["resolved"] is True
        assert fact["variant"] == "no-rollupIntervalSecs"
        assert fact["requests"] == 2

    def test_repeated_values_serialise_as_repeated_parameters(self):
        # The one place the comma-joined rule is bypassed, and only through this type.
        assert (
            encode_params({"metricNames": Repeated(("kReadIos", "kWriteIos"))})
            == "metricNames=kReadIos&metricNames=kWriteIos"
        )


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
        # The run-list fixture is only reached when runs/summary has failed, so the fallback
        # is pointed at directly rather than left as the one shipped body nothing exercises.
        client._runs_source = client_module.RUNS_SOURCE_LIST
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


class TestProtectedObjectProbe:
    """Ticket 16's one question: is objects[].object.uuid the VMware BIOS UUID?

    Everything asserted here is about the probe staying a question. It costs one request for
    the life of the client, it cannot run twice, and no failure of it is allowed to reach the
    run collection that v0.1.7 finally got working.
    """

    def group(self, group_id, *, status="Succeeded", end=100, paused=False, deleted=False):
        return domain.ProtectionGroup(
            id=group_id,
            name=f"job-{group_id}",
            environment="kVMware",
            last_run_status=status,
            last_run_end_time_usecs=end,
            is_paused=paused,
            is_deleted=deleted,
        )

    def object_details_body(self):
        return {
            "runs": [
                {
                    "id": "r-1",
                    "environment": "kVMware",
                    "objects": [
                        {
                            "object": {
                                "id": 4001,
                                "name": "PLACEHOLDER-VM-A",
                                "uuid": "00112233-4455-6677-8899-aabbccddeeff",
                                "vCenterSummary": {"type": "kVCenter"},
                            }
                        }
                    ],
                }
            ]
        }

    def probe_transport(self, groups, body=None):
        transport = RecordingTransport(
            {group_runs_path(item.id): body or self.object_details_body() for item in groups}
        )
        transport.bodies[client_module.PROTECTION_GROUPS_PATH] = {"protectionGroups": []}
        return transport

    def details_calls(self, transport):
        return [
            (path, params)
            for path, params in transport.calls
            if params.get("includeObjectDetails")
        ]

    def test_the_probe_asks_one_group_for_one_run_with_object_details(self):
        groups = [self.group("g-1")]
        transport = self.probe_transport(groups)

        client_with(transport).probe_protected_objects(groups)

        assert self.details_calls(transport) == [
            (
                group_runs_path("g-1"),
                {
                    "numRuns": client_module.OBJECT_DETAILS_RUNS,
                    "includeObjectDetails": True,
                },
            )
        ]

    def test_the_probe_happens_once_per_client_not_once_per_poll(self):
        # The answer is a property of the cluster's data model, not of the poll, and this
        # cluster is unhealthy enough that a per-poll extra request is a real cost.
        groups = [self.group("g-1")]
        transport = self.probe_transport(groups)
        client = client_with(transport)

        for _ in range(5):
            client.probe_protected_objects(groups)

        assert len(self.details_calls(transport)) == 1

    def test_a_second_client_asks_again(self):
        groups = [self.group("g-1")]
        transport = self.probe_transport(groups)

        client_with(transport).probe_protected_objects(groups)
        client_with(transport).probe_protected_objects(groups)

        assert len(self.details_calls(transport)) == 2

    def test_the_probe_reports_the_fields_the_vmware_block_and_the_verdict(self):
        groups = [self.group("g-1")]
        client = client_with(self.probe_transport(groups))

        client.probe_protected_objects(groups)

        fact = [item for item in client.take_diagnostics() if item["kind"] == "protected_objects"][0]
        assert fact["environment"] == "kVMware"
        assert fact["objects"] == 1
        assert fact["fields"] == ["id", "name", "uuid", "vCenterSummary"]
        assert fact["vmwareKey"] == "vCenterSummary"
        assert fact["vmwareFields"] == ["type"]
        assert fact["uuids"] == [
            {
                "value": "00112233-4455-6677-8899-aabbccddeeff",
                "verdict": domain.UUID_VERDICT_CANONICAL,
            }
        ]

    def test_a_probe_that_fails_is_recorded_and_never_retried(self):
        groups = [self.group("g-1")]
        transport = self.probe_transport(groups)
        transport.raise_for[group_runs_path("g-1")] = api_error(403, "no privilege")
        client = client_with(transport)

        client.probe_protected_objects(groups)
        client.probe_protected_objects(groups)

        fact = [item for item in client.take_diagnostics() if item["kind"] == "protected_objects"][0]
        assert fact["error"] == "CohesityApiError"
        assert fact["status"] == 403
        assert fact["objects"] == 0
        assert len(self.details_calls(transport)) == 1

    def test_a_probe_that_fails_does_not_break_run_collection(self):
        # The whole safety property. Run collection is the thing that works; an enrichment
        # experiment that could take it down would not be worth the answer.
        groups = [self.group("g-1", end=1)]
        transport = self.probe_transport(groups, body={"runs": []})
        transport.raise_for[group_runs_path("g-1")] = read_timeout()
        transport.bodies[client_module.PROTECTION_RUNS_PATH] = {
            "protectionRunsSummary": [
                {"id": "r-9", "protectionGroupId": "g-1", "status": "Succeeded"}
            ]
        }
        client = client_with(transport)

        client.probe_protected_objects(groups)
        runs = client.new_protection_runs(groups=groups)

        assert [run.id for run in runs] == ["r-9"]

    def test_an_unexpected_failure_is_caught_as_readily_as_a_cluster_one(self):
        # error_facts exists precisely because a parser KeyError has no status and no path,
        # and that is the failure the old logger.exception call used to swallow.
        groups = [self.group("g-1")]
        transport = self.probe_transport(groups)
        transport.raise_for[group_runs_path("g-1")] = KeyError("nope")
        client = client_with(transport)

        client.probe_protected_objects(groups)

        fact = [item for item in client.take_diagnostics() if item["kind"] == "protected_objects"][0]
        assert fact["error"] == "KeyError"

    def test_the_group_asked_is_one_whose_last_run_succeeded_most_recently(self):
        # A failed run can carry no objects at all, which would answer "this cluster publishes
        # no identifiers" when nothing was actually asked.
        groups = [
            self.group("g-old", end=10),
            self.group("g-failed", status="Failed", end=9_000),
            self.group("g-recent", end=5_000),
        ]
        transport = self.probe_transport(groups)

        client_with(transport).probe_protected_objects(groups)

        assert self.details_calls(transport)[0][0] == group_runs_path("g-recent")

    def test_a_paused_group_loses_to_a_running_one_that_succeeded_less_recently(self):
        groups = [
            self.group("g-paused", end=9_000, paused=True),
            self.group("g-live", end=5_000),
        ]
        transport = self.probe_transport(groups)

        client_with(transport).probe_protected_objects(groups)

        assert self.details_calls(transport)[0][0] == group_runs_path("g-live")

    def test_a_deleted_group_is_never_asked(self):
        # Its objects describe an estate that is gone, so a uuid from one proves nothing.
        groups = [self.group("g-gone", end=9_000, deleted=True), self.group("g-here", end=1)]
        transport = self.probe_transport(groups)

        client_with(transport).probe_protected_objects(groups)

        assert self.details_calls(transport)[0][0] == group_runs_path("g-here")

    def test_a_cluster_with_no_askable_group_reports_that_rather_than_calling(self):
        transport = self.probe_transport([])
        client = client_with(transport)

        client.probe_protected_objects([])

        fact = [item for item in client.take_diagnostics() if item["kind"] == "protected_objects"][0]
        assert fact["objects"] == 0
        assert "error" not in fact
        assert self.details_calls(transport) == []

    def test_the_probe_fetches_the_groups_itself_when_it_is_not_handed_any(self):
        groups = [self.group("g-1")]
        transport = self.probe_transport(groups)
        transport.bodies[client_module.PROTECTION_GROUPS_PATH] = {
            "protectionGroups": [
                {"id": "g-1", "name": "job", "environment": "kVMware",
                 "lastRun": {"localBackupInfo": {"status": "Succeeded", "endTimeUsecs": 5}}}
            ]
        }

        client_with(transport).probe_protected_objects()

        assert self.details_calls(transport)[0][0] == group_runs_path("g-1")

    def test_replay_mode_does_not_probe(self):
        # Replay resolves a file per request path; the per-group runs path is keyed by a group
        # id that exists in only one fixture set, so a missing-fixture ERROR on every replayed
        # poll would be a worse answer than no answer. The e2e fake cluster covers this path.
        client = CohesityClient(config(fixture_dir=str(FIXTURE_DIR)))

        client.probe_protected_objects([self.group("g-9001")])

        assert [item for item in client.take_diagnostics() if item["kind"] == "protected_objects"] == []


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


class TestUnknownRunStatusDiagnostic:
    """"status: unknown" says a hole exists; only the field list says where to look.

    Guessing at a schema is how the hole got there, so the extension reports what the cluster
    actually sent - key names, one level deep, no values - exactly as it did for the storage
    domain stats fields.
    """

    def body(self, *runs):
        return {"runs": list(runs)}

    def client_for(self, body):
        transport = RecordingTransport({client_module.PROTECTION_RUNS_LIST_PATH: body})
        transport.bodies[client_module.CLUSTER_STATUS_PATH] = status_body()
        transport.raise_for[client_module.PROTECTION_RUNS_PATH] = CohesityApiError("no summary")
        return client_with(transport)

    def facts(self, client):
        return [
            fact for fact in client.take_diagnostics() if fact["kind"] == "run_status_unknown"
        ]

    def test_a_classifiable_run_produces_no_diagnostic(self):
        client = self.client_for(
            self.body({"id": "r-1", "protectionGroupId": "g-1", "status": "Succeeded"})
        )

        client.protection_runs()

        assert self.facts(client) == []

    def test_an_unclassifiable_run_reports_the_key_names_it_did_carry(self):
        client = self.client_for(
            self.body(
                {
                    "id": "r-1",
                    "protectionGroupId": "g-1",
                    "protectionGroupName": "Archive-only",
                    "archivalInfo": {"someUndocumentedBlock": {"state": "done"}},
                }
            )
        )

        client.protection_runs()
        fact = self.facts(client)[0]

        assert fact["runs"] == 1
        assert "archivalInfo" in fact["fields"]
        assert "archivalInfo.someUndocumentedBlock" in fact["fields"]
        # Names, never values - the same rule every other diagnostic here follows.
        assert "Archive-only" not in fact["fields"]

    def test_it_is_reported_once_per_client_not_once_per_poll(self):
        # The answer is a property of this cluster's run shapes. The same field list every
        # five minutes is noise that teaches nobody anything new.
        client = self.client_for(self.body({"id": "r-1", "protectionGroupId": "g-1"}))

        for _ in range(4):
            client.protection_runs()
            client.take_diagnostics()

        client = self.client_for(self.body({"id": "r-1", "protectionGroupId": "g-1"}))
        client.protection_runs()

        assert len(self.facts(client)) == 1

    def test_the_record_names_the_blocks_that_were_searched(self):
        client = self.client_for(self.body({"id": "r-1", "protectionGroupId": "g-1"}))
        client.protection_runs()
        event = metrics.diagnostic_log_events("1", "prod", "7.4", self.facts(client))[0]

        assert event["severity"] == metrics.SEVERITY_WARN
        assert "localBackupInfo" in event["content"]
        assert 'status="unknown"' in event["content"]


class TestHostLinkFanout:
    """Ticket 16's collection, and the bounds that keep it from being a cardinality bomb.

    Almost every test here is about what this does NOT do: it does not run unless asked, it
    does not ask a non-VMware group, it does not spend more than a handful of requests per
    poll, and it does not publish more than its cap of series. The one thing it must do
    positively is say so - loudly - when the objects carry no BIOS UUID, because that retires
    the whole design on that cluster and would otherwise look identical to "no edges appeared".
    """

    def group(self, group_id, *, environment="kVMware", end=100, deleted=False):
        return domain.ProtectionGroup(
            id=group_id,
            name=f"job-{group_id}",
            environment=environment,
            last_run_status="Succeeded",
            last_run_end_time_usecs=end,
            is_deleted=deleted,
        )

    def run_body(self, *uuids, instance=""):
        """Objects carrying their BIOS uuid where a VMware object carries it.

        Under `vCenterSummary`, which is the block the 7.3.2 reference documents and the one
        the v0.1.8 probe saw on the customer's cluster - NOT under `object.uuid`, which is
        vCenter's instanceUuid and is what v0.1.9 wrongly emitted.
        """
        objects = [
            {
                "object": {
                    "id": 4000 + n,
                    "vCenterSummary": {"biosUuid": uuid, "instanceUuid": instance},
                }
            }
            for n, uuid in enumerate(uuids)
        ]
        return {"runs": [{"id": "r-1", "environment": "kVMware", "objects": objects}]}

    def legacy_run_body(self, *uuids):
        """What v0.1.9 read: an object carrying ONLY `uuid`, which is the instanceUuid."""
        objects = [{"object": {"id": 4000 + n, "uuid": uuid}} for n, uuid in enumerate(uuids)]
        return {"runs": [{"id": "r-1", "environment": "kVMware", "objects": objects}]}

    def uuid_for(self, seed: str) -> str:
        return f"00112233-4455-6677-7303-{sum(map(ord, seed)):012d}"

    def transport_for(self, groups, body=None):
        transport = RecordingTransport(
            {
                group_runs_path(item.id): (
                    body if body is not None else self.run_body(self.uuid_for(item.id))
                )
                for item in groups
            }
        )
        transport.bodies[client_module.CLUSTER_STATUS_PATH] = status_body()
        transport.bodies[client_module.PROTECTION_GROUPS_PATH] = {"protectionGroups": []}
        return transport

    def detail_calls(self, transport):
        return [path for path, params in transport.calls if params.get("includeObjectDetails")]

    def fact(self, client):
        facts = [item for item in client.take_diagnostics() if item["kind"] == "host_link"]
        assert len(facts) == 1, facts
        return facts[0]

    def test_it_does_nothing_at_all_unless_switched_on(self):
        # Off by default, and the only collection that is: one series per protected VM is
        # exactly the cardinality ticket 04 ruled out of v1.
        groups = [self.group("g-1")]
        transport = self.transport_for(groups)

        links = client_with(transport, collect_host_link=False).protected_object_links(groups)

        assert links == []
        assert self.detail_calls(transport) == []

    def test_only_vmware_groups_are_asked(self):
        """SQL objects carry no uuid field AT ALL - the v0.1.8 probe established that.

        So a kSQL group can only ever spend a request to learn nothing, and on the customer's
        59-group estate (18 SQL, 9 Oracle) that is most of the fan-out.
        """
        groups = [
            self.group("g-sql", environment="kSQL"),
            self.group("g-vmware"),
            self.group("g-physical", environment="kPhysical"),
        ]
        transport = self.transport_for(groups)

        client_with(transport, collect_host_link=True).protected_object_links(groups)

        assert self.detail_calls(transport) == [group_runs_path("g-vmware")]

    def test_a_deleted_group_is_not_asked(self):
        # Its objects describe an estate that is gone, and an edge from a job that no longer
        # exists is worse than no edge.
        groups = [self.group("g-gone", deleted=True), self.group("g-live")]
        transport = self.transport_for(groups)

        client_with(transport, collect_host_link=True).protected_object_links(groups)

        assert self.detail_calls(transport) == [group_runs_path("g-live")]

    def test_one_poll_cannot_spend_more_than_the_request_bound(self):
        groups = [self.group(f"g-{n}", end=100 - n) for n in range(40)]
        transport = self.transport_for(groups)

        client_with(transport, collect_host_link=True).protected_object_links(groups)

        assert len(self.detail_calls(transport)) == client_module.HOST_LINK_GROUPS_PER_POLL

    def test_turning_the_run_fanout_dial_down_turns_this_down_too(self):
        """Lowering maxRunFanoutGroups means "spend fewer requests on this cluster".

        A second fan-out that ignored the operator's one dial would make it a decoration.
        """
        groups = [self.group(f"g-{n}", end=100 - n) for n in range(40)]
        transport = self.transport_for(groups)

        client_with(
            transport, collect_host_link=True, max_run_fanout_groups=2
        ).protected_object_links(groups)

        assert len(self.detail_calls(transport)) == 2

    def test_the_bound_is_a_rotation_so_no_group_is_starved(self):
        # Same reasoning as the runs fan-out: a cap applied by truncation makes the groups past
        # it invisible forever, which is how v0.1.6 could be working and still report nothing.
        groups = [self.group(f"g-{n}", end=100 - n) for n in range(12)]
        transport = self.transport_for(groups)
        client = client_with(transport, collect_host_link=True)

        for _ in range(3):
            client.protected_object_links(groups)

        assert len(set(self.detail_calls(transport))) == 12

    def test_one_run_per_group_is_enough_to_read_current_membership(self):
        groups = [self.group("g-1")]
        transport = self.transport_for(groups)

        client_with(transport, collect_host_link=True).protected_object_links(groups)

        params = [p for path, p in transport.calls if path == group_runs_path("g-1")][0]
        assert params == {"numRuns": client_module.HOST_LINK_RUNS, "includeObjectDetails": True}

    def test_a_link_carries_the_namespaced_group_id_the_topology_already_uses(self):
        # A raw group id here resolves to a second, empty copy of the entity rather than to
        # the one every other metric feeds.
        groups = [self.group("g-1")]
        transport = self.transport_for(groups)

        links = client_with(transport, collect_host_link=True).protected_object_links(groups)

        assert links[0].protection_group_id == f"{FIXTURE_CLUSTER_ID}_g-1"

    def test_uuids_are_normalised_before_they_leave_the_client(self):
        groups = [self.group("g-1")]
        transport = self.transport_for(groups, self.run_body("00112233445566778899AABBCCDDEEFF"))

        links = client_with(transport, collect_host_link=True).protected_object_links(groups)

        assert links[0].uuid == "00112233-4455-6677-8899-aabbccddeeff"

    def test_an_object_with_no_usable_uuid_is_skipped_not_emitted_partially(self):
        groups = [self.group("g-1")]
        transport = self.transport_for(
            groups,
            self.run_body("00112233-4455-6677-8899-aabbccddeeff", "1234567890123456", ""),
        )

        links = client_with(transport, collect_host_link=True).protected_object_links(groups)

        assert [link.uuid for link in links] == ["00112233-4455-6677-8899-aabbccddeeff"]

    def test_the_object_cap_truncates_and_says_so(self):
        """Partial data that cannot be silent.

        Half a lookup table is indistinguishable from half an estate going unprotected, and
        nothing else in the product would say which it was.
        """
        groups = [self.group("g-1")]
        uuids = [f"00112233-4455-6677-7303-{n:012d}" for n in range(20)]
        transport = self.transport_for(groups, self.run_body(*uuids))
        client = client_with(transport, collect_host_link=True, max_host_link_objects=5)

        links = client.protected_object_links(groups)
        fact = self.fact(client)

        assert len(links) == 5
        assert fact["capped"] is True
        assert fact["cap"] == 5

    def test_the_cap_is_reported_as_a_warning_naming_what_was_left_out(self):
        groups = [self.group("g-1")]
        uuids = [f"00112233-4455-6677-7303-{n:012d}" for n in range(20)]
        transport = self.transport_for(groups, self.run_body(*uuids))
        client = client_with(transport, collect_host_link=True, max_host_link_objects=5)

        client.protected_object_links(groups)
        event = metrics.diagnostic_log_events("1", "prod", "7.4", [self.fact(client)])[0]

        assert event["severity"] == metrics.SEVERITY_WARN
        assert "PARTIAL" in event["content"]

    def test_vmware_objects_with_no_uuid_are_reported_loudly_and_nothing_is_emitted(self):
        """The answer that retires the design, and the one the ticket says must be loud.

        No fallback join on object names is attempted - it would draw confident wrong edges,
        which is worse than no edges.
        """
        groups = [self.group("g-1")]
        transport = self.transport_for(groups, self.run_body("1234567890123456", ""))
        client = client_with(transport, collect_host_link=True)

        links = client.protected_object_links(groups)
        fact = self.fact(client)
        event = metrics.diagnostic_log_events("1", "prod", "7.4", [fact])[0]

        assert links == []
        assert fact["objects_seen"] == 2
        assert fact["objects_linked"] == 0
        assert set(fact["verdicts"]) == {domain.UUID_VERDICT_NUMERIC, domain.UUID_VERDICT_MISSING}
        assert event["severity"] == metrics.SEVERITY_ERROR
        # A FIELD NAME answer, and it has to say so in those words - the other zero-edge
        # outcome ("every object had a BIOS uuid, no host matched one") is a coverage answer
        # and needs a completely different next step.
        assert "FIELD NAME" in event["content"]

    def test_a_cluster_with_no_vmware_group_says_so_and_asks_nothing(self):
        groups = [self.group("g-sql", environment="kSQL")]
        transport = self.transport_for(groups)
        client = client_with(transport, collect_host_link=True)

        links = client.protected_object_links(groups)
        fact = self.fact(client)

        assert links == []
        assert fact["groups_vmware"] == 0
        assert self.detail_calls(transport) == []

    def test_one_group_refusing_does_not_cost_the_others(self):
        groups = [self.group("g-1", end=200), self.group("g-2", end=100)]
        transport = self.transport_for(groups)
        transport.raise_for[group_runs_path("g-1")] = CohesityApiError("g-1 refused")
        client = client_with(transport, collect_host_link=True)

        links = client.protected_object_links(groups)

        assert len(links) == 1
        assert self.fact(client)["groups_errored"] == 1

    def test_the_instance_uuid_is_carried_beside_the_bios_uuid_not_instead_of_it(self):
        groups = [self.group("g-1")]
        instance = "50000000-1111-4222-8333-444444444401"
        body = self.run_body("42000000-1111-4222-8333-444444444401", instance=instance)
        transport = self.transport_for(groups, body)

        links = client_with(transport, collect_host_link=True).protected_object_links(groups)

        assert links[0].uuid == "42000000-1111-4222-8333-444444444401"
        assert links[0].instance_uuid == instance

    def test_an_object_carrying_only_object_uuid_yields_nothing(self):
        """v0.1.9's bug, asserted as an absence.

        `object.uuid` is vCenter's instanceUuid. Emitting it produced 200 series on the
        customer's cluster that matched zero hosts and looked, from every diagnostic and every
        chart, exactly like a working feature.
        """
        groups = [self.group("g-1")]
        body = self.legacy_run_body("50000000-1111-4222-8333-444444444401")
        transport = self.transport_for(groups, body)
        client = client_with(transport, collect_host_link=True)

        links = client.protected_object_links(groups)

        assert links == []
        assert self.fact(client)["candidates"] == ["uuid"]

    def test_the_diagnostic_names_the_field_that_won_and_every_candidate_present(self):
        # The same trick that settled the storage-domain field names: the cluster is asked
        # what it publishes, and the answer leaves as NAMES on the diagnostics channel.
        groups = [self.group("g-1")]
        body = self.run_body(
            "42000000-1111-4222-8333-444444444401",
            instance="50000000-0000-0000-0000-000000000003",
        )
        transport = self.transport_for(groups, body)
        client = client_with(transport, collect_host_link=True)

        client.protected_object_links(groups)
        fact = self.fact(client)
        event = metrics.diagnostic_log_events("1", "prod", "7.4", [fact])[0]

        assert fact["bios_field"] == "vCenterSummary.biosUuid"
        assert fact["candidates"] == [
            "vCenterSummary.biosUuid",
            "vCenterSummary.instanceUuid",
        ]
        assert event["cohesity.host_link_bios_field"] == "vCenterSummary.biosUuid"
        assert "vCenterSummary.biosUuid" in event["cohesity.host_link_uuid_candidates"]

    def test_bios_uuids_are_counted_before_the_cap_so_coverage_stays_readable(self):
        """"200 objects, 200 BIOS uuids" and "200 objects, 0 BIOS uuids" are different answers.

        The first is a coverage question about which VMs run a OneAgent; the second is a
        field-name question about this cluster's API. Counting the BIOS uuids after the
        per-poll cap would truncate the first into looking like the second.
        """
        groups = [self.group("g-1")]
        uuids = [f"42000000-0000-0000-0000-{n:012d}" for n in range(20)]
        transport = self.transport_for(groups, self.run_body(*uuids))
        client = client_with(transport, collect_host_link=True, max_host_link_objects=5)

        client.protected_object_links(groups)
        fact = self.fact(client)

        assert fact["objects_seen"] == 20
        assert fact["objects_bios"] == 20
        assert fact["objects_linked"] == 5

    def test_the_healthy_record_says_which_answer_a_zero_edge_join_would_be(self):
        groups = [self.group("g-1")]
        transport = self.transport_for(groups, self.run_body("42000000-1111-4222-8333-444444444401"))
        client = client_with(transport, collect_host_link=True)

        client.protected_object_links(groups)
        event = metrics.diagnostic_log_events("1", "prod", "7.4", [self.fact(client)])[0]

        assert event["severity"] == metrics.SEVERITY_INFO
        assert "COVERAGE" in event["content"]
        assert "vCenterSummary.biosUuid" in event["content"]

    def test_uuids_shaped_like_an_instance_uuid_announce_themselves_once(self):
        """The shape sanity check, and the reason it has its own marker.

        A BIOS uuid starts 42 or 564d; vCenter's instanceUuid starts 50. If the field this
        cluster calls a BIOS uuid hands back 50s, the outcome record would still read a
        perfectly healthy "linked" - which is precisely how v0.1.9 shipped.
        """
        groups = [self.group("g-1")]
        body = self.run_body("50000000-1111-4222-8333-444444444401")
        transport = self.transport_for(groups, body)
        client = client_with(transport, collect_host_link=True)

        client.protected_object_links(groups)
        facts = [item for item in client.take_diagnostics() if item["kind"] == "host_link"]
        suspect = [item for item in facts if item.get("suspect")]
        event = metrics.diagnostic_log_events("1", "prod", "7.4", suspect)[0]

        assert len(suspect) == 1
        assert suspect[0]["instance_shaped"] == 1
        # ERROR because every one of them is 50-shaped: that is a wrong field name, not chance.
        assert event["severity"] == metrics.SEVERITY_ERROR
        assert "instanceUuid" in event["content"]

    def test_a_clean_poll_raises_no_shape_warning_at_all(self):
        groups = [self.group("g-1")]
        transport = self.transport_for(groups, self.run_body("42000000-1111-4222-8333-444444444401"))
        client = client_with(transport, collect_host_link=True)

        client.protected_object_links(groups)

        assert self.fact(client)["instance_shaped"] == 0

    def test_an_unexpected_failure_becomes_a_diagnostic_rather_than_an_exception(self):
        # An enrichment that could break the run collection would not be worth the edge.
        groups = [self.group("g-1")]
        transport = self.transport_for(groups)
        transport.raise_for[client_module.CLUSTER_STATUS_PATH] = RuntimeError("boom")
        client = client_with(transport, collect_host_link=True)

        assert client.protected_object_links(groups) == []
        assert self.fact(client)["error"]

    def test_replay_mode_is_skipped(self):
        # A per-group runs path resolves to a fixture key built from the group id, and only one
        # fixture set could carry it. The e2e fake cluster speaks real HTTP and does cover it.
        client = CohesityClient(config(fixture_dir=str(FIXTURE_DIR), collect_host_link=True))

        assert client.protected_object_links([self.group("g-9001")]) == []
