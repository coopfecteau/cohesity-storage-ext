# End-to-end harness

A disposable EC2 instance runs a real **Environment ActiveGate** and the repo's **fake Cohesity
cluster** on its own `127.0.0.1`. From the laptop, `e2e/loop.py` builds a uniquely versioned
package, signs it, uploads it, activates it, points a monitoring configuration at that
ActiveGate's group, waits, and then asserts through DQL that metrics arrived and Smartscape nodes
and edges exist. Each step prints `PASS`/`FAIL`; the exit code is non-zero on any failure.

> **What this proves, and what it does not.** A green run proves the *Dynatrace side* end to end:
> the package is accepted and trusted, the EEC runs it on a real ActiveGate against a real TLS
> socket, metrics land in Grail, and the bundled OpenPipeline config turns them into Smartscape
> nodes and edges. Every number comes from **SYNTHETIC** fixtures. It says nothing about whether
> a real Cohesity cluster's responses match those fixtures.

```
laptop                                     EC2 (Ubuntu 24.04, no inbound ports)
e2e/loop.py ──upload/activate/config──▶ tenant ◀── ActiveGate (group cohesity-e2e)
     │                                               │ EEC runs the extension
     └── dtctl query (DQL) ◀── Grail/Smartscape      ▼
                                           https://127.0.0.1:8443 fake cluster (--drift)
```

## Before anything: trust the signing CA on the tenant

Trust is per tenant, and a tenant that has never seen this CA rejects the upload. Upload the
**public** certificate `~/.dynatrace/certificates/ca.pem` to the tenant's credential vault as
type **Public certificate**, scope **Extension validation**. Never upload `ca.key` or
`developer.pem`. `loop.py --preflight` checks this without writing anything (see below).

The same CA is also copied onto the ActiveGate by the boot script, because the ActiveGate checks
the signature again at assignment time.

## Prerequisites

| What | Why | Exact scope |
|---|---|---|
| **Installer token** (e.g. `DT_OPERATOR_TOKEN`) | Terraform's boot script downloads the ActiveGate installer with it | `InstallerDownload` (classic `dt0c01.` token, sent as `Api-Token`), or a platform token with `fleet-management:activegates:download` (sent as `Bearer`) |
| **Platform token** (`DT_BEARER_TOKEN`, `dt0s16.`) | `loop.py` upload / activate / configure / cleanup | `extensions:definitions:read`, `extensions:definitions:write`, `extensions:configurations:read`, `extensions:configurations:write`, `openpipeline:configurations:write` |
| `dtctl` context (default `hsn`) | DQL assertions run under your SSO session - no token | `dtctl auth login --context hsn --environment https://<env>.sprint.apps.dynatracelabs.com` |
| AWS credentials | Terraform | EC2, VPC, IAM role/instance profile, SSM parameter |
| Existing certs in `~/.dynatrace/certificates` | Signing | `developer.pem` + `ca.pem`. Never regenerated - a new CA would untrust every existing package |

The `:read` scopes are needed because the loop lists versions (cleanup) and existing monitoring
configurations (update-in-place rather than piling up duplicates). `openpipeline:configurations:write`
is for the OpenPipeline source/pipeline bundled in `extension.yaml`, which activation installs.

### Where tokens live

Nowhere in this repository. Keep them in your own dotenv file outside the repo and point the
tooling at it; the file is **parsed, never executed**, and values are never printed:

```
DT_BEARER_TOKEN=dt0s16....        # platform token (loop.py)
DT_OPERATOR_TOKEN=dt0c01....      # installer token (terraform)
DT_APPS_HOST=https://<env>.sprint.apps.dynatracelabs.com
DT_API_URL=https://<env>.sprint.dynatracelabs.com/api
DT_CONTEXT=hsn
```

A variable already set in your shell wins over the file. The repo's `.gitignore` covers
`e2e/**/*.tfvars`, `*.tfstate*`, `.terraform/`, `*.env` and `.env*` under `e2e/`, in case one
lands here by accident.

