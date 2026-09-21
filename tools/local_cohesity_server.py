"""Run a throwaway Cohesity cluster on localhost so `dt-sdk run` has something to talk to.

    python tools/local_cohesity_server.py              # https://127.0.0.1:8443, any apiKey
    python tools/local_cohesity_server.py --software-version 7.2   # pre-7.3: top-views 404s
    python tools/local_cohesity_server.py --http       # plain HTTP, for curl
    python tools/local_cohesity_server.py --drift      # numbers move; new runs every 5 min

It serves the recorded fixtures in ``fixtures/``, which are SYNTHETIC unless a capture has
replaced them - every response the extension parses here was hand-written from Cohesity's
published schemas, so this proves the loop works, not that the numbers are right.

``activation.json`` already points at the HTTPS default with TLS verification off, which is
also how a real self-signed Cohesity has to be configured until its CA is on the ActiveGate.

Development only: it never runs anything, holds no state, and accepts any non-empty API key
unless ``--api-key`` is given.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from cohesity_storage.fixtures import FixtureStore  # noqa: E402
from tests.cohesity_fake_cluster import (  # noqa: E402
    SYNTHETIC_FIXTURE_ANCHOR,
    FakeCohesityCluster,
    self_signed_certificate,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8443)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--fixtures",
        default=str(REPO_ROOT / "fixtures"),
        help="directory of recorded responses to serve",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="require exactly this key. Default: accept any non-empty apiKey header, reject an absent one",
    )
    parser.add_argument(
        "--software-version",
        default=None,
        help=(
            "what /v2/clusters/status reports. Below 7.3 makes /v2/stats/top-views "
            "return 404, exactly as a real cluster does"
        ),
    )
    parser.add_argument(
        "--no-anchor",
        action="store_true",
        help="serve fixture timestamps verbatim instead of shifting them to now",
    )
    parser.add_argument("--http", action="store_true", help="serve plain HTTP instead of HTTPS")
    parser.add_argument(
        "--certfile", default="", help="PEM with cert and key; default is a throwaway self-signed one"
    )
    parser.add_argument(
        "--drift",
        action="store_true",
        help=(
            "make capacity, IOPS, latency and throughput move over time and mint a new completed "
            "protection run per group every few minutes, so charts are not flat lines and run "
            "counters increment. Still SYNTHETIC. Default: serve the recorded values"
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    store = FixtureStore(args.fixtures)
    keys = store.fixture_keys()
    if not keys:
        print(f"No fixtures found in {args.fixtures} - nothing to serve.")
        return 1

    certfile = ""
    if not args.http:
        certfile = args.certfile or self_signed_certificate(Path(tempfile.gettempdir()), args.host)

    cluster = FakeCohesityCluster(
        store=store,
        api_key=args.api_key,
        software_version=args.software_version,
        anchor=None if args.no_anchor else SYNTHETIC_FIXTURE_ANCHOR,
        host=args.host,
        port=args.port,
        certfile=certfile,
        drift=args.drift,
    )
    cluster.start()

    print(f"Fake Cohesity cluster listening on {cluster.url}")
    print(f"  fixtures    {args.fixtures} ({len(keys)} recorded responses)")
    print(f"  apiKey      {args.api_key or 'any non-empty value; an absent header gets 401'}")
    print(f"  version     {args.software_version or 'as recorded in v2_clusters_status.json'}")
    print(f"  timestamps  {'verbatim' if args.no_anchor else 'shifted forward to now'}")
    print(f"  drift       {'on - values move, new runs appear' if args.drift else 'off - recorded values'}")
    if certfile:
        print(f"  certificate {certfile} (self-signed - set verifyTls false, or point caCertPath here)")

    provenance = store.provenance_summary()
    for label, names in sorted(provenance.items()):
        # Printed loudly on purpose. A synthetic fixture makes the extension look finished; the
        # one moment someone will read this is when they are about to believe a number.
        print(f"  {label.upper():<9} {len(names)} fixture(s): {', '.join(names)}")

    print(f"Try: curl -k -H 'apiKey: demo' {cluster.url}/v2/stats/cluster-storage")
    print("Ctrl+C to stop.")

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\nStopping after {len(cluster.requests)} request(s).")
    finally:
        cluster.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
