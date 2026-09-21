"""extension.yaml, the activation schema and the OpenPipeline config, checked against the code.

None of this is validated locally by `dt-sdk build` - it zips and signs whatever is in
`extension/`. The first feedback on a mistake here is a rejected upload, or worse, a silent
one: a metric declared under a key nothing emits ingests nothing, and a node type without an
EXT_ prefix is refused by a rule the published settings schema never mentions.

So these are the checks that would otherwise be a round trip to a tenant.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import yaml

from cohesity_storage import metrics

EXTENSION_DIR = Path(__file__).resolve().parent.parent / "extension"
PIPELINE_PATH = EXTENSION_DIR / "openpipeline" / "metrics.pipeline.json"
SOURCE_PATH = EXTENSION_DIR / "openpipeline" / "metrics.source.json"

NODE_TYPES = {
    "EXT_COHESITY_CLUSTER": "dt.smartscape.cohesity_cluster",
    "EXT_COHESITY_STORAGE_DOMAIN": "dt.smartscape.cohesity_storage_domain",
    "EXT_COHESITY_PROTECTION_GROUP": "dt.smartscape.cohesity_protection_group",
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    return yaml.safe_load((EXTENSION_DIR / "extension.yaml").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def pipeline() -> dict:
    return json.loads(PIPELINE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def activation_schema() -> dict:
    return json.loads((EXTENSION_DIR / "activationSchema.json").read_text(encoding="utf-8"))


def node_processors(pipeline: dict) -> list[dict]:
    return pipeline["smartscapeNodeExtraction"]["processors"]


def edge_processors(pipeline: dict) -> list[dict]:
    return pipeline["smartscapeEdgeExtraction"]["processors"]


class TestManifest:
    def test_the_committed_version_is_a_release_not_an_e2e_dev_build(self, manifest):
        # e2e/loop.py rewrites the version to 0.99.<minutes> for each dev build and restores it
        # afterwards. If that restore ever failed, a dev version would ride into a commit - and
        # from there into a release. Pinning a literal here instead broke on every bump.
        version = str(manifest["version"])
        assert re.fullmatch(r"\d+\.\d+\.\d+", version), version
        assert not version.startswith("0.99."), f"e2e dev version committed: {version}"

    def test_local_activation_only_requests_feature_sets_that_exist(self, manifest):
        # activation.json once asked for "cluster" after the sets were renamed. The local SDK
        # tolerated it; a real EEC may refuse a monitoring config naming an unknown set.
        declared = {entry["featureSet"] for entry in manifest["python"]["featureSets"]}
        activation = json.loads((EXTENSION_DIR.parent / "activation.json").read_text(encoding="utf-8"))

        assert set(activation["featureSets"]) <= declared

    def test_every_declared_key_is_one_the_code_can_emit(self, manifest):
        declared = {entry["key"] for entry in manifest["metrics"]}

        assert declared == set(metrics.ALL_METRIC_KEYS)

    def test_every_key_carries_a_unit_display_name_and_description(self, manifest):
        for entry in manifest["metrics"]:
            metadata = entry["metadata"]
            for field in ("unit", "displayName", "description"):
                assert metadata.get(field), f"{entry['key']} is missing {field}"

    def test_latency_is_declared_in_microseconds(self, manifest):
        units = {entry["key"]: entry["metadata"]["unit"] for entry in manifest["metrics"]}

        # Sub-millisecond is the signal on flash. MilliSecond here would round it away.
        assert units["cohesity.cluster.io.latency"] == "MicroSecond"

    def test_byte_and_percent_units_are_right(self, manifest):
        units = {entry["key"]: entry["metadata"]["unit"] for entry in manifest["metrics"]}

        assert units["cohesity.cluster.capacity.total"] == "Byte"
        assert units["cohesity.cluster.cpu.usage"] == "Percent"
        assert units["cohesity.protectiongroup.run.duration"] == "MilliSecond"
        assert units["cohesity.protectiongroup.last_success.age"] == "MilliSecond"
        assert units["cohesity.protectiongroup.run.outcome"] == "Count"

    def test_every_key_is_in_exactly_one_feature_set(self, manifest):
        assigned = []
        for feature_set in manifest["python"]["featureSets"]:
            assigned.extend(entry["key"] for entry in feature_set["metrics"])

        assert sorted(assigned) == sorted(metrics.ALL_METRIC_KEYS)

    def test_self_monitoring_and_cluster_metrics_live_in_the_always_on_set(self, manifest):
        default = next(
            feature_set
            for feature_set in manifest["python"]["featureSets"]
            if feature_set["featureSet"] == "default"
        )
        keys = {entry["key"] for entry in default["metrics"]}

        # "default" cannot be switched off, which is where the only signal separating "Cohesity
        # is healthy" from "the extension is broken" has to live.
        assert metrics.CLUSTER_COLLECTION_SUCCESS in keys
        assert metrics.CLUSTER_CAPACITY_TOTAL in keys

    def test_feature_sets_mirror_the_activation_toggles(self, manifest, activation_schema):
        """One switch, not two (ticket 06).

        Every feature set other than the always-on default must have a matching per-cluster
        toggle, and every toggle must have a feature set. Two switches that can disagree
        produce "metric missing, both switches look fine".
        """
        feature_sets = {
            feature_set["featureSet"]
            for feature_set in manifest["python"]["featureSets"]
            if feature_set["featureSet"] != "default"
        }
        endpoint = activation_schema["types"][
            "dynatrace.datasource.python:cohesity-cluster-endpoint"
        ]["properties"]
        toggles = {
            name for name in endpoint if name.startswith("collect")
        }

        expected = {f"collect{''.join(part.title() for part in name.split('_'))}" for name in feature_sets}
        assert expected == toggles

    def test_the_dead_node_toggle_is_gone(self, activation_schema):
        # It switched nothing: Node is not a v1 entity and no metric is collected for it.
        endpoint = activation_schema["types"][
            "dynatrace.datasource.python:cohesity-cluster-endpoint"
        ]["properties"]

        assert "collectNodes" not in endpoint

    def test_no_nullable_property_declares_an_empty_default(self, activation_schema):
        # Dynatrace rejects `"default": ""` on a nullable property - commit e59287a fixed
        # exactly this, and it costs an upload round trip to rediscover.
        endpoint = activation_schema["types"][
            "dynatrace.datasource.python:cohesity-cluster-endpoint"
        ]["properties"]

        for name, prop in endpoint.items():
            if prop.get("nullable"):
                assert "default" not in prop, name

    def test_topology_is_openpipeline_not_the_deprecated_classic_section(self, manifest):
        assert "openpipeline" in manifest
        # The classic `topology:` path is deprecated since 1.334, and on Gen3 there is no REST
        # entity-creation API either.
        assert "topology" not in manifest

    def test_the_openpipeline_files_are_referenced_and_present(self, manifest):
        openpipeline = manifest["openpipeline"]

        assert openpipeline["sources"][0]["configScope"] == "metrics"
        assert openpipeline["pipelines"][0]["configScope"] == "metrics"
        assert (EXTENSION_DIR / openpipeline["sources"][0]["sourcePath"]).is_file()
        assert (EXTENSION_DIR / openpipeline["pipelines"][0]["pipelinePath"]).is_file()


class TestPipelineSource:
    def test_the_source_routes_this_extension_to_this_pipeline(self, manifest, pipeline):
        source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))

        assert source["sourceType"] == "extension"
        assert source["source"] == manifest["name"]
        # A routing entry that names a pipeline id nothing declares silently routes nowhere.
        assert source["staticRouting"]["pipelineId"] == pipeline["customId"]
        assert source["enabled"] is True


class TestSmartscapeNodes:
    def test_the_three_v1_node_types_are_declared(self, pipeline):
        types = {
            processor["smartscapeNode"]["nodeType"]
            for processor in node_processors(pipeline)
        }

        assert types == set(NODE_TYPES)

    def test_every_node_type_carries_the_mandatory_ext_prefix(self, pipeline):
        # Bare COHESITY_* is rejected server-side by a rule the published settings schema never
        # mentions: "Must start with one of ['CUSTOM_, EXT_']". Ticket 02.
        for processor in node_processors(pipeline):
            assert processor["smartscapeNode"]["nodeType"].startswith("EXT_")

    def test_each_node_type_always_uses_the_same_id_field(self, pipeline):
        for processor in node_processors(pipeline):
            node = processor["smartscapeNode"]
            assert node["nodeIdFieldName"] == NODE_TYPES[node["nodeType"]]

    def test_identity_is_ids_only_never_names(self, pipeline):
        # Storage domains and protection groups can both be renamed. A name in the identity
        # orphans the entity and silently creates a second one.
        for processor in node_processors(pipeline):
            for component in processor["smartscapeNode"]["idComponents"]:
                assert component["referencedFieldName"].endswith(".id"), processor["id"]

    def test_id_components_match_the_entity_model(self, pipeline):
        components = {}
        for processor in node_processors(pipeline):
            node = processor["smartscapeNode"]
            components[node["nodeType"]] = tuple(
                entry["idComponent"] for entry in node["idComponents"]
            )

        assert components["EXT_COHESITY_CLUSTER"] == ("cluster_id",)
        assert components["EXT_COHESITY_STORAGE_DOMAIN"] == ("cluster_id", "storagedomain_id")
        assert components["EXT_COHESITY_PROTECTION_GROUP"] == (
            "cluster_id",
            "protectiongroup_id",
        )

    def test_every_id_component_references_a_dimension_the_code_emits(self, pipeline):
        emitted = {
            metrics.DIM_CLUSTER_ID,
            metrics.DIM_STORAGE_DOMAIN_ID,
            metrics.DIM_PROTECTION_GROUP_ID,
        }
        for processor in node_processors(pipeline):
            for component in processor["smartscapeNode"]["idComponents"]:
                assert component["referencedFieldName"] in emitted, processor["id"]

    def test_each_owned_node_type_has_exactly_one_extracting_rule(self, pipeline):
        extracting = [
            processor["smartscapeNode"]["nodeType"]
            for processor in node_processors(pipeline)
            if processor["smartscapeNode"].get("extractNode")
        ]

        # We own all three, so each is created once. A second extracting rule for the same type
        # would upsert the node from a metric that does not carry its name.
        assert sorted(extracting) == sorted(NODE_TYPES)

    def test_node_names_come_from_a_name_dimension_with_a_fallback(self, pipeline):
        for processor in node_processors(pipeline):
            node = processor["smartscapeNode"]
            if not node.get("extractNode"):
                continue
            name = node["nodeName"]
            assert name["type"] == "field"
            assert name["field"]["sourceFieldName"].endswith(".name")
            # Without a default, a metric arriving before the name dimension does would create
            # a nameless node.
            assert name["field"]["defaultValue"]

    def test_the_storage_domain_is_only_named_from_its_own_metrics(self, pipeline):
        # Protection group metrics carry the domain id but not its name, so extracting the node
        # from them would rename every domain to the default.
        processor = next(
            processor
            for processor in node_processors(pipeline)
            if processor["smartscapeNode"]["nodeType"] == "EXT_COHESITY_STORAGE_DOMAIN"
            and processor["smartscapeNode"].get("extractNode")
        )

        assert f"{metrics.PREFIX_STORAGE_DOMAIN}." in processor["matcher"]

    def test_the_protection_group_rule_does_not_require_a_storage_domain(self, pipeline):
        # A protection group with no storage domain should still exist as an entity, just
        # without the edge. Requiring it would make a missing field delete the entity.
        processor = next(
            processor
            for processor in node_processors(pipeline)
            if processor["smartscapeNode"]["nodeType"] == "EXT_COHESITY_PROTECTION_GROUP"
        )

        assert metrics.DIM_STORAGE_DOMAIN_ID not in processor["matcher"]

    def test_every_matcher_guards_on_the_ids_that_rule_needs(self, pipeline):
        # This is what requiredDimensions buys in classic topology: a partial poll must not
        # mint a phantom entity.
        for processor in node_processors(pipeline):
            for component in processor["smartscapeNode"]["idComponents"]:
                field = component["referencedFieldName"]
                assert f"isNotNull({field})" in processor["matcher"], processor["id"]


class TestSmartscapeEdges:
    def test_the_three_v1_edges_exist(self, pipeline):
        edges = {
            (
                processor["smartscapeEdge"]["sourceType"],
                processor["smartscapeEdge"]["edgeType"],
                processor["smartscapeEdge"]["targetType"],
            )
            for processor in edge_processors(pipeline)
        }

        assert edges == {
            ("EXT_COHESITY_CLUSTER", "contains", "EXT_COHESITY_STORAGE_DOMAIN"),
            ("EXT_COHESITY_CLUSTER", "contains", "EXT_COHESITY_PROTECTION_GROUP"),
            ("EXT_COHESITY_PROTECTION_GROUP", "writes_to", "EXT_COHESITY_STORAGE_DOMAIN"),
        }

    def test_edge_types_are_lowercase_and_within_the_length_limit(self, pipeline):
        for processor in edge_processors(pipeline):
            edge_type = processor["smartscapeEdge"]["edgeType"]
            assert edge_type == edge_type.lower()
            assert len(edge_type) <= 32

    def test_edge_attributes_are_exactly_the_five_the_schema_accepts(self, pipeline):
        for processor in edge_processors(pipeline):
            assert set(processor["smartscapeEdge"]) == {
                "sourceType",
                "sourceIdFieldName",
                "edgeType",
                "targetType",
                "targetIdFieldName",
            }

    def test_edges_reference_node_id_fields_a_node_rule_produces(self, pipeline):
        produced = {
            processor["smartscapeNode"]["nodeIdFieldName"]
            for processor in node_processors(pipeline)
        }

        for processor in edge_processors(pipeline):
            edge = processor["smartscapeEdge"]
            assert edge["sourceIdFieldName"] in produced
            assert edge["targetIdFieldName"] in produced
            assert edge["sourceIdFieldName"] == NODE_TYPES[edge["sourceType"]]
            assert edge["targetIdFieldName"] == NODE_TYPES[edge["targetType"]]

    def test_the_writes_to_edge_has_a_target_id_resolver(self, pipeline):
        """The edge needs the domain node id computed from a protection group metric.

        A non-extracting node rule is the only way to get it: targetIdFieldName refers to a
        smartscape node-id field, not to a raw dimension, and the extracting storage domain
        rule never sees a protection group metric.
        """
        resolvers = [
            processor
            for processor in node_processors(pipeline)
            if processor["smartscapeNode"]["nodeType"] == "EXT_COHESITY_STORAGE_DOMAIN"
            and not processor["smartscapeNode"].get("extractNode")
        ]

        assert len(resolvers) == 1
        assert metrics.PREFIX_PROTECTION_GROUP in resolvers[0]["matcher"]

    def test_the_writes_to_edge_requires_both_ends(self, pipeline):
        processor = next(
            processor
            for processor in edge_processors(pipeline)
            if processor["smartscapeEdge"]["edgeType"] == "writes_to"
        )

        assert f"isNotNull({metrics.DIM_PROTECTION_GROUP_ID})" in processor["matcher"]
        assert f"isNotNull({metrics.DIM_STORAGE_DOMAIN_ID})" in processor["matcher"]


class TestProcessorHygiene:
    def all_processors(self, pipeline: dict) -> list[dict]:
        return node_processors(pipeline) + edge_processors(pipeline)

    def test_processor_ids_are_unique(self, pipeline):
        ids = [processor["id"] for processor in self.all_processors(pipeline)]

        assert len(ids) == len(set(ids))

    def test_processor_ids_fit_the_server_side_constraints(self, pipeline):
        for processor in self.all_processors(pipeline):
            identifier = processor["id"]
            assert 4 <= len(identifier) <= 100, identifier
            # Reserved namespaces - the server refuses them.
            assert not identifier.startswith("dt.")
            assert not identifier.startswith("dynatrace.")

    def test_every_processor_has_a_non_blank_description(self, pipeline):
        # The schema marks description NOT_BLANK, and an unexplained topology rule is the kind
        # of thing that survives three years because nobody dares delete it.
        for processor in self.all_processors(pipeline):
            assert processor["description"].strip()

    def test_every_processor_is_enabled_and_typed(self, pipeline):
        for processor in node_processors(pipeline):
            assert processor["enabled"] is True
            assert processor["type"] == "smartscapeNode"
        for processor in edge_processors(pipeline):
            assert processor["enabled"] is True
            assert processor["type"] == "smartscapeEdge"

    def test_matchers_only_reference_dimensions_the_extension_emits(self, pipeline):
        """A matcher naming a field nothing sends matches nothing, silently and forever."""
        emitted = {
            metrics.DIM_CLUSTER_ID,
            metrics.DIM_CLUSTER_NAME,
            metrics.DIM_STORAGE_DOMAIN_ID,
            metrics.DIM_STORAGE_DOMAIN_NAME,
            metrics.DIM_PROTECTION_GROUP_ID,
            metrics.DIM_PROTECTION_GROUP_NAME,
        }
        for processor in self.all_processors(pipeline):
            for fragment in processor["matcher"].split("isNotNull(")[1:]:
                field = fragment.split(")")[0]
                assert field in emitted, f"{processor['id']} guards on unknown field {field}"

    def test_matchers_only_reference_declared_metric_prefixes(self, pipeline):
        prefixes = (
            metrics.PREFIX_CLUSTER,
            metrics.PREFIX_STORAGE_DOMAIN,
            metrics.PREFIX_PROTECTION_GROUP,
        )
        for processor in self.all_processors(pipeline):
            for fragment in processor["matcher"].split('matchesValue(metric.key, "')[1:]:
                pattern = fragment.split('"')[0]
                assert pattern.rstrip(".*") in prefixes, processor["id"]
