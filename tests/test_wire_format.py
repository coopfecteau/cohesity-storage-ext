"""The lines that actually leave the extension, checked against the ingestion protocol.

Everything else in the suite checks samples - keys, values, dimension dicts. None of that
catches a line the ingest refuses, because the SDK turns a dict into a line with
``f'{k}="{v}"'`` and neither escapes nor validates anything on the way. Two bugs shipped through
that gap and surfaced only as "invalid metric lines found" on a real ActiveGate: camelCase
dimension keys (the protocol requires lowercase) and unescaped names (a quote, a backslash or a
newline in a Cohesity name breaks the line).

So these tests go one step further than the rest: through the SDK's own ``Metric.to_mint_line``,
and into a strict parser of the protocol (:mod:`tests.mint`).
"""

from __future__ import annotations

import math
import socket
from datetime import datetime
from pathlib import Path

import pytest
from dynatrace_extension import Extension
from dynatrace_extension.sdk.metric import Metric, MetricType

from cohesity_storage import metrics
from cohesity_storage.__main__ import EXTENSION_NAME, ExtensionImpl
from cohesity_storage.config import ClusterConfig
from cohesity_storage.fixtures import FixtureStore
from tests import mint
from tests.cohesity_fake_cluster import (
    FakeCohesityCluster,
    hostile_name,
    self_signed_certificate,
)
from tests.test_reporting import replay_client, replay_samples
from tools import validate_metric_lines

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "fixtures"
NOW_MS = 1_789_600_000_000

BACKSLASH = "\\"
ADVERSARIAL_NAMES = [
    'a"b',
    f"C:{BACKSLASH}share{BACKSLASH}x",
    "line1\nline2",
    "tab\there",
    "carriage\rreturn",
    "crlf\r\nend",
    "Zürich ✓ 東京 ﬁ",
    "",
    "   ",
    "x" * 300,
    '"' * 300,
    BACKSLASH,
    BACKSLASH * 3,
    '"',
    f"ends in a backslash{BACKSLASH}",
    # A backslash landing exactly on the truncation boundary: cut after escaping, this would
    # leave half an escape pair and swallow the closing quote.
    "a" * 249 + BACKSLASH + "b" * 20,
    "control" + chr(0) + chr(7) + chr(0x1F) + chr(0x7F) + "chars",
]


def line_for(sample_or_key, value=None, dimensions=None, *, delta=False, timestamp=None) -> str:
    """The exact line the SDK would build, after the extension's own sanitising."""
    if isinstance(sample_or_key, metrics.Sample):
        sample = sample_or_key
        sample_or_key, value, dimensions, delta = sample.key, sample.value, sample.dimensions, sample.delta
    return Metric(
        sample_or_key,
        value,
        metrics.wire_dimensions(dimensions),
        MetricType.DELTA if delta else MetricType.GAUGE,
        timestamp,
    ).to_mint_line()


class TestNamesMatchTheSpec:
    def all_dimension_keys(self) -> dict[str, str]:
        return {name: value for name, value in vars(metrics).items() if name.startswith("DIM_")}

    def test_every_dimension_key_constant_is_a_valid_dimension_key(self):
        keys = self.all_dimension_keys()
        assert keys, "no DIM_* constants found - did they move?"
        for name, key in keys.items():
            assert mint.dimension_key_problem(key) is None, f"{name} = {key!r}"

    def test_every_metric_key_is_a_valid_metric_key(self):
        for key in metrics.ALL_METRIC_KEYS:
            assert mint.metric_key_problem(key) is None, key

    def test_the_protection_group_flags_are_namespaced_lowercase_keys(self):
        # The regression: Cohesity's own spelling (isPaused) is invalid as a dimension key.
        assert metrics.DIM_SLA_VIOLATED == "cohesity.protectiongroup.sla_violated"
        assert metrics.DIM_PAUSED == "cohesity.protectiongroup.paused"
        assert metrics.DIM_ACTIVE == "cohesity.protectiongroup.active"


class TestShippedFixtures:
    def test_every_replayed_sample_becomes_a_valid_line(self):
        samples = replay_samples(replay_client())
        assert samples
        for sample in samples:
            line = line_for(sample)
            assert mint.problem(line) is None, (mint.problem(line), line)

    def test_a_timestamped_line_is_valid_inside_the_window(self):
        sample = replay_samples(replay_client())[0]
        stamp = datetime.fromtimestamp(NOW_MS / 1000)

        line = line_for(sample, timestamp=stamp)

        assert mint.parse(line, now_ms=NOW_MS).timestamp_ms == NOW_MS

    def test_delta_lines_use_the_count_delta_payload(self):
        outcomes = [
            sample
            for sample in replay_samples(replay_client())
            if sample.key == metrics.PROTECTION_GROUP_RUN_OUTCOME
        ]
        assert outcomes
        for sample in outcomes:
            assert mint.parse(line_for(sample)).payload_type == "count,delta"


