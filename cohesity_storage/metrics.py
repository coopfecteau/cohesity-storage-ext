"""Metric keys, dimensions, and the payload-to-number mapping.

Two rules drive everything here, both learned the hard way on earlier extensions:

*Prefix is topology.* A metric binds to an entity by its key prefix
(``condition: $prefix(cohesity.storage_domain)``), so the prefix of a key decides which
entity the metric will hang off once topology rules land. Keys are named
``cohesity.<entity>.<measure>`` for that reason, not for readability, and no prefix may be
a prefix of another entity's.

*Ids, never names.* Entity identity comes from the cluster's own ids. Cohesity ids are
cluster-scoped int64s and will collide the moment a second cluster is added, so every id
that leaves this module is namespaced ``{clusterId}_{objectId}`` even though v1 monitors a
single cluster.
"""

from __future__ import annotations

from .domain import namespace_id

# One prefix per entity type. Adding a measure under one of these is a topology decision.
PREFIX_CLUSTER = "cohesity.cluster"
PREFIX_STORAGE_DOMAIN = "cohesity.storage_domain"
PREFIX_NODE = "cohesity.node"
PREFIX_PROTECTION_GROUP = "cohesity.protection_group"

CLUSTER_TOTAL_CAPACITY_BYTES = f"{PREFIX_CLUSTER}.total_capacity_bytes"
CLUSTER_COLLECTION_SUCCESS = f"{PREFIX_CLUSTER}.collection_success"

# SEAM (ticket 04): the rest of the metric set lands here - storage domain usage, node
# capacity and health, protection group run outcomes. Declare each new key in
# extension/extension.yaml under both `metrics:` and a feature set, or the EEC drops it
# silently.

# Dimension keys. These are what a later topology rule and any detector's `by: {}` clause
# will match on, so they are part of the contract, not decoration.
DIM_CLUSTER_ID = "cohesity.cluster.id"
DIM_CLUSTER_NAME = "cohesity.cluster.name"
DIM_STORAGE_DOMAIN_ID = "cohesity.storage_domain.id"
DIM_NODE_ID = "cohesity.node.id"
DIM_PROTECTION_GROUP_ID = "cohesity.protection_group.id"

def entity_id(cluster_id: str | int, object_id: str | int) -> str:
    """Namespace a cluster-scoped Cohesity id so it stays unique across clusters.

    One implementation, in :func:`cohesity_storage.domain.namespace_id`, because the client
    namespaces ids too and two spellings of this rule would eventually disagree - at which point
    half the metrics would hang off a second copy of every entity.
    """
    return namespace_id(cluster_id, object_id)


def cluster_dimensions(cluster_id: str | int, cluster_name: str) -> dict[str, str]:
    """Dimensions carried by every metric, whatever entity it belongs to.

    The cluster id is not namespaced: it *is* the namespace.
    """
    return {
        DIM_CLUSTER_ID: str(cluster_id),
        DIM_CLUSTER_NAME: cluster_name,
    }


def storage_domain_dimensions(
    cluster_id: str | int, cluster_name: str, storage_domain_id: str | int
) -> dict[str, str]:
    dimensions = cluster_dimensions(cluster_id, cluster_name)
    dimensions[DIM_STORAGE_DOMAIN_ID] = entity_id(cluster_id, storage_domain_id)
    return dimensions


def node_dimensions(cluster_id: str | int, cluster_name: str, node_id: str | int) -> dict[str, str]:
    dimensions = cluster_dimensions(cluster_id, cluster_name)
    dimensions[DIM_NODE_ID] = entity_id(cluster_id, node_id)
    return dimensions


def protection_group_dimensions(
    cluster_id: str | int, cluster_name: str, protection_group_id: str | int
) -> dict[str, str]:
    dimensions = cluster_dimensions(cluster_id, cluster_name)
    dimensions[DIM_PROTECTION_GROUP_ID] = entity_id(cluster_id, protection_group_id)
    return dimensions
