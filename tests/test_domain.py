"""Parsing rules that are easy to get wrong and silent when they are.

Each class here corresponds to a trap ticket 04 found in the published schemas. They are tested
against hand-built bodies rather than the fixtures so that the fixture and the rule cannot drift
into agreeing with each other.
"""

from __future__ import annotations

import pytest

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


class TestEntityIdCandidateSources:
    """The ids a cluster-identity response can offer the entityId probe."""

    def test_the_v1_body_offers_its_id(self):
        assert domain.cluster_id_candidates({"id": 42, "name": "prod"}) == ["42"]

    def test_both_spellings_are_read_in_the_order_asked_for(self):
        payload = {"id": 1, "clusterId": 2}

        assert domain.cluster_id_candidates(payload) == ["1", "2"]
        assert domain.cluster_id_candidates(payload, ("clusterId", "id")) == ["2", "1"]

    def test_incarnation_ids_are_a_separate_tier(self):
        payload = {"clusterId": 2, "clusterIncarnationId": 99}

        assert domain.cluster_id_candidates(payload, domain.CLUSTER_ALTERNATE_ID_FIELDS) == ["99"]

    def test_a_source_with_nothing_to_offer_costs_the_probe_nothing(self):
        # A 403 hands back None, and a cluster may simply not carry the field.
        assert domain.cluster_id_candidates(None) == []
        assert domain.cluster_id_candidates({"name": "prod"}) == []
        assert domain.cluster_id_candidates({"clusterId": None}) == []

    def test_booleans_are_not_ids(self):
        assert domain.cluster_id_candidates({"id": True}) == []

    def test_duplicates_collapse_so_no_request_is_wasted(self):
        # v1 id and v2 clusterId are the same int64 on most clusters, and every duplicate would
        # otherwise cost one wasted request per schema on the first poll.
        assert domain.ordered_unique(["7", "7", "", "8", None, " 7 "]) == ["7", "8"]


class TestStorageDomainFieldAliases:
    """Two usage fields the published model names, and a customer cluster does not."""

    def domain_with(self, stats: dict) -> domain.StorageDomain:
        return domain.parse_storage_domains({"storageDomains": [{"id": "5", "stats": stats}]})[0]

    @pytest.mark.parametrize("name", domain.STORAGE_DOMAIN_LOGICAL_FIELDS)
    def test_every_logical_alias_is_picked_up(self, name):
        assert self.domain_with({name: 123}).total_logical_usage_bytes == 123

    @pytest.mark.parametrize("name", domain.STORAGE_DOMAIN_PHYSICAL_FIELDS)
    def test_every_physical_alias_is_picked_up(self, name):
        assert self.domain_with({name: 456}).local_total_physical_usage_bytes == 456

    def test_the_published_name_wins_when_several_are_present(self):
        stats = {"totalLogicalUsageBytes": 1, "logicalUsageBytes": 2, "dataInBytes": 3}

        assert self.domain_with(stats).total_logical_usage_bytes == 1

    def test_a_null_alias_falls_through_to_the_next_rather_than_ending_the_search(self):
        # A field published with no value has told us nothing, and a later alias may carry it.
        stats = {"totalLogicalUsageBytes": None, "logicalUsageBytes": 9}

        assert self.domain_with(stats).total_logical_usage_bytes == 9

    def test_no_candidate_present_reads_as_none_rather_than_zero(self):
        # The whole metric layer rests on this: a reported zero usage reads as an outage.
        parsed = self.domain_with({"localTierResiliencyImpactBytes": 7})

        assert parsed.total_logical_usage_bytes is None
        assert parsed.local_total_physical_usage_bytes is None
        assert parsed.local_tier_resiliency_impact_bytes == 7

    def test_the_customer_shape_is_read_once_the_aliases_are_in(self):
        # Resiliency arrived from this same object while the other two did not, which is what
        # ruled out a missing stats object and left only the field names.
        parsed = self.domain_with(
            {
                "logicalUsageBytes": 100,
                "totalPhysicalUsageBytes": 40,
                "localTierResiliencyImpactBytes": 8,
            }
        )

        assert (parsed.total_logical_usage_bytes, parsed.local_total_physical_usage_bytes) == (100, 40)


