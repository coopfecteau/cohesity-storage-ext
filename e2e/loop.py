"""End-to-end loop: build, sign, upload, activate, configure, then prove data arrived via DQL.

Runs from the laptop against a disposable ActiveGate (see ``e2e/terraform``) that polls the fake
Cohesity cluster on its own 127.0.0.1. Stdlib only; run it with the extension venv's python so
``dt-sdk`` and ``dt`` sit next to the interpreter::

    ..\\ssh_ext\\.venv\\Scripts\\python.exe e2e\\loop.py --env-file <path> [--cleanup]
    ..\\ssh_ext\\.venv\\Scripts\\python.exe e2e\\loop.py --env-file <path> --preflight
    ..\\ssh_ext\\.venv\\Scripts\\python.exe e2e\\loop.py --dry-run --env-url https://<env>.apps.<domain>

Every step prints PASS or FAIL; the exit code is non-zero if anything failed.

What a PASS proves: the Dynatrace side works end to end - the signed package is accepted, the
EEC runs it on a real ActiveGate, metrics land in Grail and OpenPipeline turns them into
Smartscape nodes and edges. It does NOT prove real Cohesity responses match the fixtures: the
cluster behind it is fake and every number is SYNTHETIC.

Secrets: the platform token is read from the environment or from ``--env-file``, used only to
build an Authorization header, and never printed, logged, written or included in an error.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
EXTENSION_YAML = REPO_ROOT / "extension" / "extension.yaml"
BUILD_DIR = REPO_ROOT / "e2e" / ".dist"
CERT_DIR = Path.home() / ".dynatrace" / "certificates"

DEFAULT_CONTEXT = "hsn"
DEFAULT_AG_GROUP = "cohesity-e2e"
DEFAULT_TOKEN_VAR = "DT_BEARER_TOKEN"
DEFAULT_OPERATOR_TOKEN_VAR = "DT_OPERATOR_TOKEN"
DEFAULT_ENV_URL_VAR = "DT_APPS_HOST"
DEFAULT_API_URL_VAR = "DT_API_URL"
DEFAULT_CONTEXT_VAR = "DT_CONTEXT"

# e2e builds live in their own minor so they can never collide with, or be mistaken for, a
# real release, and --cleanup can find them by prefix alone.
DEV_VERSION_PREFIX = "0.99."

# Mirrors e2e/terraform/userdata: the fake cluster's fixed key and the CA the boot script mints.
# The key is not a secret - it only unlocks synthetic fixtures on the instance's loopback.
FAKE_CLUSTER = {
    "host": "127.0.0.1",
    "port": 8443,
    "apiKey": "e2e-key",
    "caCertPath": "/etc/cohesity-fake/ca.crt",
}

NODE_TYPES = ("EXT_COHESITY_CLUSTER", "EXT_COHESITY_STORAGE_DOMAIN", "EXT_COHESITY_PROTECTION_GROUP")
EDGES = (
    ("contains", "EXT_COHESITY_CLUSTER", "EXT_COHESITY_STORAGE_DOMAIN"),
    ("contains", "EXT_COHESITY_CLUSTER", "EXT_COHESITY_PROTECTION_GROUP"),
    ("writes_to", "EXT_COHESITY_PROTECTION_GROUP", "EXT_COHESITY_STORAGE_DOMAIN"),
)
COLLECTION_SUCCESS = "cohesity.cluster.collection_success"

# Doc references for every call below. The platform paths were checked against the published
# SDK source (@dynatrace-sdk/client-extensions-v2 3.1.0, cjs/index.js), which is generated from
# the same OpenAPI spec as the platform, and against the Manage Extensions guide.
DOC_MANAGE = "https://docs.dynatrace.com/docs/ingest-from/extensions/manage-extensions"
DOC_SDK = "https://developer.dynatrace.com/develop/sdks/client-extensions-v2/v3/"
DOC_AG_VERSIONS = (
    "https://docs.dynatrace.com/docs/dynatrace-api/environment-api/deployment/activegate/"
    "get-activegate-versions"
)
DOC_CREDENTIALS = "https://docs.dynatrace.com/docs/dynatrace-api/environment-api/credential-vault/get-all"


class ConfigError(Exception):
    """Bad or missing input. Messages name variables and files, never their values."""


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------------------------
# Secrets and settings
# ---------------------------------------------------------------------------------------------


def parse_env_file(text: str) -> dict[str, str]:
    """Parse simple dotenv: KEY=VALUE lines, ``#`` comments, blank lines, optional quotes.

    Deliberately not a shell: nothing is expanded or executed, so pointing this at a file
    someone else maintains cannot run their code.
    """
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        values[key] = value
    return values


@dataclass
class Settings:
    """Where values come from: the real environment first, then the env file."""

    env_file: Path | None = None
    _file_values: dict[str, str] | None = None

    def _file(self) -> dict[str, str]:
        if self._file_values is None:
            if self.env_file is None:
                self._file_values = {}
            else:
                try:
                    text = self.env_file.read_text(encoding="utf-8-sig")
                except OSError as exception:
                    msg = f"cannot read env file {self.env_file}: {exception.strerror or exception}"
                    raise ConfigError(msg) from None
                self._file_values = parse_env_file(text)
        return self._file_values

    def get(self, name: str) -> str | None:
        value = os.environ.get(name)
        if value:
            return value
        value = self._file().get(name)
        return value or None

    def missing(self, name: str) -> str:
        """The message for an absent variable: its name and where we looked, never a value."""
        where = f"the environment or in {self.env_file}" if self.env_file else "the environment"
        return f"{name} is not set in {where}"

    def require(self, name: str) -> str:
        value = self.get(name)
        if value:
            return value
        raise ConfigError(self.missing(name))


class Redactor:
    """Scrubs every known secret out of text before it can reach the terminal."""

    def __init__(self) -> None:
        self._secrets: list[str] = []

    def add(self, secret: str | None) -> None:
        if secret and len(secret) >= 4 and secret not in self._secrets:
            self._secrets.append(secret)

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        return text


def normalise_url(value: str) -> str:
    value = value.strip().rstrip("/")
    if value and "://" not in value:
        value = "https://" + value
    return value


def classic_url(apps_url: str) -> str:
    """The classic (``/api/v2``, ``/api/v1/deployment``) host for an apps host.

    Production: ``<env>.apps.dynatrace.com`` -> ``<env>.live.dynatrace.com``. Sprint and dev
    drop the ``.apps`` label: ``<env>.sprint.apps.dynatracelabs.com`` ->
    ``<env>.sprint.dynatracelabs.com``. Anything else is returned unchanged - pass --api-url.
    """
    parsed = urllib.parse.urlsplit(normalise_url(apps_url))
    host = parsed.hostname or ""
    if host.endswith(".apps.dynatrace.com"):
        host = host[: -len(".apps.dynatrace.com")] + ".live.dynatrace.com"
    elif ".apps." in host:
        host = host.replace(".apps.", ".", 1)
    return f"{parsed.scheme}://{host}"


def api_base_url(value: str) -> str:
    """DT_API_URL conventionally ends in ``/api``; the calls below add their own ``/api/...``."""
    value = normalise_url(value)
    return value[: -len("/api")] if value.endswith("/api") else value


# ---------------------------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------------------------


@dataclass
class Report:
    redact: Redactor
    results: list[tuple[str, bool | None, str]] = field(default_factory=list)

    def record(self, name: str, ok: bool | None, detail: str = "") -> bool | None:
        label = {True: "PASS", False: "FAIL", None: "SKIP"}[ok]
        detail = self.redact(detail)
        self.results.append((name, ok, detail))
        print(f"[{label}] {name:<24} {detail}", flush=True)
        return ok

    def info(self, text: str) -> None:
        print(f"       {self.redact(text)}", flush=True)

    @property
    def failed(self) -> bool:
        return any(ok is False for _, ok, _ in self.results)

    def summary(self) -> None:
        print("\nSummary")
        for name, ok, _ in self.results:
            print(f"  { ({True: 'PASS', False: 'FAIL', None: 'SKIP'}[ok]) }  {name}")


# ---------------------------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------------------------


@dataclass
class Api:
    """Minimal JSON client. The token only ever becomes an Authorization header."""

    base_url: str
    token: str | None
    scheme: str
    redact: Redactor
    dry_run: bool = False
    timeout: float = 120.0

    def request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        json_body: Any = None,
        data: bytes | None = None,
        content_type: str | None = None,
        doc: str = "",
    ) -> tuple[int, Any]:
        url = self.base_url + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {"Accept": "application/json"}
        if json_body is not None:
            data = json.dumps(json_body).encode("utf-8")
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type

        if self.dry_run:
            shown = dict(headers, Authorization=f"{self.scheme} ***")
            print(f"  DRY-RUN {method} {url}")
            print(f"          headers {shown}")
            if json_body is not None:
                print(
                    "          body    "
                    + self.redact(json.dumps(json_body, indent=2)).replace("\n", "\n          ")
                )
            elif data is not None:
                print(f"          body    <{len(data)} bytes {content_type}>")
            if doc:
                print(f"          doc     {doc}")
            return 0, None

        if not self.token:
            msg = "no token available for this call"
            raise ConfigError(msg)
        headers["Authorization"] = f"{self.scheme} {self.token}"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:  # noqa: S310
                raw = response.read()
                return response.status, _json_or_text(raw)
        except urllib.error.HTTPError as error:
            body = _json_or_text(error.read())
            raise ApiError(error.code, self.redact(friendly_error(method, path, error.code, body))) from None
        except urllib.error.URLError as error:
            msg = self.redact(f"{method} {path}: cannot reach {self.base_url} ({error.reason})")
            raise ApiError(0, msg) from None


def _json_or_text(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")


def friendly_error(method: str, path: str, status: int, body: Any) -> str:
    """An actionable sentence first, the server's own words as the suffix."""
    message = ""
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            message = str(error.get("message", ""))
            violations = error.get("constraintViolations") or []
            if violations:
                message += " | " + "; ".join(
                    f"{v.get('path', '')}: {v.get('message', '')}" for v in violations if isinstance(v, dict)
                )
    elif isinstance(body, str):
        message = body
    hint = {
        401: "the token was rejected - wrong tenant, expired, or not a platform token",
        403: "the token lacks a scope (the server's message names it) - create a new token with it",
        404: "not found - check the environment URL and that the extension/version exists",
        409: "the tenant is still processing a previous upload - retry in a few seconds",
        415: "wrong Content-Type for this endpoint",
    }.get(status, "request failed")
    return f"{method} {path}: HTTP {status}, {hint}. Raw: {message[:500]}"


