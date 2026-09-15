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


class CohesityError(Exception):
    """Base class for every failure that stops a poll from producing metrics."""


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
