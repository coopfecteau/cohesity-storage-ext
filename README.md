# cohesity-storage-ext

A Dynatrace Extension 2.0 that polls a Cohesity cluster's REST API from an ActiveGate and
reports its storage and protection metrics to Grail.

```
ActiveGate ──https + apiKey──▶ Cohesity cluster ──REST──▶ metrics ──▶ Grail
```

| | |
|---|---|
| Extension name | `custom:cohesity.storage` |
| Version | `0.0.1` |
| Data source | Python (Extension 2.0) |
| Runs on | ActiveGate, **remote activation only** |
| Python runtime | 3.14 only |
| Floors | `minDynatraceVersion 1.341.0`, `minEECVersion 1.333.0` (ActiveGate 1.333+) |
| Needs on the cluster | A cluster API key. No agent — Cohesity is an appliance. |

## Status: v0.2.1 — running against a real cluster

22 metric keys across three entity prefixes, three Smartscape node types with three edges, a
packaged dashboard, and cluster alerts as log records. Deployed and polling a production
Cohesity cluster on an evaluation tenant.

| Piece | Lives in |
|---|---|
| Metric keys, units and dimensions | `cohesity_storage/metrics.py`, `extension/extension.yaml` |
| Parsed data to `report_metric` | `ExtensionImpl._poll` and the `_report_*` methods |
| Entity types and relationships | `extension/openpipeline/metrics.pipeline.json` |
| Dashboard | `extension/documents/cohesity-storage.dashboard.json` |

### What the real cluster taught us

**Five metrics have never produced a point** — `cpu.usage`, `memory.usage`, `io.iops`,
`io.latency`, `garbage.bytes`. They are not five failures: they are exactly the five fed by
`/stats/time-series-stats`, which answers HTTP 500 for all 20 `entityId` candidates on that
cluster. One broken endpoint. Their dashboard tiles are labelled rather than deleted, because
this class of outage recovers on its own — `view.throughput` was dead for days and now answers
roughly a fifth of intervals.

**The cluster's stats subsystem fails intermittently, not permanently.** The same endpoint
answers one poll and 500s the next, and which parameter shape works varies between polls. Any
"this endpoint is broken" conclusion here has a shelf life.

**Host linkage is parked.** See below.

**The diagnostics channel is the reason any of this is known.** The extension's own log lines
do not reach Grail on that tenant; `report_log_events` does. Every fact above came off
`log.source == "cohesity_storage.diagnostics"`, usually in one deploy where inference had
already failed several times.

### Still open

- Alerting rules and Davis anomaly detectors.
- The run-dedup ledger is in memory, so an ActiveGate restart re-counts recently completed runs.
- `run.outcome` arrives with no `status` dimension for a real fraction of runs (66 of 689 in a
  24h sample). The dashboard labels these "Unknown"; the parsing gap behind it is untraced.
- `run.objects` never carries `result: "total"` although `extension.yaml` documents it.

**Every fixture in `fixtures/` is synthetic.** They were hand-written from Cohesity's published
6.8–7.4 response schemas. They prove the code parses the documented shapes, not that the shapes
are right — and on more than one occasion the real cluster has disagreed with them. Each one
says so in its own `_fixture.provenance`, the extension logs a warning every poll while
replaying one, and a test fails the moment one arrives unmarked.


## Why remote activation only

Cohesity is an appliance. No OneAgent can be installed on it, so there is no local host to
run from — the extension has to reach the cluster over the network from an ActiveGate. The
extension declares `python.activation.remote` and nothing else, the same shape as the Dell
Isilon vendor extension and as this author's `custom:ssh.command.logs`.

## Why an API key, not a username and password

Cohesity's username/password flow returns a session token valid for **24 hours**. Using it
would mean building and operating a token-refresh lifecycle inside the extension for no
benefit. A cluster API key is sent as the `apiKey` header on every request, with no
lifecycle to manage. Create one in the Cohesity UI under
**Settings ▸ Access Management ▸ API Keys**; it is shown once.

The key is never written to a log record, and the configuration errors that mention it
report its length rather than its value.

### Two ways to supply the key

**Inline** (the default) — paste the key into **Cluster API key**. It is a `secret`
property, so Dynatrace stores it encrypted with the monitoring configuration.

**Credential vault** — turn **Use credential vault** on and pick a stored credential under
**Select vault credentials**. The key then lives once in **Settings ▸ Credential vault**,
shared by every configuration that points at it and rotated in one place by whoever owns it,
rather than being pasted into each monitoring configuration by whoever edits that page.

The two are mutually exclusive, in the schema (each field has a `precondition` on the
switch) and in the code. A configuration with both a selected vault credential and the
switch off is refused, because it *looks* like the vault is in use while the inline key is
what would actually be sent. A configuration with neither is refused too.

### The field-name trap

The extension does **not** call any credential-vault API, and there is no SDK helper for one —
`dynatrace_extension` has none. The ActiveGate's EEC resolves the vault entry and injects its
values into the activation config *before* the extension runs, so all the extension ever does
is read a field out of a dict.

The trap is that **it is not the same field in both modes**, and nothing in
`activationSchema.json` says so. The vendor NetApp ONTAP 3.0.8 extension — the only verified
example of the pattern — reads:

```python
if endpoint.get("useCredentialVault", False):
    user = endpoint["username"]     # vault mode
else:
    user = endpoint["user"]         # inline mode - DIFFERENT KEY NAME
password = endpoint["password"]     # same key in both modes
```

Cohesity's credential is a single API key rather than a pair, so there is no username half —
but the same uncertainty applies to the secret. This extension asks for
`referencedType: "TOKEN"`, which is what an API key semantically is; NetApp only proves
`USERNAME_PASSWORD`. **TOKEN is unverified** — the extension has not yet been uploaded and
observed, so which field the EEC injects into is a guess.

So `cohesity_storage.config.VAULT_SECRET_FIELDS` **probes** rather than picking:
`token`, then `password`, then `apiKey`, taking the first non-empty value. The fastcheck log
line names the field the key was actually read from, which is the only place that name is
ever observable — read it after the first upload.

**Documented fallback:** if `referencedType: "TOKEN"` is rejected at upload time, change it to
`"USERNAME_PASSWORD"`, put the API key in the password half and leave the username half
unused. That needs no code change — the probe already covers `password`.

### Which failure is it