# ---------------------------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------------------------


def extension_name(yaml_text: str) -> str:
    match = re.search(r"^name:\s*(\S+)\s*$", yaml_text, re.MULTILINE)
    if not match:
        msg = f"no top-level name: in {EXTENSION_YAML}"
        raise ConfigError(msg)
    return match.group(1).strip("\"'")


def extension_version(yaml_text: str) -> str:
    match = re.search(r"^version:\s*(\S+)\s*$", yaml_text, re.MULTILINE)
    if not match:
        msg = f"no top-level version: in {EXTENSION_YAML}"
        raise ConfigError(msg)
    return match.group(1).strip("\"'")


def expected_metric_keys(yaml_text: str) -> list[str]:
    """Every metric key the manifest declares - the contract ticket 06 fixed at 21."""
    return sorted(set(re.findall(r"^\s*-\s*key:\s*(\S+)\s*$", yaml_text, re.MULTILINE)))


def dev_version(now: float) -> str:
    """``0.99.<unix-minutes>``: unique per minute and ordered, so the newest is obvious."""
    return f"{DEV_VERSION_PREFIX}{int(now // 60)}"


def with_version(yaml_text: str, version: str) -> str:
    new_text, count = re.subn(r"^version:.*$", f"version: {version}", yaml_text, count=1, flags=re.MULTILINE)
    if count != 1:
        msg = f"no top-level version: line in {EXTENSION_YAML}"
        raise ConfigError(msg)
    return new_text


