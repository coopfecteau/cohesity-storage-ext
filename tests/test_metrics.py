"""Metric keys and id namespacing.

Pulling numbers out of a response body moved to cohesity_storage.domain in ticket 07; this
module is now only the Dynatrace-facing naming contract.
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
            metrics.storage_domain_dimensions(1001, "east", 4),
            metrics.node_dimensions(1001, "east", 7),
            metrics.protection_group_dimensions(1001, "east", "9:123"),
        ):
            assert dimensions[metrics.DIM_CLUSTER_ID] == "1001"
            assert dimensions[metrics.DIM_CLUSTER_NAME] == "east"


class TestMetricKeys:
    def test_no_entity_prefix_is_a_prefix_of_another(self):
        # Prefix is what binds a metric to an entity, so an overlap would hang one entity's
        # metrics off another's topology rule.
        prefixes = [
            metrics.PREFIX_CLUSTER,
            metrics.PREFIX_STORAGE_DOMAIN,
            metrics.PREFIX_NODE,
            metrics.PREFIX_PROTECTION_GROUP,
        ]
        for prefix in prefixes:
            others = [other for other in prefixes if other != prefix]
            assert not [other for other in others if other.startswith(f"{prefix}.")]

    def test_keys_sit_under_their_entity_prefix(self):
        assert metrics.CLUSTER_TOTAL_CAPACITY_BYTES.startswith(f"{metrics.PREFIX_CLUSTER}.")
        assert metrics.CLUSTER_COLLECTION_SUCCESS.startswith(f"{metrics.PREFIX_CLUSTER}.")
