"""Recorded Cohesity responses on disk, and the provenance that says how much to trust them.

Access to a real cluster is uncertain and may stay uncertain, so the extension has to be
buildable and demonstrable without one. That is what these are for. It is also exactly why they
are dangerous: a hand-written fixture makes the extension *look* finished while encoding a
guess from the published schema as if it were an observed fact. Every fixture therefore carries
a provenance marker, and anything reading fixtures can tell captured from invented.

File format - an envelope around the body the cluster would have returned::

    {
      "_fixture": {
        "provenance": "synthetic",          // or "captured"
        "description": "...",
        "source": "https://developers.cohesity.com/...",
        "capturedAt": null,                  // ISO-8601 when provenance is "captured"
        "clusterVersion": "7.4"
      },
      "body": { ... }                        // verbatim response body
    }

A bare JSON document with no envelope is still loaded, as provenance ``unknown`` - dropping a
raw capture into the directory should work. Unknown is treated as untrusted, not as captured:
the point of the marker is that the absence of evidence is visible.

Naming. The file name is derived from the request, by :func:`fixture_key`, so the replay
transport and the fake server resolve the same file for the same call without a lookup table
that can drift. Query parameters that *select different data* (``schemaName``, ``metric``, and
the ``include*`` flags) become name suffixes; parameters that only bound a time window do not,
because a fixture is a shape, not a moment.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import CohesityFixtureError

PROVENANCE_CAPTURED = "captured"
PROVENANCE_SYNTHETIC = "synthetic"
PROVENANCE_UNKNOWN = "unknown"

# Parameters whose value selects which data comes back, so a fixture per value is needed.
_SELECTOR_PARAMS = ("schemaName", "metric")

# Boolean parameters that change the *shape* of the response rather than its contents.
_SHAPE_FLAGS = (
    ("includeTimeSeriesSchema", "timeseriesschema"),
    ("includeStats", "stats"),
)

_TRUTHY = ("true", "1", "yes", "on")


def fixture_key(path: str, params: dict[str, Any] | None = None) -> str:
    """Stable file stem for one request. ``/v2/stats/top-views?metric=kNumBytesRead`` ->
    ``v2_stats_top-views__kNumBytesRead``.
    """
    params = params or {}
    parts = [path.strip("/").replace("/", "_")]
    for name in _SELECTOR_PARAMS:
        value = params.get(name)
        if value:
            parts.append(str(value))
    for name, label in _SHAPE_FLAGS:
        if str(params.get(name, "")).strip().lower() in _TRUTHY:
            parts.append(label)
    return "__".join(parts)


@dataclass(frozen=True)
class Fixture:
    """One recorded response plus what is known about where it came from."""

    key: str
    path: Path
    body: Any
    provenance: str = PROVENANCE_UNKNOWN
    description: str = ""
    source: str = ""
    captured_at: str = ""
    cluster_version: str = ""

    @property
    def is_captured(self) -> bool:
        """True only for a response a real cluster actually sent."""
        return self.provenance == PROVENANCE_CAPTURED

    def caveat(self) -> str:
        """A one-line warning to log, or empty when the fixture is a real capture.

        Returned rather than logged so this module stays free of the SDK logger and can be
        used by the fake server and the tests as well as by the extension.
        """
        if self.is_captured:
            return ""
        if self.provenance == PROVENANCE_SYNTHETIC:
            return (
                f"{self.key}: SYNTHETIC fixture - hand-written from the published response "
                f"schema, never seen on a cluster. Field names are doc-derived and the values "
                f"are invented; do not read anything into the numbers"
            )
        return (
            f"{self.key}: fixture declares no provenance, so it cannot be told apart from a "
            f"guess. Add a '_fixture' envelope saying whether it was captured or hand-written"
        )


class FixtureStore:
    """A directory of recorded responses, addressed the same way requests are."""

    def __init__(self, directory: str | Path):
        self.directory = Path(directory)

    def path_for(self, key: str) -> Path:
        return self.directory / f"{key}.json"

    def fixture_keys(self) -> list[str]:
        if not self.directory.is_dir():
            return []
        return sorted(item.stem for item in self.directory.glob("*.json"))

    def load(self, key: str) -> Fixture:
        """Read one fixture by key.

        Raises:
            CohesityFixtureError: naming the key, the directory and how to produce the file.
        """
        if not self.directory.is_dir():
            msg = (
                f"fixture directory {self.directory} does not exist. Replay mode is on because "
                f"'fixtureDir' is set in the monitoring configuration; clear it to talk to a "
                f"real cluster, or point it at a directory of recorded responses"
            )
            raise CohesityFixtureError(msg)

        path = self.path_for(key)
        if not path.is_file():
            available = ", ".join(self.fixture_keys()) or "none"
            msg = (
                f"no fixture for '{key}' in {self.directory}. Record one by saving the response "
                f"body to {path.name}, or copy an existing fixture and edit it (available: "
                f"{available})"
            )
            raise CohesityFixtureError(msg)

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exception:
            msg = f"fixture {path} could not be read as JSON. Fix or re-record it ({exception})"
            raise CohesityFixtureError(msg) from exception

        return _fixture_from(key, path, raw)

    def get(self, path: str, params: dict[str, Any] | None = None) -> Fixture:
        """The fixture that answers this request."""
        return self.load(fixture_key(path, params))

    def provenance_summary(self) -> dict[str, list[str]]:
        """Fixture keys grouped by provenance - what a fastcheck reports and a runbook prints."""
        summary: dict[str, list[str]] = {}
        for key in self.fixture_keys():
            try:
                provenance = self.load(key).provenance
            except CohesityFixtureError:
                provenance = PROVENANCE_UNKNOWN
            summary.setdefault(provenance, []).append(key)
        return summary


def _fixture_from(key: str, path: Path, raw: Any) -> Fixture:
    if isinstance(raw, dict) and isinstance(raw.get("_fixture"), dict):
        meta = raw["_fixture"]
        provenance = str(meta.get("provenance") or PROVENANCE_UNKNOWN).strip().lower()
        if provenance not in (PROVENANCE_CAPTURED, PROVENANCE_SYNTHETIC):
            provenance = PROVENANCE_UNKNOWN
        return Fixture(
            key=key,
            path=path,
            # "body" absent is a real possibility for a fixture that records an empty response;
            # None is a legitimate body and must not be turned into an error here.
            body=raw.get("body"),
            provenance=provenance,
            description=str(meta.get("description") or ""),
            source=str(meta.get("source") or ""),
            captured_at=str(meta.get("capturedAt") or ""),
            cluster_version=str(meta.get("clusterVersion") or ""),
        )
    return Fixture(key=key, path=path, body=raw, provenance=PROVENANCE_UNKNOWN)
