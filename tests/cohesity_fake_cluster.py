"""A stand-in Cohesity cluster that serves the recorded fixtures over HTTP(S).

Access to a real cluster is uncertain and may stay uncertain, so the extension has to be
runnable end to end without one. This is the other half of that: replay mode proves the client
parses recorded bodies, this proves the whole loop - socket, TLS, header auth, HTTP status
codes, `dt-sdk run` - works before anyone has credentials.

It is deliberately more than a file server. The three things most likely to be got wrong in the
field are modelled, so they can be seen failing here rather than at a customer:

* **The API key header.** A request with no ``apiKey`` header gets a 401, not a body. That is
  the path the extension's auth message is written for.
* **The 7.3 version fork.** ``--software-version 7.2`` makes ``/v2/stats/top-views`` return 404,
  exactly as a pre-7.3 cluster does, so the fallback is exercised rather than assumed.
* **Stale fixture timestamps.** Run times in a hand-written fixture are frozen at the day they
  were written, so an age-since-last-success computed against them grows without bound. The
  server shifts time fields forward by default so the numbers stay plausible; ``anchor=None``
  turns that off and serves the bodies verbatim.
* **Flat lines.** Static fixtures make every metric a constant, which hides whether a chart,
  a rate or a run counter is actually wired. ``drift_now`` (``--drift`` on the wrapper) makes
  the numbers move plausibly over time and mints a fresh completed protection run per group
  every few minutes, so counters increment. Off by default: every other caller still sees the
  recorded bodies.

Following ``ssh_ext``: the server lives here so the tests can use it in process, and
``tools/local_cohesity_server.py`` is the runnable wrapper around it.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import ipaddress
import json
import ssl
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from cohesity_storage.fixtures import FixtureStore, fixture_key

# The instant the shipped synthetic fixtures were written against. Time-shifting is relative to
# this, so it is meaningful only for those fixtures - a real capture carries its own anchor and
# should be served verbatim.
SYNTHETIC_FIXTURE_ANCHOR = datetime.datetime(2026, 9, 15, 12, 0, 0, tzinfo=datetime.UTC)

TOP_VIEWS_PATH = "/v2/stats/top-views"
CLUSTER_STATUS_PATH = "/v2/clusters/status"
TOP_VIEWS_MIN_VERSION = (7, 3)

# Field-name suffixes Cohesity uses for absolute instants, and the multiplier that turns one
# second into that field's units.
_TIME_SUFFIXES = (
    ("TimeUsecs", 1_000_000),
    ("TimestampUsec", 1_000_000),
    ("TimestampUsecs", 1_000_000),
    ("timestampMsecs", 1_000),
    ("TimeMsecs", 1_000),
)


@dataclass
class Response:
    status: int
    body: Any


def resolve(
    store: FixtureStore,
    path: str,
    params: dict[str, str],
    headers: dict[str, str],
    *,
    api_key: str | None = None,
    software_version: str | None = None,
    shift_seconds: float = 0.0,
    drift_now: float | None = None,
) -> Response:
    """Answer one request. Pure, so the whole surface is testable without a socket.

    Args:
        api_key: the one key to accept, or None to accept any non-empty key. Empty is never
            accepted - an extension that forgets the header must see a 401, not data.
        software_version: overrides what /v2/clusters/status reports, which is what makes the
            7.3 views fork demonstrable from a fixture set recorded on 7.4.
        drift_now: a unix time to drift the numbers to (see :func:`drift`), or None to serve
            the recorded values. Applied after time-shifting, so generated runs are dated
            against the same clock as the shifted ones.
    """
    presented = (headers.get("apikey") or "").strip()
    if not presented or (api_key is not None and presented != api_key):
        return Response(
            401,
            {
                "errorCode": "KUnauthorized",
                "message": "Authentication failed. Provide a valid cluster API key in the apiKey header.",
            },
        )

    effective_version = software_version or _fixture_software_version(store)
    if path == TOP_VIEWS_PATH and not _has_top_views(effective_version):
        return Response(
            404,
            {
                "errorCode": "KNotFound",
                "message": (
                    f"/stats/top-views is not available on {effective_version}. "
                    f"It was introduced in 7.3; use /stats/views."
                ),
            },
        )

    try:
        fixture = store.get(path, params)
    except Exception as exception:  # noqa: BLE001 - any fixture problem is a 404 to the caller
        return Response(
            404,
            {
                "errorCode": "KNotFound",
                "message": f"No fixture recorded for {fixture_key(path, params)} ({exception})",
            },
        )

    body = fixture.body
    if software_version and path == CLUSTER_STATUS_PATH and isinstance(body, dict):
        body = dict(body)
        body["softwareVersion"] = software_version
    if shift_seconds:
        body = shift_times(body, shift_seconds)
    if drift_now is not None:
        body = drift(path, body, drift_now)
    return Response(200, body)


def shift_times(payload: Any, seconds: float) -> Any:
    """Move every absolute-time field forward, so frozen fixture data still reads as recent.

    Only fields whose *name* says they are an instant are touched. Durations, sizes and counts
    are left exactly as recorded - shifting one of those would quietly invent data rather than
    merely re-date it.
    """
    if isinstance(payload, dict):
        shifted = {}
        for name, value in payload.items():
            multiplier = _time_multiplier(name)
            if multiplier and isinstance(value, int) and not isinstance(value, bool):
                shifted[name] = value + int(seconds * multiplier)
            else:
                shifted[name] = shift_times(value, seconds)
        return shifted
    if isinstance(payload, list):
        return [shift_times(item, seconds) for item in payload]
    return payload


def _time_multiplier(name: str) -> int:
    for suffix, multiplier in _TIME_SUFFIXES:
        if name.endswith(suffix):
            return multiplier
    return 0


def _has_top_views(software_version: str) -> bool:
    parts: list[int] = []
    for chunk in software_version.split("_")[0].split("."):
        if not chunk.isdigit():
            break
        parts.append(int(chunk))
    return bool(parts) and tuple(parts) >= TOP_VIEWS_MIN_VERSION


def _fixture_software_version(store: FixtureStore) -> str:
    try:
        body = store.load(fixture_key(CLUSTER_STATUS_PATH)).body
    except Exception:  # noqa: BLE001 - no status fixture means no version claim to make
        return ""
    return str(body.get("softwareVersion", "")) if isinstance(body, dict) else ""


# -- drift ----------------------------------------------------------------------------------
#
# Everything below is a pure function of (path, body, now), so a test can pin the clock and a
# restarted server picks up where it left off instead of resetting every series. Hash-derived
# noise rather than `random` for the same reason: the same minute always yields the same value.

# One completed run per protection group per period. Five minutes is well inside a 10-minute
# e2e timeout yet slow enough that a 1-minute poll sees each run several times, which is what
# exercises the run-id dedup rather than bypassing it.
DRIFT_RUN_PERIOD_SECONDS = 300
# How many past periods of generated runs a response carries. More than one, because a real
# runs/summary window overlaps earlier polls and the extension must not recount those.
DRIFT_RUN_LOOKBACK = 3
DRIFT_FAILURE_RATE = 0.1
# Capacity creeps up over a day and falls back, like ingest followed by garbage collection -
# a monotonic creep would fill the synthetic cluster within weeks of uptime.
DRIFT_CAPACITY_CYCLE_SECONDS = 24 * 3600
DRIFT_CAPACITY_SWING = 0.02
# +/- this fraction on rates, latencies and throughput, re-drawn every minute.
DRIFT_NOISE = 0.15

CLUSTER_STORAGE_PATH = "/v2/stats/cluster-storage"
TIME_SERIES_STATS_PATH = "/v2/stats/time-series-stats"
VIEWS_PATHS = (TOP_VIEWS_PATH, "/v2/stats/views")
STORAGE_DOMAINS_PATH = "/v2/storage-domains"
RUNS_SUMMARY_PATH = "/v2/data-protect/runs/summary"


def drift(path: str, body: Any, now: float) -> Any:
    """Return a copy of ``body`` with its numbers moved to where they would be at ``now``.

    Values a real cluster reports as flat (ids, names, node counts, total capacity) are left
    alone; only values that genuinely move - capacity used, IOPS, latency, CPU, throughput, and
    the stream of completed runs - drift. The fixture itself is never touched, so its provenance
    envelope still says SYNTHETIC, which is what these numbers remain.
    """
    body = copy.deepcopy(body)
    if not isinstance(body, dict):
        return body
    if path == CLUSTER_STORAGE_PATH:
        _drift_cluster_storage(body, now)
    elif path == TIME_SERIES_STATS_PATH:
        _drift_time_series(body, now)
    elif path in VIEWS_PATHS:
        _drift_views(body, now)
    elif path == STORAGE_DOMAINS_PATH:
        _drift_storage_domains(body, now)
    elif path == RUNS_SUMMARY_PATH:
        _drift_runs(body, now)
    return body


def _unit(*parts: Any) -> float:
    """A stable pseudo-random number in [0, 1) for the given key."""
    digest = hashlib.blake2b("|".join(str(part) for part in parts).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big") / 2**64


def _noise(now: float, *key: Any) -> float:
    return 1.0 + DRIFT_NOISE * (2.0 * _unit(int(now // 60), *key) - 1.0)


def _capacity_phase(now: float) -> float:
    return (now % DRIFT_CAPACITY_CYCLE_SECONDS) / DRIFT_CAPACITY_CYCLE_SECONDS


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _scaled(value: Any, factor: float) -> Any:
    if not _is_number(value):
        return value
    return int(round(value * factor)) if isinstance(value, int) else round(value * factor, 3)


def _drift_cluster_storage(body: dict, now: float) -> None:
    total, used = body.get("totalCapacityBytes"), body.get("localUsageBytes")
    if not (_is_number(total) and _is_number(used)):
        return
    growth = int(total * DRIFT_CAPACITY_SWING * _capacity_phase(now))
    used = min(int(used) + growth, int(total))
    body["localUsageBytes"] = used
    # Kept consistent with used, or used + available != total gives the fake away on a chart.
    body["localAvailableBytes"] = int(total) - used
    if _is_number(body.get("dataProtectionPhysicalUsageBytes")):
        body["dataProtectionPhysicalUsageBytes"] = int(body["dataProtectionPhysicalUsageBytes"]) + growth
    if _is_number(body.get("dataProtectionLogicalUsageBytes")):
        # Logical grows faster than physical: that gap is the data reduction ratio.
        body["dataProtectionLogicalUsageBytes"] = int(body["dataProtectionLogicalUsageBytes"]) + 5 * growth


def _drift_time_series(body: dict, now: float) -> None:
    for series in body.get("timeSeriesStats") or []:
        if not isinstance(series, dict):
            continue
        name = str(series.get("metricName", ""))
        for index, point in enumerate(series.get("dataPoints") or []):
            if not isinstance(point, dict):
                continue
            factor = _noise(now, name, index)
            for field_name in ("int64Value", "doubleValue"):
                value = _scaled(point.get(field_name), factor)
                if _is_number(value) and name.endswith("Pct"):
                    value = min(value, 100.0)
                point[field_name] = value


def _drift_views(body: dict, now: float) -> None:
    for view in body.get("viewsStats") or []:
        if not isinstance(view, dict):
            continue
        factor = _noise(now, "view", view.get("viewId"))
        for stat in view.get("stats") or []:
            windows = stat.get("valueInLastHours") if isinstance(stat, dict) else None
            for window in windows or []:
                if not isinstance(window, dict):
                    continue
                for field_name, value in list(window.items()):
                    if field_name != "lastHours":
                        window[field_name] = _scaled(value, factor)


def _drift_storage_domains(body: dict, now: float) -> None:
    factor = 1.0 + DRIFT_CAPACITY_SWING * _capacity_phase(now)
    for domain in body.get("storageDomains") or []:
        stats = domain.get("stats") if isinstance(domain, dict) else None
        if not isinstance(stats, dict):
            continue
        for field_name in (
            "totalLogicalUsageBytes",
            "localTotalPhysicalUsageBytes",
            "localTierResiliencyImpactBytes",
        ):
            stats[field_name] = _scaled(stats.get(field_name), factor)


def _drift_runs(body: dict, now: float) -> None:
    """Append freshly completed runs, one per group per period, with ids never served before."""
    runs = body.get("protectionRunsSummary")
    if not isinstance(runs, list):
        return
    templates: dict[str, dict] = {}
    for run in runs:
        if isinstance(run, dict) and run.get("protectionGroupId") and run.get("id"):
            templates.setdefault(str(run["protectionGroupId"]), run)

    period = DRIFT_RUN_PERIOD_SECONDS
    # The newest period that has fully elapsed, so every generated run has already ended.
    latest = int(now // period) - 1
    for bucket in range(latest - DRIFT_RUN_LOOKBACK + 1, latest + 1):
        for group_id, template in templates.items():
            start = bucket * period + int(10 + 50 * _unit(group_id, bucket, "start"))
            end = start + int(30 + (period - 120) * _unit(group_id, bucket, "duration"))
            failed = _unit(group_id, bucket, "outcome") < DRIFT_FAILURE_RATE
            factor = 0.5 + _unit(group_id, bucket, "size")
            total_objects = template.get("totalObjectsCount")
            succeeded_objects = total_objects
            if failed and _is_number(total_objects):
                succeeded_objects = int(total_objects * _unit(group_id, bucket, "objects"))
            runs.append(
                {
                    **template,
                    # Deterministic per period: re-polling serves the same run again, which
                    # exercises the extension's dedup instead of inventing a second run.
                    "id": f"{template['id']}-drift-{bucket}",
                    "status": "Failed" if failed else "Succeeded",
                    "startTimeUsecs": start * 1_000_000,
                    "endTimeUsecs": end * 1_000_000,
                    "bytesWritten": 0 if failed else _scaled(template.get("bytesWritten"), factor),
                    "logicalSizeBytes": _scaled(template.get("logicalSizeBytes"), factor),
                    "isSlaViolated": failed,
                    "successObjectsCount": succeeded_objects,
                    "totalObjectsCount": total_objects,
                }
            )


@dataclass
class FakeCohesityCluster:
    """The resolver above, wrapped in a real socket so `dt-sdk run` can talk to it."""

    store: FixtureStore
    api_key: str | None = None
    software_version: str | None = None
    anchor: datetime.datetime | None = SYNTHETIC_FIXTURE_ANCHOR
    host: str = "127.0.0.1"
    port: int = 0
    certfile: str = ""
    drift: bool = False
    requests: list[str] = field(default_factory=list)

    def __post_init__(self):
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def shift_seconds(self) -> float:
        if self.anchor is None:
            return 0.0
        now = datetime.datetime.now(tz=datetime.UTC)
        return (now - self.anchor).total_seconds()

    @property
    def url(self) -> str:
        scheme = "https" if self.certfile else "http"
        return f"{scheme}://{self.host}:{self.port}"

    def start(self) -> tuple[str, int]:
        cluster = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):  # noqa: N802 - BaseHTTPRequestHandler's naming
                parsed = urllib.parse.urlparse(self.path)
                params = {
                    name: values[0] for name, values in urllib.parse.parse_qs(parsed.query).items()
                }
                headers = {name.lower(): value for name, value in self.headers.items()}
                cluster.requests.append(self.path)
                answer = resolve(
                    cluster.store,
                    parsed.path,
                    params,
                    headers,
                    api_key=cluster.api_key,
                    software_version=cluster.software_version,
                    shift_seconds=cluster.shift_seconds,
                    drift_now=time.time() if cluster.drift else None,
                )
                encoded = json.dumps(answer.body).encode("utf-8")
                self.send_response(answer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format, *_args):
                # Silent by default; the runnable wrapper prints its own one-line access log.
                return

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        if self.certfile:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(self.certfile)
            self._server.socket = context.wrap_socket(self._server.socket, server_side=True)
        self.host, self.port = self._server.server_address[0], self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.host, self.port

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FakeCohesityCluster:
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()


def self_signed_certificate(directory: Path, host: str = "127.0.0.1") -> str:
    """Write a throwaway cert+key PEM so the fake cluster can serve HTTPS.

    The client always speaks HTTPS - Cohesity is an appliance on 443 and weakening that for a
    test would be testing the wrong thing - so the fake needs a certificate. It is self-signed,
    which is also what a real Cohesity ships, so the extension meets the same trust problem here
    that it will meet in the field: either point caCertPath at this file or set verifyTls false.

    Raises:
        RuntimeError: if `cryptography` is not installed. It is a development-only dependency,
            never imported by the extension itself.
    """
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.x509.oid import NameOID
    except ImportError as exception:  # pragma: no cover - depends on the developer's venv
        msg = (
            "generating a certificate needs the 'cryptography' package. Install it, pass "
            "--certfile with your own PEM, or run the server with --http"
        )
        raise RuntimeError(msg) from exception

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)])
    now = datetime.datetime.now(tz=datetime.UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.SubjectAlternativeName(_subject_names(host)), critical=False)
        .sign(key, hashes.SHA256())
    )

    directory.mkdir(parents=True, exist_ok=True)
    pem_path = directory / "fake-cohesity.pem"
    pem_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        + certificate.public_bytes(serialization.Encoding.PEM)
    )
    return str(pem_path)


def _subject_names(host: str) -> list:
    """SANs covering however the developer spells the loopback address.

    An IP literal needs an IPAddress SAN, not a DNSName one - a certificate that only names
    "localhost" fails verification the moment someone points caCertPath at it and connects to
    127.0.0.1, which is the exact thing this certificate exists to let them try.
    """
    from cryptography import x509

    names = [x509.DNSName("localhost")]
    try:
        names.append(x509.IPAddress(ipaddress.ip_address(host)))
    except ValueError:
        names.append(x509.DNSName(host))
    return names
