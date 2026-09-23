"""The host link (ticket 16): UUID normalisation, the bridge metric, and the lookup table.

Three separate pieces of code reduce a VM's BIOS UUID to one string - the extension's Python,
the workflow's JavaScript, and the reference encoder in ``tools/host_link.py``. If any two of
them disagree by a single character the join matches nothing, and a join that matches nothing
is indistinguishable from an estate with no overlap. So the worked examples live here once and
all three are held to them.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cohesity_storage import domain, metrics
from tools.host_link import EMPTY_TABLE, host_ids_by_uuid, lookup_table, tables

REPO_ROOT = Path(__file__).resolve().parent.parent
TASK_JS = REPO_ROOT / "dynatrace" / "sync-host-link-task.js"

# The canonical answer, and the two spellings of it that have to meet.
CANONICAL = "00112233-4455-6677-8899-aabbccddeeff"
DYNATRACE_SERIAL = "VMware-00 11 22 33 44 55 66 77-88 99 aa bb cc dd ee ff"

# (input, expected) for every shape either side can produce. Shared by the Python tests and by
# the test that holds the JavaScript twin to the same answers.
NORMALISATION_CASES = [
    # The Dynatrace side: vendor prefix, byte spacing, a dash in the middle of the bytes.
    (DYNATRACE_SERIAL, CANONICAL),
    # The Cohesity side: already canonical, and must survive untouched.
    (CANONICAL, CANONICAL),
    # Case only. Dynatrace publishes upper-case hex on some hosts.
    (CANONICAL.upper(), CANONICAL),
    # Undashed 32 hex, which the v0.1.8 probe's `uuid-32-hex-undashed` verdict anticipated.
    ("00112233445566778899aabbccddeeff", CANONICAL),
    # A UUID whose first group is all letters. This is the case that forbids "strip everything
    # that is not a hex digit, then drop a prefix": 'abcdefab' looks exactly like a prefix.
    ("ABCDEFAB-1234-5678-9abc-def012345678", "abcdefab-1234-5678-9abc-def012345678"),
    # Junk.
    ("not a uuid", ""),
    ("", ""),
    (None, ""),
    # Cohesity's own id scheme - an int64, or colon-joined int64s. These are the answers that
    # must never come back positive, because they would build a table of edges to nothing.
    ("1234567890123456", ""),
    ("1234567890123456:9876543210", ""),
    ("1234567890123456_9876543210", ""),
    # THE TRAP: 32 decimal digits are also 32 valid hex digits. A structural hex test alone
    # would turn a Cohesity int64 into a perfectly well-formed UUID and answer ticket 16
    # exactly backwards.
    ("12345678901234567890123456789012", ""),
    # A physical host's service tag, which is what non-VMware hardware puts in system.serial.
    ("Dell-7XK2M93", ""),
]


class TestNormalisation:
    @pytest.mark.parametrize(("value", "expected"), NORMALISATION_CASES)
    def test_every_shape_either_side_can_produce(self, value, expected):
        assert domain.normalise_object_uuid(value) == expected

    def test_the_two_sides_of_the_join_meet(self):
        # The whole ticket in one assertion: what Cohesity says a VM is, and what Dynatrace
        # says the same VM is, reduce to the same string.
        assert domain.normalise_object_uuid(DYNATRACE_SERIAL) == domain.normalise_object_uuid(
            CANONICAL
        )

    def test_the_vendor_prefix_is_not_removed_by_stripping_non_hex(self):
        """'vmware' is made of hex digits ('a', 'e') plus non-hex ones.

        A blind strip leaves 34 digits, which is not 32, so the value would be silently
        rejected - and the symptom would be "the join finds nothing", with no error anywhere.
        """
        stripped = re.sub(r"[^0-9a-f]", "", DYNATRACE_SERIAL.lower())

        assert len(stripped) == 34
        assert domain.normalise_object_uuid(DYNATRACE_SERIAL) == CANONICAL

    def test_output_is_always_canonical_or_empty(self):
        shape = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
        for value, _ in NORMALISATION_CASES:
            result = domain.normalise_object_uuid(value)
            assert result == "" or shape.fullmatch(result), value


class TestTheJavaScriptTwin:
    """The workflow cannot import Python, so it carries a copy. A copy has to be checked."""

    @pytest.fixture(scope="class")
    @classmethod
    def script(cls) -> str:
        return TASK_JS.read_text(encoding="utf-8")

    def test_it_guards_the_decimal_trap_before_testing_for_hex(self, script):
        # Ordering is the whole correctness of the function, in both languages.
        decimal_guard = script.index("/^[0-9]+(:[0-9]+)*$/")
        hex_strip = script.index("replace(/[^0-9a-f]/g")

        assert decimal_guard < hex_strip

    def test_it_strips_the_vendor_prefix_before_stripping_non_hex(self, script):
        prefix_drop = script.index("text.slice(dash + 1)")
        hex_strip = script.index("replace(/[^0-9a-f]/g")

        assert prefix_drop < hex_strip

    def test_it_requires_exactly_thirty_two_digits(self, script):
        assert "digits.length !== 32" in script

    def test_it_reads_exactly_the_dimensions_the_python_emits(self, script):
        for dimension in (
            metrics.DIM_CLUSTER_ID,
            metrics.DIM_PROTECTION_GROUP_ID,
            metrics.DIM_OBJECT_UUID,
        ):
            assert dimension in script, dimension

    def test_it_queries_the_metric_key_the_python_emits(self, script):
        assert metrics.PROTECTION_GROUP_PROTECTS in script

    def test_it_rewrites_only_the_two_lookup_processors(self, script):
        """Named explicitly, so a processor rename in the pipeline breaks here, not on a tenant.

        The node and edge processors live in the same settings object. A workflow that wrote a
        remembered copy of the whole value instead of modifying the current one would retire
        the edge this exists to draw.
        """
        pipeline = json.loads(
            (REPO_ROOT / "extension" / "openpipeline" / "host-link.pipeline.json").read_text(
                encoding="utf-8"
            )
        )
        lookups = [
            processor["id"]
            for processor in pipeline["processing"]["processors"]
            if processor["type"] == "inlineLookup"
        ]

        assert len(lookups) == 2
        for identifier in lookups:
            assert identifier in script, identifier


class TestTheBridgeMetric:
    """The dimensions ARE the metric. The value is a constant nobody reads."""

    def link(self, uuid=CANONICAL, group="1001_g-1", name="Nightly-VMware"):
        return domain.ProtectedObjectLink(
            protection_group_id=group, protection_group_name=name, uuid=uuid
        )

    def test_it_carries_exactly_the_four_fields_the_workflow_joins_on(self):
        sample = metrics.protected_object_samples(1001, "east", [self.link()])[0]

        assert sample.key == metrics.PROTECTION_GROUP_PROTECTS
        assert sample.dimensions == {
            metrics.DIM_CLUSTER_ID: "1001",
            metrics.DIM_CLUSTER_NAME: "east",
            metrics.DIM_PROTECTION_GROUP_ID: "1001_g-1",
            metrics.DIM_PROTECTION_GROUP_NAME: "Nightly-VMware",
            metrics.DIM_OBJECT_UUID: CANONICAL,
        }

    def test_the_value_is_a_constant_one_and_not_a_delta(self):
        # A gauge: it asserts "this pair exists right now". A delta counter would accumulate
        # and mean nothing at all.
        sample = metrics.protected_object_samples(1001, "east", [self.link()])[0]

        assert sample.value == 1
        assert sample.delta is False

    def test_it_binds_to_the_protection_group_entity_rather_than_minting_one(self):
        # The prefix IS the binding. Under `cohesity.object.*` it would match no node rule and
        # float unattached, or worse, invite a per-VM entity type.
        assert metrics.PROTECTION_GROUP_PROTECTS.startswith(
            f"{metrics.PREFIX_PROTECTION_GROUP}."
        )

    def test_the_group_id_is_not_namespaced_a_second_time(self):
        """The client namespaces it, because only the client knows which cluster answered.

        Doing it again here yields `{cluster}_{cluster}_{group}`, which matches no entity -
        and matching no entity is the failure mode that produces no error anywhere.
        """
        sample = metrics.protected_object_samples(1001, "east", [self.link()])[0]

        assert sample.dimensions[metrics.DIM_PROTECTION_GROUP_ID] == "1001_g-1"

    def test_a_partial_link_is_dropped_rather_than_emitted(self):
        # A line missing either id or the uuid would be paired with the wrong group by the
        # workflow, and a wrong edge says a VM is backed up by a job that does not touch it.
        links = [self.link(uuid=""), self.link(group=""), self.link()]

        assert len(metrics.protected_object_samples(1001, "east", links)) == 1

    def test_a_group_with_no_name_still_produces_a_line(self):
        # The name is for a human reading the table; the join does not need it.
        sample = metrics.protected_object_samples(1001, "east", [self.link(name="")])[0]

        assert metrics.DIM_PROTECTION_GROUP_NAME not in sample.dimensions
        assert sample.dimensions[metrics.DIM_OBJECT_UUID] == CANONICAL


class TestWhichOfTheTwoUuids:
    """v0.2.0. A VMware VM has two 8-4-4-4-12 identifiers and only one of them can ever join.

    v0.1.9 emitted the other one. Two hundred bridge-metric series flowed, every diagnostic on
    the channel read as success, and the join matched nothing - because both identifiers are
    well-formed uuids for the same VM and nothing downstream could tell them apart. These are
    the tests that make that specific mistake fail loudly instead of silently.
    """

    #: What Dynatrace publishes on a HOST as system.serial. VMware firmware mints 42 and 564d.
    BIOS = "42000000-1111-4222-8333-444444444401"
    #: What Cohesity's object.uuid actually returns. vCenter mints 50, and no HOST reports it.
    INSTANCE = "50000000-1111-4222-8333-444444444401"

    def obj(self, **summary):
        return {"id": 4001, "name": "vm-a", "uuid": self.INSTANCE, "vCenterSummary": summary}

    def test_the_bios_uuid_is_taken_and_the_instance_uuid_is_not(self):
        uuid, field = domain.bios_uuid(self.obj(biosUuid=self.BIOS, instanceUuid=self.INSTANCE))

        assert uuid == self.BIOS
        assert field == "vCenterSummary.biosUuid"

    def test_object_uuid_is_never_a_fallback_for_a_missing_bios_uuid(self):
        """The whole fix in one assertion.

        An identifier that cannot match is worse than none: it looks like the feature works.
        Falling back here would restore v0.1.9 exactly, and no diagnostic would notice.
        """
        assert domain.bios_uuid({"id": 4001, "uuid": self.INSTANCE}) == ("", "")
        assert "uuid" not in domain.BIOS_UUID_FIELDS

    def test_the_candidates_are_tried_in_their_declared_order(self):
        # Alias tolerance with a preference, exactly as the storage-domain stats do it: the
        # most-trusted spelling wins wherever it appears, not whichever block is searched first.
        obj = self.obj(hardwareUuid="00112233-4455-6677-8899-aabbccddeeff", biosUuid=self.BIOS)

        assert domain.bios_uuid(obj)[0] == self.BIOS

    def test_a_present_but_unusable_candidate_falls_through_to_the_next(self):
        # A field published with null in it has told us nothing; a later alias may still carry
        # the identifier. The same rule first_number follows for the storage-domain aliases.
        obj = self.obj(biosUuid=None, hardwareUuid=self.BIOS)

        assert domain.bios_uuid(obj) == (self.BIOS, "vCenterSummary.hardwareUuid")

    def test_the_object_root_is_searched_as_well_as_the_vmware_block(self):
        # 6.8-7.4 moves this field around; which block it lives in is not worth guessing wrong.
        uuid, field = domain.bios_uuid({"id": 4001, "biosUuid": self.BIOS})

        assert (uuid, field) == (self.BIOS, "biosUuid")

    def test_the_instance_uuid_is_read_separately_and_from_object_uuid_last(self):
        assert domain.instance_uuid({"uuid": self.INSTANCE}) == self.INSTANCE
        assert domain.instance_uuid(self.obj(instanceUuid=self.INSTANCE)) == self.INSTANCE

    def test_candidate_field_names_leave_as_names_and_never_as_values(self):
        obj = self.obj(biosUuid=self.BIOS, instanceUuid=self.INSTANCE)

        found = domain.candidate_uuid_fields(obj)

        assert found == ("uuid", "vCenterSummary.biosUuid", "vCenterSummary.instanceUuid")
        assert not any(self.BIOS in name or self.INSTANCE in name for name in found)

    @pytest.mark.parametrize(
        ("uuid", "suspect"),
        [
            ("50000000-1111-4222-8333-444444444401", True),
            ("50000000-1111-2222-3333-444455556666", True),
            ("42000000-1111-4222-8333-444444444401", False),
            ("564d0000-aaaa-bbbb-cccc-ddddeeeeffff", False),
            ("", False),
        ],
    )
    def test_the_fifty_byte_marks_a_uuid_as_vcenters_not_the_firmwares(self, uuid, suspect):
        assert domain.looks_like_instance_uuid(uuid) is suspect

    def test_a_shape_verdict_separates_absent_from_present_but_wrong(self):
        """Two different next steps, so they cannot share one answer.

        `missing` means no candidate field exists and a NAME has to be added; anything else
        means a candidate exists and its VALUE is the wrong shape.
        """
        assert domain.bios_uuid_verdict({"uuid": self.INSTANCE}) == domain.UUID_VERDICT_MISSING
        assert (
            domain.bios_uuid_verdict(self.obj(biosUuid="1234567890123456"))
            == domain.UUID_VERDICT_NUMERIC
        )

    def test_the_parser_reports_which_field_won_and_what_was_available(self):
        payload = {
            "runs": [
                {
                    "id": "r-1",
                    "objects": [
                        {"object": self.obj(biosUuid=self.BIOS, instanceUuid=self.INSTANCE)}
                    ],
                }
            ]
        }

        parsed = domain.parse_protected_object_links(payload, group_name="j")

        assert parsed.bios_field == "vCenterSummary.biosUuid"
        assert "vCenterSummary.biosUuid" in parsed.candidate_fields
        assert parsed.links[0].uuid == self.BIOS
        assert parsed.links[0].instance_uuid == self.INSTANCE
        assert parsed.instance_shaped == 0

    def test_an_object_with_only_an_instance_uuid_produces_no_link_at_all(self):
        payload = {"runs": [{"id": "r-1", "objects": [{"object": {"uuid": self.INSTANCE}}]}]}

        parsed = domain.parse_protected_object_links(payload)

        assert parsed.links == ()
        assert parsed.objects_seen == 1
        # And the record says WHY, by name, so the next step is "add a field name" rather
        # than "the join is impossible on this cluster".
        assert parsed.candidate_fields == ("uuid",)

    def test_a_bios_field_handing_back_fifty_uuids_is_counted_not_dropped(self):
        # One real BIOS uuid in 256 starts 50 by chance, so rejecting on the byte would
        # silently lose real hosts - the very failure mode this release exists to end. The
        # RATIO is the evidence: all of them means the field name is wrong, one is chance.
        payload = {
            "runs": [
                {
                    "id": "r-1",
                    "objects": [
                        {"object": {"biosUuid": self.INSTANCE}},
                        {"object": {"biosUuid": self.BIOS}},
                    ],
                }
            ]
        }

        parsed = domain.parse_protected_object_links(payload)

        assert len(parsed.links) == 2
        assert parsed.instance_shaped == 1


class TestTheBridgeMetricCarriesBothUuids:
    def link(self, instance=""):
        return domain.ProtectedObjectLink(
            protection_group_id="1001_g-1",
            protection_group_name="Nightly-VMware",
            uuid=CANONICAL,
            instance_uuid=instance,
        )

    def test_the_instance_uuid_rides_as_its_own_dimension(self):
        # One dimension, not one series: it is 1:1 with the uuid already on the line, so it
        # adds no cardinality - and it is the key a vCenter-side join would need later.
        instance = "50000000-1111-4222-8333-444444444401"

        sample = metrics.protected_object_samples(1001, "east", [self.link(instance)])[0]

        assert sample.dimensions[metrics.DIM_OBJECT_UUID] == CANONICAL
        assert sample.dimensions[metrics.DIM_OBJECT_INSTANCE_UUID] == instance

    def test_the_join_key_dimension_keeps_its_name_so_the_contract_is_unchanged(self):
        """Only what FILLS cohesity.object.uuid moved in v0.2.0, not what it is called.

        The workflow, the pipeline and every query written against v0.1.9 still read the same
        dimension; renaming it would have retired all of them in order to fix a value.
        """
        assert metrics.DIM_OBJECT_UUID == "cohesity.object.uuid"

    def test_no_instance_uuid_omits_the_dimension_rather_than_sending_it_empty(self):
        sample = metrics.protected_object_samples(1001, "east", [self.link()])[0]

        assert metrics.DIM_OBJECT_INSTANCE_UUID not in sample.dimensions


class TestVmwareSelection:
    @pytest.mark.parametrize(
        ("environment", "expected"),
        [
            ("kVMware", True),
            ("kvmware", True),
            # Whatever a later Cohesity calls it, as long as it still says VMware.
            ("kVMwareVCenter", True),
            # The environments the v0.1.8 probe found carry no uuid field at all.
            ("kSQL", False),
            ("kOracle", False),
            ("kPhysical", False),
            ("", False),
            (None, False),
        ],
    )
    def test_only_vmware_is_worth_a_request(self, environment, expected):
        assert domain.is_vmware_environment(environment) is expected


class TestLookupTableEncoder:
    def test_the_shape_is_a_list_of_key_groups(self):
        # [[[keys...],"value"],...] - a list of groups, not a map. A plain object parses as a
        # table with no rows, which is an inert lookup nothing complains about.
        encoded = lookup_table({"HOST-1": "g-a", "HOST-2": "g-a", "HOST-3": "g-b"})

        assert json.loads(encoded) == [[["HOST-1", "HOST-2"], "g-a"], [["HOST-3"], "g-b"]]

    def test_hosts_sharing_a_value_share_one_row(self):
        rows = json.loads(lookup_table({f"HOST-{n}": "g-a" for n in range(5)}))

        assert len(rows) == 1
        assert len(rows[0][0]) == 5

    def test_the_encoding_is_stable_across_input_order(self):
        """The workflow compares the encoded string to decide whether to write at all.

        An unstable order would rewrite the settings object every hour forever, on a table
        that never changed, and bury any real change in the history.
        """
        forward = lookup_table({"HOST-1": "g-a", "HOST-2": "g-b"})
        backward = lookup_table({"HOST-2": "g-b", "HOST-1": "g-a"})

        assert forward == backward

    def test_empty_encodes_as_the_inert_table_the_pipeline_ships_with(self):
        # So a sync that finds nothing RESTORES the shipped state rather than leaving a stale
        # mapping asserting edges for hosts nothing protects any more.
        assert lookup_table({}) == EMPTY_TABLE

    def test_a_half_row_is_dropped_rather_than_encoded(self):
        assert lookup_table({"HOST-1": "", "": "g-a"}) == EMPTY_TABLE

    def test_the_shipped_pipeline_starts_from_that_empty_table(self):
        pipeline = json.loads(
            (REPO_ROOT / "extension" / "openpipeline" / "host-link.pipeline.json").read_text(
                encoding="utf-8"
            )
        )
        for processor in pipeline["processing"]["processors"]:
            assert processor["inlineLookup"]["inlineLookupTable"] == lookup_table({})


class TestTheJoin:
    HOSTS = [
        {"id": "HOST-AAA", "serial": DYNATRACE_SERIAL},
        {"id": "HOST-BBB", "serial": "VMware-42 1f 9a 3b 88 c0 4f 11-9d 2e 6b 7a 10 cc 45 ef"},
        # Physical hardware: a service tag, not a UUID. Must not land in the table.
        {"id": "HOST-CCC", "serial": "Dell-7XK2M93"},
        {"id": "HOST-DDD"},
    ]

    def link(self, uuid: str, group: str = "1001_g-1", cluster: str = "1001") -> dict:
        return {
            metrics.DIM_OBJECT_UUID: uuid,
            metrics.DIM_PROTECTION_GROUP_ID: group,
            metrics.DIM_CLUSTER_ID: cluster,
        }

    def test_hosts_are_indexed_by_their_normalised_serial(self):
        indexed = host_ids_by_uuid(self.HOSTS)

        assert indexed[CANONICAL] == "HOST-AAA"
        # Two hosts have BIOS UUIDs, two do not.
        assert len(indexed) == 2

    def test_both_tables_are_keyed_on_the_host_id(self):
        """Because that is the inlineLookup's sourceField. Keying on anything else is inert."""
        result = tables([self.link(CANONICAL)], self.HOSTS)

        assert json.loads(result["protectionGroupTable"]) == [[["HOST-AAA"], "1001_g-1"]]
        assert json.loads(result["clusterTable"]) == [[["HOST-AAA"], "1001"]]

    def test_the_two_tables_carry_the_two_id_components_separately(self):
        """The node id is built from cluster_id AND protectiongroup_id, and the group id the
        extension emits is already namespaced. One table cannot carry both for one key."""
        result = tables([self.link(CANONICAL)], self.HOSTS)
        groups = json.loads(result["protectionGroupTable"])[0][1]
        cluster = json.loads(result["clusterTable"])[0][1]

        assert groups.startswith(f"{cluster}_")

    def test_a_protected_vm_with_no_dynatrace_host_is_left_alone(self):
        # The expected majority. No entity minted, no edge drawn, no row in the table - which
        # is the whole of ticket 16's "degrade quietly" requirement.
        result = tables([self.link("9e1c0000-0000-4000-8000-000000000001")], self.HOSTS)

        assert result["mapped"] == 0
        assert result["protectionGroupTable"] == EMPTY_TABLE
        assert result["unmatched"] == ["9e1c0000-0000-4000-8000-000000000001"]

    def test_a_host_protected_by_two_groups_picks_one_and_says_so(self):
        # An inlineLookup holds one value per key, so one job has to win. Being arbitrary is
        # unavoidable; being arbitrary in silence is not.
        result = tables(
            [self.link(CANONICAL, group="1001_g-2"), self.link(CANONICAL, group="1001_g-1")],
            self.HOSTS,
        )

        assert result["multiplyProtected"] == ["HOST-AAA"]
        assert json.loads(result["protectionGroupTable"]) == [[["HOST-AAA"], "1001_g-1"]]

    def test_an_unnormalised_serial_still_joins(self):
        """The reason normalisation exists at all: neither side is stored canonically."""
        result = tables([self.link(CANONICAL.upper())], self.HOSTS)

        assert result["mapped"] == 1
