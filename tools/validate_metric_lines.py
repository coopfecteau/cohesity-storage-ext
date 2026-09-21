"""Check every metric line in a `dt-sdk run` log against the ingestion protocol.

    dt-sdk run 2>&1 | tee run.log
    python tools/validate_metric_lines.py run.log
    python tools/validate_metric_lines.py < run.log

A local `dt-sdk run` never talks to an ingest, so an invalid line there looks exactly like a
valid one - the first anyone hears of it is a real ActiveGate reporting "N invalid metric lines
found" with no key and no reason. This reads the ``send_metric: <line>`` entries the SDK logs
instead of sending, validates each with the same rules as the test suite (``tests/mint.py``),
and exits non-zero on any failure.

Pair it with ``tools/local_cohesity_server.py --hostile-names`` to prove that names carrying
quotes, backslashes and newlines still produce valid lines.

A raw newline inside a value splits the logged line in two; the first half then fails as an
unterminated value, which is the right verdict - the ingest would split it the same way.
"""

from __future__ import annotations

import argparse
import datetime
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tests import mint  # noqa: E402

MARKER = "send_metric: "
# The SDK's log format starts "%(asctime)s", local time with comma milliseconds. When present it
# is the reference for the timestamp window, so a log can be checked long after the run.
LOG_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}),(\d{3})")


def extract(log_line: str) -> tuple[str, int | None] | None:
    """The metric line a log line carries and the log's own time in ms, or None."""
    index = log_line.find(MARKER)
    if index < 0:
        return None
    metric_line = log_line[index + len(MARKER) :].rstrip("\r\n")
    now_ms = None
    if match := LOG_TIME.match(log_line):
        logged = datetime.datetime.strptime(match.group(1), "%Y-%m-%d %H:%M:%S")
        now_ms = int(logged.timestamp() * 1000) + int(match.group(2))
    return metric_line, now_ms


def validate(log_lines) -> tuple[int, list[tuple[int, str, str]]]:
    """Return (lines checked, [(log line number, reason, metric line)] for every failure)."""
    checked = 0
    failures: list[tuple[int, str, str]] = []
    for number, log_line in enumerate(log_lines, start=1):
        found = extract(log_line)
        if found is None:
            continue
        checked += 1
        metric_line, now_ms = found
        if reason := mint.problem(metric_line, now_ms):
            failures.append((number, reason, metric_line))
    return checked, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("log", nargs="?", help="a dt-sdk run log; default: read stdin")
    parser.add_argument("--show", type=int, default=10, help="how many failures to print (default 10)")
    args = parser.parse_args(argv)
    # Hostile names are non-ASCII on purpose; a Windows console must not crash on reporting them.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")

    if args.log:
        with open(args.log, encoding="utf-8", errors="replace") as handle:
            checked, failures = validate(handle)
    else:
        checked, failures = validate(sys.stdin)

    print(f"{checked} metric line(s) checked, {len(failures)} invalid")
    for number, reason, metric_line in failures[: args.show]:
        print(f"  log line {number}: {reason}")
        print(f"    {metric_line[:300]}")
    if len(failures) > args.show:
        print(f"  ... and {len(failures) - args.show} more")
    if checked == 0:
        # Almost always the wrong file, or a run that never reached a poll. A pass here would
        # be a false "all clear".
        print("No 'send_metric:' lines found - was this a dt-sdk run log?")
        return 2
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