Hosts: sprint tenants use `<env>.sprint.apps.dynatracelabs.com` (platform APIs, `loop.py`) and
`<env>.sprint.dynatracelabs.com` (classic APIs, installer download); production uses
`<env>.apps.dynatrace.com` / `<env>.live.dynatrace.com`. `loop.py` derives the classic host
from the apps host when `DT_API_URL` is absent.

## 1. Stand up the instance

`fal` and `hsn` are **shared** tenants. The harness only touches the `cohesity-e2e` ActiveGate
group and the `custom:cohesity.storage` extension, so nothing else on the tenant is affected.

Push first: the instance `git clone`s this public repo at `repo_ref` (default `master`) to get the
fake server, `--drift` and `e2e/scripts/fake-cluster-cert.sh`.

```powershell
# PowerShell - token passes from your file to terraform's process only, then is cleared
.\e2e\tf.ps1 -EnvFile C:\path\to\your.env init
.\e2e\tf.ps1 -EnvFile C:\path\to\your.env apply
```

```bash
# bash equivalent
e2e/tf.sh --env-file /path/to/your.env -- init
e2e/tf.sh --env-file /path/to/your.env -- apply
```

Or set `TF_VAR_dt_paas_token` / `TF_VAR_dt_environment_url` yourself and run `terraform` in
`e2e/terraform`, or copy `terraform.tfvars.example` to the gitignored `terraform.tfvars`.

Boot takes ~5 minutes. Watch it with the `ssm_session` and `bootstrap_log` outputs. The boot
script:

1. installs the ActiveGate with `--set-group=cohesity-e2e` (installer token read from SSM
   Parameter Store, not templated into user-data);
2. places the signing CA in `/var/lib/dynatrace/remotepluginmodule/agent/conf/certificates/`,
   owned by `dtuserag` (missing or unreadable = "checking signature failed" at assignment);
3. clones this repo and runs `tools/local_cohesity_server.py --drift --api-key e2e-key` as the
   `cohesity-fake` systemd service on `127.0.0.1:8443`;
4. mints a throwaway CA + leaf for `IP:127.0.0.1` (`e2e/scripts/fake-cluster-cert.sh`) so the
   monitoring configuration uses **`verifyTls: true`** with `caCertPath /etc/cohesity-fake/ca.crt`
   - real certificate verification, not verification switched off. The script builds a proper CA
   because Python 3.13+ verifies strictly and rejects a bare self-signed server certificate;
5. proves the fake cluster answers over verified TLS, then restarts the ActiveGate.

Tear down with `.\e2e\tf.ps1 -EnvFile ... destroy`. Nothing on the tenant is removed by that - the
ActiveGate simply goes offline; run the loop with `--cleanup` beforehand to drop dev versions.

**Cost** (us-east-1, on demand): t3.medium ≈ $0.042/h, 20 GB gp3 ≈ $0.07/day, public IPv4 ≈
$0.005/h - about **$1.10 per day, ~$34 per month** if left running. Destroy it between sessions.

## 2. Run the loop

```powershell
$py = "..\ssh_ext\.venv\Scripts\python.exe"

& $py e2e\loop.py --env-file C:\path\to\your.env --preflight   # read-only checks, no writes
& $py e2e\loop.py --env-file C:\path\to\your.env --cleanup     # build, ship, assert, prune
& $py e2e\loop.py --dry-run --env-url https://example.apps.dynatrace.com   # print, do nothing
```

| Flag | Default | |
|---|---|---|
| `--env-file PATH` | none | dotenv to read tokens/URLs from; env vars win |
| `--token-var NAME` | `DT_BEARER_TOKEN` | platform token variable |
| `--operator-token-var NAME` | `DT_OPERATOR_TOKEN` | installer token (preflight only) |
| `--env-url` / `--env-url-var` | `$DT_APPS_HOST` | apps host |
| `--api-url` / `--api-url-var` | `$DT_API_URL`, else derived | classic host (trailing `/api` ok) |
| `--context` | `$DT_CONTEXT`, else `hsn` | dtctl context for DQL |
| `--ag-group` | `cohesity-e2e` | monitoring configuration scope `ag_group-<group>` |
| `--skip-build` | off | upload the newest zip in `e2e/.dist/` or `dist/` |
| `--timeout` | 600 s | how long DQL assertions may take |
| `--cleanup` | off | delete every other `0.99.*` version after activation |
| `--dry-run` | off | print each HTTP request (token shown as `***`) and each DQL; perform nothing |

