"""The Dynatrace-facing naming contract: keys, prefixes and dimensions.

Pulling numbers out of a response body lives in cohesity_storage.domain; turning parsed objects
into samples is exercised in test_reporting.py. This module is only the contract from tickets
05 and 06, asserted key by key so a rename has to be deliberate.
"""

from __future__ import annotations

from cohesity_storage import metrics


class TestEntityIds:
    def test_ids_are_namespaced_by_cluster(self):
        # Cohesity ids are cluster-scoped int64s. Two clusters will hand out the same domain id.
        assert metrics.entity_id(6489393267063001, 4) == "6489393267063001_4"

    def test_the_same_object_id_on_two_clusters_does_not_collide(self):
        first = metrics.storage_domain_dimensions(1001, "east", 4)
        second = metrics.storage_domain_dimensions(1002, "west", 4)

        assert first[metrics.DIM_STORAGE_DOMAIN_ID] != second[metrics.DIM_STORAGE_DOMAIN_ID]

    def test_every_entity_carries_its_cluster(self):
        for dimensions in (
            metrics.storage_domain_dimensions(1001, "east", 4, "Default"),
            metrics.protection_group_dimensions(1001, "east", "9:123", "Nightly"),
        ):
            assert dimensions[metrics.DIM_CLUSTER_ID] == "1001"
            assert dimensions[metrics.DIM_CLUSTER_NAME] == "east"

    def test_the_cluster_id_is_not_namespaced_against_itself(self):
        assert metrics.cluster_dimensions(1001, "east")[metrics.DIM_CLUSTER_ID] == "1001"


class TestMetricKeys:
    # The contract from ticket 06, written out so a rename cannot happen by accident. Metric
    # prefixes are matched by the smartscape node rules, so these strings are topology config.
    CONTRACT = {
        "cohesity.cluster.capacity.total",
        "cohesity.cluster.capacity.used",
        "cohesity.cluster.capacity.available",
        "cohesity.cluster.usage.logical",
        "cohesity.cluster.usage.physical",
        "cohesity.cluster.cpu.usage",
        "cohesity.cluster.memory.usage",
        "cohesity.cluster.io.iops",
        "cohesity.cluster.io.latency",
        "cohesity.cluster.garbage.bytes",
        "cohesity.cluster.view.throughput",
        "cohesity.cluster.collection_success",
        "cohesity.storagedomain.usage.logical",
        "cohesity.storagedomain.usage.physical",
        "cohesity.storagedomain.resiliency.bytes",
        "cohesity.protectiongroup.run.outcome",
        "cohesity.protectiongroup.run.duration",
        "cohesity.protectiongroup.run.bytes_written",
        "cohesity.protectiongroup.run.bytes_logical",
        "cohesity.protectiongroup.run.objects",
        "cohesity.protectiongroup.last_success.age",
        "cohesity.protectiongroup.protects",
    }

    def test_the_key_set_is_exactly_ticket_06s_contract(self):
        assert set(metrics.ALL_METRIC_KEYS) == self.CONTRACT

    def test_no_key_is_declared_twice(self):
        assert len(metrics.ALL_METRIC_KEYS) == len(set(metrics.ALL_METRIC_KEYS))

    def test_entity_prefixes_have_no_underscore_separator(self):
        # The pipeline rules match `cohesity.storagedomain.*`, not `cohesity.storage_domain.*`.
        # An underscore here detaches every storage domain metric from its entity, silently.
        assert metrics.PREFIX_STORAGE_DOMAIN == "cohesity.storagedomain"
        assert metrics.PREFIX_PROTECTION_GROUP == "cohesity.protectiongroup"

    def test_no_entity_prefix_is_a_prefix_of_another(self):
        prefixes = [
            metrics.PREFIX_CLUSTER,
            metrics.PREFIX_STORAGE_DOMAIN,
            metrics.PREFIX_PROTECTION_GROUP,
        ]
        for prefix in prefixes:
            others = [other for other in prefixes if other != prefix]
            assert not [other for other in others if other.startswith(f"{prefix}.")]

    def test_every_key_sits_under_an_entity_prefix(self):
        prefixes = (
            metrics.PREFIX_CLUSTER,
            metrics.PREFIX_STORAGE_DOMAIN,
            metrics.PREFIX_PROTECTION_GROUP,
        )
        for key in metrics.ALL_METRIC_KEYS:
            assert any(key.startswith(f"{prefix}.") for prefix in prefixes), key

    def test_view_throughput_binds_to_the_cluster(self):
        # View is dimensions rather than an entity (ticket 05), so a `cohesity.view` prefix
        # would match no entity rule and the metric would float, absent from every entity page.
        assert metrics.CLUSTER_VIEW_THROUGHPUT.startswith(f"{metrics.PREFIX_CLUSTER}.")

    def test_there_is_no_node_prefix(self):
        # Node is out of v1 for lack of metrics. A prefix with nothing under it invites a key
        # that binds to an entity type no rule creates.
        assert not hasattr(metrics, "PREFIX_NODE")