@contextlib.contextmanager
def temporary_version(path: Path, version: str) -> Iterator[None]:
    """Rewrite the manifest version for the duration of a build, and always put it back.

    Restored from the original bytes, not re-rendered, so line endings and everything else in
    the file survive byte for byte even if the build crashes or is interrupted.
    """
    original = path.read_bytes()
    path.write_bytes(with_version(original.decode("utf-8"), version).encode("utf-8"))
    try:
        yield
    finally:
        path.write_bytes(original)


def zip_name(name: str, version: str) -> str:
    # dt-sdk's ExtensionYaml.zip_file_name(): the colon in custom:... is not filename-safe.
    return f"{name.replace(':', '_')}-{version}.zip"


def build_command(scripts_dir: Path) -> list[str]:
    dt_sdk = shutil.which("dt-sdk", path=str(scripts_dir)) or str(scripts_dir / "dt-sdk")
    return [
        dt_sdk,
        "build",
        "-e",
        "manylinux2014_x86_64",
        "-p",
        "3.14",
        "-k",
        str(CERT_DIR / "developer.pem"),
        "-t",
        str(BUILD_DIR),
        str(REPO_ROOT),
    ]


def build(report: Report, name: str, version: str, dry_run: bool) -> Path | None:
    scripts_dir = Path(sys.executable).parent
    command = build_command(scripts_dir)
    target = BUILD_DIR / zip_name(name, version)
    if dry_run:
        print(f"  DRY-RUN set {EXTENSION_YAML.name} version -> {version} (restored afterwards)")
        print(f"  DRY-RUN PATH={scripts_dir};... {' '.join(command)}")
        report.record("build", None, f"dry run - would produce {target.relative_to(REPO_ROOT)}")
        return target
    for needed in ("developer.pem", "ca.pem"):
        # Existence only. The signing key is never read here and never regenerated: a new CA
        # would invalidate every package already trusted by a tenant.
        if not (CERT_DIR / needed).is_file():
            report.record("build", False, f"missing {CERT_DIR / needed} - the existing certs are required")
            return None
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    # dt-sdk shells out to `dt`, which lives next to it in the venv's Scripts directory.
    env["PATH"] = str(scripts_dir) + os.pathsep + env.get("PATH", "")
    with temporary_version(EXTENSION_YAML, version):
        completed = subprocess.run(
            command, cwd=REPO_ROOT, env=env, capture_output=True, text=True, check=False
        )  # noqa: S603
    if extension_version(EXTENSION_YAML.read_text(encoding="utf-8")) == version:
        report.record("build", False, f"{EXTENSION_YAML} was NOT restored - fix it before committing")
        return None
    if completed.returncode != 0 or not target.is_file():
        tail = (completed.stdout + completed.stderr)[-1500:]
        report.record("build", False, f"dt-sdk build exited {completed.returncode}: {tail}")
        return None
    report.record("build", True, f"{version} -> {target.relative_to(REPO_ROOT)}")
    return target


