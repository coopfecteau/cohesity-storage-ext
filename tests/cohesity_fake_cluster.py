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

Following ``ssh_ext``: the server lives here so the tests can use it in process, and
``tools/local_cohesity_server.py`` is the runnable wrapper around it.
"""

from __future__ import annotations

import datetime
import ipaddress
import json
import ssl
import threading
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
) -> Response:
    """Answer one request. Pure, so the whole surface is testable without a socket.

    Args:
        api_key: the one key to accept, or None to accept any non-empty key. Empty is never
            accepted - an extension that forgets the header must see a 401, not data.
        software_version: overrides what /v2/clusters/status reports, which is what makes the
            7.3 views fork demonstrable from a fixture set recorded on 7.4.
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
