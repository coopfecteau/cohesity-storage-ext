"""Parsing rules that are easy to get wrong and silent when they are.

Each class here corresponds to a trap ticket 04 found in the published schemas. They are tested
against hand-built bodies rather than the fixtures so that the fixture and the rule cannot drift
into agreeing with each other.
"""

from __future__ import annotations

from cohesity_storage import domain


class TestDataPointValues:
    """`dataPoints[]` entries have no `value` field. This is the KeyError everyone hits first."""

    def test_an_int64_point_is_read_from_int64_value(self):
        point = domain.parse_data_point(
            {"timestampMsecs": 1789200000000, "int64Value": 4211, "doubleValue": None, "stringValue": None},
            "kInt64",
        )

        assert point.value == 4211
        assert point.timestamp_msecs == 1789200000000

    def test_a_double_point_is_read_from_double_value(self):
        point = domain.parse_data_point({"timestampMsecs": 1, "doubleValue": 12.5}, "kDouble")

        assert point.value == 12.5

    def test_a_string_point_is_read_from_string_value(self):
        point = domain.parse_data_point({"timestampMsecs": 1, "stringValue": "kHealthy"}, "kString")

        assert point.value == "kHealthy"

    def test_the_declared_type_wins_when_two_fields_carry_a_value(self):
        # A cluster that populates both should be read as what it says it is.
        point = domain.parse_data_point({"int64Value": 7, "doubleValue": 7.9}, "kInt64")

        assert point.value == 7

    def test_an_unknown_type_still_finds_the_value(self):
        # The type enum is the field most likely to drift between versions; losing a reading
        # over an unrecognised enum value would be a poor trade.
        point = domain.parse_data_point({"doubleValue": 3.5}, "kSomethingNew")

        assert point.value == 3.5

    def test_an_all_null_point_yields_none_rather_than_raising(self):
        point = domain.parse_data_point(
            {"timestampMsecs": None, "int64Value": None, "doubleValue": None, "stringValue": None},
            "kInt64",
        )

        assert point.value is None
        assert point.timestamp_msecs is None

    def test_a_zero_value_is_kept(self):
        # Zero is falsy and is also a perfectly good reading. Any `or` chain loses it.
        point = domain.parse_data_point({"int64Value": 0}, "kInt64")

        assert point.value == 0


class TestTimeSeries:
    def test_metrics_are_keyed_by_name(self):
        series = domain.parse_time_series(
            {
                "timeSeriesStats": [
                    {"metricName": "kReadIos", "type": "kInt64", "dataPoints": [{"int64Value": 5}]},
                    {"metricName": "kWriteIos", "type": "kInt64", "dataPoints": [{"int64Value": 6}]},
                ]
            }
        )

        assert sorted(series) == ["kReadIos", "kWriteIos"]
        assert series["kWriteIos"].latest_value() == 6

    def test_the_latest_non_null_point_wins_over_an_empty_tail(self):
        # The newest bucket is routinely flushed before it is filled.
        series = domain.parse_time_series(
            {
                "timeSeriesStats": [
                    {
                        "metricName": "kCpuUsagePct",
                        "type": "kDouble",
                        "dataPoints": [
                            {"timestampMsecs": 1, "doubleValue": 10.0},
                            {"timestampMsecs": 2, "doubleValue": 20.0},
                            {"timestampMsecs": 3, "doubleValue": None},
                        ],
                    }
                ]
            }
        )

        assert series["kCpuUsagePct"].latest_value() == 20.0

    def test_an_empty_series_is_a_present_metric_with_no_points(self):
        # This is what a wrong entityId looks like: a 200, a named metric, and nothing in it.
        series = domain.parse_time_series(
            {"timeSeriesStats": [{"metricName": "kCpuUsagePct", "type": "kDouble", "dataPoints": []}]}
        )

        assert series["kCpuUsagePct"].latest() is None

    def test_a_malformed_body_yields_no_metrics_rather_than_raising(self):
        assert domain.parse_time_series({"timeSeriesStats": "nonsense"}) == {}
        assert domain.parse_time_series(None) == {}


class TestNamespacing:
    def test_ids_are_namespaced_by_cluster(self):
        assert domain.namespace_id(6489393267063001, 4) == "6489393267063001_4"

    def test_the_same_object_id_on_two_clusters_does_not_collide(self):
        domain_a = domain.StorageDomain(id="4", name="Default")
        domain_b = domain.StorageDomain(id="4", name="Default")

        assert domain_a.entity_id(1001) != domain_b.entity_id(1002)

    def test_a_protection_group_namespaces_the_same_way(self):
        group = domain.ProtectionGroup(id="g-9001", name="Nightly")

        assert group.entity_id(1001) == "1001_g-9001"


