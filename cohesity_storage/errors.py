"""The failure taxonomy, in one place because the *distinctions* are the valuable part.

Every message follows the workspace convention: name the thing that failed, say what to do
about it, and keep the raw error as a parenthesised suffix so the original is never lost.

The four kinds read differently on purpose, because they need four different people:

* :class:`CohesityAuthError` - the key is wrong, expired or under-privileged. A Cohesity admin
  fixes this.
* :class:`CohesityConnectError` - the ActiveGate cannot reach the cluster, or does not trust its
  certificate. A network or PKI problem; nobody should go looking at the API key for it.
* :class:`CohesityEndpointError` - the cluster answered, and said this path does not exist.
  On a 6.8-7.4 estate that usually means the endpoint is newer than the cluster rather than
  that anything is broken.
* :class:`CohesityApiError` - the cluster answered with something unusable.

Collapsing any two of these into "request failed" is how an afternoon gets spent rotating a
credential that was never the problem.
"""

from __future__ import annotations

import re


class CohesityError(Exception):
    """Base class for every failure that stops a poll from producing metrics."""

    #: Where the failure happened, filled in by :func:`annotate` at the point of failure and
    #: left at these defaults for a failure that never reached the transport. Declared here so
    #: every handler can read them without knowing which kind it caught.
    status: int | None = None
    path: str = ""
    param_names: tuple[str, ...] = ()


class CohesityConnectError(CohesityError):
    """The cluster could not be reached, or the TLS handshake failed."""


class CohesityAuthError(CohesityError):
    """The cluster rejected the API key, or the key's owner lacks a required privilege."""


class CohesityApiError(CohesityError):
    """The cluster answered, but not with a usable response."""


class CohesityEndpointError(CohesityApiError):
    """The endpoint is not present on this Cohesity version.

    Separate from :class:`CohesityApiError` because it is recoverable: the caller may know an
    older path that does the same job. ``/v2/stats/top-views`` does not exist before 7.3 and
    ``/v2/stats/views`` is its byte-identical predecessor, which is the one case v1 relies on.
    """


class CohesityFixtureError(CohesityError):
    """Replay mode was asked for a response that is not on disk.

    A distinct class so a missing fixture can never be mistaken for a cluster problem. It is a
    development-time failure with a development-time fix, and it should say so.
    """


# ---------------------------------------------------------------------------
# What a failure is allowed to say about itself
#
# Extension log lines do not reach Grail on the tenant this was debugged against, so a section
# that raises is invisible: _section() catches it, writes it to self.logger, and nobody ever
# sees it. The diagnostics log-event channel does arrive, which is why failures now travel as
# facts rather than only as log lines - and why a failure has to be describable as structured
# data rather than only as a sentence.
#
# The structured fields below are attached by the transport at the point the failure happens,
# because that is the only place that still knows the path, the query parameter names and the
# HTTP status. Reconstructing them from the message later would mean parsing English.
# ---------------------------------------------------------------------------

#: How much of a failure's own message a diagnostic carries. Long enough for the cluster's
#: sentence and the actionable half of ours, short enough that a diagnostic can never become
#: the log stream.
MAX_ERROR_DETAIL_CHARS = 200

REDACTED = "[redacted]"

# Anything spelled "<something that sounds like a credential> = <value>". The value goes, the
# name stays - knowing that an apiKey was present is useful, knowing which one is not.
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(api[-_ ]?keys?|keys?|tokens?|secrets?|passwords?|passwd|pwd|credentials?"
    r"|authorization)\b\s*[=:]\s*\S+"
)

# A long opaque run of credential-shaped characters. Deliberately greedy about what it eats:
# over-redacting a diagnostic costs a word of context, under-redacting one puts a cluster API
# key in a log stream that a different team can read. Twenty characters is above every word in
# the extension's own messages and below every API key and vault id seen so far.
_LONG_OPAQUE = re.compile(r"(?<![\w./-])[A-Za-z0-9+/_-]{20,}={0,2}(?![\w])")


def redact(text: str, secrets: tuple[str, ...] | list[str] = ()) -> str:
    """Strip anything credential-shaped out of a message bound for a log record.

    Two passes, and both are needed. Known values first - the configured API key and the
    credential-vault id are things this process actually holds, so they can be removed by
    identity rather than by guesswork. Then shape, for everything the cluster itself might echo
    back that nobody here has ever seen.
    """
    for secret in secrets:
        if secret and len(secret) >= 4:
            text = text.replace(secret, REDACTED)
    text = _SECRET_ASSIGNMENT.sub(lambda match: f"{match.group(1)}={REDACTED}", text)
    return _LONG_OPAQUE.sub(REDACTED, text)


def annotate(
    error: BaseException,
    *,
    path: str = "",
    params: tuple[str, ...] | list[str] = (),
    status: int | None = None,
) -> BaseException:
    """Record where a failure happened on the exception, and return it ready to raise.

    Parameter *names* only. A query value can be an entityId, a time window or - on some other
    API one day - a token, and a diagnostic that carries values is one nobody can safely widen
    the audience of later.
    """
    error.path = path
    error.param_names = tuple(params)
    error.status = status
    return error


def error_facts(
    exception: BaseException, secrets: tuple[str, ...] | list[str] = ()
) -> dict[str, object]:
    """The reportable shape of a failure: what kind, where, and how the cluster put it.

    ``getattr`` throughout because this must work for an exception that never went near the
    transport - an unexpected ``KeyError`` from a parser is exactly the case the old
    ``self.logger.exception`` call was swallowing, and it has no path or status to give.
    """
    return {
        "error": type(exception).__name__,
        "status": getattr(exception, "status", None),
        "path": getattr(exception, "path", "") or "",
        "params": list(getattr(exception, "param_names", ()) or ()),
        "detail": redact(str(exception), secrets)[:MAX_ERROR_DETAIL_CHARS],
    }