def newest_build(name: str) -> tuple[Path, str] | None:
    pattern = re.compile(re.escape(name.replace(":", "_")) + r"-(\d+\.\d+\.\d+)\.zip$")
    found = []
    for directory in (BUILD_DIR, REPO_ROOT / "dist"):
        if directory.is_dir():
            for candidate in directory.iterdir():
                match = pattern.match(candidate.name)
                if match:
                    found.append((candidate.stat().st_mtime, candidate, match.group(1)))
    if not found:
        return None
    _, path, version = max(found)
    return path, version


# ---------------------------------------------------------------------------------------------
# Tenant configuration
# ---------------------------------------------------------------------------------------------


def monitoring_value(version: str, group: str) -> dict[str, Any]:
    """The monitoring configuration, field for field against extension/activationSchema.json.

    verifyTls is ON with caCertPath at the CA the instance minted at boot, so the run exercises
    real certificate verification rather than switching it off. Every collection toggle is on
    and the interval is the 1-minute minimum, so the assertions do not wait on a 5-minute poll.
    """
    return {
        "enabled": True,
        "description": f"cohesity e2e on ag_group-{group} - SYNTHETIC fake cluster, managed by e2e/loop.py",
        "version": version,
        "activationContext": "REMOTE",
        # "default" is always on and cannot be listed off; these mirror the three toggles.
        "featureSets": ["storage_domains", "views", "protection"],
        "pythonRemote": {
            "endpoints": [
                {
                    "name": "cohesity-e2e-fake",
                    "host": FAKE_CLUSTER["host"],
                    "port": FAKE_CLUSTER["port"],
                    "useCredentialVault": False,
                    "apiKey": FAKE_CLUSTER["apiKey"],
                    "verifyTls": True,
                    "caCertPath": FAKE_CLUSTER["caCertPath"],
                    "collectStorageDomains": True,
                    "collectViews": True,
                    "collectProtection": True,
                    "intervalMinutes": 1,
                    "requestTimeoutSeconds": 30,
                }
            ]
        },
    }


def ext_path(name: str, suffix: str = "") -> str:
    # Unencoded, as the SDK sends it: the colon in custom:... is legal in a path segment.
    return f"/platform/extensions/v2/extensions/{name}{suffix}"


def upload(api: Api, report: Report, package: Path) -> bool:
    data = b"" if api.dry_run else package.read_bytes()
    for attempt in range(6):
        try:
            # POST /platform/extensions/v2/extensions, body = the signed zip as octet-stream.
            # dtctl 0.37 omits the Content-Type and gets 415. Docs: DOC_MANAGE ("Upload an
            # extension with Dynatrace API"); SDK uploadExtension, scope extensions:definitions:write.
            status, body = api.request(
                "POST",
                "/platform/extensions/v2/extensions",
                data=data,
                content_type="application/octet-stream",
                doc=DOC_MANAGE,
            )
        except ApiError as error:
            if error.status == 409 and attempt < 5:
                time.sleep(5)
                continue
            report.record("upload", False, str(error))
            return False
        if api.dry_run:
            report.record("upload", None, "dry run")
            return True
        version = body.get("version") if isinstance(body, dict) else "?"
        report.record("upload", True, f"HTTP {status}, version {version}")
        return True
    return False


def activate(api: Api, report: Report, name: str, version: str) -> bool:
    try:
        current = None
        if not api.dry_run:
            try:
                # GET .../environment-configuration - SDK getActiveExtensionEnvironmentConfiguration,
                # scope extensions:definitions:read. A 404 means no version is active yet.
                _, body = api.request("GET", ext_path(name, "/environment-configuration"), doc=DOC_SDK)
                current = body.get("version") if isinstance(body, dict) else None
            except ApiError as error:
                if error.status != 404:
                    raise
        # PUT switches the active version; POST activates the first one. Both take
        # {"version": ...}. Docs: DOC_MANAGE ("Activate ..." / "Update active configuration
        # version"); SDK update/activateExtensionEnvironmentConfiguration, extensions:definitions:write.
        method = "PUT" if current else "POST"
        if api.dry_run:
            print("  (dry run: PUT if a version is already active, POST otherwise)")
        api.request(
            method,
            ext_path(name, "/environment-configuration"),
            json_body={"version": version},
            doc=DOC_MANAGE,
        )
    except ApiError as error:
        report.record("activate", False, str(error))
        return False
    detail = "dry run" if api.dry_run else f"{method} {current or 'none'} -> {version}"
    report.record("activate", None if api.dry_run else True, detail)
    return True


