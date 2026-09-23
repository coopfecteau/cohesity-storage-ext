"""How a request reaches a response: over HTTPS to a cluster, or off disk from a fixture.

Both transports expose one method, ``get(path, params) -> parsed JSON``, and raise the same
errors. Nothing above them can tell which is in use - that is the whole point. Replay is
selected by setting ``fixtureDir`` in the monitoring configuration, in one place
(:func:`build_transport`), rather than by an ``if replay:`` sprinkled through the callers.

stdlib ``urllib`` rather than ``requests``, deliberately: nothing then has to be vendored into
``extension/lib``, so the first signed build has no wheel-compatibility surface at all. The
cost is no connection pooling and no retry helper. At nine calls per five minutes that cost is
not yet worth a vendored dependency tree; revisit if the poll ever fans out per object.
"""

from __future__ import annotations

import json
import ssl
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import ClusterConfig
from .errors import (
    CohesityApiError,
    CohesityAuthError,
    CohesityConnectError,
    CohesityEndpointError,
    CohesityError,
    annotate,
)
from .fixtures import Fixture, FixtureStore

# Cohesity's v2 API is served under this prefix on the cluster VIP. The reference sets are
# published under "v1-cluster-<version>" slugs, which is portal versioning, not API versioning:
# every page in them declares COHESITY REST API V2 and a /v2 server URL.
API_PREFIX = "/v2"

# The legacy "public" API is served from its own prefix on the same VIP. Nothing in the metric
# set comes from it; it is reachable only so the entityId probe can ask v1 /public/cluster for
# the id Cohesity's own community exporters pass to every cluster-level time-series call.
V1_PREFIX = "/irisservices/api/v1"


class Repeated(tuple):
    """Query values to send as repeated ``name=value`` pairs rather than comma-joined.

    The opposite of what :func:`encode_params` does to every other sequence, and deliberately
    hard to reach. Cohesity declares ``metricNames`` as ``explode: false``, so the comma-joined
    form is the documented one and the repeated form is the mistake most HTTP clients make by
    default - the cluster then reads only the last value and the series is silently empty.

    This type exists for exactly one caller: the parameter-variant probe in :mod:`.client`,
    which has to be able to ask the *un*documented shape when a cluster answers HTTP 500 to the
    documented one. Nothing else should construct one.
    """

    __slots__ = ()


