"""From a recorded Cohesity body to the samples that reach report_metric.

This is the whole metric contract exercised end to end - the same path the extension takes in
replay mode - with no EEC, no tenant and no SDK import. The client runs against the shipped
fixtures, so a change to a key, a unit assumption or a dimension shows up here rather than on a
tenant three days later.

Two properties are asserted repeatedly because both have bitten this domain before:

*Silence is not zero.* A field the cluster did not send must produce no sample at all. A zero
capacity reads as an outage and a zero last-success age reads as "just backed up".

*Identity is the id.* Every sample carries the cluster id, and every non-cluster sample carries
its own namespaced id. Names ride along and are never part of an identity.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cohesity_storage import domain, errors, metrics
from cohesity_storage.client import CLUSTER_STATS_CALLS, VIEW_METRICS, CohesityClient
from cohesity_storage.config import ClusterConfig
from cohesity_storage.errors import CohesityApiError, CohesityAuthError, annotate, error_facts

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
CLUSTER_ID = "1234567890123456"
CLUSTER_NAME = "cohesity-demo-01"

# A fixed "now" well after the fixture timestamps, so age assertions are deterministic.
NOW_USECS = 1_789_600_000_000_000


def replay_client() -> CohesityClient:
    return CohesityClient(
        ClusterConfig(
            name="cohesity-prod",
            host="10.20.30.40",
            api_key="demo-key",
            fixture_dir=str(FIXTURE_DIR),
        )
    )


@pytest.fixture
def client() -> CohesityClient:
    return replay_client()


def replay_samples(client: CohesityClient) -> list[metrics.Sample]:
    """Every sample one full poll produces - the same sections, in the same order, as query().

    A module-level function rather than a method so the wire-format and manifest tests check
    exactly this set instead of a second, drifting copy of it.
    """
    samples = list(
        metrics.cluster_storage_samples(CLUSTER_ID, CLUSTER_NAME, client.cluster_storage())
    )
    for call in CLUSTER_STATS_CALLS:
        samples.extend(
            metrics.cluster_time_series_samples(
                CLUSTER_ID,
                CLUSTER_NAME,
                call["schemaName"],
                client.cluster_time_series(call),
            )
        )
    for view_metric in VIEW_METRICS:
        samples.extend(
            metrics.view_samples(
                CLUSTER_ID, CLUSTER_NAME, view_metric, client.view_stats(view_metric)
            )
        )
    samples.extend(
        metrics.storage_domain_samples(CLUSTER_ID, CLUSTER_NAME, client.storage_domains())
    )
    groups = client.protection_groups()
    samples.extend(metrics.protection_group_samples(CLUSTER_ID, CLUSTER_NAME, groups, NOW_USECS))
    samples.extend(
        metrics.protection_run_samples(
            CLUSTER_ID, CLUSTER_NAME, client.new_protection_runs(), groups
        )
    )
    return samples


def by_key(samples: list[metrics.Sample], key: str) -> list[metrics.Sample]:
    return [sample for sample in samples if sample.key == key]


def one(samples: list[metrics.Sample], key: str, **dimensions) -> metrics.Sample:
    """The single sample with this key and these dimension values."""
    matches = [
        sample
        for sample in by_key(samples, key)
        if all(sample.dimensions.get(name) == value for name, value in dimensions.items())
    ]
    assert len(matches) == 1, f"expected one {key} {dimensions}, got {len(matches)}"
    return matches[0]


class TestClusterCapacity:
    def test_the_seven_scalars_become_seven_samples(self, client):
        storage = client.cluster_storage()

        samples = metrics.cluster_storage_samples(CLUSTER_ID, CLUSTER_NAME, storage)

        assert len(samples) == 7

    def test_capacity_keys_match_the_contract(self, client):
        samples = metrics.cluster_storage_samples(
            CLUSTER_ID, CLUSTER_NAME, client.cluster_storage()
        )

        assert one(samples, "cohesity.cluster.capacity.total").value == 105553116266496
        assert one(samples, "cohesity.cluster.capacity.used").value == 41234567890123
        assert one(samples, "cohesity.cluster.capacity.available").value == 64318548376373

    def test_usage_is_one_key_split_by_service(self, client):
        samples = metrics.cluster_storage_samples(
            CLUSTER_ID, CLUSTER_NAME, client.cluster_storage()
        )

        logical = by_key(samples, "cohesity.cluster.usage.logical")
        assert {sample.dimensions[metrics.DIM_SERVICE] for sample in logical} == {
            "dataprotect",
            "fileservices",
        }
        assert one(samples, "cohesity.cluster.usage.physical", service="fileservices").value == (
            4727272931328
        )

    def test_a_missing_field_produces_no_sample_rather_than_a_zero(self):
        # Every field in this response is nullable. Reporting 0 would read as an outage.
        storage = domain.ClusterStorage(total_capacity_bytes=10, local_usage_bytes=None)

        samples = metrics.cluster_storage_samples(CLUSTER_ID, CLUSTER_NAME, storage)

        assert [sample.key for sample in samples] == ["cohesity.cluster.capacity.total"]

    def test_every_sample_carries_the_cluster(self, client):
        samples = metrics.cluster_storage_samples(
            CLUSTER_ID, CLUSTER_NAME, client.cluster_storage()
        )

        for sample in samples:
            assert sample.dimensions[metrics.DIM_CLUSTER_ID] == CLUSTER_ID
            assert sample.dimensions[metrics.DIM_CLUSTER_NAME] == CLUSTER_NAME


class TestClusterTimeSeries:
    def collect(self, client) -> list[metrics.Sample]:
        samples = []
        for call in CLUSTER_STATS_CALLS:
            series = client.cluster_time_series(call)
            samples.extend(
                metrics.cluster_time_series_samples(
                    CLUSTER_ID, CLUSTER_NAME, call["schemaName"], series
                )
            )
        return samples

    def test_every_polled_series_is_mapped(self, client):
        samples = self.collect(client)

        # 2 sentry + 4 bridge logical + 1 bridge = seven series across three calls.
        assert len(samples) == 7

    def test_cpu_and_memory_come_from_the_sentry_schema(self, client):
        samples = self.collect(client)

        assert by_key(samples, "cohesity.cluster.cpu.usage")
        assert by_key(samples, "cohesity.cluster.memory.usage")

    def test_iops_and_latency_are_split_by_operation(self, client):
        samples = self.collect(client)

        for key in ("cohesity.cluster.io.iops", "cohesity.cluster.io.latency"):
            operations = {sample.dimensions[metrics.DIM_OPERATION] for sample in by_key(samples, key)}
            assert operations == {"read", "write"}

    def test_latency_is_reported_in_microseconds_unconverted(self, client):
        # Flash-backed Cohesity serves sub-millisecond. Dividing by 1000 here would round the
        # whole signal away, which is exactly what ticket 06 refused.
        series = client.cluster_time_series(CLUSTER_STATS_CALLS[1])
        raw = series["kReadLatencyUsecs"].latest_value()

        sample = one(
            metrics.cluster_time_series_samples(
                CLUSTER_ID, CLUSTER_NAME, "kBridgeClusterLogicalStats", series
            ),
            "cohesity.cluster.io.latency",
            operation="read",
        )

        assert sample.value == raw

    def test_the_latest_non_null_point_is_used(self):
        series = {
            "kCpuUsagePct": domain.TimeSeriesMetric(
                metric_name="kCpuUsagePct",
                value_type="kDouble",
                data_points=(
                    domain.DataPoint(1, 10.0),
                    domain.DataPoint(2, 42.0),
                    # The tail is the most likely to be null - the bucket is flushed but not
                    # yet filled - so the scan has to go backwards past it.
                    domain.DataPoint(3, None),
                ),
            )
        }

        sample = one(
            metrics.cluster_time_series_samples(
                CLUSTER_ID, CLUSTER_NAME, "kSentryClusterStats", series
            ),
            "cohesity.cluster.cpu.usage",
        )

        assert sample.value == 42.0

    def test_an_empty_series_produces_nothing(self):
        # All-empty is the signature of a wrong entityId, and it arrives with no error. Silence
        # here is what lets the caller warn instead of ingesting invented zeros.
        series = {
            "kCpuUsagePct": domain.TimeSeriesMetric("kCpuUsagePct", "kDouble", ()),
        }

        assert not metrics.cluster_time_series_samples(
            CLUSTER_ID, CLUSTER_NAME, "kSentryClusterStats", series
        )

    def test_an_unmapped_metric_name_is_ignored(self):
        series = {
            "kSomethingNew": domain.TimeSeriesMetric(
                "kSomethingNew", "kInt64", (domain.DataPoint(1, 5),)
            )
        }

        assert not metrics.cluster_time_series_samples(
            CLUSTER_ID, CLUSTER_NAME, "kSentryClusterStats", series
        )

    def test_a_string_valued_point_is_not_reported_as_a_metric(self):
        series = {
            "kCpuUsagePct": domain.TimeSeriesMetric(
                "kCpuUsagePct", "kString", (domain.DataPoint(1, "kHealthy"),)
            )
        }

        assert not metrics.cluster_time_series_samples(
            CLUSTER_ID, CLUSTER_NAME, "kSentryClusterStats", series
        )


class TestViews:
    def collect(self, client) -> list[metrics.Sample]:
        samples = []
        for view_metric in VIEW_METRICS:
            samples.extend(
                metrics.view_samples(
                    CLUSTER_ID, CLUSTER_NAME, view_metric, client.view_stats(view_metric)
                )
            )
        return samples

    def test_views_report_under_the_cluster_prefix(self, client):
        samples = self.collect(client)

        assert samples
        assert {sample.key for sample in samples} == {"cohesity.cluster.view.throughput"}

    def test_read_and_write_are_one_key_split_by_operation(self, client):
        samples = self.collect(client)

        assert {sample.dimensions[metrics.DIM_OPERATION] for sample in samples} == {"read", "write"}

    def test_view_ids_are_namespaced_by_cluster(self, client):
        samples = self.collect(client)

        # View is not an entity, but the same int64 means a different view on another cluster.
        assert all(
            sample.dimensions[metrics.DIM_VIEW_ID].startswith(f"{CLUSTER_ID}_")
            for sample in samples
        )

    def test_view_name_rides_as_a_dimension(self, client):
        samples = self.collect(client)

        assert "prod-nfs-share" in {
            sample.dimensions.get(metrics.DIM_VIEW_NAME) for sample in samples
        }

    def test_an_unknown_view_metric_maps_to_nothing(self):
        views = [domain.ViewStats(view_id="1", view_name="v", value=5)]

        assert not metrics.view_samples(CLUSTER_ID, CLUSTER_NAME, "kSomethingElse", views)


class TestStorageDomains:
    def test_three_keys_per_domain(self, client):
        domains = client.storage_domains()

        samples = metrics.storage_domain_samples(CLUSTER_ID, CLUSTER_NAME, domains)

        assert len(samples) == len(domains) * 3
        assert {sample.key for sample in samples} == {
            "cohesity.storagedomain.usage.logical",
            "cohesity.storagedomain.usage.physical",
            "cohesity.storagedomain.resiliency.bytes",
        }

    def test_each_domain_carries_a_namespaced_id_and_its_name(self, client):
        samples = metrics.storage_domain_samples(
            CLUSTER_ID, CLUSTER_NAME, client.storage_domains()
        )

        sample = one(
            samples,
            "cohesity.storagedomain.usage.logical",
            **{metrics.DIM_STORAGE_DOMAIN_NAME: "DefaultStorageDomain"},
        )
        assert sample.dimensions[metrics.DIM_STORAGE_DOMAIN_ID] == f"{CLUSTER_ID}_1001"

    def test_a_domain_without_an_id_is_skipped(self):
        # A metric with a name but no id would mint an entity that a rename orphans.
        domains = [domain.StorageDomain(id="", name="Nameless", total_logical_usage_bytes=5)]

        assert not metrics.storage_domain_samples(CLUSTER_ID, CLUSTER_NAME, domains)


class TestProtectionGroups:
    def test_last_success_age_is_reported_only_for_groups_that_succeeded(self, client):
        groups = client.protection_groups()

        samples = metrics.protection_group_samples(CLUSTER_ID, CLUSTER_NAME, groups, NOW_USECS)

        succeeded = [
            group.name
            for group in groups
            if group.last_run_status in ("Succeeded", "SucceededWithWarning")
            and group.last_run_end_time_usecs is not None
        ]
        assert len(samples) == len(succeeded)
        assert {sample.key for sample in samples} == {"cohesity.protectiongroup.last_success.age"}

    def test_a_group_that_last_failed_emits_no_age(self, client):
        groups = client.protection_groups()
        failed = next(group for group in groups if group.last_run_status == "Failed")

        samples = metrics.protection_group_samples(
            CLUSTER_ID, CLUSTER_NAME, [failed], NOW_USECS
        )

        # Emitting 0 would read as "just backed up" for a job that did not succeed.
        assert samples == []

    def test_the_age_is_measured_from_the_last_successful_run(self, client):
        groups = client.protection_groups()
        group = next(group for group in groups if group.name == "Nightly-VMware-Tier1")

        sample = one(
            metrics.protection_group_samples(CLUSTER_ID, CLUSTER_NAME, [group], NOW_USECS),
            "cohesity.protectiongroup.last_success.age",
        )

        assert sample.value == (NOW_USECS - group.last_run_end_time_usecs) / 1000.0

    def test_the_age_gauge_carries_the_explanatory_flags(self, client):
        groups = client.protection_groups()
        group = next(group for group in groups if group.name == "Nightly-VMware-Tier1")

        sample = one(
            metrics.protection_group_samples(CLUSTER_ID, CLUSTER_NAME, [group], NOW_USECS),
            "cohesity.protectiongroup.last_success.age",
        )

        # last_success.age detects the silence; isPaused explains it.
        assert sample.dimensions[metrics.DIM_PAUSED] == "false"
        assert sample.dimensions[metrics.DIM_ACTIVE] == "true"
        assert sample.dimensions[metrics.DIM_STATUS] == "Succeeded"

    def test_the_storage_domain_dimension_is_present_for_the_edge(self, client):
        groups = client.protection_groups()
        group = next(group for group in groups if group.name == "Nightly-VMware-Tier1")

        sample = one(
            metrics.protection_group_samples(CLUSTER_ID, CLUSTER_NAME, [group], NOW_USECS),
            "cohesity.protectiongroup.last_success.age",
        )

        assert sample.dimensions[metrics.DIM_STORAGE_DOMAIN_ID] == f"{CLUSTER_ID}_1001"

    def test_a_group_with_no_storage_domain_still_reports(self):
        # It should exist as an entity, just without the writes_to edge.
        group = domain.ProtectionGroup(
            id="g-1",
            name="Orphan",
            storage_domain_id="",
            last_run_status="Succeeded",
            last_run_end_time_usecs=NOW_USECS - 1_000_000,
        )

        sample = one(
            metrics.protection_group_samples(CLUSTER_ID, CLUSTER_NAME, [group], NOW_USECS),
            "cohesity.protectiongroup.last_success.age",
        )

        assert metrics.DIM_STORAGE_DOMAIN_ID not in sample.dimensions


class TestProtectionRuns:
    def test_run_outcome_is_a_delta_counter_per_new_run(self, client):
        groups = client.protection_groups()
        runs = client.new_protection_runs()

        samples = metrics.protection_run_samples(CLUSTER_ID, CLUSTER_NAME, runs, groups)

        outcomes = by_key(samples, "cohesity.protectiongroup.run.outcome")
        assert len(outcomes) == len(runs)
        assert all(sample.delta and sample.value == 1 for sample in outcomes)

    def test_non_terminal_runs_never_reach_the_metric(self, client):
        # The fixture holds a Running run. Counting it now would attribute the wrong status,
        # because the same run comes back later with its real outcome.
        runs = client.new_protection_runs()

        assert "Running" not in {run.status for run in runs}

    def test_outcomes_are_dimensioned_by_terminal_status(self, client):
        samples = metrics.protection_run_samples(
            CLUSTER_ID, CLUSTER_NAME, client.new_protection_runs(), client.protection_groups()
        )

        statuses = {
            sample.dimensions[metrics.DIM_STATUS]
            for sample in by_key(samples, "cohesity.protectiongroup.run.outcome")
        }
        assert statuses == {"Succeeded", "Failed", "SucceededWithWarning", "Missed"}

    def test_every_counted_outcome_carries_a_status_dimension(self, client):
        """The whole of the customer-tenant bug, asserted at the chokepoint.

        `by:{status}` answered `Succeeded 64` / `None 28`: a quarter of the counted runs had
        no status dimension at all, and nothing anywhere said so. An empty value is dropped by
        wire_dimensions - correctly - so the guarantee has to be that there is never an empty
        value to drop.
        """
        samples = metrics.protection_run_samples(
            CLUSTER_ID, CLUSTER_NAME, client.new_protection_runs(), client.protection_groups()
        )

        for sample in by_key(samples, "cohesity.protectiongroup.run.outcome"):
            assert sample.dimensions.get(metrics.DIM_STATUS), sample.dimensions

    def test_a_run_with_no_status_is_counted_as_unknown_rather_than_undimensioned(self):
        # Counted, and visible. A run that is counted but unclassifiable is most of this
        # metric's value gone; one dimensioned "unknown" is a number somebody can chart, alert
        # on and go and investigate.
        run = domain.ProtectionRun(
            id="r-1", protection_group_id="g-1", protection_group_name="Archive-only", status=""
        )

        sample = by_key(
            metrics.protection_run_samples(CLUSTER_ID, CLUSTER_NAME, [run]),
            "cohesity.protectiongroup.run.outcome",
        )[0]

        assert sample.dimensions[metrics.DIM_STATUS] == "unknown"

    def test_the_unknown_status_survives_the_wire_format(self):
        # The dimension is only real if it reaches report_metric. "" would be dropped here.
        run = domain.ProtectionRun(
            id="r-1", protection_group_id="g-1", protection_group_name="Archive-only", status=""
        )

        sample = by_key(
            metrics.protection_run_samples(CLUSTER_ID, CLUSTER_NAME, [run]),
            "cohesity.protectiongroup.run.outcome",
        )[0]

        assert metrics.wire_dimensions(sample.dimensions)[metrics.DIM_STATUS] == "unknown"

    def test_a_second_poll_counts_nothing_twice(self, client):
        first = client.new_protection_runs()
        second = client.new_protection_runs()

        assert first
        # The poll window deliberately overlaps, so the same completed runs come back. Without
        # the ledger every failure would be counted once per overlap.
        assert metrics.protection_run_samples(CLUSTER_ID, CLUSTER_NAME, second, []) == []

    def test_run_measurements_are_reported_per_run(self, client):
        groups = client.protection_groups()
        runs = client.new_protection_runs()
        succeeded = [run for run in runs if run.id == "r-70001"]

        samples = metrics.protection_run_samples(CLUSTER_ID, CLUSTER_NAME, succeeded, groups)

        assert one(samples, "cohesity.protectiongroup.run.bytes_written").value == 96636764160
        assert one(samples, "cohesity.protectiongroup.run.bytes_logical").value == 1099511627776
        assert one(samples, "cohesity.protectiongroup.run.duration").value == 580000.0

    def test_objects_are_split_by_result(self, client):
        groups = client.protection_groups()
        runs = [run for run in client.new_protection_runs() if run.id == "r-70002"]

        samples = metrics.protection_run_samples(CLUSTER_ID, CLUSTER_NAME, runs, groups)

        assert one(samples, "cohesity.protectiongroup.run.objects", result="success").value == 14
        assert one(samples, "cohesity.protectiongroup.run.objects", result="total").value == 18

    def test_run_metrics_carry_the_storage_domain_from_the_group(self, client):
        groups = client.protection_groups()
        runs = [run for run in client.new_protection_runs() if run.id == "r-70003"]

        sample = one(
            metrics.protection_run_samples(CLUSTER_ID, CLUSTER_NAME, runs, groups),
            "cohesity.protectiongroup.run.outcome",
        )

        # NAS-Weekly writes to domain 1003. The runs endpoint does not return it; the group does.
        assert sample.dimensions[metrics.DIM_STORAGE_DOMAIN_ID] == f"{CLUSTER_ID}_1003"

    def test_a_run_whose_group_is_unknown_still_reports_without_the_edge(self, client):
        runs = [run for run in client.new_protection_runs() if run.id == "r-70001"]

        sample = one(
            metrics.protection_run_samples(CLUSTER_ID, CLUSTER_NAME, runs, []),
            "cohesity.protectiongroup.run.outcome",
        )

        assert metrics.DIM_STORAGE_DOMAIN_ID not in sample.dimensions
        assert sample.dimensions[metrics.DIM_PROTECTION_GROUP_ID] == f"{CLUSTER_ID}_g-9001"

    def test_a_run_with_no_duration_reports_the_rest(self):
        # A negative or unknown duration is dropped rather than reported as a negative number.
        run = domain.ProtectionRun(
            id="r-1",
            protection_group_id="g-1",
            protection_group_name="Nightly",
            status="Failed",
            start_time_usecs=200,
            end_time_usecs=100,
            bytes_written=5,
        )

        samples = metrics.protection_run_samples(CLUSTER_ID, CLUSTER_NAME, [run], [])

        assert not by_key(samples, "cohesity.protectiongroup.run.duration")
        assert by_key(samples, "cohesity.protectiongroup.run.bytes_written")


class TestDiagnosticLogEvents:
    """The records that carry back the two facts a silent metric gap needs, and nothing else.

    They exist because this extension's own log lines are not reaching Grail on the tenant it
    was debugged against - which left "which entityId does this schema answer to" and "what does
    this cluster call its usage fields" unreadable, and those were exactly the two answers
    needed. Log ingest is a different path and it arrives.
    """

    VERSION_FACT = {
        "kind": "cluster_version",
        "version": "7.4.1_u2_release",
        "nodeCount": 3,
        "source": "https://cohesity.example/v2",
    }
    RESOLVED_FACT = {
        "kind": "entity_id_probe",
        "schema": "kSentryClusterStats",
        "entityId": "778899",
        "candidates": ["778899", "1234567890123456"],
        "resolved": True,
    }
    FAILED_FACT = {
        "kind": "entity_id_probe",
        "schema": "kBridgeClusterStats",
        "entityId": "",
        "candidates": ["778899", "1234567890123456"],
        "resolved": False,
    }
    FIELDS_FACT = {
        "kind": "storage_domain_stats_fields",
        "fields": ["dataInBytes", "localTierResiliencyImpactBytes", "logicalUsageBytes"],
    }
    PROBE_ERROR_FACT = {
        "kind": "entity_id_probe",
        "schema": "kBridgeClusterStats",
        "entityId": "",
        "candidates": ["1234", "5678"],
        "attempts": ["1234=CohesityAuthError HTTP 403"],
        "resolved": False,
        "error": "CohesityAuthError",
        "status": 403,
        "path": "/stats/time-series-stats",
        "params": ["schemaName", "metricNames", "entityId"],
        "detail": "cohesity-prod: the cluster rejected the API key",
    }
    SECTION_FAILURE_FACT = {
        "kind": "section_failure",
        "section": "cluster time series",
        "error": "CohesityAuthError",
        "status": 403,
        "path": "/stats/time-series-stats",
        "params": ["schemaName", "metricNames", "entityId"],
        "detail": "cohesity-prod: the cluster rejected the API key (HTTP 403 Forbidden)",
    }

    OBJECTS_FACT = {
        "kind": "protected_objects",
        "environment": "kVMware",
        "objects": 142,
        "fields": ["id", "name", "objectHash", "uuid", "vCenterSummary"],
        "vmwareKey": "vCenterSummary",
        "vmwareFields": ["isCloudEnv", "type"],
        "uuids": [
            {"value": "00112233-4455-6677-8899-aabbccddeeff", "verdict": "uuid-8-4-4-4-12"},
            {"value": "5088628705619046646:1789472940012", "verdict": "numeric-id"},
            {"value": "", "verdict": "missing"},
        ],
    }
    OBJECTS_FAILED_FACT = {
        "kind": "protected_objects",
        "environment": "",
        "objects": 0,
        "error": "CohesityAuthError",
        "status": 403,
        "path": "/data-protect/protection-groups/g-1/runs",
        "params": ["numRuns", "includeObjectDetails"],
        "detail": "cohesity-prod: the cluster rejected the API key",
    }

    def events(self, *facts) -> list[dict]:
        return metrics.diagnostic_log_events(CLUSTER_ID, CLUSTER_NAME, "7.4.1_u2", list(facts))

    def one(self, fact) -> dict:
        events = self.events(fact)
        assert len(events) == 1
        return events[0]

    def test_every_record_carries_the_cluster_it_is_about(self, ):
        for event in self.events(self.VERSION_FACT, self.RESOLVED_FACT, self.FIELDS_FACT):
            assert event[metrics.DIM_CLUSTER_ID] == CLUSTER_ID
            assert event[metrics.DIM_CLUSTER_NAME] == CLUSTER_NAME
            assert event["cohesity.cluster.version"] == "7.4.1_u2"
            assert event["log.source"] == metrics.LOG_SOURCE_DIAGNOSTICS

    def test_an_unreported_version_says_so_rather_than_reading_as_blank(self):
        events = metrics.diagnostic_log_events(CLUSTER_ID, CLUSTER_NAME, "", [self.VERSION_FACT])

        assert events[0]["cohesity.cluster.version"] == "unreported"

    def test_the_version_record_carries_the_software_version(self):
        event = self.one(self.VERSION_FACT)

        assert "7.4.1_u2_release" in event["content"]
        assert event["severity"] == metrics.SEVERITY_INFO

    def test_a_resolved_probe_names_the_winner_and_everything_tried(self):
        event = self.one(self.RESOLVED_FACT)

        assert event["severity"] == metrics.SEVERITY_INFO
        assert event["cohesity.schema"] == "kSentryClusterStats"
        assert event["cohesity.entity_id"] == "778899"
        assert event["cohesity.entity_id_candidates"] == "778899, 1234567890123456"

    def test_a_failed_probe_is_a_warning_that_still_names_the_candidates(self):
        # The list of what was tried IS the state of the investigation; losing it would leave
        # the next person exactly where this one started.
        event = self.one(self.FAILED_FACT)

        assert event["severity"] == metrics.SEVERITY_WARN
        assert event["cohesity.entity_id"] == ""
        assert "1234567890123456" in event["cohesity.entity_id_candidates"]

    def test_the_stats_field_record_carries_names_and_no_values(self):
        event = self.one(self.FIELDS_FACT)

        assert event["cohesity.stats_fields"] == (
            "dataInBytes, localTierResiliencyImpactBytes, logicalUsageBytes"
        )
        assert "3" in event["content"]

    def test_a_cluster_that_publishes_absurdly_many_fields_is_capped(self):
        fact = {"kind": "storage_domain_stats_fields", "fields": [f"f{n}" for n in range(500)]}

        event = self.one(fact)

        assert event["cohesity.stats_fields"].count(",") == metrics.MAX_DIAGNOSTIC_FIELDS - 1

    def test_a_probe_that_raised_reads_as_an_error_and_keeps_what_it_learned(self):
        # Empty and refused are different facts with different fixes, and before 0.1.5 the
        # refused case produced no record at all.
        event = self.one(self.PROBE_ERROR_FACT)

        assert event["severity"] == metrics.SEVERITY_ERROR
        assert event["cohesity.http_status"] == "403"
        assert event["cohesity.entity_id_attempts"] == "1234=CohesityAuthError HTTP 403"
        assert event["cohesity.entity_id_candidates"] == "1234, 5678"
        assert "/stats/time-series-stats" in event["content"]

    def test_the_object_probe_answers_ticket_16_in_one_sentence(self):
        event = self.one(self.OBJECTS_FACT)

        assert event["severity"] == metrics.SEVERITY_INFO
        assert event["cohesity.object_environment"] == "kVMware"
        assert event["cohesity.object_count"] == "142"
        assert event["cohesity.object_fields"] == "id, name, objectHash, uuid, vCenterSummary"
        assert event["cohesity.object_vmware_key"] == "vCenterSummary"
        assert event["cohesity.object_vmware_fields"] == "isCloudEnv, type"
        assert event["cohesity.object_uuid_verdicts"] == (
            "uuid-8-4-4-4-12, numeric-id, missing"
        )
        assert "00112233-4455-6677-8899-aabbccddeeff" in event["cohesity.object_uuids"]
        assert "vCenterSummary" in event["content"]

    def test_the_object_probe_never_carries_an_object_name(self):
        # "name" appears as a FIELD NAME and must never appear as a value. The whole record is
        # searched rather than one field, because a name leaking into the sentence would be
        # just as bad as one leaking into an attribute.
        fact = dict(self.OBJECTS_FACT)
        fact["uuids"] = [{"value": "00112233-4455-6677-8899-aabbccddeeff", "verdict": "uuid"}]

        event = self.one(fact)

        assert "PLACEHOLDER" not in json.dumps(event)
        for value in event.values():
            assert "web-server" not in str(value)
        assert event["cohesity.object_fields"].split(", ") == fact["fields"]

    def test_an_absent_uuid_reads_as_absent_rather_than_as_an_empty_gap(self):
        event = self.one(self.OBJECTS_FACT)

        assert "absent -> missing" in event["content"]

    def test_a_cluster_with_no_vmware_block_is_told_so_in_capitals(self):
        fact = {**self.OBJECTS_FACT, "vmwareKey": "", "vmwareFields": []}

        event = self.one(fact)

        assert "NO VMware-specific sub-object" in event["content"]

    def test_no_objects_at_all_is_a_warning_that_closes_the_question(self):
        fact = {"kind": "protected_objects", "environment": "kPhysical", "objects": 0}

        event = self.one(fact)

        assert event["severity"] == metrics.SEVERITY_WARN
        assert "kPhysical" in event["content"]
        assert event["cohesity.object_uuids"] == ""

    def test_a_probe_that_failed_is_a_warning_not_an_error(self):
        # No metric is missing because of it, and calling it an ERROR would train someone to
        # ignore the ERRORs that do mean a metric is gone.
        event = self.one(self.OBJECTS_FAILED_FACT)

        assert event["severity"] == metrics.SEVERITY_WARN
        assert event["cohesity.http_status"] == "403"
        assert "Protection-run collection is unaffected" in event["content"]

    def test_a_section_failure_names_the_section_the_status_and_the_path(self):
        event = self.one(self.SECTION_FAILURE_FACT)

        assert event["severity"] == metrics.SEVERITY_ERROR
        assert event["cohesity.section"] == "cluster time series"
        assert event["cohesity.error"] == "CohesityAuthError"
        assert event["cohesity.http_status"] == "403"
        assert event["cohesity.path"] == "/stats/time-series-stats"
        for piece in ("cluster time series", "403", "/stats/time-series-stats"):
            assert piece in event["content"]

    def test_a_section_failure_carries_parameter_names_and_no_parameter_values(self):
        event = self.one(self.SECTION_FAILURE_FACT)

        assert event["cohesity.query_params"] == "schemaName, metricNames, entityId"
        # Names, so the call is identifiable. No "name=value" anywhere, because the value is
        # how an entityId, a time window or one day a token ends up in a log stream.
        assert "=" not in event["cohesity.query_params"]
        assert "entityId=" not in json.dumps(event)

    def test_a_failure_with_no_request_behind_it_says_so_rather_than_inventing_one(self):
        fact = {"kind": "section_failure", "section": "views", "error": "KeyError"}

        event = self.one(fact)

        assert event["cohesity.http_status"] == ""
        assert event["cohesity.path"] == ""
        assert "no single endpoint" in event["content"]

    def test_a_secret_the_error_message_embedded_never_reaches_the_record(self):
        # The auth message names the credential vault entry on purpose - a rejected credential
        # and an unresolved one have different fixes. A log record has a wider audience than
        # the ActiveGate's own logs, so it comes back out on the way to Grail.
        secret = "Ab3kQ9zR7mT1xW5vN2pL8sD4"
        error = CohesityAuthError(
            f"cohesity-prod: the cluster rejected apiKey={secret}, read from credential "
            f"vault entry {secret} (HTTP 403 Forbidden)"
        )
        annotate(error, path="/stats/top-views", params=("metric",), status=403)
        fact = {"kind": "section_failure", "section": "views", **error_facts(error, (secret,))}

        event = self.one(fact)

        assert secret not in json.dumps(event)
        assert "[redacted]" in event["cohesity.detail"]
        # Redacting must not cost the part that says what to do about it.
        assert event["cohesity.http_status"] == "403"
        assert "403" in event["content"]

    def test_a_credential_shaped_string_nobody_configured_is_dropped_too(self):
        # Not every secret in a message is one this process holds - the cluster can echo one
        # back. Shape, not identity, is the only defence available for those.
        error = CohesityApiError("cohesity-prod: /stats/views refused token=Zz09QqWwEeRrTtYyUu11")

        event = self.one({"kind": "section_failure", "section": "views", **error_facts(error)})

        assert "Zz09QqWwEeRrTtYyUu11" not in json.dumps(event)

    def test_a_failure_detail_cannot_become_the_log_stream(self):
        error = CohesityApiError("x" * 5000)

        fact = error_facts(error)

        assert len(fact["detail"]) <= errors.MAX_ERROR_DETAIL_CHARS

    VARIANT_RESOLVED_FACT = {
        "kind": "param_variant",
        "path": "/stats/time-series-stats",
        "variant": "no-rollupIntervalSecs",
        "attempts": ["metricNames-repeated=CohesityApiError HTTP 500", "no-rollupIntervalSecs=ok"],
        "requests": 2,
        "resolved": True,
    }
    VARIANT_FAILED_FACT = {
        "kind": "param_variant",
        "path": "/stats/top-views",
        "variant": "",
        "attempts": ["no-protocol=CohesityApiError HTTP 500", "metric-only=CohesityApiError HTTP 500"],
        "requests": 4,
        "resolved": False,
        "error": "CohesityApiError",
        "status": 500,
        "params": ["metric", "numTopViews"],
        "detail": "cohesity-prod: /stats/top-views failed on the cluster side",
    }
    RUNS_SOURCE_FACT = {
        "kind": "runs_source",
        "source": "protection-runs",
        "attempts": ["runs/summary=CohesityConnectError", "protection-runs=ok"],
        "runs": 4,
    }

    def test_a_working_parameter_shape_is_named_along_with_what_it_cost(self):
        event = self.one(self.VARIANT_RESOLVED_FACT)

        # A warning, not an info: the endpoint is working again, and it is still broken.
        assert event["severity"] == metrics.SEVERITY_WARN
        assert event["cohesity.variant"] == "no-rollupIntervalSecs"
        assert event["cohesity.path"] == "/stats/time-series-stats"
        assert event["cohesity.variant_requests"] == "2"
        assert "no-rollupIntervalSecs=ok" in event["cohesity.variant_attempts"]

    def test_a_probe_that_found_nothing_says_what_it_tried(self):
        # The list of shapes tried IS the state of the investigation, exactly as the candidate
        # list is for the entityId probe.
        event = self.one(self.VARIANT_FAILED_FACT)

        assert event["severity"] == metrics.SEVERITY_ERROR
        assert event["cohesity.variant"] == ""
        assert event["cohesity.http_status"] == "500"
        assert "no-protocol" in event["cohesity.variant_attempts"]
        assert "metric-only" in event["content"]

    def test_the_runs_endpoint_that_answered_is_named(self):
        # The run metrics carry the same keys whichever endpoint produced them, so this record
        # is the only place the answer to "which one worked" exists.
        event = self.one(self.RUNS_SOURCE_FACT)

        assert event["severity"] == metrics.SEVERITY_WARN
        assert event["cohesity.runs_source"] == "protection-runs"
        assert "runs/summary=" in event["cohesity.runs_attempts"]
        assert "4 run(s)" in event["content"]

    def test_the_documented_runs_endpoint_working_is_not_a_warning(self):
        fact = {**self.RUNS_SOURCE_FACT, "source": "runs/summary", "attempts": ["runs/summary=ok"]}

        assert self.one(fact)["severity"] == metrics.SEVERITY_INFO

    def test_no_runs_endpoint_answering_reads_as_an_error_not_as_a_quiet_cluster(self):
        # Twenty-four hours of zero looked like a quiet cluster and was three broken endpoints.
        fact = {
            "kind": "runs_source",
            "source": "",
            "attempts": ["runs/summary=CohesityConnectError"],
            "runs": 0,
            "error": "CohesityConnectError",
            "status": None,
            "path": "/data-protect/runs/summary",
            "params": ["startTimeUsecs", "endTimeUsecs"],
            "detail": "did not answer within 120s",
        }

        event = self.one(fact)

        assert event["severity"] == metrics.SEVERITY_ERROR
        assert event["cohesity.runs_source"] == ""
        assert "no run outcome" in event["content"]
        assert event["cohesity.query_params"] == "startTimeUsecs, endTimeUsecs"

    RUNS_FANOUT_FACT = {
        "kind": "runs_fanout",
        "groups_total": 42,
        "groups_queried": 20,
        "groups_errored": 0,
        "runs_per_group": 3,
        "requests": 20,
        "runs_seen": 14,
        "runs_new": 2,
    }

    def test_the_fan_out_record_separates_a_quiet_cluster_from_a_blind_extension(self):
        # v0.1.6 reported "0 run(s) in the window" for twenty minutes and there was no way to
        # tell whether the fan-out was asking the right groups. These three counts are the way.
        event = self.one(self.RUNS_FANOUT_FACT)

        assert event["severity"] == metrics.SEVERITY_INFO
        assert event["cohesity.runs_groups_queried"] == "20"
        assert event["cohesity.runs_groups_total"] == "42"
        assert event["cohesity.runs_seen"] == "14"
        assert event["cohesity.runs_new"] == "2"
        assert "20 of 42" in event["content"]

    def test_groups_that_did_not_answer_make_the_fan_out_record_a_warning(self):
        # A fan-out that half worked reports runs and is still a fault - on a cluster already
        # answering HTTP 500 from two endpoints, that is the difference worth seeing.
        fact = {**self.RUNS_FANOUT_FACT, "groups_errored": 4}

        event = self.one(fact)

        assert event["severity"] == metrics.SEVERITY_WARN
        assert event["cohesity.runs_groups_errored"] == "4"

    def test_a_fact_kind_nobody_wrote_a_record_for_is_dropped_not_guessed_at(self):
        assert self.events({"kind": "something_invented_later"}) == []
        assert self.events({}) == []

    def test_no_record_carries_a_value_from_the_cluster(self):
        # Names of things, never values of things. A capacity figure or a key in the log stream
        # would be a different decision from the one this was meant to be.
        events = self.events(self.VERSION_FACT, self.RESOLVED_FACT, self.FAILED_FACT, self.FIELDS_FACT)

        text = json.dumps(events)
        assert "219902325555200" not in text
        assert "demo-key" not in text
        assert "apiKey" not in text


class TestWholePoll:
    """Every sample a full replay poll produces, checked as one set."""

    def all_samples(self, client) -> list[metrics.Sample]:
        return replay_samples(client)

    def test_every_key_emitted_is_a_declared_key(self, client):
        # An undeclared key is dropped by the EEC without a word.
        for sample in self.all_samples(client):
            assert sample.key in metrics.ALL_METRIC_KEYS, sample.key

    def test_all_three_entity_prefixes_are_exercised(self, client):
        keys = {sample.key for sample in self.all_samples(client)}

        for prefix in (
            metrics.PREFIX_CLUSTER,
            metrics.PREFIX_STORAGE_DOMAIN,
            metrics.PREFIX_PROTECTION_GROUP,
        ):
            assert any(key.startswith(f"{prefix}.") for key in keys), prefix

    def test_every_sample_carries_the_cluster_identity(self, client):
        for sample in self.all_samples(client):
            assert sample.dimensions[metrics.DIM_CLUSTER_ID] == CLUSTER_ID

    def test_every_dimension_value_is_a_non_empty_string(self, client):
        for sample in self.all_samples(client):
            for name, value in sample.dimensions.items():
                assert isinstance(value, str), (sample.key, name)
                assert value != "", (sample.key, name)

    def test_every_value_is_a_number(self, client):
        for sample in self.all_samples(client):
            assert isinstance(sample.value, (int, float))
            assert not isinstance(sample.value, bool)

    def test_only_run_outcomes_are_deltas(self, client):
        for sample in self.all_samples(client):
            assert sample.delta is (sample.key == metrics.PROTECTION_GROUP_RUN_OUTCOME)