def configure(api: Api, report: Report, name: str, version: str, group: str) -> str | None:
    scope = f"ag_group-{group}"
    value = monitoring_value(version, group)
    try:
        existing = None
        if not api.dry_run:
            # GET .../monitoring-configurations - SDK listExtensionMonitoringConfigurations,
            # scope extensions:configurations:read. Query params are kebab-case (page-size).
            _, body = api.request(
                "GET", ext_path(name, "/monitoring-configurations"), query={"page-size": 500}, doc=DOC_SDK
            )
            for item in (body or {}).get("items") or []:
                if item.get("scope") == scope:
                    existing = item.get("objectId")
                    break
        if existing:
            # PUT .../monitoring-configurations/{id} with {"value": ...} - SDK
            # updateExtensionMonitoringConfiguration, scope extensions:configurations:write.
            api.request(
                "PUT",
                ext_path(name, f"/monitoring-configurations/{existing}"),
                json_body={"value": value},
                doc=DOC_SDK,
            )
            object_id = existing
            action = "updated"
        else:
            # POST .../monitoring-configurations with ONE {"scope", "value"} object (the platform
            # API takes an object; the classic /api/v2 one takes an array). Docs: DOC_MANAGE
            # ("Start monitoring with Dynatrace API"); SDK createExtensionMonitoringConfiguration.
            if api.dry_run:
                print(
                    "  (dry run: PUT .../monitoring-configurations/<id> instead if one exists for this scope)"
                )
            _, body = api.request(
                "POST",
                ext_path(name, "/monitoring-configurations"),
                json_body={"scope": scope, "value": value},
                doc=DOC_MANAGE,
            )
            object_id = (body or {}).get("objectId") if isinstance(body, dict) else None
            action = "created"
    except ApiError as error:
        report.record("monitoring config", False, str(error))
        return None
    if api.dry_run:
        report.record("monitoring config", None, f"dry run - scope {scope}")
        return "dry-run"
    report.record("monitoring config", True, f"{action} {object_id} on {scope}")
    return object_id


def configuration_status(api: Api, name: str, object_id: str) -> str:
    """Diagnostic only - the DQL checks are the verdict."""
    try:
        # GET .../monitoring-configurations/{id}/status - SDK getExtensionMonitoringConfigurationStatus.
        _, body = api.request(
            "GET", ext_path(name, f"/monitoring-configurations/{object_id}/status"), doc=DOC_SDK
        )
    except ApiError as error:
        return f"status unavailable ({error.status})"
    return str((body or {}).get("status", "?")) if isinstance(body, dict) else "?"


def cleanup(api: Api, report: Report, name: str, keep: str) -> None:
    try:
        # GET /platform/extensions/v2/extensions/{name} - SDK listExtensionVersions,
        # scope extensions:definitions:read. DOC_MANAGE: "list all available versions".
        _, body = api.request("GET", ext_path(name), query={"page-size": 100}, doc=DOC_MANAGE)
        versions = (
            [] if api.dry_run else [item.get("version", "") for item in (body or {}).get("items") or []]
        )
        stale = [v for v in versions if v.startswith(DEV_VERSION_PREFIX) and v != keep]
        for version in stale:
            # DELETE /platform/extensions/v2/extensions/{name}/{version} - DOC_MANAGE "Delete
            # extension version with API"; 202 on success. Only 0.99.* is ever touched.
            api.request("DELETE", ext_path(name, f"/{version}"), doc=DOC_MANAGE)
    except ApiError as error:
        report.record("cleanup", False, str(error))
        return
    if api.dry_run:
        print(f"  (dry run: then DELETE every {DEV_VERSION_PREFIX}* version except {keep})")
        report.record("cleanup", None, "dry run")
        return
    report.record("cleanup", True, f"deleted {len(stale)} old dev version(s): {', '.join(stale) or 'none'}")


# ---------------------------------------------------------------------------------------------
# DQL assertions
# ---------------------------------------------------------------------------------------------


def dql_metric_discovery(minutes: int) -> str:
    # `metrics` lists metric keys that had data in the window (dt-dql-essentials: "Metric
    # Discovery"). Filtering on the bare "cohesity" substring finds the keys whether or not
    # Grail puts a prefix in front of them.
    return (
        f"metrics from: now() - {minutes}m\n"
        '| filter contains(metric.key, "cohesity")\n'
        "| summarize n = count(), by: {metric.key}"
    )


def dql_collection_success(metric_key: str) -> str:
    key = metric_key if re.fullmatch(r"[A-Za-z0-9_.]+", metric_key) else f"`{metric_key}`"
    return (
        f"timeseries v = max({key}), from: now() - 5m, interval: 1m\n"
        "| fieldsAdd best = arrayMax(v)\n"
        "| summarize best = max(best)"
    )


def dql_nodes() -> str:
    return 'smartscapeNodes "EXT_COHESITY_*"\n| summarize n = count(), by: {type}'