class TestVersionFork:
    def test_a_release_suffix_does_not_defeat_the_comparison(self):
        assert domain.parse_version("7.3.1_u2_release-20250104_abcdef12") == (7, 3, 1)

    def test_an_unreadable_version_is_empty_not_zero(self):
        # An empty tuple compares less than every real version, and callers must treat it as
        # "unknown" rather than "ancient" - which is why it is distinguishable from (0,).
        assert domain.parse_version("release-unknown") == ()
        assert domain.parse_version(None) == ()
        assert domain.parse_version("0") == (0,)


class TestClusterStatus:
    def test_identity_and_version_are_read(self):
        status = domain.parse_cluster_status(
            {
                "clusterId": 1234567890123456,
                "name": "prod",
                "softwareVersion": "7.4.1_u2",
                "nodeStatuses": [{}, {}],
            }
        )

        assert status.cluster_id == "1234567890123456"
        assert status.name == "prod"
        assert status.version == (7, 4, 1)
        assert status.node_count == 2

    def test_the_stats_entity_id_is_the_cluster_id(self):
        # Marked as an assumption in domain.py: nothing in v2 documents the entityId for
        # cluster-level schemas, and getting it wrong returns empty data with no error.
        status = domain.parse_cluster_status({"clusterId": 42})

        assert status.stats_entity_id == "42"

    def test_a_body_without_identity_yields_empty_strings(self):
        status = domain.parse_cluster_status({})

        assert (status.cluster_id, status.name) == ("", "")


class TestClusterStorage:
    def test_the_seven_scalars_are_read(self):
        storage = domain.parse_cluster_storage(
            {"totalCapacityBytes": 1000, "localUsageBytes": 250, "localAvailableBytes": 750}
        )

        assert storage.total_capacity_bytes == 1000
        assert storage.used_pct == 25.0

    def test_a_missing_field_is_none_rather_than_zero(self):
        # A reported zero capacity reads as an outage on a chart; nothing reads as nothing.
        storage = domain.parse_cluster_storage({"localUsageBytes": 250})

        assert storage.total_capacity_bytes is None
        assert storage.used_pct is None

    def test_zero_capacity_does_not_divide(self):
        storage = domain.parse_cluster_storage({"totalCapacityBytes": 0, "localUsageBytes": 0})

        assert storage.used_pct is None


class TestRunDeduplication:
    """The hazard ticket 04 flagged: overlapping windows re-return the same run."""

    def build(self, run_id, status="Succeeded"):
        return domain.ProtectionRun(
            id=run_id, protection_group_id="g-1", protection_group_name="Nightly", status=status
        )

    def test_a_run_is_counted_once_across_overlapping_windows(self):
        ledger = domain.RunLedger()
        runs = [self.build("r-1"), self.build("r-2")]

        first = domain.new_terminal_runs(runs, ledger)
        second = domain.new_terminal_runs(runs, ledger)

        assert [run.id for run in first] == ["r-1", "r-2"]
        assert second == []

    def test_a_running_run_is_skipped_and_still_counts_when_it_finishes(self):
        # Recording it while in flight would lose the outcome entirely - the failure would
        # never be counted, which is the worst possible direction to be wrong in.
        ledger = domain.RunLedger()

        in_flight = domain.new_terminal_runs([self.build("r-9", "Running")], ledger)
        finished = domain.new_terminal_runs([self.build("r-9", "Failed")], ledger)

        assert in_flight == []
        assert [run.status for run in finished] == ["Failed"]

    def test_every_non_terminal_status_is_skipped(self):
        ledger = domain.RunLedger()
        runs = [self.build(f"r-{status}", status) for status in domain.NON_TERMINAL_RUN_STATUSES]

        assert domain.new_terminal_runs(runs, ledger) == []

    def test_an_unrecognised_status_is_counted_once_rather_than_dropped(self):
        # Cohesity's status enum has grown across versions. Counting an unknown outcome under
        # its own raw name is recoverable; silently never counting it is not.
        ledger = domain.RunLedger()

        counted = domain.new_terminal_runs([self.build("r-new", "CompletedWithSomethingNew")], ledger)

        assert [run.id for run in counted] == ["r-new"]

    def test_the_ledger_is_bounded(self):
        ledger = domain.RunLedger(capacity=3)
        for index in range(10):
            ledger.claim(f"r-{index}")

        assert len(ledger) == 3

    def test_a_run_that_keeps_reappearing_is_not_evicted_and_recounted(self):
        ledger = domain.RunLedger(capacity=2)
        ledger.claim("long-running")
        ledger.claim("a")
        ledger.claim("long-running")  # refreshes recency
        ledger.claim("b")

        assert ledger.claim("long-running") is False

    def test_a_run_without_an_id_is_dropped(self):
        # It could not be deduplicated, so it would be counted once per overlapping window.
        runs = domain.parse_protection_runs({"protectionRunsSummary": [{"status": "Succeeded"}]})

        assert runs == []