Steps:

1. **build** - rewrites `extension/extension.yaml` to `0.99.<unix-minutes>` (restored byte for
   byte in a `finally`, even on Ctrl+C), runs `dt-sdk build -e manylinux2014_x86_64 -p 3.14`
   with the venv's `Scripts` on `PATH`, output in `e2e/.dist/`. The `0.99` minor never collides
   with a real release and is what `--cleanup` keys on.
2. **upload** - `POST /platform/extensions/v2/extensions`, `application/octet-stream` (dtctl 0.37
   omits the Content-Type and gets 415, hence direct REST).
3. **activate** - `PUT` (or `POST` for the first) `.../environment-configuration {"version"}`.
4. **monitoring config** - update the existing configuration on `ag_group-<group>` or create
   one: `host 127.0.0.1`, `port 8443`, inline `apiKey e2e-key`, `verifyTls true`, every
   collection toggle on, `intervalMinutes 1`.
5. **assert** - polls every 30 s until all pass or the timeout:

| Check | DQL | Proves |
|---|---|---|
| metrics present | `metrics` filtered on `cohesity`, window starting at this run | all **22** keys from `extension.yaml` reached Grail (a Grail-side prefix such as `ext:` is tolerated and reported) |
| collection_success | `timeseries max(cohesity.cluster.collection_success)` over 5 min | the extension reached the cluster over verified TLS with the key, this interval |
| smartscape nodes | `smartscapeNodes "EXT_COHESITY_*"` by type | OpenPipeline node extraction produced ≥1 `EXT_COHESITY_CLUSTER`, `_STORAGE_DOMAIN`, `_PROTECTION_GROUP` |
| smartscape edges | `smartscapeEdges "contains", "writes_to"` | cluster `contains` domain and `contains` group, group `writes_to` domain. Rows are counted client-side: a `startsWith` filter on `source_id` in `smartscapeEdges` silently matches nothing and reads as a missing edge |

Run metrics depend on completed protection runs; with static fixtures they would only appear on
the first poll after a restart. `--drift` mints a new completed run per group every 5 minutes
and moves capacity/IOPS/latency/throughput, so run counters increment and charts are not flat.

### Preflight

`--preflight` performs only reads and one validate (which persists nothing) and prints
PASS/FAIL with scope names, never values:

1. platform token lists extensions on this tenant (`extensions:definitions:read`; a token for
   another tenant is rejected);
2. installer token lists ActiveGate installer versions (`InstallerDownload`);
3. the newest built zip passes `POST /platform/extensions/v2/extensions:validate` - the same
   checks as an upload, signature included, so it proves the tenant trusts your CA (and
   `extensions:definitions:write`). As secondary evidence it lists credentials for a
   `PUBLIC_CERTIFICATE` scoped `EXTENSION_AUTHENTICATION`; that API never returns certificate
   contents, so it cannot prove it is *this* CA, and it needs `credentialVault.read`, which a
   platform token may lack - then it reports SKIP;
4. the dtctl context answers a trivial DQL query.

Not checkable without writing: `extensions:configurations:write` and
`openpipeline:configurations:write`.

## Troubleshooting

- **"checking signature failed"** - CA missing from the tenant vault (upload fails) or from the
  ActiveGate's certificates directory / wrong owner (assignment fails). Check `bootstrap_log`.
- **collection_success never 1** - `sudo journalctl -u cohesity-fake`, and the extension logs under
  `/var/lib/dynatrace/remotepluginmodule/log/` on the host.
- **upload rejected for too many versions** - a tenant holds at most 10 versions of an extension
  (Manage Extensions docs); run with `--cleanup`, which keeps only the version it just activated.