class TestStorageDomainStatsFieldNames:
    """The diagnostic that makes the aliasing above answerable from Grail."""

    def test_the_first_domains_keys_come_back_sorted(self):
        payload = {
            "storageDomains": [
                {"id": "1", "stats": {"zBytes": 1, "aBytes": 2}},
                {"id": "2", "stats": {"otherBytes": 3}},
            ]
        }

        assert domain.storage_domain_stats_fields(payload) == ("aBytes", "zBytes")

    def test_a_domain_without_stats_is_stepped_over(self):
        payload = {"storageDomains": [{"id": "1"}, {"id": "2", "stats": {"bBytes": 1}}]}

        assert domain.storage_domain_stats_fields(payload) == ("bBytes",)

    def test_nothing_to_report_is_empty_rather_than_an_error(self):
        assert domain.storage_domain_stats_fields({"storageDomains": []}) == ()
        assert domain.storage_domain_stats_fields(None) == ()


class TestUuidShape:
    """The verdict that decides ticket 16, and the one way of getting it wrong that matters."""

    def test_a_vmware_bios_uuid_reads_as_a_uuid(self):
        # The exact normalised form of what a Dynatrace HOST publishes as
        # host.additional_system_info["system.serial"]. If Cohesity says this, the join is on.
        assert (
            domain.uuid_shape("00112233-4455-6677-8899-aabbccddeeff")
            == domain.UUID_VERDICT_CANONICAL
        )

    def test_case_and_surrounding_space_do_not_change_the_answer(self):
        assert (
            domain.uuid_shape("  00112233-4455-6677-8899-AABBCCDDEEFF  ")
            == domain.UUID_VERDICT_CANONICAL
        )

    def test_an_undashed_uuid_is_told_apart_from_a_dashed_one(self):
        assert domain.uuid_shape("00112233445566778899aabbccddeeff") == domain.UUID_VERDICT_HEX32

    def test_a_uuid_that_needs_its_separators_stripped_says_so(self):
        # Whoever builds the lookup table has to write that normalisation, so "it works after
        # you strip things" is a different answer from "it works".
        assert (
            domain.uuid_shape("00112233 4455 6677 8899 aabbccddeeff")
            == domain.UUID_VERDICT_NORMALISES
        )

    def test_a_thirty_two_digit_cohesity_id_is_not_mistaken_for_a_uuid(self):
        # The trap this whole verdict exists to avoid: 32 decimal digits are also 32 valid hex
        # digits, so a structural hex test alone answers the ticket exactly backwards.
        assert domain.uuid_shape("5" * 32) == domain.UUID_VERDICT_NUMERIC

    def test_a_colon_joined_int64_pair_is_a_cohesity_id(self):
        assert (
            domain.uuid_shape("5088628705619046646:1789472940012")
            == domain.UUID_VERDICT_NUMERIC
        )

    def test_an_ordinary_string_is_not_a_uuid(self):
        assert domain.uuid_shape("sql-instance-primary") == domain.UUID_VERDICT_OTHER

    def test_absent_and_empty_are_missing_rather_than_not_a_uuid(self):
        # Different facts with different follow-ups: "this cluster does not publish a uuid"
        # is not the same finding as "it publishes one that is useless".
        assert domain.uuid_shape(None) == domain.UUID_VERDICT_MISSING
        assert domain.uuid_shape("   ") == domain.UUID_VERDICT_MISSING