class TestDimensionKeys:
    def test_identity_dimensions_are_cohesity_prefixed(self):
        # Collision-safe against built-in fields and other extensions, at the cost of verbosity.
        for key in (
            metrics.DIM_CLUSTER_ID,
            metrics.DIM_CLUSTER_NAME,
            metrics.DIM_STORAGE_DOMAIN_ID,
            metrics.DIM_STORAGE_DOMAIN_NAME,
            metrics.DIM_PROTECTION_GROUP_ID,
            metrics.DIM_PROTECTION_GROUP_NAME,
            metrics.DIM_VIEW_ID,
            metrics.DIM_VIEW_NAME,
        ):
            assert key.startswith("cohesity.")

    def test_identity_dimensions_match_the_entity_prefixes(self):
        # The pipeline's idComponents reference these exact field names.
        assert metrics.DIM_STORAGE_DOMAIN_ID == "cohesity.storagedomain.id"
        assert metrics.DIM_PROTECTION_GROUP_ID == "cohesity.protectiongroup.id"

    def test_a_protection_group_without_a_storage_domain_still_has_dimensions(self):
        # It should exist as an entity, just without the writes_to edge.
        dimensions = metrics.protection_group_dimensions(1001, "east", "g-1", "Nightly")

        assert metrics.DIM_PROTECTION_GROUP_ID in dimensions
        assert metrics.DIM_STORAGE_DOMAIN_ID not in dimensions

    def test_the_storage_domain_dimension_is_added_for_the_edge(self):
        dimensions = metrics.protection_group_dimensions(
            1001, "east", "g-1", "Nightly", storage_domain_id=4
        )

        assert dimensions[metrics.DIM_STORAGE_DOMAIN_ID] == "1001_4"

    def test_flags_are_absent_rather_than_false_when_unreported(self):
        # A missing flag and a flag that is off are different facts. Collapsing them would let
        # an alert on paused=="false" silently cover jobs whose state was never reported.
        dimensions = metrics.protection_group_dimensions(
            1001, "east", "g-1", "Nightly", is_paused=None, is_active=True
        )

        assert metrics.DIM_PAUSED not in dimensions
        assert dimensions[metrics.DIM_ACTIVE] == "true"

    def test_the_join_key_is_cohesity_prefixed_and_lowercase(self):
        # Lowercase because the ingest protocol rejects any dimension key with an uppercase
        # letter - silently, as an "invalid metric lines" count with no key attached.
        assert metrics.DIM_OBJECT_UUID == "cohesity.object.uuid"
        assert not [char for char in metrics.DIM_OBJECT_UUID if char.isupper()]

    def test_every_dimension_value_is_a_string(self):
        dimensions = metrics.protection_group_dimensions(
            1001,
            "east",
            9001,
            "Nightly",
            storage_domain_id=4,
            status="Succeeded",
            is_sla_violated=False,
            is_paused=False,
            is_active=True,
        )

        assert all(isinstance(value, str) for value in dimensions.values())