class HttpTransport:
    """Authenticated GETs against one Cohesity cluster."""

    def __init__(self, config: ClusterConfig):
        self._config = config
        self._ssl_context = build_ssl_context(config)

    def close(self) -> None:
        """Release anything held open between polls.

        Nothing to release: urlopen opens and closes a connection per call. Kept so a pooled
        session can be introduced later without hunting down every shutdown path.
        """

    def describe(self) -> str:
        return f"{self._config.base_url}{API_PREFIX}"

    def get(
        self, path: str, params: dict[str, Any] | None = None, *, prefix: str = API_PREFIX
    ) -> Any:
        """GET a JSON document from the cluster.

        ``prefix`` exists only for the one v1 call the entityId probe makes; everything else
        takes the default and stays on /v2.

        Raises:
            CohesityAuthError, CohesityConnectError, CohesityEndpointError, CohesityApiError:
                each with a message naming the cluster, saying what to do, and keeping the raw
                error as a suffix.
        """
        url = f"{self._config.base_url}{prefix}{path}"
        query = encode_params(params)
        if query:
            url = f"{url}?{query}"
        # Names, never values. Every failure below carries these so a diagnostic can say which
        # call went wrong without anyone having to guess from the sentence.
        param_names = tuple(params or ())

        request = urllib.request.Request(  # noqa: S310 - scheme is fixed https by base_url
            url,
            headers={
                # Cohesity's cluster API key auth. Not a Bearer token: the username/password
                # flow returns one of those and it expires after 24 hours, which an unattended
                # extension would then have to refresh for no benefit.
                "apiKey": self._config.api_key,
                "Accept": "application/json",
            },
            method="GET",
        )

        try:
            with urllib.request.urlopen(  # noqa: S310 - see above
                request, timeout=self._config.request_timeout_seconds, context=self._ssl_context
            ) as response:
                body = response.read()
        except urllib.error.HTTPError as exception:
            error = self._http_error(path, exception)
            raise annotate(
                error, path=path, params=param_names, status=exception.code
            ) from exception
        except urllib.error.URLError as exception:
            error = self._url_error(path, exception)
            raise annotate(error, path=path, params=param_names) from exception
        except ssl.SSLError as exception:
            # Reached when the handshake fails outside a URLError wrapper, which happens for
            # protocol-version and cipher mismatches against older cluster TLS stacks.
            raise annotate(self._tls_error(exception), path=path, params=param_names) from exception
        except TimeoutError as exception:
            msg = (
                f"{self._config.name}: {path} did not answer within "
                f"{self._config.request_timeout_seconds}s. Raise the request timeout, or check "
                f"whether the cluster is under load ({exception})"
            )
            error = CohesityConnectError(msg)
            raise annotate(error, path=path, params=param_names) from exception

        try:
            return self._decode(path, body)
        except CohesityError as exception:
            # Annotated rather than rebuilt: a body that is not JSON is still a failure of this
            # request, and the diagnostic wants to name it like any other.
            annotate(exception, path=path, params=param_names)
            raise

    def _http_error(self, path: str, exception: urllib.error.HTTPError):
        if exception.code in (401, 403):
            # 403 usually means the key is valid but its owner lacks a privilege.
            # /v2/stats/time-series-stats alone needs five, which is why it is named here.
            #
            # The auth source is named because an empty credential and a wrong credential both
            # arrive here as a 401. config.py has already refused an empty one, so reaching this
            # point means a real value was sent and the *cluster* refused it - which is the fix
            # nobody should be guessing at when a vault entry is in play.
            msg = (
                f"{self._config.name}: the cluster rejected the API key on {path}. A non-empty "
                f"key was sent, read from {self._config.auth_source}, so it resolved correctly "
                f"and the cluster is refusing it - this is not a credential vault problem. "
                f"Confirm the key is still listed under Settings > Access Management > API Keys, "
                f"and that its owner holds CLUSTER_VIEW, TENANT_VIEW, STORAGE_DOMAIN_VIEW, "
                f"STORAGE_VIEW and PROTECTION_VIEW (HTTP {exception.code} {exception.reason})"
            )
            return CohesityAuthError(msg)
        if exception.code == 404:
            msg = (
                f"{self._config.name}: {path} is not present on this cluster. Endpoints were "
                f"added across the 6.8-7.4 range - /v2/stats/top-views, for one, does not exist "
                f"before 7.3 - so this is most likely a cluster older than the call rather than "
                f"a fault. Check softwareVersion from /v2/clusters/status "
                f"(HTTP 404 {exception.reason})"
            )
            return CohesityEndpointError(msg)
        if exception.code == 429 or exception.code >= 500:
            msg = (
                f"{self._config.name}: {path} failed on the cluster side and should be retried "
                f"next interval. Check the cluster's own alerts and audit log for this "
                f"timestamp (HTTP {exception.code} {exception.reason})"
            )
            return CohesityApiError(msg)
        msg = (
            f"{self._config.name}: {path} was refused. Check the request against the cluster's "
            f"API version (HTTP {exception.code} {exception.reason})"
        )
        return CohesityApiError(msg)

    def _url_error(self, path: str, exception: urllib.error.URLError):
        reason = exception.reason
        if isinstance(reason, ssl.SSLError):
            return self._tls_error(reason)
        msg = (
            f"{self._config.name}: cannot reach {self._config.base_url}{API_PREFIX}{path}. Check "
            f"that the ActiveGate can route to the cluster VIP on port {self._config.port} and "
            f"that no firewall sits between them ({reason})"
        )
        return CohesityConnectError(msg)

    def _tls_error(self, reason: ssl.SSLError) -> CohesityConnectError:
        if isinstance(reason, ssl.SSLCertVerificationError):
            msg = (
                f"{self._config.name}: the cluster's TLS certificate was not trusted. Cohesity "
                f"ships a self-signed certificate, so point 'CA certificate file path' at its CA "
                f"on the ActiveGate, or turn TLS verification off for a lab cluster ({reason})"
            )
        else:
            msg = (
                f"{self._config.name}: the TLS handshake with the cluster failed before any "
                f"request was sent. This is a transport problem, not a credential one - check "
                f"that port {self._config.port} really serves HTTPS and that the cluster's TLS "
                f"version is still accepted by the ActiveGate's Python ({reason})"
            )
        return CohesityConnectError(msg)

    def _decode(self, path: str, body: bytes) -> Any:
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exception:
            # Almost always an HTML login page from a proxy or load balancer in front of the
            # cluster, which answers 200 and looks like success until you read the body.
            msg = (
                f"{self._config.name}: {path} answered with something that is not JSON. Check "
                f"whether a proxy or load balancer is intercepting the request ({exception})"
            )
            raise CohesityApiError(msg) from exception