class TestAdversarialNames:
    @pytest.mark.parametrize("name", ADVERSARIAL_NAMES, ids=range(len(ADVERSARIAL_NAMES)))
    def test_any_name_produces_a_valid_line_that_round_trips(self, name):
        dimensions = metrics.protection_group_dimensions(
            "123", name, "g-1", name, storage_domain_id="7", status="Failed", is_paused=True
        )

        line = line_for(metrics.PROTECTION_GROUP_LAST_SUCCESS_AGE, 5, dimensions)

        parsed = mint.parse(line)
        expected = metrics.clean_dimension_value(name)
        if expected:
            assert parsed.dimensions[metrics.DIM_PROTECTION_GROUP_NAME] == expected
            assert parsed.dimensions[metrics.DIM_CLUSTER_NAME] == expected
        else:
            # An empty value is left out, not sent as "".
            assert metrics.DIM_PROTECTION_GROUP_NAME not in parsed.dimensions
            assert metrics.DIM_CLUSTER_NAME not in parsed.dimensions
        assert parsed.dimensions[metrics.DIM_PAUSED] == "true"

    def test_whitespace_runs_collapse_to_one_space(self):
        assert metrics.clean_dimension_value("  a\r\n\t b\n") == "a b"

    def test_values_are_truncated_to_250_characters_before_escaping(self):
        cleaned = metrics.clean_dimension_value('"' * 300)

        assert cleaned == '"' * 250
        assert metrics.wire_dimensions({"abc": '"' * 300})["abc"] == (BACKSLASH + '"') * 250

    def test_a_value_of_one_backslash_is_sent_as_an_escaped_backslash(self):
        assert metrics.wire_dimensions({"abc": BACKSLASH}) == {"abc": BACKSLASH * 2}

    def test_none_and_empty_values_are_dropped(self):
        assert metrics.wire_dimensions({"abc": None, "def": "", "ghi": " \n "}) == {}

    def test_the_raw_sdk_line_for_a_hostile_name_is_what_broke(self):
        # Without the chokepoint: exactly what reached the ingest before this fix.
        raw = Metric("cohesity.cluster.capacity.total", 1, {"cohesity.cluster.name": 'a"b'}).to_mint_line()

        assert mint.problem(raw) is not None


class TestNonFiniteValues:
    @pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf, True, False, "7", None])
    def test_values_the_protocol_cannot_carry_are_refused(self, value):
        assert metrics.wire_value(value) is None

    @pytest.mark.parametrize("value", [0, 1, -3, 1.5, 10**15])
    def test_finite_numbers_pass(self, value):
        assert metrics.wire_value(value) == value


class TestValidatorRejects:
    """The validator must fail bad lines, or every test above passes vacuously."""

    GOOD = 'cohesity.cluster.capacity.total,cohesity.cluster.id="1",cohesity.cluster.name="a" gauge,5'

    def test_the_good_line_passes(self):
        assert mint.problem(self.GOOD) is None
        assert mint.problem("cohesity.cluster.capacity.total 5") is None
        assert mint.problem('abc,def="x" count,delta=1 1789600000000', now_ms=NOW_MS) is None

    @pytest.mark.parametrize(
        "line",
        [
            'cohesity.protectiongroup.run.outcome,isPaused="true" count,delta=1',
            'cohesity.cluster.capacity.total,cohesity.cluster.name="a"b" gauge,5',
            'cohesity.cluster.capacity.total,cohesity.cluster.name="a\nb" gauge,5',
            'cohesity.cluster.capacity.total,cohesity.cluster.name="a\rb" gauge,5',
            "ab gauge,5",
            'cohesity.cluster.capacity.total,ab="x" gauge,5',
            "cohesity.cluster.capacity.total gauge,nan",
            "cohesity.cluster.capacity.total gauge,inf",
            "cohesity.cluster.capacity.total gauge,NaN",
            "cohesity.cluster.capacity.total gauge,True",
            "1cohesity.cluster gauge,5",
            "-cohesity.cluster gauge,5",
            "cohesity.-cluster gauge,5",
            'cohesity.cluster.capacity.total,abc="x",abc="y" gauge,5',
            "cohesity.cluster.capacity.total,abc=x gauge,5",
            'cohesity.cluster.capacity.total,abc="x gauge,5',
            'cohesity.cluster.capacity.total,abc="a\\" gauge,5',
            'cohesity.cluster.capacity.total,abc="\\q" gauge,5',
            "cohesity.cluster.capacity.total",
            "cohesity.cluster.capacity.total gauge,5 1789600000000 extra",
            "cohesity.cluster.capacity.total  gauge,5",
            "cohesity.cluster.capacity.total gauge,min=1,max=2",
        ],
    )
    def test_malformed_lines_are_rejected(self, line):
        assert mint.problem(line) is not None, line

    def test_more_than_fifty_dimensions_is_rejected(self):
        dimensions = ",".join(f'dim{index:03d}="v"' for index in range(51))

        assert mint.problem(f"cohesity.cluster.capacity.total,{dimensions} gauge,5") is not None
        fifty = ",".join(f'dim{index:03d}="v"' for index in range(50))
        assert mint.problem(f"cohesity.cluster.capacity.total,{fifty} gauge,5") is None

    def test_a_timestamp_outside_the_window_is_rejected(self):
        hour = 60 * 60 * 1000
        stale = f"cohesity.cluster.capacity.total gauge,5 {NOW_MS - hour - 1}"
        future = f"cohesity.cluster.capacity.total gauge,5 {NOW_MS + 11 * 60 * 1000}"

        assert mint.problem(stale, now_ms=NOW_MS) is not None
        assert mint.problem(future, now_ms=NOW_MS) is not None