class TestProtectedObjectShape:
    """What comes back from includeObjectDetails=true, reduced to something reportable."""

    def vmware_payload(self, count: int = 2) -> dict:
        return {
            "runs": [
                {
                    "id": "r-1",
                    "environment": "kVMware",
                    "objects": [
                        {
                            "object": {
                                "id": 4000 + index,
                                "name": f"PLACEHOLDER-VM-{index}",
                                "environment": "kVMware",
                                "uuid": f"00112233-4455-6677-8899-aabbccddee0{index}",
                                "globalId": f"9:{4000 + index}",
                                "vCenterSummary": {"isCloudEnv": False, "type": "kVCenter"},
                            }
                        }
                        for index in range(count)
                    ],
                }
            ]
        }

    def test_the_object_keys_come_back_sorted_and_by_name_only(self):
        shape = domain.parse_protected_object_shape(self.vmware_payload())

        assert shape.object_fields == (
            "environment",
            "globalId",
            "id",
            "name",
            "uuid",
            "vCenterSummary",
        )

    def test_the_vmware_sub_object_is_found_and_its_keys_reported(self):
        shape = domain.parse_protected_object_shape(self.vmware_payload())

        assert shape.vmware_key == "vCenterSummary"
        assert shape.vmware_fields == ("isCloudEnv", "type")

    def test_a_source_with_no_vmware_block_says_so_rather_than_guessing(self):
        payload = {
            "runs": [
                {"id": "r-1", "objects": [{"object": {"id": 1, "uuid": "9:1"}}]},
            ]
        }

        shape = domain.parse_protected_object_shape(payload)

        assert shape.vmware_key == ""
        assert shape.vmware_fields == ()

    def test_the_field_names_are_a_union_not_the_first_objects_keys(self):
        # Cohesity omits null fields, so reading only the first object would report "this
        # cluster has no vCenterSummary" off an object that merely lacked one.
        payload = {
            "runs": [
                {
                    "id": "r-1",
                    "objects": [
                        {"object": {"id": 1}},
                        {"object": {"id": 2, "vCenterSummary": {"type": "kVCenter"}}},
                    ],
                }
            ]
        }

        shape = domain.parse_protected_object_shape(payload)

        assert shape.object_fields == ("id", "vCenterSummary")
        assert shape.vmware_key == "vCenterSummary"

    def test_at_most_three_uuids_are_carried_however_many_objects_there_are(self):
        shape = domain.parse_protected_object_shape(self.vmware_payload(count=12))

        assert shape.objects_seen == 12
        assert len(shape.uuid_samples) == domain.MAX_UUID_SAMPLES

    def test_each_sample_carries_the_value_and_its_verdict(self):
        shape = domain.parse_protected_object_shape(self.vmware_payload(count=1))

        assert shape.uuid_samples == (
            ("00112233-4455-6677-8899-aabbccddee00", domain.UUID_VERDICT_CANONICAL),
        )

    def test_an_object_with_no_uuid_is_reported_as_missing_rather_than_skipped(self):
        # "The object is there and carries no uuid" is the answer that ends the ticket, so it
        # must not look like "no objects came back".
        payload = {"runs": [{"id": "r-1", "objects": [{"object": {"id": 1, "name": "x"}}]}]}

        shape = domain.parse_protected_object_shape(payload)

        assert shape.objects_seen == 1
        assert shape.uuid_samples == (("", domain.UUID_VERDICT_MISSING),)

    def test_the_groups_environment_wins_over_the_runs(self):
        shape = domain.parse_protected_object_shape(self.vmware_payload(), environment="kVMware")

        assert shape.environment == "kVMware"

    def test_the_runs_environment_is_used_when_the_group_did_not_say(self):
        shape = domain.parse_protected_object_shape(self.vmware_payload(), environment="")

        assert shape.environment == "kVMware"

    def test_a_response_with_no_objects_parses_to_an_empty_shape(self):
        for payload in ({"runs": [{"id": "r-1"}]}, {"runs": []}, None, {"runs": [{"objects": 7}]}):
            shape = domain.parse_protected_object_shape(payload)

            assert shape.objects_seen == 0
            assert shape.object_fields == ()
            assert shape.uuid_samples == ()


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