class FixtureTransport:
    """Serves recorded responses from disk, with the same interface and the same errors.

    Keeps the provenance of everything it served, so a caller can report once per poll that the
    numbers came out of hand-written files rather than off a cluster. Without that, replay mode
    produces a demo indistinguishable from a working integration.
    """

    def __init__(self, config: ClusterConfig, store: FixtureStore | None = None):
        self._config = config
        self.store = store or FixtureStore(config.fixture_dir)
        self.served: list[Fixture] = []

    def close(self) -> None:
        self.served.clear()

    def describe(self) -> str:
        return f"replay from {self.store.directory}"

    def get(
        self, path: str, params: dict[str, Any] | None = None, *, prefix: str = API_PREFIX
    ) -> Any:
        # Keyed off the full request path, prefix included, so a fixture file name matches what
        # the fake server sees on the wire and a capture can be dropped straight in.
        try:
            fixture = self.store.get(f"{prefix}{path}", params)
        except CohesityError as exception:
            # Replay has to produce the same failure shape as the wire, diagnostics included,
            # or a missing fixture is the one failure the tests cannot see reported.
            annotate(exception, path=path, params=tuple(params or ()))
            raise
        self.served.append(fixture)
        return fixture.body

    def caveats(self) -> list[str]:
        """One line per distinct non-captured fixture served, ready to log."""
        seen: dict[str, str] = {}
        for fixture in self.served:
            caveat = fixture.caveat()
            if caveat:
                seen[fixture.key] = caveat
        return [seen[key] for key in sorted(seen)]


def build_transport(config: ClusterConfig):
    """The one place replay mode is chosen. Driven by configuration, never by a code branch."""
    if config.fixture_dir:
        return FixtureTransport(config)
    return HttpTransport(config)


def encode_params(params: dict[str, Any] | None) -> str:
    """Serialise query parameters the way Cohesity's specs declare them.

    ``metricNames`` on /v2/stats/time-series-stats is ``style: form, explode: false``, i.e.
    ``metricNames=kReadIos,kWriteIos`` in ONE parameter - not repeated. Most HTTP clients
    default to the repeated form and the cluster then reads only the last value. Joining lists
    here makes the repeated form unreachable rather than merely discouraged - the one exception
    being a value explicitly wrapped in :class:`Repeated`, which is how the variant probe asks
    for the undocumented shape on a cluster that rejects the documented one.
    """
    if not params:
        return ""
    pairs: list[tuple[str, str]] = []
    for name, value in params.items():
        if value is None:
            continue
        # Checked before the sequence branch below, because Repeated *is* a tuple.
        if isinstance(value, Repeated):
            pairs.extend((name, str(item)) for item in value)
        elif isinstance(value, (list, tuple)):
            pairs.append((name, ",".join(str(item) for item in value)))
        elif isinstance(value, bool):
            pairs.append((name, "true" if value else "false"))
        else:
            pairs.append((name, str(value)))
    return urllib.parse.urlencode(pairs)


def build_ssl_context(config: ClusterConfig) -> ssl.SSLContext:
    if not config.verify_tls:
        # Explicitly unverified, chosen in the monitoring configuration. config.py refuses the
        # combination of this with a CA path, so there is no way to reach here believing a CA
        # is in force.
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context

    if config.ca_cert_path:
        if not Path(config.ca_cert_path).is_file():
            # Checked before create_default_context so the message can say "not found" rather
            # than the OSError's less helpful wording, and so fastcheck can fail on it.
            msg = (
                f"{config.name}: no CA certificate at {config.ca_cert_path}. The path is on the "
                f"ActiveGate, not on the machine that configured the extension"
            )
            raise CohesityConnectError(msg)
        try:
            return ssl.create_default_context(cafile=config.ca_cert_path)
        except (OSError, ssl.SSLError) as exception:
            msg = (
                f"{config.name}: the CA certificate at {config.ca_cert_path} could not be "
                f"loaded. It must be PEM encoded and readable by the ActiveGate service user "
                f"({exception})"
            )
            raise CohesityConnectError(msg) from exception

    return ssl.create_default_context()