# ---------------------------------------------------------------------------
# End to end: a hostile fake cluster, the real ExtensionImpl, the real SDK line builder.
# ---------------------------------------------------------------------------


class Recorder:
    """Stands in for the SDK logger, so a test can count warnings."""

    def __init__(self):
        self.warnings: list[str] = []

    def warning(self, message, *_args, **_kwargs):
        self.warnings.append(str(message))

    def info(self, *_args, **_kwargs):
        pass

    def error(self, *_args, **_kwargs):
        pass

    def exception(self, *_args, **_kwargs):
        pass


@pytest.fixture
def extension():
    # Extension is a process-wide singleton; a leftover instance would carry another test's
    # buffered lines.
    Extension._instance = None
    instance = ExtensionImpl(name=EXTENSION_NAME)
    instance.logger = Recorder()
    instance._metrics = []
    yield instance
    Extension._instance = None


@pytest.fixture
def hostile_cluster(tmp_path):
    pytest.importorskip("cryptography")
    server = FakeCohesityCluster(
        store=FixtureStore(FIXTURE_DIR),
        certfile=self_signed_certificate(tmp_path),
        hostile=True,
    )
    server.start()
    yield server
    server.stop()


def cluster_config(cluster: FakeCohesityCluster, **overrides) -> ClusterConfig:
    values = {
        "name": "hostile",
        "host": cluster.host,
        "port": cluster.port,
        "api_key": "demo",
        "verify_tls": False,
    }
    values.update(overrides)
    return ClusterConfig(**values)


class TestHostileClusterEndToEnd:
    def test_every_line_a_poll_sends_is_valid(self, extension, hostile_cluster):
        extension._poll(cluster_config(hostile_cluster))

        lines = extension._metrics
        assert len(lines) > 20
        for line in lines:
            assert mint.problem(line) is None, (mint.problem(line), line)

    def test_the_names_arrive_escaped_and_round_trip(self, extension, hostile_cluster):
        extension._poll(cluster_config(hostile_cluster))

        parsed = [mint.parse(line) for line in extension._metrics]
        names = {
            key: {line.dimensions[key] for line in parsed if key in line.dimensions}
            for key in (
                metrics.DIM_CLUSTER_NAME,
                metrics.DIM_STORAGE_DOMAIN_NAME,
                metrics.DIM_VIEW_NAME,
                metrics.DIM_PROTECTION_GROUP_NAME,
            )
        }
        assert names[metrics.DIM_CLUSTER_NAME] == {
            metrics.clean_dimension_value(hostile_name("cohesity-demo-01"))
        }
        for key, values in names.items():
            assert values, f"no line carried {key}"
            for value in values:
                assert '"' in value and BACKSLASH in value and "Zürich" in value, (key, value)
                assert "\n" not in value and "\t" not in value, (key, value)

    def test_the_protection_flags_arrive_under_their_new_keys(self, extension, hostile_cluster):
        extension._poll(cluster_config(hostile_cluster))

        keys = {key for line in extension._metrics for key in mint.parse(line).dimensions}
        assert metrics.DIM_PAUSED in keys
        assert metrics.DIM_ACTIVE in keys
        assert not any(key != key.lower() for key in keys)

    def test_the_failure_line_is_valid_for_a_hostile_configured_name(self, extension):
        # Nothing listens here, so the poll fails and _report_failure reports under the
        # configured name - which an operator typed, and which is just as free-text.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = ClusterConfig(
            name='prod "east"\n' + BACKSLASH + "dc1",
            host="127.0.0.1",
            port=port,
            api_key="demo",
            verify_tls=False,
            request_timeout_seconds=2,
        )

        extension._poll(config)

        assert len(extension._metrics) == 1
        parsed = mint.parse(extension._metrics[0])
        assert parsed.key == metrics.CLUSTER_COLLECTION_SUCCESS
        assert parsed.value == 0
        assert parsed.dimensions[metrics.DIM_CLUSTER_NAME] == 'prod "east" ' + BACKSLASH + "dc1"

    def test_the_log_validator_passes_a_hostile_run(self, extension, hostile_cluster):
        extension._poll(cluster_config(hostile_cluster))
        log = [
            f"2026-09-21 10:00:00,000 [INFO] api (MainThread): send_metric: {line}\n"
            for line in extension._metrics
        ]

        checked, failures = validate_metric_lines.validate(log)

        assert checked == len(extension._metrics)
        assert failures == []


