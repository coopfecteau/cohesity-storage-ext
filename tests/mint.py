"""A strict validator for one Dynatrace metric ingestion (MINT) line.

The ingest's only feedback on a bad line is "N invalid metric lines found", with no key and no
reason, so a mistake in a dimension key or an unescaped name is invisible until a real
ActiveGate reports it. This is the check that would have caught both before they shipped.

Implements the published protocol
(docs.dynatrace.com/docs/ingest-from/extend-dynatrace/extend-metrics/reference/metric-ingestion-protocol):

* metric key: letters, digits, ``-`` and ``_`` in ``.``-separated sections; it cannot start with
  a digit or ``-``, no section can start with ``-``; 3-250 characters.
* dimension key: LOWERCASE letters, digits, ``-``, ``.``, ``:`` and ``_``; 3-250 characters.
* dimension value: a quoted string in which ``"`` and ``\\`` are backslash-escaped. At most 50
  dimensions.
* payload: ``gauge,<number>``, ``count,delta=<number>`` or a bare ``<number>``.
* timestamp (optional): UTC milliseconds, at most 1 hour in the past and 10 minutes ahead.

It is stricter than the ingest in two places, both on purpose: a value must be quoted (the SDK
always quotes, so an unquoted one means something else built the line), and the only escapes
accepted are ``\\"`` and ``\\\\`` (the only two the extension ever writes).

Shared by the tests and ``tools/validate_metric_lines.py`` so there is one definition of
"valid", not two that drift.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

METRIC_KEY_MIN, METRIC_KEY_MAX = 3, 250
DIMENSION_KEY_MIN, DIMENSION_KEY_MAX = 3, 250
MAX_DIMENSIONS = 50
TIMESTAMP_MAX_PAST_MS = 60 * 60 * 1000
TIMESTAMP_MAX_FUTURE_MS = 10 * 60 * 1000

# First section may not start with a digit or '-'; later sections only may not start with '-'.
METRIC_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*(\.[A-Za-z0-9_][A-Za-z0-9_-]*)*")
DIMENSION_KEY_RE = re.compile(r"[a-z0-9_.:-]+")
NUMBER_RE = re.compile(r"[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?")
TIMESTAMP_RE = re.compile(r"\d{1,19}")


class MintLineError(ValueError):
    """The line would be rejected. The message says which rule, and where."""


@dataclass
class MintLine:
    key: str
    #: Values un-escaped, i.e. the text a Grail query would see.
    dimensions: dict[str, str] = field(default_factory=dict)
    payload_type: str = "gauge"
    value: float = 0.0
    timestamp_ms: int | None = None


def metric_key_problem(key: str) -> str | None:
    if not METRIC_KEY_MIN <= len(key) <= METRIC_KEY_MAX:
        return f"metric key {key!r} is {len(key)} characters, outside {METRIC_KEY_MIN}-{METRIC_KEY_MAX}"
    if not METRIC_KEY_RE.fullmatch(key):
        return f"metric key {key!r} has a character or section start the protocol does not allow"
    return None


def dimension_key_problem(key: str) -> str | None:
    if not DIMENSION_KEY_MIN <= len(key) <= DIMENSION_KEY_MAX:
        return (
            f"dimension key {key!r} is {len(key)} characters, "
            f"outside {DIMENSION_KEY_MIN}-{DIMENSION_KEY_MAX}"
        )
    if not DIMENSION_KEY_RE.fullmatch(key):
        return f"dimension key {key!r} may only contain lowercase letters, digits, '-', '.', ':', '_'"
    return None


def parse(line: str, now_ms: int | None = None) -> MintLine:
    """Parse one line, or raise :class:`MintLineError` naming the first rule it breaks.

    ``now_ms`` enables the timestamp window check; without it only the timestamp's form is
    checked, which is what a log read long after the run needs.
    """
    if "\n" in line or "\r" in line:
        raise MintLineError("line contains a raw line break - it would be split into two lines")

    # Key: up to the first ',' (dimensions follow) or ' ' (payload follows).
    end = _find_any(line, ", ", 0)
    if end < 0:
        raise MintLineError("no payload: a line needs at least '<key> <value>'")
    key = line[:end]
    if problem := metric_key_problem(key):
        raise MintLineError(problem)

    parsed = MintLine(key=key)
    position = end
    while position < len(line) and line[position] == ",":
        position = _parse_dimension(line, position + 1, parsed)
    if len(parsed.dimensions) > MAX_DIMENSIONS:
        raise MintLineError(f"{len(parsed.dimensions)} dimensions, over the limit of {MAX_DIMENSIONS}")

    if position >= len(line) or line[position] != " ":
        raise MintLineError(f"expected a space before the payload at column {position}")
    rest = line[position + 1 :].split(" ")
    if len(rest) not in (1, 2) or "" in rest:
        raise MintLineError(f"expected '<payload>[ <timestamp>]', got {line[position + 1 :]!r}")

    parsed.payload_type, parsed.value = _parse_payload(rest[0])
    if len(rest) == 2:
        parsed.timestamp_ms = _parse_timestamp(rest[1], now_ms)
    return parsed


def problem(line: str, now_ms: int | None = None) -> str | None:
    """The reason ``line`` is invalid, or None if it is valid."""
    try:
        parse(line, now_ms)
    except MintLineError as exception:
        return str(exception)
    return None


def unescape(value: str) -> str:
    """Undo the escaping of a quoted dimension value (without its quotes)."""
    return re.sub(r"\\(.)", r"\1", value)


def _find_any(text: str, characters: str, start: int) -> int:
    hits = [index for character in characters if (index := text.find(character, start)) >= 0]
    return min(hits) if hits else -1


def _parse_dimension(line: str, start: int, parsed: MintLine) -> int:
    equals = line.find("=", start)
    if equals < 0:
        raise MintLineError(f"dimension at column {start} has no '='")
    key = line[start:equals]
    if problem_text := dimension_key_problem(key):
        raise MintLineError(problem_text)
    if key in parsed.dimensions:
        raise MintLineError(f"dimension key {key!r} appears twice")

    position = equals + 1
    if position >= len(line) or line[position] != '"':
        raise MintLineError(f"value of {key!r} is not quoted")
    position += 1
    characters: list[str] = []
    while True:
        if position >= len(line):
            raise MintLineError(f"value of {key!r} has no closing quote")
        character = line[position]
        if character == "\\":
            if position + 1 >= len(line) or line[position + 1] not in '"\\':
                raise MintLineError(f"value of {key!r} has a backslash that escapes nothing valid")
            characters.append(line[position + 1])
            position += 2
            continue
        if character == '"':
            position += 1
            break
        characters.append(character)
        position += 1

    # After the closing quote only the next dimension or the payload may follow. Anything else
    # is the tail of a value that an unescaped quote cut short.
    if position < len(line) and line[position] not in ", ":
        raise MintLineError(f"value of {key!r} is followed by {line[position]!r} - an unescaped quote?")
    parsed.dimensions[key] = "".join(characters)
    return position


def _parse_payload(token: str) -> tuple[str, float]:
    if token.startswith("gauge,"):
        kind, number = "gauge", token[len("gauge,") :]
    elif token.startswith("count,delta="):
        kind, number = "count,delta", token[len("count,delta=") :]
    else:
        kind, number = "gauge", token
    if not NUMBER_RE.fullmatch(number):
        raise MintLineError(f"payload {token!r} is not one of gauge,<n> / count,delta=<n> / <n>")
    return kind, float(number)


def _parse_timestamp(token: str, now_ms: int | None) -> int:
    if not TIMESTAMP_RE.fullmatch(token):
        raise MintLineError(f"timestamp {token!r} is not an integer of milliseconds")
    timestamp = int(token)
    if now_ms is not None:
        if timestamp < now_ms - TIMESTAMP_MAX_PAST_MS:
            raise MintLineError(f"timestamp {timestamp} is more than an hour in the past")
        if timestamp > now_ms + TIMESTAMP_MAX_FUTURE_MS:
            raise MintLineError(f"timestamp {timestamp} is more than ten minutes in the future")
    return timestamp
