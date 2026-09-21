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

## Status: v0.1.0 — full metric set and Smartscape topology

21 metric keys across three entity prefixes, and three Smartscape node types with three edges,
all derived from the metric stream by OpenPipeline. Nothing has yet run against a real Cohesity
cluster; every number below has only ever come from a synthetic fixture.

| Piece | Lives in |
|---|---|
| Metric keys, units and dimensions | `cohesity_storage/metrics.py`, `extension/extension.yaml` |
| Parsed data to `report_metric` | `ExtensionImpl._poll` and the `_report_*` methods |
| Entity types and relationships | `extension/openpipeline/metrics.pipeline.json` |

Still open: dashboards, alerting, and the fact that the run-dedup ledger is in memory, so an
ActiveGate restart re-counts recently completed protection runs.

**Every fixture in `fixtures/` is synthetic.** They were hand-written from Cohesity's published
6.8–7.4 response schemas; no cluster has been reached. They prove the code parses the documented
shapes, not that the shapes are right. Each one says so in its own `_fixture.provenance`, the
extension logs a warning every poll while replaying one, and a test fails the moment one arrives
unmarked.

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
  by counting `protection_runs()` — the latter triple-counts by design and the symptom looks like
  a Cohesity problem.
- `metricNames` on `/v2/stats/time-series-stats` is `explode: false`: comma-joined into one
  query parameter. Repeat it and the cluster reads only the last value, silently.
- `dataPoints[]` entries have no `value` field — `int64Value` / `doubleValue` / `stringValue`,
  all nullable, chosen by the sibling `type`.
- A wrong `entityId` on time-series-stats returns empty `dataPoints` and **no error**. The
  assumption that `ClusterStatus.clusterId` is that id is flagged in `domain.py` and warned about
  at runtime; it is unverified against a real cluster.

## Layout

```
extension/
  extension.yaml          name, version, floors, python runtime, feature sets, metric metadata
  activationSchema.json   the monitoring configuration UI
  openpipeline/
    metrics.source.json   routes this extension's metrics to the pipeline below
    metrics.pipeline.json smartscape node and edge extraction - the entity model
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
tools/
  local_cohesity_server.py  runnable fake cluster for dt-sdk run
```

## License

MIT — see [LICENSE](LICENSE).
