# Host link — tenant assets

Everything here is installed **once, by hand, on the tenant**. None of it ships in the
extension zip; the extension only ships the pipeline these assets fill and route.

What it buys: a `protects` edge in Smartscape from an `EXT_COHESITY_PROTECTION_GROUP` to the
`HOST` Dynatrace already monitors — the backup estate joined to the infrastructure.

| file | what it is |
|---|---|
| `routing-host-link.json` | the routing entry that sends host metrics into the pipeline |
| `sync-host-link-task.js` | the workflow script, readable on its own |
| `workflow-sync-host-link.json` | the same script wrapped as an importable workflow |

## How the pieces fit

```
extension (opt-in, VMware groups only, capped)
   │  cohesity.protectiongroup.protects   value 1
   │  dims: cohesity.cluster.id, cohesity.protectiongroup.id, cohesity.object.uuid
   ▼
 Grail ──────────────┐
                     │  workflow, hourly
 smartscapeNodes ────┤    join on the normalised BIOS UUID
   HOST serial       │    write [[[hostIds…],"value"],…]
                     ▼
        extension:cohesity-host-link pipeline
          inlineLookup  dt.smartscape.host → cohesity_protectiongroup_id
          inlineLookup  dt.smartscape.host → cohesity_cluster_id
          smartscapeNode  extractNode:false → dt.smartscape.cohesity_protection_group
          smartscapeEdge  EXT_COHESITY_PROTECTION_GROUP --protects--> HOST
                     ▲
 dt.host.cpu.usage ──┘  via the routing entry
```

The **edge direction is protection group → HOST**. `protects` reads with the actor first (the
job protects the machine, not the other way round), and that is also the direction whose
server-side acceptance is established: a custom source type pointing at a built-in target
type. Reversing it would put `HOST` in the source position, which is unverified.

The **HOST is resolved, never created**. `extractNode: false` on the node rule computes the id
of the protection-group entity the extension's own metrics already own; the HOST id arrives on
the record as `dt.smartscape.host` and no rule here extracts a node for it. Nothing in this
pipeline mints an entity.

## Deploy

Five steps. Steps 1–2 are enough to see the bridge metric; the edge appears after step 5.

**1. Turn the collection on.** In the Cohesity monitoring configuration, set **Link protection
groups to Dynatrace hosts (VMware only)** on, per cluster. It is off by default — it publishes
one series per protected VM. Leave **Protected objects per poll** at 200 unless you have
counted the cardinality you are willing to add.

Confirm within one poll interval:

```dql
timeseries n = avg(cohesity.protectiongroup.protects),
  by: { `cohesity.protectiongroup.id`, `cohesity.object.uuid` }, from: -1h
```

Nothing there? The extension will have said why, as one record:

```dql
fetch logs, from: -2h
| filter log.source == "cohesity_storage.diagnostics"
| filter cohesity.diagnostic == "host_link"
| fields timestamp, severity, content
```

An `ERROR` there means the VMware objects on this cluster carry no BIOS UUID and the join is
not possible from run data — stop here. A `WARN` about the cap means the mapping is partial.

**2. Find the pipeline's settings object id.** The extension installs the pipeline; what the
routing entry and the workflow need is its *object id*, not its `customId`.

```bash
dtctl get settings-objects --schema builtin:openpipeline.metrics.pipelines -o json \
  | jq -r '.[] | select(.value.customId == "extension:cohesity-host-link") | .objectId'
```

**3. Create the routing entry.** Take `routing-host-link.json`, drop the `_comment` key, paste
the object id into `pipelineId`, and create it under `builtin:openpipeline.metrics.routing`
(Settings → OpenPipeline → Metrics → Dynamic routing). Read the `_comment` first — the matcher
must not reference a field the pipeline itself creates, or no record ever arrives.

**4. Import the workflow.** Import `workflow-sync-host-link.json` (Workflows → ⋯ → Upload), then
set `PIPELINE_OBJECT` at the top of its script to the object id from step 2.

**5. Give the workflow actor its scopes**, then run it once by hand.

| scope | why |
|---|---|
| `storage:metrics:read` | read the bridge metric |
| `storage:buckets:read` | required alongside it to query Grail |
| `storage:smartscape:read` | `smartscapeNodes "HOST"` for the serials |
| `settings:objects:read` | read the pipeline before modifying it |
| `settings:objects:write` | write the two lookup tables back |

The run returns `{updated|unchanged|skipped, hosts, mapped, unmatched, multiplyProtected}`.
`mapped` is the number of hosts that will get an edge; `unmatched` is protected VMs with no
Dynatrace host, which is the expected majority and not a failure.

Then:

```dql
smartscapeEdges "protects"
```

Count the rows client-side. Do **not** filter with `startsWith(source_id, "EXT_COHESITY")` —
that silently matches nothing on `smartscapeEdges` and reads exactly like a missing edge. That
mismeasurement already cost this extension a release.

## Keeping it current

The workflow re-derives both tables from scratch every hour and writes only when the encoded
result differs from what is stored. So a new VM, a rebuilt host (new Smartscape id, same BIOS
UUID), a VM moved between jobs and a job deleted all correct themselves within an hour with
nobody editing configuration. An empty result encodes as `[]`, which restores the inert state
rather than leaving a stale mapping asserting edges for hosts nothing protects any more.

A host protected by more than one job keeps the lowest group id and is named in
`multiplyProtected` — an `inlineLookup` holds one value per key, so one job has to win, and
saying which hosts were affected is the honest way to be arbitrary.

## The successor design

This works, and it proves the edge mechanism end to end, but carrying the mapping on a metric
is the wrong long-term shape and the README's "Linking protection groups to hosts" section
says why in full: the VM-to-job mapping is configuration, its cost should scale with how often
it changes rather than with how many objects exist, and a bridge metric puts it on the
five-minute poll path of a cluster that already answers HTTP 500 on two endpoints. The better
version is what NetApp does — the workflow calls the storage API for the mapping itself and
writes the tables, with no bridge metric and no extra per-poll requests at all. Only step 1
above would go away; steps 2–5 are the same.
