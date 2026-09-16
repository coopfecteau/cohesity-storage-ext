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

from pathlib import Path

import pytest

from cohesity_storage import domain, metrics
from cohesity_storage.client import CLUSTER_STATS_CALLS, VIEW_METRICS, CohesityClient
from cohesity_storage.config import ClusterConfig

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures"
CLUSTER_ID = "1234567890123456"
CLUSTER_NAME = "cohesity-demo-01"

# A fixed "now" well after the fixture timestamps, so age assertions are deterministic.
NOW_USECS = 1_789_600_000_000_000


@pytest.fixture
def client() -> CohesityClient:
    return CohesityClient(
        ClusterConfig(
            name="cohesity-prod",
            host="10.20.30.40",
            api_key="demo-key",
            fixture_dir=str(FIXTURE_DIR),
        )
    )


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


class TestWholePoll:
    """Every sample a full replay poll produces, checked as one set."""

    def all_samples(self, client) -> list[metrics.Sample]:
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
        samples.extend(
            metrics.protection_group_samples(CLUSTER_ID, CLUSTER_NAME, groups, NOW_USECS)
        )
        samples.extend(
            metrics.protection_run_samples(
                CLUSTER_ID, CLUSTER_NAME, client.new_protection_runs(), groups
            )
        )
        return samples

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