class TestRunListShape:
    """The shape the two fallback endpoints answer with, when runs/summary will not.

    Different from runs/summary in three ways that all matter: the facts sit under
    ``localBackupInfo``, the bytes sit under ``localSnapshotStats`` inside that, and the times
    are spelled ``runStartTimeUsecs``. Only one of the two endpoints is in the 7.3.2 reference
    this extension was written from, so both spellings are read rather than one guessed at.
    """

    def body(self, **overrides):
        run = {
            "id": "r-1",
            "protectionGroupId": "g-1",
            "protectionGroupName": "Nightly",
            "environment": "kVMware",
            "localBackupInfo": {
                "status": "Failed",
                "runStartTimeUsecs": 1_000_000_000,
                "runEndTimeUsecs": 1_060_000_000,
                "isSlaViolated": True,
                "successObjectsCount": 14,
                "totalObjectsCount": 18,
                "localSnapshotStats": {"bytesWritten": 1024, "logicalSizeBytes": 4096},
            },
        }
        run.update(overrides)
        return {"runs": [run]}

    def test_the_nested_shape_reads_into_the_same_record(self):
        run = domain.parse_run_list(self.body())[0]

        assert run.id == "r-1"
        assert run.status == "Failed"
        assert run.duration_msecs == 60_000
        assert run.bytes_written == 1024
        assert run.logical_size_bytes == 4096
        assert run.success_objects_count == 14
        assert run.is_sla_violated is True
        assert run.environment == "kVMware"

    def test_the_summary_spellings_are_accepted_too(self):
        # The flat list endpoint is not in the 7.3.2 reference at all, so which of the two
        # spellings it uses is unknown. Reading both is cheaper than being wrong.
        runs = domain.parse_run_list(
            {
                "runs": [
                    {
                        "id": "r-2",
                        "status": "Succeeded",
                        "startTimeUsecs": 1_000_000,
                        "endTimeUsecs": 3_000_000,
                        "bytesWritten": 7,
                    }
                ]
            }
        )

        assert runs[0].status == "Succeeded"
        assert runs[0].duration_msecs == 2_000
        assert runs[0].bytes_written == 7

    def test_the_runs_summary_wrapper_is_read_by_the_same_parser(self):
        runs = domain.parse_run_list(
            {"protectionRunsSummary": [{"id": "r-3", "status": "Succeeded"}]}
        )

        assert [run.id for run in runs] == ["r-3"]

    def test_the_group_in_the_url_fills_in_what_the_body_omits(self):
        # The per-group endpoint does not repeat the job it was asked about; it is in the path.
        runs = domain.parse_run_list(
            {"runs": [{"id": "r-4", "status": "Succeeded"}]},
            group_id="g-7",
            group_name="Weekly-NAS",
        )

        assert runs[0].protection_group_id == "g-7"
        assert runs[0].protection_group_name == "Weekly-NAS"

    def test_what_the_cluster_states_beats_what_the_caller_assumed(self):
        runs = domain.parse_run_list(self.body(), group_id="wrong", group_name="wrong")

        assert runs[0].protection_group_id == "g-1"

    def test_zero_bytes_written_survives_the_lookup(self):
        # A failed run writes zero bytes, and zero is falsy - an `or` chain would drop it and
        # report no sample where "it wrote nothing" is the answer.
        body = self.body()
        body["runs"][0]["localBackupInfo"]["localSnapshotStats"]["bytesWritten"] = 0

        assert domain.parse_run_list(body)[0].bytes_written == 0

    def test_a_run_with_no_id_is_dropped(self):
        # Same rule as the summary parser: an un-deduplicable run is counted once per window.
        runs = domain.parse_run_list({"runs": [{"status": "Succeeded"}, {"id": "r-5"}]})

        assert [run.id for run in runs] == ["r-5"]

    def test_a_non_terminal_run_is_still_recognised_through_this_shape(self):
        body = self.body()
        body["runs"][0]["localBackupInfo"]["status"] = "Running"

        assert domain.parse_run_list(body)[0].is_terminal is False