def dql_edges() -> str:
    return (
        'smartscapeEdges "contains", "writes_to"\n'
        '| filter matchesValue(source_type, "EXT_COHESITY_*")\n'
        '    and matchesValue(target_type, "EXT_COHESITY_*")\n'
        "| summarize n = count(), by: {type, source_type, target_type}"
    )


def match_metric_keys(expected: list[str], discovered: set[str]) -> dict[str, str | None]:
    """Map each expected key to what Grail calls it, tolerating a prefix such as ``ext:``."""
    matches: dict[str, str | None] = {}
    for key in expected:
        exact = key if key in discovered else None
        prefixed = sorted(d for d in discovered if d.endswith(("." + key, ":" + key)))
        matches[key] = exact or (prefixed[0] if prefixed else None)
    return matches


@dataclass
class Dtctl:
    context: str
    dry_run: bool = False
    runner: Callable[..., subprocess.CompletedProcess] = subprocess.run

    def query(self, dql: str) -> list[dict[str, Any]]:
        if self.dry_run:
            print(f"  DRY-RUN dtctl query -f - --context {self.context} -o json <<DQL")
            print("          " + dql.replace("\n", "\n          "))
            print("          DQL")
            return []
        dtctl = shutil.which("dtctl") or "dtctl"
        completed = self.runner(
            [dtctl, "query", "-f", "-", "--context", self.context, "-o", "json"],
            input=dql,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            envelope = json.loads(completed.stdout)
        except ValueError:
            output = (completed.stderr or completed.stdout)[-800:]
            msg = f"dtctl query failed (exit {completed.returncode}): {output}"
            raise ApiError(completed.returncode, msg) from None
        if not envelope.get("ok", False):
            msg = f"dtctl query rejected: {json.dumps(envelope.get('error', envelope))[:800]}"
            raise ApiError(completed.returncode, msg)
        return ((envelope.get("result") or {}).get("records")) or []


@dataclass
class Check:
    name: str
    passed: bool = False
    detail: str = "not run"


def evaluate_nodes(records: list[dict[str, Any]]) -> tuple[bool, str]:
    counts = {r.get("type"): int(r.get("n") or 0) for r in records}
    missing = [t for t in NODE_TYPES if counts.get(t, 0) < 1]
    detail = ", ".join(f"{t}={counts.get(t, 0)}" for t in NODE_TYPES)
    return not missing, detail


def evaluate_edges(records: list[dict[str, Any]]) -> tuple[bool, str]:
    counts = {
        (r.get("type"), r.get("source_type"), r.get("target_type")): int(r.get("n") or 0) for r in records
    }
    missing = [edge for edge in EDGES if counts.get(edge, 0) < 1]
    short = {t: t.removeprefix("EXT_COHESITY_") for t in NODE_TYPES}
    detail = ", ".join(f"{short[s]} -{t}-> {short[d]}={counts.get((t, s, d), 0)}" for t, s, d in EDGES)
    return not missing, detail


def evaluate_collection(records: list[dict[str, Any]]) -> tuple[bool, str]:
    best = None
    for record in records:
        value = record.get("best")
        if value is not None:
            best = max(best or 0.0, float(value))
    return best == 1.0, f"max over last 5m = {best}"


def assert_via_dql(
    report: Report,
    dtctl: Dtctl,
    expected: list[str],
    timeout: float,
    started: float,
    poll: float = 30.0,
    status: Callable[[], str] | None = None,
) -> None:
    checks = {
        "metrics present": Check("metrics present"),
        "collection_success": Check("collection_success"),
        "smartscape nodes": Check("smartscape nodes"),
        "smartscape edges": Check("smartscape edges"),
    }
    deadline = time.monotonic() + timeout
    resolved_success_key = COLLECTION_SUCCESS
    while True:
        minutes = max(5, int((time.time() - started) // 60) + 2)
        try:
            if not checks["metrics present"].passed:
                discovered = {r.get("metric.key", "") for r in dtctl.query(dql_metric_discovery(minutes))}
                matches = match_metric_keys(expected, discovered)
                missing = [k for k, v in matches.items() if v is None]
                resolved_success_key = matches.get(COLLECTION_SUCCESS) or COLLECTION_SUCCESS
                renamed = {k: v for k, v in matches.items() if v and v != k}
                detail = f"{len(expected) - len(missing)}/{len(expected)} keys"
                if renamed:
                    detail += f" (Grail prefix seen, e.g. {next(iter(renamed.values()))})"
                if missing:
                    detail += "; missing: " + ", ".join(missing)
                checks["metrics present"] = Check("metrics present", not missing, detail)
            if not checks["collection_success"].passed:
                ok, detail = evaluate_collection(dtctl.query(dql_collection_success(resolved_success_key)))
                checks["collection_success"] = Check("collection_success", ok, detail)
            if not checks["smartscape nodes"].passed:
                ok, detail = evaluate_nodes(dtctl.query(dql_nodes()))
                checks["smartscape nodes"] = Check("smartscape nodes", ok, detail)
            if not checks["smartscape edges"].passed:
                ok, detail = evaluate_edges(dtctl.query(dql_edges()))
                checks["smartscape edges"] = Check("smartscape edges", ok, detail)
        except ApiError as error:
            report.record("dql", False, str(error))
            return
        if dtctl.dry_run or all(c.passed for c in checks.values()) or time.monotonic() >= deadline:
            break
        waiting = ", ".join(c.name for c in checks.values() if not c.passed)
        extra = f"; config status {status()}" if status else ""
        report.info(f"waiting on: {waiting}{extra} ({int(deadline - time.monotonic())}s left)")
        time.sleep(poll)
    for check in checks.values():
        report.record(
            check.name, None if dtctl.dry_run else check.passed, "dry run" if dtctl.dry_run else check.detail
        )


# ---------------------------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------------------------


def auth_scheme(token: str | None) -> str:
    # Classic access tokens (dt0c01) go in an Api-Token header; platform tokens are Bearer.
    return "Api-Token" if (token or "").startswith("dt0c01.") else "Bearer"


def preflight(
    args: argparse.Namespace, settings: Settings, report: Report, redact: Redactor, name: str
) -> None:
    apps, classic = args.env_url, args.api_url
    token = settings.get(args.token_var)
    redact.add(token)
    operator = settings.get(args.operator_token_var)
    redact.add(operator)

    # 1. Platform token: can it read extensions on THIS tenant? A token for another tenant 401s.
    if not token:
        report.record("platform token", False, settings.missing(args.token_var))
    else:
        api = Api(apps, token, "Bearer", redact)
        try:
            # GET /platform/extensions/v2/extensions - SDK listExtensions, extensions:definitions:read.
            api.request("GET", "/platform/extensions/v2/extensions", query={"page-size": 1}, doc=DOC_SDK)
            report.record("platform token", True, "extensions:definitions:read works on this tenant")
        except ApiError as error:
            report.record("platform token", False, str(error))

    # 2. Operator/PaaS token: can it see ActiveGate installers? Same scope as the download.
    if not operator:
        report.record(
            "installer token",
            False,
            settings.missing(args.operator_token_var),
        )
    else:
        api = Api(classic, operator, auth_scheme(operator), redact)
        try:
            # GET /api/v1/deployment/installer/gateway/versions/unix - DOC_AG_VERSIONS.
            # InstallerDownload (classic) or fleet-management:activegates:download (platform).
            _, body = api.request(
                "GET",
                "/api/v1/deployment/installer/gateway/versions/unix",
                query={"arch": "amd64"},
                doc=DOC_AG_VERSIONS,
            )
            count = len(body.get("availableVersions", [])) if isinstance(body, dict) else "?"
            report.record("installer token", True, f"InstallerDownload works ({count} AG versions listed)")
        except ApiError as error:
            report.record("installer token", False, str(error))

    # 3. Signing CA trusted by the tenant. validate runs the same checks as upload, signature
    # included, and persists nothing - the only way to test the CA without writing.
    built = newest_build(name)
    if not token:
        report.record("signing CA trusted", None, "needs the platform token")
    elif built is None:
        report.record(
            "signing CA trusted", None, "no built zip in e2e/.dist or dist/ to validate - build once first"
        )
    else:
        package, version = built
        api = Api(apps, token, "Bearer", redact)
        try:
            # POST /platform/extensions/v2/extensions:validate - SDK validateExtension ("same set
            # of operations as upload but doesn't persist"), extensions:definitions:write.
            api.request(
                "POST",
                "/platform/extensions/v2/extensions:validate",
                data=package.read_bytes(),
                content_type="application/octet-stream",
                doc=DOC_SDK,
            )
            report.record(
                "signing CA trusted", True, f"{package.name} ({version}) validates, signature included"
            )
        except ApiError as error:
            report.record("signing CA trusted", False, str(error))
        # Secondary evidence only: the classic credentials list shows a PUBLIC_CERTIFICATE with
        # EXTENSION_AUTHENTICATION scope exists, but never returns certificate contents, so it
        # cannot prove it is THIS CA. Needs credentialVault.read, which a platform token may lack.
        try:
            # GET /api/v2/credentials - DOC_CREDENTIALS.
            _, body = Api(classic, token, "Bearer", redact).request(
                "GET", "/api/v2/credentials", doc=DOC_CREDENTIALS
            )
            certs = [
                c
                for c in (body or {}).get("credentials", [])
                if c.get("type") == "PUBLIC_CERTIFICATE"
                and "EXTENSION_AUTHENTICATION" in (c.get("scopes") or [c.get("scope")])
            ]
            report.record(
                "vault has extension CA",
                bool(certs),
                f"{len(certs)} public certificate(s) scoped Extension validation",
            )
        except ApiError as error:
            report.record(
                "vault has extension CA", None, f"not checkable with this token (HTTP {error.status})"
            )

    # 4. dtctl context answers DQL at all.
    try:
        Dtctl(args.context).query("data record(ok = 1)")
        report.record("dtctl context", True, f"--context {args.context} answers DQL")
    except (ApiError, OSError) as error:
        report.record("dtctl context", False, f"--context {args.context}: {error}")

    # Write scopes other than validate cannot be proven without writing; say so.
    report.info(
        "not checked (would need a write): extensions:configurations:write, and the "
        "openpipeline:configurations:write that activation needs to install the bundled pipeline"
    )


# ---------------------------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--env-file", type=Path, help="dotenv file to read tokens/URLs from (env vars win)")
    parser.add_argument("--token-var", default=DEFAULT_TOKEN_VAR, help="platform token variable name")
    parser.add_argument(
        "--operator-token-var",
        default=DEFAULT_OPERATOR_TOKEN_VAR,
        help="installer token variable (preflight only)",
    )
    parser.add_argument("--env-url", help="apps host, e.g. https://<env>.apps.dynatrace.com")
    parser.add_argument("--env-url-var", default=DEFAULT_ENV_URL_VAR, help="variable to read --env-url from")
    parser.add_argument("--api-url", help="classic host for /api/*; derived from --env-url if omitted")
    parser.add_argument("--api-url-var", default=DEFAULT_API_URL_VAR, help="variable to read --api-url from")
    parser.add_argument(
        "--context", help=f"dtctl context for DQL (default: ${DEFAULT_CONTEXT_VAR}, else {DEFAULT_CONTEXT})"
    )
    parser.add_argument(
        "--ag-group", default=DEFAULT_AG_GROUP, help="ActiveGate group the config is scoped to"
    )
    parser.add_argument(
        "--skip-build", action="store_true", help="upload the newest zip in e2e/.dist or dist/"
    )
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for DQL assertions")
    parser.add_argument("--poll", type=float, default=30.0, help="seconds between DQL polls")
    parser.add_argument("--cleanup", action="store_true", help=f"delete older {DEV_VERSION_PREFIX}* versions")
    parser.add_argument(
        "--preflight", action="store_true", help="non-destructive credential checks, then exit"
    )
    parser.add_argument("--dry-run", action="store_true", help="print every request and DQL; perform nothing")
    return parser.parse_args(argv)


def resolve_urls(args: argparse.Namespace, settings: Settings) -> None:
    env_url = args.env_url or settings.get(args.env_url_var)
    if not env_url:
        msg = f"no environment URL: pass --env-url or set {args.env_url_var}"
        raise ConfigError(msg)
    args.env_url = normalise_url(env_url)
    api_url = args.api_url or settings.get(args.api_url_var)
    args.api_url = api_base_url(api_url) if api_url else classic_url(args.env_url)
    args.context = args.context or settings.get(DEFAULT_CONTEXT_VAR) or DEFAULT_CONTEXT


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    redact = Redactor()
    report = Report(redact)
    settings = Settings(args.env_file)
    try:
        resolve_urls(args, settings)
        yaml_text = EXTENSION_YAML.read_text(encoding="utf-8")
        name = extension_name(yaml_text)
        expected = expected_metric_keys(yaml_text)
        print(f"extension {name}  tenant {args.env_url}  api {args.api_url}  dtctl --context {args.context}")
        print(f"AG group  {args.ag_group}  expected metric keys {len(expected)}")

        if args.preflight:
            preflight(args, settings, report, redact, name)
            report.summary()
            return 1 if report.failed else 0

        token = settings.get(args.token_var) if args.dry_run else settings.require(args.token_var)
        redact.add(token)
        api = Api(args.env_url, token, "Bearer", redact, dry_run=args.dry_run)
        started = time.time()

        if args.skip_build:
            built = newest_build(name)
            if built is None:
                report.record("build", False, "--skip-build but no zip in e2e/.dist or dist/")
                return finish(report)
            package, version = built
            report.record("build", None, f"skipped - using {package.relative_to(REPO_ROOT)}")
        else:
            version = dev_version(started)
            package = build(report, name, version, args.dry_run)
            if package is None:
                return finish(report)

        if not upload(api, report, package):
            return finish(report)
        if not activate(api, report, name, version):
            return finish(report)
        object_id = configure(api, report, name, version, args.ag_group)
        if object_id is None:
            return finish(report)
        if args.cleanup:
            cleanup(api, report, name, keep=version)

        status = None if args.dry_run else (lambda: configuration_status(api, name, object_id))
        assert_via_dql(
            report,
            Dtctl(args.context, dry_run=args.dry_run),
            expected,
            args.timeout,
            started,
            args.poll,
            status,
        )
    except ConfigError as error:
        print(f"[FAIL] config                   {redact(str(error))}")
        return 2
    except KeyboardInterrupt:
        print("\ninterrupted")
        return 130
    return finish(report)


def finish(report: Report) -> int:
    report.summary()
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