class TestProtectionRuns:
    def test_the_documented_fields_are_read(self):
        runs = domain.parse_protection_runs(
            {
                "protectionRunsSummary": [
                    {
                        "id": "r-1",
                        "protectionGroupId": "g-1",
                        "protectionGroupName": "Nightly",
                        "status": "Failed",
                        "startTimeUsecs": 1_000_000_000,
                        "endTimeUsecs": 1_060_000_000,
                        "bytesWritten": 1024,
                        "logicalSizeBytes": 4096,
                        "isSlaViolated": True,
                        "successObjectsCount": 14,
                        "totalObjectsCount": 18,
                        "environment": "kSQL",
                    }
                ]
            }
        )

        run = runs[0]
        assert run.status == "Failed"
        assert run.duration_msecs == 60_000
        assert run.is_sla_violated is True
        assert run.environment == "kSQL"

    def test_a_run_with_no_end_time_has_no_duration(self):
        run = domain.ProtectionRun(
            id="r", protection_group_id="g", protection_group_name="n", status="Running",
            start_time_usecs=1_000_000,
        )

        assert run.duration_msecs is None

    def test_a_negative_duration_is_refused(self):
        run = domain.ProtectionRun(
            id="r", protection_group_id="g", protection_group_name="n", status="Succeeded",
            start_time_usecs=2_000_000, end_time_usecs=1_000_000,
        )

        assert run.duration_msecs is None


class TestProtectionGroups:
    def build(self, status, end_usecs):
        return domain.parse_protection_groups(
            {
                "protectionGroups": [
                    {
                        "id": "g-1",
                        "name": "Nightly",
                        "environment": "kVMware",
                        "isPaused": False,
                        "lastRun": {"localBackupInfo": {"status": status, "endTimeUsecs": end_usecs}},
                    }
                ]
            }
        )[0]

    def test_age_since_last_success_is_measured_from_the_run_end(self):
        now_usecs = 10_000_000_000
        group = self.build("Succeeded", now_usecs - 3_600_000_000)

        assert group.last_success_age_msecs(now_usecs) == 3_600_000

    def test_a_warning_still_counts_as_a_success(self):
        now_usecs = 10_000_000_000
        group = self.build("SucceededWithWarning", now_usecs - 1_000_000)

        assert group.last_success_age_msecs(now_usecs) == 1000

    def test_a_failed_last_run_has_no_success_age(self):
        # Emitting zero here would read as "just backed up" for a job that has not backed up.
        group = self.build("Failed", 9_000_000_000)

        assert group.last_success_age_msecs(10_000_000_000) is None

    def test_a_group_with_no_last_run_has_no_success_age(self):
        group = domain.parse_protection_groups({"protectionGroups": [{"id": "g", "name": "n"}]})[0]

        assert group.last_success_age_msecs(1) is None


class TestViewStats:
    def test_the_requested_metric_is_picked_out(self):
        views = domain.parse_views_stats(
            {
                "viewsStats": [
                    {
                        "viewId": 501,
                        "viewName": "prod-nfs",
                        "protocols": ["NFS"],
                        "stats": [
                            {
                                "metric": "kNumBytesWritten",
                                "valueInLastHours": [{"lastHours": 1, "value": 5}],
                            },
                            {
                                "metric": "kNumBytesRead",
                                "valueInLastHours": [{"lastHours": 1, "value": 9}],
                            },
                        ],
                    }
                ]
            },
            "kNumBytesRead",
        )

        assert views[0].value == 9
        assert views[0].view_id == "501"

    def test_a_view_without_the_metric_has_no_value(self):
        views = domain.parse_views_stats(
            {"viewsStats": [{"viewId": 1, "viewName": "x", "stats": []}]}, "kNumBytesRead"
        )

        assert views[0].value is None