class TestRunStatusIsNeverAbsent:
    """Measured on the customer tenant: 28 of 92 counted runs carried NO status dimension.

    `timeseries sum(cohesity.protectiongroup.run.outcome), by:{status}` answered
    `Succeeded 64` / `None 28`. The runs were counted and then could not be classified, and
    the totals looked perfectly healthy - the hole is invisible unless somebody groups by
    status. A run whose only target is an archive or a replica has no `localBackupInfo`, so
    the two-place lookup found nothing and `wire_dimensions` dropped the empty value.
    """

    def run(self, **overrides):
        run = {"id": "r-1", "protectionGroupId": "g-1"}
        run.update(overrides)
        return {"runs": [run]}

    def test_status_in_the_local_backup_block(self):
        runs = domain.parse_run_list(self.run(localBackupInfo={"status": "Succeeded"}))

        assert runs[0].status == "Succeeded"

    def test_status_at_the_run_root(self):
        runs = domain.parse_run_list(self.run(status="Failed"))

        assert runs[0].status == "Failed"

    def test_the_local_backup_block_wins_over_the_run_root(self):
        # The local backup is the run as an operator means it; a root status on the same run
        # is the roll-up across every target.
        runs = domain.parse_run_list(
            self.run(status="Succeeded", localBackupInfo={"status": "Failed"})
        )

        assert runs[0].status == "Failed"

    def test_status_only_in_an_archival_target_result(self):
        # The actual shape of the 28. No local copy at all, so no localBackupInfo block.
        runs = domain.parse_run_list(
            self.run(archivalInfo={"archivalTargetResults": [{"status": "Succeeded"}]})
        )

        assert runs[0].status == "Succeeded"

    def test_status_only_in_a_replication_target_result(self):
        runs = domain.parse_run_list(
            self.run(replicationInfo={"replicationTargetResults": [{"status": "Failed"}]})
        )

        assert runs[0].status == "Failed"

    def test_status_only_in_a_cloud_spin_target_result(self):
        runs = domain.parse_run_list(
            self.run(cloudSpinInfo={"cloudSpinTargetResults": [{"status": "Running"}]})
        )

        assert runs[0].status == "Running"

    def test_status_only_in_the_original_backup_block(self):
        runs = domain.parse_run_list(self.run(originalBackupInfo={"status": "Succeeded"}))

        assert runs[0].status == "Succeeded"

    def test_status_nowhere_at_all_is_exactly_unknown(self):
        # Never "", which vanishes. "unknown" is countable, chartable and alertable.
        runs = domain.parse_run_list(self.run(someFutureBlock={"nothing": 1}))

        assert runs[0].status == domain.RUN_STATUS_UNKNOWN
        assert runs[0].status == "unknown"

    def test_the_summary_parser_has_the_same_guarantee(self):
        runs = domain.parse_protection_runs({"protectionRunsSummary": [{"id": "r-1"}]})

        assert runs[0].status == domain.RUN_STATUS_UNKNOWN

    def test_an_empty_string_status_does_not_survive_as_one(self):
        # Cohesity omits null fields, but an explicit "" is a shape a cluster can send and it
        # would reach the ingest as a dropped dimension exactly like a missing one.
        runs = domain.parse_run_list(self.run(status="", localBackupInfo={"status": ""}))

        assert runs[0].status == domain.RUN_STATUS_UNKNOWN

    def test_an_empty_target_result_list_does_not_resolve_a_status(self):
        runs = domain.parse_run_list(self.run(archivalInfo={"archivalTargetResults": []}))

        assert runs[0].status == domain.RUN_STATUS_UNKNOWN

    def test_the_first_target_result_that_states_a_status_wins(self):
        runs = domain.parse_run_list(
            self.run(
                archivalInfo={"archivalTargetResults": [{"noStatusHere": 1}, {"status": "Failed"}]}
            )
        )

        assert runs[0].status == "Failed"

    def test_the_field_names_of_an_unclassifiable_run_are_reportable(self):
        """Names only - the storage-domain trick, applied to the same kind of blind spot.

        "unknown" says the status is missing; only the field list says where it really is.
        """
        payload = self.run(
            protectionGroupName="Nightly",
            archivalInfo={"archivalTargetResults": [], "someOtherKey": 1},
        )
        names = domain.run_field_names(payload, "r-1")

        assert "archivalInfo" in names
        assert "archivalInfo.someOtherKey" in names
        assert "protectionGroupName" in names
        # The NAME of the field, never what a Cohesity admin typed into it.
        assert "Nightly" not in names

    def test_the_field_names_of_a_run_that_is_not_there_are_empty(self):
        assert domain.run_field_names(self.run(), "r-other") == ()


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
