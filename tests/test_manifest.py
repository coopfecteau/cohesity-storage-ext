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
from tests.test_reporting import replay_client, replay_samples

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


@pytest.fixture(scope="module")
def emitted_dimensions() -> set[str]:
    """Every dimension key a full replay poll actually sends - not what the constants claim."""
    return {name for sample in replay_samples(replay_client()) for name in sample.dimensions}


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
        # Alerts are the exception, and a deliberate one: feature sets gate METRICS, and the
        # alert collection emits log records. There is no metric to list, so there is no
        # feature set to mirror - its only switch is the toggle itself.
        log_only = {"collectAlerts"}
        toggles = {
            name
            for name in endpoint
            if name.startswith("collect") and name not in log_only
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

    def test_containment_uses_the_built_in_edge_type(self, pipeline):
        """0.1.5 renamed both containment edges, and it should not have.

        The rename was made on a DQL reading: ``smartscapeEdges "contains" | filter
        startsWith(source_id, "EXT_COHESITY")`` returned 0. So does the same query for
        ``writes_to``, which demonstrably has 42 edges - the ``startsWith`` filter on
        ``source_id`` in ``smartscapeEdges`` silently matches nothing. Counting the unfiltered
        rows instead shows ``contains`` carrying 51 Cohesity edges and always having done.

        Asserted as the intended edge type rather than as a blocklist, because the blocklist
        encoded the wrong conclusion: a built-in edge type is fine here, and the two spellings
        coexisting is what produced duplicate edges for one relationship.
        """
        containment = [
            processor
            for processor in edge_processors(pipeline)
            if processor["smartscapeEdge"]["sourceType"] == "EXT_COHESITY_CLUSTER"
        ]

        assert len(containment) == 2
        for processor in containment:
            assert processor["smartscapeEdge"]["edgeType"] == "contains", processor["id"]

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

    def test_matchers_only_reference_dimensions_the_extension_emits(self, pipeline, emitted_dimensions):
        """A matcher naming a field nothing sends matches nothing, silently and forever."""
        for processor in self.all_processors(pipeline):
            for fragment in processor["matcher"].split("isNotNull(")[1:]:
                field = fragment.split(")")[0]
                assert field in emitted_dimensions, f"{processor['id']} guards on unknown field {field}"

    def test_every_field_a_node_rule_reads_is_a_dimension_the_extension_emits(
        self, pipeline, emitted_dimensions
    ):
        """fieldsToExtract naming a field nothing sends extracts nothing - and says nothing.

        This is what the camelCase rename would have broken silently: the Python moving to
        ``cohesity.protectiongroup.paused`` while the pipeline still read ``isPaused``.
        """
        for processor in node_processors(pipeline):
            node = processor["smartscapeNode"]
            referenced = [entry["referencedFieldName"] for entry in node["idComponents"]]
            referenced += [entry["referencedFieldName"] for entry in node.get("fieldsToExtract", [])]
            if node.get("nodeName", {}).get("type") == "field":
                referenced.append(node["nodeName"]["field"]["sourceFieldName"])
            for name in referenced:
                assert name in emitted_dimensions, f"{processor['id']} reads unknown field {name}"

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


@pytest.fixture(scope="module")
def dashboard(manifest) -> dict:
    """The dashboard document as the tenant will parse it.

    Packaged dashboards are the dashboard *content* - version, variables, tiles, layouts -
    not the `{name, type, content}` envelope the Document Store API returns. Loading it here
    is itself half the test: a trailing comma ships silently and fails on install.
    """
    entry = manifest["documents"]["dashboards"][0]
    return json.loads((EXTENSION_DIR / entry["path"]).read_text(encoding="utf-8"))


def dashboard_queries(dashboard: dict) -> dict[str, str]:
    return {
        tile_id: tile["query"]
        for tile_id, tile in dashboard["tiles"].items()
        if tile.get("type") == "data"
    }



class TestDashboard:
    def test_it_ships_under_the_modern_documents_key(self, manifest):
        # `dashboards:` is Dashboards Classic, deprecated since January 2026 and targeted for
        # removal from SaaS by end of 2027. On a Gen3 tenant it may install nothing at all -
        # silently, which is the expensive part.
        assert "dashboards" not in manifest
        entry = manifest["documents"]["dashboards"][0]

        assert entry["displayName"]
        assert entry["path"].endswith(".dashboard.json")
        # The 10-per-extension ceiling.
        assert len(manifest["documents"]["dashboards"]) <= 10

    def test_the_referenced_file_exists_and_is_valid_json(self, manifest, dashboard):
        assert (EXTENSION_DIR / manifest["documents"]["dashboards"][0]["path"]).is_file()
        # The `dashboard` fixture already parsed it; assert the shape the tenant needs.
        assert dashboard["tiles"]
        assert isinstance(dashboard["version"], int)

    def test_every_tile_has_a_layout_and_every_layout_a_tile(self, dashboard):
        # A tile with no layout entry never renders; a layout with no tile is a blank hole.
        assert set(dashboard["tiles"]) == set(dashboard["layouts"])

    def test_tiles_fit_the_grid(self, dashboard):
        columns = dashboard.get("settings", {}).get("gridLayout", {}).get("columnsCount", 24)
        for tile_id, layout in dashboard["layouts"].items():
            assert layout["x"] + layout["w"] <= columns, tile_id
            assert layout["h"] > 0 and layout["w"] > 0, tile_id

    def test_every_data_tile_has_a_query_and_a_visualization(self, dashboard):
        for tile_id, tile in dashboard["tiles"].items():
            if tile.get("type") != "data":
                continue
            assert tile["query"].strip(), tile_id
            assert tile["visualization"], tile_id

    def test_every_metric_key_referenced_is_one_the_extension_emits(self, dashboard):
        """A tile querying a key nothing ingests renders empty forever, with no error.

        Same failure mode as an undeclared key in the manifest, one layer further out, and
        the reason this test reuses ``metrics.ALL_METRIC_KEYS`` rather than keeping its own
        list to drift out of step.
        """
        emitted = set(metrics.ALL_METRIC_KEYS)
        seen = set()
        for tile_id, query in dashboard_queries(dashboard).items():
            # A metric key only ever appears as the first argument of a timeseries
            # aggregation; everything else spelled `cohesity.*` is a dimension.
            keys = re.findall(
                r"\b(?:sum|avg|min|max|count|median|percentile)\(\s*(cohesity[A-Za-z0-9_.]+)",
                query,
            )
            for key in keys:
                assert key in emitted, f"tile {tile_id} queries unknown metric {key}"
            seen.update(keys)

        assert seen, "no tile queries any metric at all"

    def test_every_cohesity_token_in_a_query_is_a_real_key_or_dimension(
        self, dashboard, emitted_dimensions
    ):
        known = set(metrics.ALL_METRIC_KEYS) | set(emitted_dimensions)
        for tile_id, query in dashboard_queries(dashboard).items():
            for token in re.findall(r"cohesity(?:\.[A-Za-z0-9_]+)+", query):
                assert token in known, f"tile {tile_id} references unknown {token}"

    def test_no_query_uses_a_retired_camelcase_dimension(self, dashboard):
        """``isPaused``/``isActive``/``isSlaViolated`` were never valid and are gone.

        The ingest protocol only accepts lowercase dimension keys - a single uppercase letter
        made the whole metric line invalid, which is what silently dropped every
        protection-group line up to v0.1.1. A dashboard is the easiest place for the old
        spelling to survive, because a query against a dimension nobody sends just renders
        blank.
        """
        retired = ("isPaused", "isActive", "isSlaViolated")
        for tile_id, query in dashboard_queries(dashboard).items():
            for name in retired:
                assert name not in query, f"tile {tile_id} uses retired dimension {name}"
            for token in re.findall(r"cohesity(?:\.[A-Za-z0-9_]+)+", query):
                assert token == token.lower(), f"tile {tile_id} uses mixed-case {token}"

    def test_run_outcome_is_summed_never_averaged(self, dashboard):
        # It is a delta counter of runs reaching a terminal status. `avg` over it answers a
        # question nobody asked and hides a second failure in the same interval.
        for tile_id, query in dashboard_queries(dashboard).items():
            if metrics.PROTECTION_GROUP_RUN_OUTCOME not in query:
                continue
            assert f"avg({metrics.PROTECTION_GROUP_RUN_OUTCOME}" not in query, tile_id
            assert f"sum({metrics.PROTECTION_GROUP_RUN_OUTCOME}" in query, tile_id

    def test_used_percent_is_computed_not_fetched(self, dashboard):
        # Ticket 06 dropped the ingested derivative on purpose: it drifts from its inputs the
        # moment one poll succeeds and the other fails. So no tile may resurrect the key, and
        # at least one must do the division itself.
        queries = dashboard_queries(dashboard)
        for tile_id, query in queries.items():
            assert "capacity.used_pct" not in query, tile_id

        computed = [
            query
            for query in queries.values()
            if metrics.CLUSTER_CAPACITY_USED in query and metrics.CLUSTER_CAPACITY_TOTAL in query
        ]
        assert computed, "no tile computes used % from used / total"

    def test_no_tile_pins_its_own_timeframe(self, dashboard):
        # The dashboard time picker owns the timeframe. A hardcoded `from:` silently ignores
        # whatever the user selected.
        for tile_id, query in dashboard_queries(dashboard).items():
            assert "from:" not in query, tile_id
            assert "timeframe:" not in query.replace("| fields timeframe", ""), tile_id


HOST_LINK_PATH = EXTENSION_DIR / "openpipeline" / "host-link.pipeline.json"

# The metric the host-link pipeline is routed on. Dynatrace's, not ours - which is the whole
# reason that pipeline is a second settings object rather than a stage of the first one.
HOST_METRIC_KEY = "dt.host.cpu.usage"


@pytest.fixture(scope="module")
def host_link() -> dict:
    return json.loads(HOST_LINK_PATH.read_text(encoding="utf-8"))


def lookup_processors(host_link: dict) -> list[dict]:
    return host_link["processing"]["processors"]


class TestHostLinkPipeline:
    """The second pipeline (ticket 16), which runs on HOST metrics rather than on ours.

    It is a separate settings object on purpose: the extension's own source statically routes
    the extension's own metrics to the first pipeline, and a host metric never enters it.
    Reaching host metrics needs a routing entry on the built-in ingest, which an extension
    package cannot create - so this one ships inert and is wired up by hand.
    """

    def test_it_is_declared_in_the_manifest_and_present_on_disk(self, manifest):
        declared = {
            entry["pipelinePath"] for entry in manifest["openpipeline"]["pipelines"]
        }

        assert "openpipeline/host-link.pipeline.json" in declared
        assert HOST_LINK_PATH.is_file()
        for entry in manifest["openpipeline"]["pipelines"]:
            assert entry["configScope"] == "metrics"

    def test_its_custom_id_does_not_collide_with_the_metrics_pipeline(self, pipeline, host_link):
        # Two pipelines sharing a customId is one pipeline, silently.
        assert host_link["customId"] != pipeline["customId"]

    def test_nothing_routes_this_extensions_own_metrics_into_it(self, host_link):
        """The extension's source must keep pointing at the metrics pipeline.

        Routing our own metrics here instead would silently stop every Cohesity entity from
        being created - the node rules live in the other file.
        """
        source = json.loads(SOURCE_PATH.read_text(encoding="utf-8"))

        assert source["staticRouting"]["pipelineId"] != host_link["customId"]

    def test_it_ships_an_empty_lookup_table_so_it_is_inert_until_populated(self, host_link):
        processors = lookup_processors(host_link)

        assert processors, "no inlineLookup processor to populate"
        for processor in processors:
            assert processor["type"] == "inlineLookup"
            assert "__cohesity_host_link_unpopulated__" in processor["inlineLookup"]["inlineLookupTable"]
            # No defaultValue: a host with no Cohesity backup must be left completely
            # untouched rather than given a placeholder that draws an edge to nothing.
            assert "defaultValue" not in processor["inlineLookup"]

    def test_both_lookups_key_on_the_host_smartscape_id(self, host_link):
        # The one field on a host metric that identifies the entity the edge points at, and the
        # only thing a workflow can populate a table with.
        for processor in lookup_processors(host_link):
            assert processor["inlineLookup"]["sourceField"] == "dt.smartscape.host"

    def test_every_field_the_rules_read_is_one_a_lookup_creates(self, host_link):
        """The host-link equivalent of the emitted-dimensions check on the other pipeline.

        Its node and edge rules cannot read `cohesity.*` dimensions - those ride on Cohesity
        metrics, and this pipeline only ever sees host metrics. Everything they read must
        therefore be produced by an inlineLookup in this same file, or be a built-in host
        field. A name that is neither matches nothing, silently and forever.
        """
        produced = {
            processor["inlineLookup"]["destinationField"]
            for processor in lookup_processors(host_link)
        } | {"dt.smartscape.host"}

        for processor in node_processors(host_link) + edge_processors(host_link):
            for fragment in processor["matcher"].split("isNotNull(")[1:]:
                assert fragment.split(")")[0] in produced, processor["id"]
            for entry in processor.get("smartscapeNode", {}).get("idComponents", []):
                assert entry["referencedFieldName"] in produced, processor["id"]

    def test_the_lookup_values_are_dimensions_the_python_actually_emits(self, host_link):
        """A table is only fillable if the extension publishes what goes in it.

        The two destination fields correspond one-for-one to the bridge metric's two identity
        dimensions; the workflow copies those values across verbatim. If the Python renamed
        one, there would be nothing to put in the table and no error to say so.
        """
        destinations = {
            processor["inlineLookup"]["destinationField"]
            for processor in lookup_processors(host_link)
        }

        assert destinations == {
            metrics.DIM_CLUSTER_ID.replace(".", "_"),
            metrics.DIM_PROTECTION_GROUP_ID.replace(".", "_"),
        }

    def test_the_computed_node_id_matches_the_extensions_own_rule_exactly(
        self, pipeline, host_link
    ):
        """Same node type, same id field, same components, same ORDER.

        This is the one thing that cannot be checked on a tenant without looking very closely:
        a different component name or order produces a DIFFERENT id, and a different id
        resolves to a second, empty copy of the protection group rather than failing.
        """
        ours = next(
            processor["smartscapeNode"]
            for processor in node_processors(pipeline)
            if processor["id"] == "cohesity-protection-group-node"
        )
        theirs = next(
            processor["smartscapeNode"]
            for processor in node_processors(host_link)
            if processor["smartscapeNode"]["nodeType"] == "EXT_COHESITY_PROTECTION_GROUP"
        )

        assert theirs["nodeType"] == ours["nodeType"]
        assert theirs["nodeIdFieldName"] == ours["nodeIdFieldName"]
        assert [entry["idComponent"] for entry in theirs["idComponents"]] == [
            entry["idComponent"] for entry in ours["idComponents"]
        ]

    def test_it_resolves_the_protection_group_rather_than_minting_a_second_one(self, host_link):
        for processor in node_processors(host_link):
            assert processor["smartscapeNode"]["extractNode"] is False

    def test_no_rule_here_extracts_a_host(self, host_link):
        # Dynatrace owns HOST. Minting our own copy is the failure ticket 16 was written to
        # avoid, and extractNode:false on our own type is only half of avoiding it.
        for processor in node_processors(host_link):
            assert processor["smartscapeNode"]["nodeType"] != "HOST"

    def test_the_edge_runs_from_the_protection_group_to_the_host(self, host_link):
        """Direction, stated once so it cannot drift.

        'protects' reads with the actor first - the job protects the machine - and custom
        source to built-in target is the direction whose server-side acceptance is
        established. Reversed, HOST would sit in the source position, which is unverified.
        """
        edge = edge_processors(host_link)[0]["smartscapeEdge"]

        assert edge["sourceType"] == "EXT_COHESITY_PROTECTION_GROUP"
        assert edge["edgeType"] == "protects"
        assert edge["targetType"] == "HOST"
        assert edge["targetIdFieldName"] == "dt.smartscape.host"

    def test_the_edge_type_fits_the_server_side_limit(self, host_link):
        edge_type = edge_processors(host_link)[0]["smartscapeEdge"]["edgeType"]

        assert edge_type == edge_type.lower()
        assert len(edge_type) <= 32

    def test_every_matcher_is_scoped_to_a_host_metric(self, host_link):
        """Not to a Cohesity one. This pipeline never sees a Cohesity metric, and a matcher
        naming one would make the whole pipeline dead weight on the host ingest path."""
        for stage in ("processing", "smartscapeNodeExtraction", "smartscapeEdgeExtraction"):
            for processor in host_link[stage]["processors"]:
                assert f'matchesValue(metric.key, "{HOST_METRIC_KEY}")' in processor["matcher"]

    def test_the_routing_sample_matches_on_that_same_metric_and_nothing_else(self):
        """The routing matcher must NOT reference a field the pipeline itself creates.

        Gating entry on `cohesity_protectiongroup_id` means no record ever arrives, the lookup
        never runs, and the symptom is a lookup table that "did not work".
        """
        routing = json.loads(
            (EXTENSION_DIR.parent / "dynatrace" / "routing-host-link.json").read_text(
                encoding="utf-8"
            )
        )

        assert HOST_METRIC_KEY in routing["matcher"]
        assert "cohesity_" not in routing["matcher"]

    def test_its_processors_pass_the_same_hygiene_rules_as_the_others(self, pipeline, host_link):
        ids = {processor["id"] for processor in node_processors(pipeline) + edge_processors(pipeline)}
        for stage in ("processing", "smartscapeNodeExtraction", "smartscapeEdgeExtraction"):
            for processor in host_link[stage]["processors"]:
                identifier = processor["id"]
                assert identifier not in ids, f"{identifier} collides with the metrics pipeline"
                assert 4 <= len(identifier) <= 100, identifier
                assert not identifier.startswith(("dt.", "dynatrace."))
                assert processor["description"].strip()
                assert processor["enabled"] is True
                ids.add(identifier)


def test_no_pipeline_processor_field_exceeds_its_server_limit(pipeline, host_link):
    """A 544-char description was rejected at UPLOAD: "Size must be lower than or equal to 512".

    --validate-only against the settings schema did NOT catch it - that validates the settings
    object, while this limit is enforced on the packaged extension asset. Two different gates.
    Keep a processor's rationale in the README, not in its description.
    """
    for document in (pipeline, host_link):
        for stage in document.values():
            if not isinstance(stage, dict):
                continue
            for processor in stage.get("processors", []):
                assert len(processor.get("description", "")) <= 512, processor.get("id")
                assert len(processor.get("id", "")) <= 100, processor.get("id")


def test_inline_lookup_tables_ship_non_empty_but_unmatchable():
    """The upload API rejects an empty inlineLookupTable ("Must not be empty"), yet the host-link
    pipeline must stay inert until the sync workflow fills it. A sentinel key no Smartscape host
    id can equal satisfies both. --validate-only does not catch this; it is an upload-time check.
    """
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "extension" / "openpipeline"
    for path in root.glob("*.json"):
        document = json.loads(path.read_text(encoding="utf-8"))

        def walk(node, name=path.name):
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "inlineLookup" and isinstance(value, dict):
                        table = value.get("inlineLookupTable")
                        assert table not in ("", "[]", None, []), f"{name}: empty table"
                    walk(value, name)
            elif isinstance(node, list):
                for value in node:
                    walk(value, name)

        walk(document)
