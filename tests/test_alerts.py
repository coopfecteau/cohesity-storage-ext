"""Cluster alerts: parsing, de-duplication, severity, and the redaction switch.

The traps here are different from the metric ones. An alert is prose, arrives repeatedly for as
long as it stays open, and is the first thing this extension sends that could carry a name out
of the protected estate. Each class below is one of those.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from cohesity_storage import client as client_module
from cohesity_storage import domain, metrics
from cohesity_storage.client import CohesityClient
from cohesity_storage.config import ClusterConfig
from cohesity_storage.errors import (
    CohesityApiError,
    CohesityEndpointError,
    CohesityError,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def alert(**overrides):
    payload = {
        "id": "a-1",
        "alertName": "Disk health critical",
        "alertDescription": "Disk 7 on node 2 is failing.",
        "alertCategory": "kDisk",
        "alertState": "kOpen",
        "severity": "kCritical",
        "latestTimestampUsecs": 1789203600000000,
    }
    payload.update(overrides)
    return payload


class TestParsing:
    def test_the_nested_alert_document_is_read(self):
        # v1 nests the sentence under alertDocument; v2 carries it flat. Both are real.
        parsed = domain.parse_alert(
            {
                "id": "a-1",
                "severity": "kCritical",
                "alertDocument": {"alertName": "Disk failed", "alertDescription": "Node 2."},
            }
        )

        assert parsed.name == "Disk failed"
        assert parsed.description == "Node 2."

    def test_the_flat_spelling_wins_over_the_nested_one(self):
        # A cluster carrying both is the newer shape and should be read as such.
        parsed = domain.parse_alert(
            {
                "id": "a-1",
                "alertName": "flat",
                "alertDocument": {"alertName": "nested"},
            }
        )

        assert parsed.name == "flat"

    def test_an_alert_with_no_id_is_dropped(self):
        # It could never be de-duplicated, so it would be re-sent on every poll for as long as
        # it stayed open - which is worse than not having it.
        assert domain.parse_alert(alert(id=None)) is None

    def test_a_wrapped_list_and_a_bare_list_parse_the_same(self):
        wrapped = domain.parse_alerts({"alerts": [alert()]})
        bare = domain.parse_alerts([alert()])

        assert [a.id for a in wrapped] == [a.id for a in bare] == ["a-1"]

    def test_one_malformed_alert_does_not_cost_the_others(self):
        parsed = domain.parse_alerts([alert(id="a-1"), "nonsense", alert(id="a-2")])

        assert [a.id for a in parsed] == ["a-1", "a-2"]

    def test_a_missing_category_is_named_not_blank(self):
        # "" cannot be filtered for, and an uncategorised alert is one worth finding.
        parsed = domain.parse_alert({"id": "a-1"})

        assert parsed.category == "unknown"
        assert parsed.state == "unknown"

    def test_a_malformed_body_yields_no_alerts_rather_than_raising(self):
        assert domain.parse_alerts(None) == []
        assert domain.parse_alerts({"alerts": "nonsense"}) == []


class TestSeverity:
    def test_the_documented_severities_map(self):
        assert domain.alert_severity("kCritical")[0] == domain.ALERT_SEVERITY_CRITICAL
        assert domain.alert_severity("kWarning")[0] == domain.ALERT_SEVERITY_WARNING
        assert domain.alert_severity("kInfo")[0] == domain.ALERT_SEVERITY_INFO

    def test_an_unknown_severity_is_named_and_its_source_kept(self):
        # Dropping it would lose the alert; guessing would misreport it. The raw value is kept
        # because it is the only thing that explains a pile of "unknown" without a redeploy.
        severity, source = domain.alert_severity("kSomethingNew")

        assert severity == domain.ALERT_SEVERITY_UNKNOWN
        assert source == "kSomethingNew"

    def test_a_mapped_severity_carries_no_source(self):
        assert domain.alert_severity("kCritical")[1] == ""

    def test_an_unknown_severity_is_reported_at_warn_not_info(self):
        # A severity the extension could not read is a thing to look at. INFO is where it would
        # stay unlooked-at.
        events = metrics.alert_log_events("1", "prod", domain.parse_alerts([alert(severity="kNew")]))

        assert events[0]["severity"] == metrics.SEVERITY_WARN
        assert events[0]["cohesity.alert.severity_source"] == "kNew"

    def test_severity_maps_onto_the_levels_the_pipeline_filters_on(self):
        levels = {
            "kCritical": metrics.SEVERITY_ERROR,
            "kWarning": metrics.SEVERITY_WARN,
            "kInfo": metrics.SEVERITY_INFO,
        }
        for raw, expected in levels.items():
            events = metrics.alert_log_events(
                "1", "prod", domain.parse_alerts([alert(severity=raw)])
            )
            assert events[0]["severity"] == expected


class TestDeduplication:
    def test_a_still_open_alert_is_sent_once(self):
        ledger = domain.RunLedger()
        alerts = domain.parse_alerts([alert()])

        assert len(domain.new_alerts(alerts, ledger)) == 1
        assert domain.new_alerts(alerts, ledger) == []

    def test_a_refired_alert_is_a_new_record(self):
        # Same id, later occurrence. De-duplicating on the id alone would swallow exactly the
        # fire somebody is being paged about.
        ledger = domain.RunLedger()
        first = domain.parse_alerts([alert(latestTimestampUsecs=1)])
        again = domain.parse_alerts([alert(latestTimestampUsecs=2)])

        assert len(domain.new_alerts(first, ledger)) == 1
        assert len(domain.new_alerts(again, ledger)) == 1

    def test_the_ledger_stays_bounded(self):
        ledger = domain.RunLedger(capacity=10)
        for index in range(50):
            domain.new_alerts(domain.parse_alerts([alert(id=f"a-{index}")]), ledger)

        assert len(ledger) == 10

    def test_an_alert_key_cannot_collide_with_a_run_id(self):
        # They share the ledger class but never the same instance; this asserts the keys would
        # not collide even if they did.
        parsed = domain.parse_alerts([alert()])[0]

        assert parsed.dedup_key.startswith("a-1:")
        assert parsed.dedup_key != parsed.id


class TestLogEvents:
    def test_the_record_carries_the_cluster_and_the_source(self):
        events = metrics.alert_log_events("42", "prod", domain.parse_alerts([alert()]))

        assert events[0]["log.source"] == metrics.LOG_SOURCE_ALERTS
        assert events[0][metrics.DIM_CLUSTER_ID] == "42"
        assert events[0][metrics.DIM_CLUSTER_NAME] == "prod"

    def test_the_cluster_clock_is_an_attribute_not_the_record_timestamp(self):
        # Ingest rejects anything more than an hour old. An alert open for a week is valid and
        # far outside that window, so using its own time would make the oldest and most serious
        # alerts vanish without a word.
        events = metrics.alert_log_events("1", "prod", domain.parse_alerts([alert()]))

        assert events[0]["cohesity.alert.latest_timestamp_usecs"] == "1789203600000000"
        assert "timestamp" not in events[0]

    def test_prose_is_collapsed_onto_one_line(self):
        parsed = domain.parse_alerts([alert(alertDescription="Disk   7\non node 2.\t Offline.")])
        events = metrics.alert_log_events("1", "prod", parsed)

        assert events[0]["cohesity.alert.description"] == "Disk 7 on node 2. Offline."
        assert "\n" not in events[0]["content"]

    def test_very_long_prose_is_truncated(self):
        parsed = domain.parse_alerts([alert(alertDescription="x" * 5000)])
        events = metrics.alert_log_events("1", "prod", parsed)

        assert len(events[0]["cohesity.alert.description"]) == metrics.ALERT_TEXT_LIMIT

    def test_no_alerts_is_no_records(self):
        assert metrics.alert_log_events("1", "prod", []) == []


class TestRedaction:
    """The description is the only alert field that can name something in the estate."""

    def test_descriptions_are_withheld_when_asked(self):
        events = metrics.alert_log_events(
            "1", "prod", domain.parse_alerts([alert()]), include_description=False
        )

        assert "cohesity.alert.description" not in events[0]
        assert "Disk 7 on node 2" not in events[0]["content"]

    def test_withholding_the_description_still_sends_the_alert(self):
        # Redaction must not silently become suppression: the alert, its severity and when it
        # happened all still arrive.
        events = metrics.alert_log_events(
            "1", "prod", domain.parse_alerts([alert()]), include_description=False
        )

        assert events[0]["cohesity.alert.id"] == "a-1"
        assert events[0]["cohesity.alert.severity"] == "critical"
        assert events[0]["cohesity.alert.name"] == "Disk health critical"
        assert events[0]["cohesity.alert.latest_timestamp_usecs"] == "1789203600000000"

    def test_descriptions_are_included_by_default(self):
        events = metrics.alert_log_events("1", "prod", domain.parse_alerts([alert()]))

        assert events[0]["cohesity.alert.description"] == "Disk 7 on node 2 is failing."


class TestDiagnosticShape:
    def test_the_shape_reports_names_and_counts_only(self):
        # The open question is whether alert prose carries estate names. A diagnostic that
        # answered it by quoting one would be the leak it exists to detect.
        shape = domain.alert_shape({"alerts": [alert()]})

        assert shape["count"] == 1
        assert "alertName" in shape["keys"]
        assert shape["with_description"] == 1
        blob = json.dumps(shape)
        assert "Disk 7 on node 2 is failing." not in blob
        assert "Disk health critical" not in blob

    def test_unmapped_severities_are_listed_so_the_map_can_be_fixed(self):
        shape = domain.alert_shape({"alerts": [alert(severity="kNovel")]})

        assert shape["unmapped"] == ["kNovel"]


class TestConfiguration:
    def test_alerts_are_off_until_asked_for(self):
        config = ClusterConfig.from_dict({"name": "c", "host": "h", "apiKey": "k"})

        assert config.collect_alerts is False
        assert "alerts" not in config.enabled_collections

    def test_descriptions_are_on_once_alerts_are(self):
        config = ClusterConfig.from_dict(
            {"name": "c", "host": "h", "apiKey": "k", "collectAlerts": True}
        )

        assert config.alert_descriptions is True
        assert "alerts" in config.enabled_collections

    def test_every_alert_setting_survives_being_absent(self):
        # The 0.1.9 upgrade was rejected over a new required property. Every setting added here
        # has to be readable from a configuration written before it existed.
        config = ClusterConfig.from_dict({"name": "c", "host": "h", "apiKey": "k"})

        assert config.alert_lookback_hours >= 1
        assert config.max_alerts >= 1

    def test_the_lookback_is_wider_than_a_default_poll(self):
        # Narrower than the interval and an alert raised just after a poll is missed entirely.
        config = ClusterConfig.from_dict({"name": "c", "host": "h", "apiKey": "k"})

        assert config.alert_lookback_hours * 60 > config.interval_minutes


class TestFixture:
    def test_the_alert_fixture_exercises_every_branch(self):
        payload = json.loads((FIXTURES / "v2_alerts.json").read_text(encoding="utf-8"))["body"]
        alerts = domain.parse_alerts(payload)

        # Five entries, one of which has no id and is dropped.
        assert len(payload["alerts"]) == 5
        assert len(alerts) == 4
        severities = {a.severity for a in alerts}
        assert severities == {"critical", "warning", "info", "unknown"}

    def test_the_fixture_alerts_all_produce_records(self):
        payload = json.loads((FIXTURES / "v2_alerts.json").read_text(encoding="utf-8"))["body"]
        events = metrics.alert_log_events("1", "prod", domain.parse_alerts(payload))

        assert len(events) == 4
        assert all(event["log.source"] == metrics.LOG_SOURCE_ALERTS for event in events)


class StubTransport:
    """Answers or refuses per path, and remembers the order it was asked in."""

    def __init__(self, answers: dict[str, object]):
        self._answers = answers
        self.asked: list[str] = []

    def get(self, path, params=None, *, prefix="/v2"):  # noqa: ARG002 - matches the real signature
        full = f"{prefix}{path}"
        self.asked.append(full)
        answer = self._answers.get(full)
        if isinstance(answer, Exception):
            raise answer
        if answer is None:
            raise CohesityEndpointError(f"no such path {full}")
        return answer

    def close(self):
        pass

    def describe(self):
        return "stub"

    def caveat(self):
        return ""


def client_with(answers):
    config = ClusterConfig.from_dict({"name": "c", "host": "h", "apiKey": "k"})
    return CohesityClient(config, transport=StubTransport(answers))


V2 = "/v2/alerts"
V1 = "/irisservices/api/v1/public/alerts"
BODY = {"alerts": [{"id": "a-1", "severity": "kCritical", "alertName": "Disk"}]}


class TestAlertSourceChain:
    """Which alert path this cluster serves is a question, not an assumption."""

    def test_the_v2_path_is_tried_first(self):
        client = client_with({V2: BODY})

        assert len(client.alerts()) == 1
        assert client._transport.asked == [V2]
        assert client.alert_source == client_module.ALERTS_SOURCE_V2

    def test_a_v2_404_falls_through_to_the_v1_path(self):
        # A 404 here is an ordinary answer: the v2 spelling is documented for 7.4 and this
        # cluster may simply not serve it.
        client = client_with({V1: BODY})

        assert len(client.alerts()) == 1
        assert client._transport.asked == [V2, V1]
        assert client.alert_source == client_module.ALERTS_SOURCE_V1

    def test_the_winner_is_remembered_rather_than_re_probed(self):
        client = client_with({V1: BODY})
        client.alerts()
        client._transport.asked.clear()

        client.alerts()

        assert client._transport.asked == [V1]

    def test_a_winner_that_later_fails_does_not_cause_permanent_silence(self):
        # This cluster has an intermittently unhealthy stats subsystem. Caching a failure would
        # turn a temporary 500 into a silence only a restart could clear.
        answers = {V1: BODY}
        client = client_with(answers)
        client.alerts()
        answers[V1] = CohesityApiError("HTTP 500")
        answers[V2] = BODY
        client._transport.asked.clear()

        assert len(client.alerts()) == 1
        assert client._transport.asked == [V1, V2]
        assert client.alert_source == client_module.ALERTS_SOURCE_V2

    def test_when_no_path_answers_the_failure_is_raised_not_swallowed(self):
        # A silent empty list would read as "this cluster has no alerts", which is the most
        # dangerous thing it could say.
        client = client_with({})

        with pytest.raises(CohesityError):
            client.alerts()

    def test_the_window_is_bounded_and_in_microseconds(self):
        client = client_with({V2: BODY})
        window = client._alert_window(24)

        assert window["endDateUsecs"] - window["startDateUsecs"] == 24 * 3600 * 1_000_000
        # Microsecond epochs are 16 digits this century; a millisecond mistake shows up here.
        assert len(str(window["startDateUsecs"])) >= 16

    def test_the_ledger_is_not_shared_with_runs(self):
        # Shared bounds would let a busy estate evict alerts and re-send them.
        client = client_with({V2: BODY})
        client.new_alerts()

        assert client.counted_alert_ids == 1
        assert client.counted_run_ids == 0


class TestEveryFactKindIsHandled:
    """A fact the client records and the metric layer drops is invisible, and silently so.

    This is not hypothetical. The alert collection shipped in v0.2.1 recording three fact kinds
    - alert_source, alert_source_failed, alert_shape - that ``_diagnostic_event`` had no branch
    for. Unknown kinds are dropped by design, which is right, so the collection worked while
    the diagnostics that were meant to explain WHICH alert path answered and WHAT the fields
    were called never reached Grail at all. Nothing failed; the answer simply was not there.

    So the rule is asserted structurally rather than remembered: every ``"kind": "..."`` literal
    in client.py must produce a record.
    """

    def kinds_recorded_by_the_client(self) -> set[str]:
        source = (
            Path(__file__).resolve().parents[1] / "cohesity_storage" / "client.py"
        ).read_text(encoding="utf-8")
        return set(re.findall(r'"kind":\s*"([a-z_]+)"', source))

    def test_the_scan_finds_the_kinds_at_all(self):
        # If the literal spelling in client.py ever changes, this test must fail loudly rather
        # than pass by matching nothing.
        kinds = self.kinds_recorded_by_the_client()

        assert len(kinds) >= 8, f"only found {kinds} - has the spelling changed?"
        assert "alert_shape" in kinds

    def test_every_recorded_kind_produces_a_record(self):
        unhandled = []
        for kind in sorted(self.kinds_recorded_by_the_client()):
            events = metrics.diagnostic_log_events("1", "prod", "7.3.2", [{"kind": kind}])
            if not events:
                unhandled.append(kind)

        assert not unhandled, (
            f"client.py records these fact kinds and metrics._diagnostic_event drops them, "
            f"so they never reach Grail: {unhandled}"
        )

    def test_an_unknown_kind_is_still_dropped_rather_than_guessed_at(self):
        # The behaviour above is a coverage requirement, not a licence to invent a record for
        # a fact nobody has written a sentence for.
        assert metrics.diagnostic_log_events("1", "prod", "7.3.2", [{"kind": "nonsense"}]) == []
