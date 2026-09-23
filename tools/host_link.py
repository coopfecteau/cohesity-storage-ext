"""Reference implementation of the host-link lookup table, and the check that the JS matches.

The table that joins a Cohesity protection group to a Dynatrace HOST is built by a workflow
written in JavaScript (``dynatrace/sync-host-link-task.js``), because it runs inside Dynatrace
and cannot import this package. That leaves two copies of three rules - how a BIOS UUID is
normalised, how hosts are grouped, and how the table is encoded - and two copies of a rule
drift.

So the rules live here in Python, where they are tested (``tests/test_host_link.py``), and the
JavaScript is held to the same worked examples. This module is a developer tool: the extension
never imports it, and nothing in ``extension/`` depends on it.

Run it to preview what a workflow run would write, from two JSON files exported from Grail::

    python tools/host_link.py --links links.json --hosts hosts.json

``links.json`` is the bridge metric grouped by its dimensions, ``hosts.json`` is
``smartscapeNodes "HOST" | fields id, serial = host.additional_system_info["system.serial"]``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from cohesity_storage.domain import normalise_object_uuid  # noqa: E402

#: The two processors whose tables a sync run rewrites, and nothing else in the pipeline.
PROTECTION_GROUP_PROCESSOR = "cohesity-host-link.protection-group-lookup"
CLUSTER_PROCESSOR = "cohesity-host-link.cluster-lookup"

#: What an empty mapping encodes to. Not "[]": the upload and settings APIs both refuse an empty
#: inlineLookupTable, so "inert" has to be spelled as a row whose key nothing can match.
EMPTY_TABLE = '[[["__cohesity_host_link_unpopulated__"],""]]'


def lookup_table(pairs: dict[str, str]) -> str:
    """``{key: value}`` encoded as OpenPipeline's ``[[[keys...],"value"],...]`` string.

    The table is a *list of groups*, not a map: every key that resolves to the same value
    shares one row. That is what keeps it small when many hosts belong to one protection
    group, and it is the shape the inlineLookup processor parses - a plain object is silently
    a table with no rows.

    Both the keys within a row and the rows themselves are sorted. Nothing downstream needs
    the order, but the workflow compares the encoded string against the one already stored to
    decide whether to write at all, and an unstable order would rewrite the settings object on
    every run - burning a change record an hour on a table that never changed.

    An empty input encodes as :data:`EMPTY_TABLE`, the same sentinel the pipeline ships with, so
    a sync that finds nothing restores the inert state rather than leaving a stale mapping
    asserting edges for hosts that are no longer protected. It is a sentinel rather than ``"[]"``
    because the extension upload API rejects an empty table outright ("Must not be empty"), and
    the settings API would reject the workflow's write for the same reason. The key cannot equal
    any Smartscape host id, so a populated-looking table still matches nothing.
    """
    grouped: dict[str, list[str]] = {}
    for key, value in pairs.items():
        if not key or not value:
            # Half a row draws an edge from or to nothing. There is no useful partial here.
            continue
        grouped.setdefault(value, []).append(key)
    rows = [[sorted(keys), value] for value, keys in sorted(grouped.items())]
    # separators without spaces: the stored value is compared as a string, and Python's default
    # ", " would differ from JSON.stringify's output for no reason at all.
    if not rows:
        return EMPTY_TABLE
    return json.dumps(rows, separators=(",", ":"))


def host_ids_by_uuid(hosts: list[dict]) -> dict[str, str]:
    """``{normalised uuid: host id}`` from ``smartscapeNodes "HOST"`` rows.

    A host with no serial, or one whose serial is not a BIOS UUID (physical hardware publishes
    a vendor service tag here), simply does not appear. It is not an error: most of a mixed
    estate is not a VMware guest.
    """
    resolved: dict[str, str] = {}
    for host in hosts:
        host_id = str(host.get("id") or "")
        uuid = normalise_object_uuid(host.get("serial"))
        if host_id and uuid:
            resolved[uuid] = host_id
    return resolved


def tables(links: list[dict], hosts: list[dict]) -> dict[str, object]:
    """Both lookup tables plus the counts a sync run reports, from the two Grail results.

    ``links`` rows carry ``cohesity.object.uuid``, ``cohesity.protectiongroup.id`` and
    ``cohesity.cluster.id`` - the bridge metric's dimensions, unchanged.

    A host protected by more than one group keeps the first group by sorted id and is listed
    under ``multiplyProtected``. An inlineLookup holds one value per key, so one of the groups
    has to win; which one is arbitrary, and saying which hosts were affected is the honest way
    to be arbitrary. A host that backs up to several jobs is common enough on a real estate
    that silently picking one would be a bug report.
    """
    by_uuid = host_ids_by_uuid(hosts)
    groups: dict[str, str] = {}
    clusters: dict[str, str] = {}
    multiply: list[str] = []
    unmatched: list[str] = []
    for row in links:
        uuid = normalise_object_uuid(row.get("cohesity.object.uuid"))
        group = str(row.get("cohesity.protectiongroup.id") or "")
        cluster = str(row.get("cohesity.cluster.id") or "")
        if not (uuid and group and cluster):
            continue
        host_id = by_uuid.get(uuid)
        if not host_id:
            # The unmatched majority, and it is the expected case rather than a failure: most
            # protected VMs are not OneAgent-monitored. No entity is minted for them and no
            # edge is drawn, which is what "degrade quietly" means in ticket 16.
            unmatched.append(uuid)
            continue
        existing = groups.get(host_id)
        if existing is not None and existing != group:
            multiply.append(host_id)
            if existing <= group:
                continue
        groups[host_id] = group
        clusters[host_id] = cluster
    return {
        "protectionGroupTable": lookup_table(groups),
        "clusterTable": lookup_table(clusters),
        "hosts": len(by_uuid),
        "mapped": len(groups),
        "unmatched": sorted(set(unmatched)),
        "multiplyProtected": sorted(set(multiply)),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--links", required=True, type=Path, help="bridge metric rows as JSON")
    parser.add_argument("--hosts", required=True, type=Path, help="smartscapeNodes HOST rows")
    args = parser.parse_args()
    result = tables(
        json.loads(args.links.read_text(encoding="utf-8")),
        json.loads(args.hosts.read_text(encoding="utf-8")),
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