An empty credential and a wrong credential both end as an HTTP 401 on the wire, and they are
fixed by different people in different products. The extension keeps them apart:

- **Vault entry did not resolve** — refused in `config.py` before any request is sent, with a
  message that names the vault entry and says *not* to rotate the Cohesity key. Causes: the
  credential was deleted, holds the wrong type, or its access scope does not let this
  ActiveGate group read it.
- **Cluster rejected the key** — a real 401/403 from Cohesity. The message states that a
  non-empty key was sent and names where it came from, so the vault is explicitly ruled out.
  Causes: the key was revoked, or its owner lacks `CLUSTER_VIEW`, `TENANT_VIEW`,
  `STORAGE_DOMAIN_VIEW`, `STORAGE_VIEW`, `PROTECTION_VIEW`.

Local `dt-sdk run` only supports the inline mode: there is no EEC to resolve anything, so a
selected vault entry would simply resolve to nothing. `activation.json` carries the vault
shape under `credentialVaultExample` for reference; the extension does not read it.

The extension does **not** check the key's format. Cohesity shows it as a UUID, but that is not
guaranteed across 6.8–7.4, and rejecting a valid key is a worse failure than accepting a bad
one: a bad key produces a 401 with an actionable message, a rejected key produces an extension
that will not start over a format claim nobody can check. Only an empty value, or one with
whitespace inside it, is refused.

## Metric and dimension conventions

Keys are `cohesity.<entity>.<measure>` — for example `cohesity.cluster.capacity.total`,
`cohesity.storagedomain.usage.physical`.

That is not a readability choice. A metric binds to a Smartscape entity by its key **prefix**:
the node-extraction rules in `extension/openpipeline/metrics.pipeline.json` match
`cohesity.cluster.*`, `cohesity.storagedomain.*` and `cohesity.protectiongroup.*`. One prefix
per entity type, no prefix may be a prefix of another, and the prefixes carry no underscore
because the rules say they carry no underscore. There are tests for all three.

| prefix | entity |
|---|---|
| `cohesity.cluster` | `EXT_COHESITY_CLUSTER` |
| `cohesity.storagedomain` | `EXT_COHESITY_STORAGE_DOMAIN` |
| `cohesity.protectiongroup` | `EXT_COHESITY_PROTECTION_GROUP` |

The bridge metric sits under the protection-group prefix for the same reason view throughput
sits under the cluster one: it binds to an entity that already exists rather than inviting a
per-VM entity type. `cohesity.object.*` would match no rule and float unattached.

Everything that discriminates goes in dimensions. Identity dimensions stay `cohesity.*`-prefixed
so they cannot collide with a built-in field: `cohesity.cluster.id`, `cohesity.cluster.name`,
`cohesity.storagedomain.id`, `cohesity.storagedomain.name`, `cohesity.protectiongroup.id`,
`cohesity.protectiongroup.name`, `cohesity.view.id`, `cohesity.view.name`. The descriptive ones
are bare, because they carry no identity and no rule reads them: `service`, `operation`,
`status` and `result`. Cohesity's `isSlaViolated`, `isPaused` and `isActive` flags report as
`cohesity.protectiongroup.sla_violated`, `.paused` and `.active` - namespaced because a bare
`paused` in Grail says nothing about what is paused, and lowercase because the ingest protocol
rejects any dimension key with an uppercase letter.

Names are free text typed by a Cohesity admin, and the SDK does not escape dimension values, so
every line leaves through one chokepoint that collapses whitespace and control characters
(a newline would end the line), truncates to 250 characters and escapes `\` and `"`. A value
that is NaN or infinite is skipped, with one warning per key.

**View is dimensions, not an entity.** The poll is a top-20 ranking, and a sampled population
makes an unstable entity set, so view throughput reports as `cohesity.cluster.view.throughput`
and binds to the cluster. Adding view dimensions does not fragment the cluster entity — identity
comes from `idComponents`, which for the cluster is the cluster id alone.

**Protection runs are a counter, not a status gauge.** `cohesity.protectiongroup.run.outcome` is
a delta counter of newly completed runs dimensioned by terminal `status`, deduplicated on
`run.id`; `cohesity.protectiongroup.last_success.age` is the gauge that sees the run which never
happened. A counter for what happened, an age gauge for what did not.

**`status` is never absent — it says `unknown` instead.** Measured on the customer tenant:
`timeseries sum(cohesity.protectiongroup.run.outcome), by:{status}` answered `Succeeded 64` and
`None 28`. A quarter of the counted runs carried no `status` dimension at all, so they could be
counted but not classified — and the totals looked perfectly healthy, because the hole is
invisible unless somebody groups by status. A run whose only target is an archive or a replica
has no `localBackupInfo` block, the two-place lookup found nothing, and `wire_dimensions`
dropped the empty value. `domain.run_status` now also reads `originalBackupInfo` and the
`archivalInfo` / `replicationInfo` / `cloudSpinInfo` target results, and falls back to the
literal string `unknown` rather than to nothing — a dimension that is absent cannot be counted,
charted or alerted on, and one that says `unknown` is all three. When it fires, the extension
also writes one WARN diagnostic listing the key names that run *did* carry, so the real location
can be read off Grail rather than guessed at.