class TestEmitChokepoint:
    def test_a_non_finite_value_is_skipped_and_warned_about_once(self, extension):
        for _ in range(3):
            extension._emit(metrics.CLUSTER_CPU_USAGE, math.nan, {metrics.DIM_CLUSTER_ID: "1"})

        assert extension._metrics == []
        assert len(extension.logger.warnings) == 1
        assert metrics.CLUSTER_CPU_USAGE in extension.logger.warnings[0]

    def test_a_line_over_the_sdk_limit_is_skipped_not_raised(self, extension):
        # Escaping can double a long name; enough of them on one line pass the SDK's 2000
        # character limit, which it enforces by raising out of report_metric.
        dimensions = {f"cohesity.name{index}": '"' * 250 for index in range(5)}

        extension._emit(metrics.CLUSTER_CPU_USAGE, 1, dimensions)
        extension._emit(metrics.CLUSTER_CPU_USAGE, 1, dimensions)

        assert extension._metrics == []
        assert len(extension.logger.warnings) == 1

    def test_dimensions_are_sanitised_on_the_way_out(self, extension):
        extension._emit(metrics.CLUSTER_CPU_USAGE, 1, {metrics.DIM_CLUSTER_NAME: 'a"b\nc'})

        expected = f'{metrics.CLUSTER_CPU_USAGE},cohesity.cluster.name="a{BACKSLASH}"b c" gauge,1'
        assert extension._metrics == [expected]


class TestLogValidatorTool:
    tool = validate_metric_lines

    def write(self, tmp_path, lines: list[str]) -> str:
        path = tmp_path / "run.log"
        path.write_text("".join(lines), encoding="utf-8")
        return str(path)

    def test_a_clean_log_exits_zero(self, tmp_path, capsys):
        log = self.write(
            tmp_path,
            [
                "2026-09-21 10:00:00,000 [INFO] api (MainThread): Start sending 1 metrics to the EEC\n",
                '2026-09-21 10:00:00,000 [INFO] api (MainThread): send_metric: abc.def,ghi="x" gauge,1\n',
                "2026-09-21 10:00:00,000 [INFO] api (MainThread): send_sfm_metric: dsfm:x gauge,1\n",
            ],
        )

        assert self.tool.main([log]) == 0
        assert "1 metric line(s) checked, 0 invalid" in capsys.readouterr().out

    def test_an_invalid_line_exits_non_zero_and_says_why(self, tmp_path, capsys):
        prefix = "2026-09-21 10:00:00,000 [INFO] api (MainThread): send_metric: "
        log = self.write(
            tmp_path,
            [
                f'{prefix}abc.def,isPaused="x" gauge,1\n',
                # A raw newline in a value, as the SDK would log it: split across two lines.
                f'{prefix}abc.def,ghi="a\n',
                'b" gauge,1\n',
            ],
        )

        assert self.tool.main([log]) == 1
        out = capsys.readouterr().out
        assert "2 metric line(s) checked, 2 invalid" in out
        assert "isPaused" in out
        assert "no closing quote" in out

    def test_a_stale_timestamp_is_judged_against_the_log_time(self):
        logged_ms = int(datetime(2026, 9, 21, 10, 0, 0).timestamp() * 1000)
        fresh = f"2026-09-21 10:00:00,000 [INFO] api: send_metric: abc.def gauge,1 {logged_ms}\n"
        stale_ms = logged_ms - 2 * 3600_000
        stale = f"2026-09-21 10:00:00,000 [INFO] api: send_metric: abc.def gauge,1 {stale_ms}\n"

        checked, failures = self.tool.validate([fresh, stale])

        assert checked == 2
        assert [number for number, _reason, _line in failures] == [2]

    def test_a_log_with_no_metric_lines_is_not_an_all_clear(self, tmp_path):
        assert self.tool.main([self.write(tmp_path, ["nothing here\n"])]) == 2