The bridge metric `cohesity.protectiongroup.protects` is the one exception to all of this: its
value is a constant 1 and means nothing, its dimensions are the whole payload, and it is off by
default. See [Linking protection groups to hosts](#linking-protection-groups-to-hosts).

Cohesity object ids are cluster-scoped int64s and **will** collide across clusters, so every
id below cluster level is namespaced `{clusterId}_{objectId}` — from day one, even though v1
monitors a single cluster. `metrics.entity_id()` is the only place that formatting lives.

## Entities in Smartscape

Three node types, three edges, all derived from the metric stream by OpenPipeline — **not** by a
classic `topology:` section, which is deprecated since 1.334. On Gen3 there is no REST
entity-creation API either, so extraction off the metric stream is the mechanism.

| node type | id components | named from |
|---|---|---|
| `EXT_COHESITY_CLUSTER` | `cluster_id` | `cohesity.cluster.name` |
| `EXT_COHESITY_STORAGE_DOMAIN` | `cluster_id`, `storagedomain_id` | `cohesity.storagedomain.name` |
| `EXT_COHESITY_PROTECTION_GROUP` | `cluster_id`, `protectiongroup_id` | `cohesity.protectiongroup.name` |

```
EXT_COHESITY_CLUSTER ──contains──▶ EXT_COHESITY_STORAGE_DOMAIN
                     ──contains──▶ EXT_COHESITY_PROTECTION_GROUP
                                       │
                                  writes_to
                                       ▼
                          EXT_COHESITY_STORAGE_DOMAIN
```

`writes_to` is the edge that earns the topology its keep. It crosses the containment tree and
answers "which backup job is filling this storage domain" — a question a Cohesity admin cannot
easily answer today. It costs one dimension on six metrics and no extra round trip, because the
protection-groups response returns `storageDomainId` on the same call that produces the metrics.

Four things here are load-bearing and each cost somebody a round trip to a tenant:

- **The `EXT_` prefix is mandatory.** Bare `COHESITY_CLUSTER` is rejected server-side with
  `Must start with one of ['CUSTOM_, EXT_']` — a rule the published settings schema never
  mentions. Edge types are lowercase and at most 32 characters.
- **Verify topology by counting unfiltered rows, not with a filtered DQL query.**
  `smartscapeEdges "contains" | filter startsWith(source_id, "EXT_COHESITY")` returns 0. So does
  the same query for `writes_to`, which demonstrably has 42 edges — a `startsWith` filter on
  `source_id` in `smartscapeEdges` silently matches nothing, and 0 rows reads exactly like a
  missing edge. Count the unfiltered rows client-side before concluding anything is absent.
  That mismeasurement cost a release: 0.1.5 renamed both containment edges to
  `has_storage_domain` and `has_protection_group` on the strength of it, when `contains` was
  carrying 51 Cohesity edges (42 cluster→protection group, 9 cluster→storage domain) the whole
  time. 0.1.6 puts both back to `contains`; the 0.1.5 `has_*` edges age out once nothing emits
  them. Built-in edge types are fine on a custom extraction rule — unlike node types, where the
  `EXT_`/`CUSTOM_` prefix really is mandatory.
- **Identity is the id, never the name.** Storage domains and protection groups can both be
  renamed; a name in the identity orphans the entity and silently mints a second one. Ids are
  namespaced `{clusterId}_{objectId}`, because Cohesity ids are cluster-scoped int64s.
- **Each rule's matcher guards on its own id dimensions** — what `requiredDimensions` buys in
  classic topology. A partial poll must not mint a phantom entity. `cohesity.storagedomain.id`
  is deliberately *not* required by the protection-group rule: a group with no storage domain
  should still exist, just without the edge.
- **The storage domain node is only ever named from its own metrics.** Protection-group metrics
  carry the domain id but not its name, so a second extracting rule there would rename every
  domain to the default. Those metrics instead feed an `extractNode: false` rule, which computes
  the domain's node id for the edge without creating a node.

## Linking protection groups to hosts

> **PARKED as of v0.2.1.** The mechanism is correct and currently links nothing. Two
> independent reasons, found on the customer cluster:
>
> 1. **The BIOS UUID is not in the protected-object listing.** That cluster returns only
>    `uuid` — vCenter's instanceUuid — under every candidate field name the extension knows.
>    The documented `vCenterSummary.biosUuid` is simply absent. The extension now emits no
>    bridge metric at all rather than publishing an instanceUuid that can never match, and says
>    so on the diagnostics channel, naming the fields the objects *do* carry.
> 2. **The ceiling is three.** That tenant has three `HOST` entities in total. Cohesity protects
>    146+ VMs there. Even a perfect join would draw three edges, because linkage can only reach
>    VMs running OneAgent. There are no `VMWARE_VM` entities either, so joining via vCenter
>    instead is not available.
>
> Reason 2 is why reason 1 was not chased further: finding the right field name costs a deploy
> and the payoff is capped at three edges. Nothing here is wrong — it degrades quietly, as
> designed — and it will start working if OneAgent coverage grows. Resume by finding where that
> cluster exposes the BIOS UUID (the protection-sources or object-detail endpoints are the
> candidates), adding the field name to `domain.BIOS_UUID_FIELDS`, and re-reading the
> `host link:` diagnostic.


**Off by default.** Turn on *Link protection groups to Dynatrace hosts (VMware only)* per
cluster, then install the two tenant assets in [`dynatrace/`](dynatrace/README.md). Until both
are done nothing changes: the metric is not collected, and the pipeline that would use it ships
with empty lookup tables.

What it draws:

```
EXT_COHESITY_PROTECTION_GROUP ──protects──▶ HOST
```

The join key is the **VMware BIOS UUID**. Dynatrace publishes it on a HOST as
``host.additional_system_info[`system.serial`]``, shaped
`VMware-00 11 22 33 44 55 66 77-88 99 aa bb cc dd ee ff`; Cohesity publishes it on a protected
object under `vCenterSummary` as `biosUuid`. Both normalise to
`00112233-4455-6677-8899-aabbccddeeff`. It is a UUID-to-UUID join, not a hostname match —
hostname matching across short name, FQDN and case produces confident wrong edges, which is
worse than none.

> **Which of the two uuids — the v0.2.0 fix.** A vSphere VM carries *two* 8-4-4-4-12
> identifiers. The **BIOS/SMBIOS uuid** is what the guest firmware reports; VMware mints it
> starting `42` or `564d`, and it is the one Dynatrace puts in `system.serial`. The
> **instanceUuid** is vCenter's own key for the VM; vCenter mints it starting `50`, and no
> Dynatrace HOST publishes it anywhere. `objects[].object.uuid` is the **second** one.
>
> v0.1.9 emitted it. Measured on the customer's cluster: Cohesity answered
> three `50…`-prefixed uuids while the three monitored hosts reported
> `42…` and `564d…` prefixed serials. Two hundred bridge-metric series flowed, every
> chart and every diagnostic read as success, and the join matched exactly nothing — because
> both sides were well-formed uuids of the right shape for the same VMs.
>
> From v0.2.0 the BIOS uuid is read from an **ordered list of candidate field names**
> (`domain.BIOS_UUID_FIELDS`: `biosUuid`, `biosUUID`, `vmBiosUuid`, `smbiosUuid`, `smBiosUuid`,
> `hardwareUuid`, `biosUuidHex`), searched on the object root and in each VMware sub-object
> (`vCenterSummary` first) — the same alias tolerance the storage-domain stats use, for the
> same reason: the spelling moves across 6.8–7.4. **`object.uuid` is not on that list and must
> never be added.** An object with no BIOS candidate yields no line at all, because an
> identifier that cannot match is worse than none — it looks like the feature works.
>
> `cohesity.object.uuid` keeps its name: the workflow, the pipeline and every query written
> against v0.1.9 still read it, and only what fills it moved. The instanceUuid now rides
> alongside as `cohesity.object.instance_uuid` — one extra dimension, not one extra series, and
> the right key for a vCenter-side join later.

Three things make the mechanism work, and each is the non-obvious choice:

- **The edge runs from the protection group, not from the host.** `protects` reads with the
  actor first, and custom-source-to-built-in-target is the direction whose server-side
  acceptance is established. Reversed, `HOST` would sit in the source position, unverified.
- **The HOST is resolved, never minted.** A HOST's Smartscape id cannot be computed from a
  UUID, so it has to come from a lookup table keyed on `dt.smartscape.host`. Our own protection
  group id *is* computable from its id components, so the computed side is ours — which is why
  this inverts the NetApp precedent it is adapted from.
- **The unmatched majority does nothing.** Most protected VMs have no Dynatrace counterpart.
  They produce no entity, no edge and no row; an unmatched object is not implied to be
  unprotected.

**Only VMware groups are asked.** Probed on the customer's estate: SQL objects carry no `uuid`
field at all — the keys are `childObjects`, `entityId`, `environment`, `id`, `name`,
`objectType`, `osType`, `protectionType`, `sourceId`. Of 59 groups, ~13 are VMware, 18 SQL and
9 Oracle, so three quarters of the estate can never join and is never asked.

If the VMware objects turn out to carry no usable UUID either, the extension says so as an
**ERROR** on `cohesity_storage.diagnostics` (`cohesity.diagnostic == "host_link"`) and emits
nothing. There is no fallback join on object names, deliberately.

**The diagnostic separates a field-name answer from a coverage answer.** Both look like "no
edges appeared in Smartscape", and in v0.1.9 they read identically. From v0.2.0 the host-link
record carries `cohesity.host_link_objects_seen`, `cohesity.host_link_objects_bios` (counted
*before* the per-poll cap), `cohesity.host_link_bios_field` — which candidate won — and
`cohesity.host_link_uuid_candidates` — every uuid-ish field name the objects actually carried:

| What the record says | What it means | What to do |
|---|---|---|
| `200 objects, 200 BIOS uuids` | The extension is publishing joinable keys | **Coverage.** Those VMs are not OneAgent-monitored. Nothing to fix here |
| `200 objects, 0 BIOS uuids` | No candidate field name matched | **Field name.** Read `…uuid_candidates` and add the right spelling to `domain.BIOS_UUID_FIELDS` |
| `n of n uuids start 50` | The chosen field is handing back instanceUuids | **Field name**, wearing a disguise — an ERROR on its own marker (`hostLink:instanceShaped`) |

That last one is a shape sanity check on the value, not the name. A BIOS uuid starts `42` or
`564d`; vCenter's instanceUuid starts `50`. Suspicious values are **counted and reported, not
dropped** — one genuine BIOS uuid in 256 starts `50` by chance, and rejecting on the byte would
silently lose real hosts. The *ratio* is the evidence: all of them means the field name is
wrong, one of them means coincidence.

### Bounded scale — read this before turning it on

The bridge metric is **one series per protected VM**. That is exactly the cardinality ticket 04
ruled out of v1, and it is not theoretical: one SQL protection group on the customer's own
cluster holds 856 objects, and a production Cohesity protects tens of thousands. So the
mechanism is bounded on purpose, four ways:

| bound | value | why |
|---|---|---|
| opt-in | off by default | nobody gets this cardinality without asking |
| environment | VMware groups only | nothing else publishes a UUID |
| requests per poll | `min(maxRunFanoutGroups, 5)` groups, rotating | the operator's existing "this cluster is struggling" dial governs both fan-outs |
| series per poll | `maxHostLinkObjects`, default 200 | request count and series count are different bounds; one group can be one request and 856 series |

When the object cap truncates, that is reported as a **WARN** naming what was left out. Partial
data here is indistinguishable from part of the estate going unprotected, and nothing else in
the product would say which — so it is never silent.

**This is the wrong long-term shape, and it is worth saying plainly.** The VM-to-protection-group
mapping is *configuration*, not telemetry: it changes when somebody edits a backup job, which is
weekly at most. Its cost should scale with how often it changes, not with how many objects
exist. Carrying it on a metric also puts per-object API calls on the five-minute poll path of a
cluster that already answers HTTP 500 on two endpoints.

The better design is the one NetApp uses: **the workflow calls the storage API for the mapping
itself and writes the lookup tables, with no bridge metric at all.** Zero added cardinality,
zero added per-poll requests, and the refresh rate matches the change rate. What is shipped here
proves the edge mechanism end to end — the pipeline, the id computation, the edge direction and
the sync loop are all the same either way; only the source of the mapping changes. Treat the
bridge metric as the part to replace.

## Cluster alerts as logs

**Off by default.** Turn on *Collect cluster alerts as logs* on a monitoring configuration.

The cluster's own health events — node down, disk failing, capacity threshold crossed — arrive
in Grail as log records under `log.source == "cohesity_storage.alerts"`:

```
fetch logs
| filter log.source == "cohesity_storage.alerts"
| filter cohesity.alert.severity == "critical"
| sort timestamp desc
```

Each record carries `cohesity.alert.id`, `.name`, `.severity`, `.category`, `.state`,
`.description` and the cluster's own `.first_timestamp_usecs` / `.latest_timestamp_usecs`,
alongside the usual `cohesity.cluster.id` and `.name`.

### What is and is not in them

These describe the **cluster**, not the data it protects. No usernames, no source addresses, no
backup content — which is what separates them from Cohesity's audit log, where all three live.

The one field that can name something in the protected estate is the free-form **description**:
a datastore, a share, occasionally a VM. It has its own switch. Turning *Include alert
descriptions* off keeps the alert — id, name, severity, category, state and timestamps — and
drops only the sentence. That is a redaction, not a suppression: the alert still arrives.

Whether that sentence may cross into Dynatrace is a decision for whoever owns the data, which
is why alerts are opt-in rather than on by default. Nothing here reaches into a protected
object.

### Why logs and not metrics

An alert is a discrete thing that happened and carries prose. A metric could count alerts —
worth adding later — but it cannot say *which disk in which node*, and that sentence is the
reason to collect these at all.

### Three things that are easy to get wrong

**De-duplication.** Alerts persist on the cluster and the poll window is deliberately wider
than the interval, so the same open alert comes back every time. The ledger is what stops it
being re-sent, and it keys on the alert id **plus its latest-occurrence timestamp** — not the
id alone. An alert that fires, resolves and fires again keeps its id, and de-duplicating on the
id would silently swallow the second fire, which is the one somebody is being paged about.

**Timestamps.** Ingest rejects anything more than an hour old. An alert that has been open for
a week is both perfectly valid and far outside that window, so the cluster's own clock is
carried as an *attribute* and the record is timestamped when it was observed. Using the alert's
own time would make exactly the oldest and most serious alerts vanish without a word.

**Which path the cluster serves.** Two are tried — the v2 spelling the 7.4 reference documents,
then the v1 path every release in the 6.8–7.4 range has served. A 404 from the first is an
ordinary answer, not a fault. The winner is remembered but never permanently trusted: if it
later fails, the chain is re-walked, because a cached failure on an intermittently unhealthy
cluster would turn a temporary 500 into a silence only a restart could clear. Which one won is
reported on the diagnostics channel as `alert_source`.

### What the customer cluster actually sends

Measured on the first poll with alerts on, across 100 alert records:

| | |
|---|---|
| severities | 43 warning, 35 info, 22 critical - all mapped, none unknown |
| categories | `kBackupRestore` 57, `kIndexing` 14, `kDataPath` 10, `kSystemService` 9, `kSecurity` 7, `kNodeHealth` 3 |
| descriptions | 100 of 100 carry one; median 74 characters |

**On the sensitivity question the description switch exists for:** in that sample there were no
usernames, no IP addresses, no filesystem paths and no quoted job or object names. The only
estate-identifying strings were three Active Directory domain-controller hostnames, inside
`AdPreferredDomainControllerNotReachable` alerts - where naming the unreachable controller is
the entire value of the alert. About a third of descriptions carry a long numeric Cohesity
entity id.

That is one cluster on one day and not a guarantee. It is a reason to look at the shape on a
new cluster before assuming, which is what the `alert_shape` diagnostic reports.

### Not collected

**Audit logs** (who did what, from where) carry usernames and source IPs — personal data under
GDPR and similar regimes. Valuable for a security use case, but that is a decision with a
data-protection dimension and a retention question attached, so it is deliberately not here.

**Per-object run failure detail** as bizevents would keep the per-object failure the run delta
counter collapses. Worth having; not yet built.

## Cohesity permissions

What to ask a cluster owner for. Everything the extension does is **read-only** — it never
writes to Cohesity.

### Cluster credentials, not Helios

Ask for a credential on the **cluster itself**, not Helios. Helios has its own separate RBAC
layer, and whether a Helios API key plus `accessClusterId` authorises against cluster or Helios
privileges is not documented. That is an unknown worth keeping out of an evaluation.

A practical consequence: the **API Keys page is reachable only by logging in to the cluster UI
directly**. It is not available through Helios. Sites that administer everything through Helios
are usually surprised by this.

### An API key, minted under a dedicated service user

The extension authenticates with a cluster API key sent as the `apiKey` header — see
[Why an API key, not a username and password](#why-an-api-key-not-a-username-and-password) for
the reasoning.

Cohesity API keys carry **no privileges of their own**. `POST /users/{userSid}/api-keys` mints a
key against a user, and it inherits that user's role. So the role is granted to the *user*, and
the key should belong to a **dedicated service user** rather than to someone's personal account —
otherwise the extension's access silently tracks that person's, and dies when they leave.

### The role

**Ask for the built-in `COHESITY_VIEWER` role first.** It is Cohesity's read-only role and is
very likely sufficient.

Alerts are the one collection whose privilege has not been confirmed on a real cluster. If
`COHESITY_VIEWER` does not cover alert read, the section fails on its own with a 403 and
says so on the diagnostics channel, naming the path it tried — every other collection keeps
working. Nothing needs to be guessed in advance; turn it on and read the diagnostic.

> Caveat worth stating honestly: Viewer's exact privilege set could not be verified from public
> documentation — Cohesity's role reference sits behind a login wall. Sufficient is probable,
> not certain.

If a security team prefers to grant exactly what is used, a custom role with these seven
privileges is provably enough:

| privilege | why it is needed |
|---|---|
| `CLUSTER_VIEW` | cluster identity, capacity, nodes, disks |
| `TENANT_VIEW` | required by the time-series endpoint |
| `STORAGE_DOMAIN_VIEW` | storage domain inventory and usage |
| `STORAGE_VIEW` | views and file-services throughput |
| `PROTECTION_VIEW` | protection groups and their runs |
| `PROTECTION_POLICY_VIEW` | protection policies |
| `ALERT_VIEW` | cluster alerts |

The ask is driven almost entirely by one endpoint. `/v2/stats/time-series-stats` — the source of
most of the metric set — requires the **first five at once**. Every other endpoint needs a subset:
`/v2/stats/cluster-storage`, `/v2/nodes` and `/v2/disks/local` need only `CLUSTER_VIEW`;
`/v2/storage-domains` needs `STORAGE_DOMAIN_VIEW`; `/v2/file-services/views` needs `STORAGE_VIEW`;
protection groups and runs need `PROTECTION_VIEW`.

### Before asking for anything

`GET /public/basicClusterInfo` requires **no privileges and no authentication**. Use it to confirm
the ActiveGate can reach the cluster's management IP before any credential exists — it separates a
networking problem from a permissions problem, and those get confused constantly.

### Three traps

**The API Keys page is hidden by default.** It does not appear in the cluster UI until
`apiKeysEnabled` is toggled at `https://<cluster>/feature-flags`. This is the most common reason a
request comes back with "there is no API Keys page".

**`GET /v2/data-protect/sources` is documented as `Unknown Privileges`.** Cohesity never filled the
field in, so no privilege can be requested for it specifically. It is the most likely source of an
unexpected 403. Each collection runs in its own error boundary, so losing protection sources does
not cost the capacity or protection-run metrics.

**Never request protection-source *refresh*.** It needs `PROTECTION_SOURCE_REFRESH`, a
modify-class privilege. Including it turns a read-only access request into a write one, which can
get the whole request refused on principle. The extension does not call it.

### Checking any endpoint not listed here

`https://developers.cohesity.com/v1-cluster-7.4/llms.txt` carries the required privilege inline for
roughly 600 endpoints. Two traps in that source: the `v1-cluster-7.x` documentation sets actually
describe the **V2** API, and privilege annotations exist only from **7.3 onward** — 6.8, 7.1 and 7.2
strip them out.

## Configure a cluster

One endpoint is one cluster: host, port, API key (inline or from the credential vault — see
[Two ways to supply the key](#two-ways-to-supply-the-key)), TLS verification, what to collect,
and how often to poll.

**TLS.** Cohesity ships a self-signed certificate. Either trust its CA on the ActiveGate and
point **CA certificate file path** at it, or turn verification off — which is offered for lab
clusters and exposes the API key to anything that can answer for the cluster address.
Setting a CA path *and* turning verification off is refused, because that configuration reads
as "we trust this CA" while trusting everything.

**Collection is one switch, not two.** The **Collect …** toggles in the monitoring
configuration are the source of truth: turning one off skips the REST calls, which is what
actually saves load on a large estate. The feature sets in `extension.yaml` mirror them one for
one — `storage_domains`, `views`, `protection`, same names, same scope — and exist only because
every metric key has to belong to one. Leave them all enabled.

This used to be two independent switches, which could disagree and produce "metric missing,
both switches look fine". If you add a feature set, add the matching toggle in the same commit;
a test asserts the two lists match.

Cluster metrics and `cohesity.cluster.collection_success` sit in the always-on `default` feature
set. They are not optional: they carry the cluster id and name every other entity's dimensions
are namespaced against, and `collection_success` is the only signal separating "Cohesity is
healthy" from "the extension is broken". **Alert on the absence of a success, never on a zero** —
a failure before the cluster identity is known reports under the configured name, which is a
different series.

## Build

Needs Python 3.14. Nothing else is supported: ActiveGate 1.347 stopped shipping the 3.10
interpreter and began rolling out on 2026-09-08, five weeks before 3.10's nominal EOL, so
3.10-only builds already fail on updated ActiveGates.

```powershell
python -m venv .venv
.venv\Scripts\activate          # or: source .venv/bin/activate
pip install "dt-extensions-sdk[cli]" pytest

dt-sdk gencerts                 # once, writes to ~/.dynatrace/certificates
dt-sdk build -e manylinux2014_x86_64 -p 3.14
```

That produces `dist/custom_cohesity.storage-<version>.zip`, signed and ready to upload.
`-e manylinux2014_x86_64` is what makes the package work on a Linux ActiveGate when you build
on Windows.

> **PATH trap.** `dt-sdk` shells out to the `dt` CLI. The venv's `Scripts` (Windows) or `bin`
> (Linux/macOS) directory must be on `PATH`, not just the interpreter — activating the venv
> does this. Without it the build fails in a way that does not mention `dt` at all.

## Sign

`dt-sdk gencerts` is enough for a development build. For a signed package the customer can
install, mint a developer certificate once:

```bash
pip install dt-cli
dt ext genca --ca-cert ca.pem --ca-key ca.key
dt ext generate-developer-pem --output dev.pem --name "cohesity-storage" \
   --ca-crt ca.pem --ca-key ca.key
```

Upload `ca.pem` **once** to the tenant, under
**Settings ▸ Web and mobile monitoring ▸ Credential vault**, as a *Public certificate* with
`Extensions` scope. Without it every signed upload is rejected as untrusted.

Then assemble, sign and upload:

```bash
dt ext assemble --source extension --output extension.zip
dt ext sign --src extension.zip --output signed.zip --key dev.pem
dt ext upload --tenant-url "$DT_ENVIRONMENT" --api-token "$DT_API_TOKEN" signed.zip
```

Required token scopes: `extensions.write`, `extensions.read`, `extensionConfigurations.write`.

`ca.key` and `dev.pem` are signing material and are gitignored. Do not commit them, and do
not commit tenant ids or tokens.

## Develop

```powershell
copy secrets.example.json secrets.json   # any non-empty value works against the fake cluster
python tools\local_cohesity_server.py    # https://127.0.0.1:8443, serving fixtures/
dt-sdk run                               # in another shell; uses activation.json
pytest -q
ruff check .
```

`activation.json` already points at the fake cluster, with `verifyTls` off because the server
mints a throwaway self-signed certificate — the same choice a real Cohesity forces until its CA
is on the ActiveGate. `dt-sdk run` prints every metric it would have sent, so the payload is
visible without a tenant.

The fake cluster is not just a file server. It rejects a request with no `apiKey` header, and
`--software-version 7.2` makes `/v2/stats/top-views` return 404 exactly as a pre-7.3 cluster
does, so the version fork is exercised rather than assumed. It shifts fixture timestamps forward
to now by default (`--no-anchor` to serve them verbatim), because a frozen run time makes
age-since-last-success grow without bound.

### Replay mode

Setting **Fixture directory** in a monitoring configuration (`fixtureDir` in `activation.json`)
makes the extension read every response from recorded JSON and issue no requests at all. Same
client, same parsing, same code above it — it is a configuration swap, not a branch, so "we got
credentials" means clearing one field.

To record a real response, save the body to `fixtures/<key>.json` wrapped in an envelope:

```jsonc
{ "_fixture": { "provenance": "captured", "capturedAt": "2026-10-01T09:00:00Z",
                "clusterVersion": "7.4", "source": "cluster <name>" },
  "body": { /* verbatim response */ } }
```

The `<key>` is the request path with `/` turned into `_`, plus a suffix for any parameter that
selects different data — `v2_stats_time-series-stats__kSentryClusterStats`,
`v2_storage-domains__stats`. `cohesity_storage.fixtures.fixture_key()` is the one place that
naming lives, and the fake server and the replay transport both use it, so a file recorded for
one works with the other.

## Known traps

- `dt-sdk` needs the venv's `Scripts`/`bin` on `PATH` (above).
- **An empty dimension value is dropped, not sent** — so a field the extension failed to find
  becomes a dimension that silently does not exist. On the customer tenant that hid 28 of 92
  counted protection runs behind `status: None`, with healthy-looking totals. Anything that
  discriminates must resolve to a real string or not be emitted at all; see
  `domain.run_status`. The general rule: a value the ingest would drop is a bug the ingest
  will not report.
- **A packaged pipeline processor `description` over 512 characters is rejected at upload**, and
  `--validate-only` does **not** catch it — that validates the settings object, while the limit
  is enforced on the packaged extension asset. Two different gates. Keep a processor's rationale
  in this README, not in its description. `test_no_pipeline_processor_field_exceeds_its_server_limit`
  is the local check.
- **A new *required* activation-schema property breaks in-place config upgrades.** Every setting
  added after the first release is `nullable: true` with no `default` in the schema, and its
  real default in `config.DEFAULTS`. A nullable property carrying an explicit `""` default is
  rejected outright by Dynatrace, which is why `_bool`, `_text` and `_int` all fall back to
  `DEFAULTS` when they are handed `None`.
- **An extension package cannot create a routing entry on the built-in metrics ingest.** Its own
  openpipeline source routes its own metrics; a pipeline that has to see Dynatrace's data needs
  a routing entry created by hand. The host-link pipeline ships inert for exactly this reason —
  see [`dynatrace/README.md`](dynatrace/README.md).
- A credential vault entry is resolved by the **EEC**, not by the extension, and the field it is
  injected into is **not** the one named in the schema — and differs between vault and inline
  mode. See [the field-name trap](#the-field-name-trap). `referencedType: TOKEN` is unverified;
  `USERNAME_PASSWORD` (password half only) is the fallback.
- Ingest goes to the **classic** host (`https://<env>.<domain>`), not the apps host, which
  returns 404 for `/platform/ingest/v1/*`. That is a host problem, not a permission problem.
- Classic access tokens (`dt0c01.…`) do not exist on Gen3 — use platform tokens (`dt0s16.…`).
- Extension-imported metric events are **disabled by default after every upload and every
  update**, not just the first. Every version bump silently disables the alerts, so re-enable
  them after each one.
- `/v2/data-protect/runs/summary` has no pagination and no job filter, only a time window, so
  overlapping polls re-return the same run. Count outcomes through `new_protection_runs()`, never
  by counting `protection_runs()` — the latter double-counts by design and the symptom looks like
  a Cohesity problem.
- **That same absence of a job filter is why the window has to be small.** On the customer
  cluster — 41 protection groups, real history — the 15-minute window v0.1.5 sent returned
  nothing at all inside 120 seconds, and the five run metrics summed to zero over 24 hours while
  `last_success.age` showed jobs finishing minutes earlier. From v0.1.6 the window is the poll
  interval plus `client.RUNS_WINDOW_OVERLAP_SECONDS` (120 s) with **both** ends sent explicitly —
  7 minutes on a 5-minute interval, against 15 before. The overlap is not optional: narrower than
  the interval drops any run that starts and finishes between two polls, which is the one failure
  this extension must never report as a success. It is affordable only because the `run.id`
  ledger discards the duplicates it causes.
- **If it still will not answer, another endpoint will.** v0.1.6 tries
  `/v2/data-protect/runs/summary`, then `/v2/data-protect/protection-runs`, then
  `/v2/data-protect/protection-groups/{id}/runs`, and keeps whichever answered. The flat list is
  **not** in the 7.3.2 reference this extension was written from, so a 404 from it is an ordinary
  answer, not a fault. The per-group path is documented for the whole 6.8–7.4 range but costs one
  request per group. All three report the same `run.id`, which is what makes switching between
  them mid-flight safe. Which one won is a diagnostic event, `cohesity.diagnostic ==
  "runs_source"`; the metrics themselves cannot tell you.
- **The per-group path asks for a count, not a window — and it rotates.** The customer cluster
  fell through to it, reported `0 run(s) in the window` every poll for twenty minutes, and
  emitted no `run.*` metric at all while `last_success.age` showed groups finishing minutes
  earlier. The endpoint was fine; the question was wrong. A ~7-minute window is almost always
  empty for any *one* group, and a run landing while a different group is being asked is missed
  forever. From v0.1.7 the per-group path sends **no window** — only `numRuns`
  (`client.RUNS_PER_GROUP`, 3), the count parameter `GetProtectionGroupRuns` documents for the
  whole 6.8–7.4 range — and lets the `run.id` ledger discard the repeats, which is what the
  ledger is for. The per-poll cap became a request budget rather than a filter: it defaults to
  20 groups (`maxRunFanoutGroups`, settable per cluster, `config.DEFAULT_RUNS_FANOUT_GROUPS`)
  and **continues where the previous poll stopped**, so on 42 groups every group is reached
  every two to three polls instead of the same top 10 forever. Most-recently-finished still
  orders the rotation; a group that fails is skipped, counted and left behind. What the fan-out
  asked and found each poll is `cohesity.diagnostic == "runs_fanout"`: groups queried, groups
  errored, runs seen, runs new after dedup. Those four are the only way to tell "nothing ran"
  from "we are not looking in the right place".
- **An HTTP 500 usually means the request shape, not the values.** The customer cluster answers
  500 on `/v2/stats/time-series-stats` for all 20 `entityId` candidates and on
  `/v2/stats/top-views` for both metrics — the same status for every value, which rules the
  values out. So from v0.1.6 a 500 (and *only* a 500 — a 401, 403, 404 or 429 is an answer with
  its own fix) is retried in a short ordered list of alternative shapes, the first that returns
  200 is cached for the life of the client, and the winner is reported as
  `cohesity.diagnostic == "param_variant"`. time-series-stats: `metricNames` repeated instead of
  comma-joined, drop `rollupIntervalSecs`, drop both rollup parameters, a 60-second window, one
  metric per request. top-views: drop `protocol`, drop `lastHours`, `numTopViews=5`, `metric`
  alone. Dropping `metric` itself is deliberately **not** a variant — the endpoint would answer
  200 with its default series and the parser would file it under the metric that was never asked
  for. The whole probe is capped at `client.MAX_VARIANT_REQUESTS_PER_POLL` (12) extra requests
  per poll across every endpoint, re-opened by `begin_poll()`, and the count is logged whenever
  it is non-zero, so a cluster that 500s on everything cannot turn one poll into a request storm.
- `metricNames` on `/v2/stats/time-series-stats` is `explode: false`: comma-joined into one
  query parameter. Repeat it and the cluster reads only the last value, silently.
- `dataPoints[]` entries have no `value` field — `int64Value` / `doubleValue` / `stringValue`,
  all nullable, chosen by the sibling `type`.
- A wrong `entityId` on time-series-stats returns empty `dataPoints` and **no error**. Up to
  v0.1.3 the extension assumed `ClusterStatus.clusterId` was that id; on a real customer cluster
  it is not, and all five cluster-level metrics went missing silently. From v0.1.4 the client
  probes an ordered list of candidates — the id from v1 `/public/cluster` first, then
  `ClusterStatus.clusterId`, then incarnation ids, then any `entityId` the storage-domain schema
  catalogue hands out — keeps the first that returns a data point, and caches it per schema for
  the life of the client. A schema no candidate satisfies emits nothing and warns by name.
- Storage-domain `stats` field names are not identical across 6.8–7.4. The same customer cluster
  published `localTierResiliencyImpactBytes` but neither `totalLogicalUsageBytes` nor
  `localTotalPhysicalUsageBytes`, so `.usage.logical` and `.usage.physical` never arrived.
  `domain.STORAGE_DOMAIN_LOGICAL_FIELDS` / `…_PHYSICAL_FIELDS` list the accepted spellings in
  order; the first present and numeric wins, and none present still means no sample, never a zero.
- Extension log *lines* were not reaching Grail on the tenant this was debugged against, which is
  why v0.1.4 also emits a handful of log *events* (a different ingest path) once per client
  lifetime: which entityId each schema resolved to, the sorted key names of the first storage
  domain's `stats` object, and the cluster software version. Names only — never values, never the
  API key, never a response body. Query them with
  `fetch logs | filter log.source == "cohesity_storage.diagnostics"`.
- **A failing section used to be invisible.** `_section()` catches a collection failure into
  `self.logger`, and those lines do not reach Grail either — so on the customer cluster six
  metrics were missing with `collection_success` reading 1 (the cluster *did* answer) and no
  error readable anywhere. From v0.1.5 every swallowed failure is also a diagnostic event,
  `cohesity.diagnostic == "section_failure"`, carrying the section label, the exception class,
  the HTTP status, the request path, the query parameter **names**, and the message truncated to
  200 characters. The entityId probe reports the same way when a call *raises* rather than
  returning empty — before v0.1.5 an exception on the first candidate discarded the whole probe
  and recorded nothing at all. Still once per client per label, and the drain runs after the
  sections so a failure leaves in the poll that produced it.
- **Can a protection group be joined to a Dynatrace `HOST`?** (ticket 16) A VMware host
  publishes its BIOS UUID as ``host.additional_system_info[`system.serial`]``, which normalises to
  8-4-4-4-12 hex. Whether Cohesity's per-object `uuid` is that same UUID or a Cohesity-internal
  id decides whether the enrichment layer is possible at all, and neither the published schema
  nor anything outside the customer's network answers it. So from v0.1.8 the client asks **one**
  protection group — the most recently succeeded, unpaused, undeleted one — for **one** run with
  `includeObjectDetails=true`, **once per client lifetime**, and reports
  `cohesity.diagnostic == "protected_objects"`: the sorted key names under `objects[].object`,
  whether a VMware-specific sub-object (`vCenterSummary`) is present and what *it* calls its
  keys, the group's environment (`kVMware`, `kSQL`, `kPhysical`, …), and for up to three objects
  the `uuid` value with a shape verdict. The verdict tests for a decimal id **before** testing
  for hex, because 32 decimal digits are also 32 valid hex digits and a purely structural test
  would report Cohesity's own int64 id as a UUID — the exact opposite of the right answer. The
  probe cannot raise, cannot run twice, and a 403 on it costs nothing: the run metrics that
  v0.1.7 finally got working are reported either way. Object names, addresses and everything
  else identifying stay behind as field *names*, never as values.
- Every diagnostic message is redacted before it leaves: the configured API key and the
  credential-vault id are removed by identity, and anything credential-shaped (`token=…`, a run
  of 20+ opaque characters) by shape. The auth message names the vault entry on purpose — a
  rejected credential and an unresolved one have different fixes — but a log record has a wider
  audience than the ActiveGate's own logs.

## Layout

```
extension/
  extension.yaml          name, version, floors, python runtime, feature sets, metric metadata
  activationSchema.json   the monitoring configuration UI
  openpipeline/
    metrics.source.json   routes this extension's metrics to the pipeline below
    metrics.pipeline.json smartscape node and edge extraction - the entity model
    host-link.pipeline.json  runs on DYNATRACE's host metrics - the protection group -> HOST edge
dynatrace/                tenant assets installed by hand, NOT shipped in the zip
  README.md               what each one is and the five steps to deploy them
  routing-host-link.json  the routing entry that feeds host metrics to the pipeline above
  sync-host-link-task.js  the workflow that keeps the lookup tables current
  workflow-sync-host-link.json  the same script, importable
cohesity_storage/
  __main__.py             scheduling and reporting - the Extension subclass
  config.py               cluster parsing and validation
  client.py               one method per Cohesity endpoint, returning parsed data
  transport.py            HTTPS to a cluster, or replay from disk - chosen by config
  domain.py               response bodies to domain objects; dedup ledger; version parsing
  fixtures.py             recorded responses and their provenance
  errors.py               the failure taxonomy: auth vs TLS vs version vs fixture
  metrics.py              metric keys, dimensions, and payload-to-sample mapping
fixtures/                 13 recorded responses - all SYNTHETIC today
tests/
  cohesity_fake_cluster.py  the stand-in cluster, used in process by the tests
  test_config.py          configuration validation
  test_domain.py          parsing traps: dataPoints value types, run dedup, version fork
  test_client.py          request shapes, error mapping, replay against the fixtures
  test_fixtures.py        fixture naming and provenance
  test_fake_cluster.py    auth, the 7.3 fork, and one pass over a real socket
  test_metrics.py         key prefixes, dimension keys, the ticket 06 contract
  test_reporting.py       fixture bodies to samples - the whole metric path, no EEC
  test_manifest.py        extension.yaml, activation schema and pipeline JSON vs the code
  test_host_link.py       uuid normalisation, the bridge metric, the lookup-table encoder
tools/
  local_cohesity_server.py  runnable fake cluster for dt-sdk run
  host_link.py              reference lookup-table encoder; the workflow JS mirrors it
```

## License

MIT — see [LICENSE](LICENSE).
