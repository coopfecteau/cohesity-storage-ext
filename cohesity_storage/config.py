"""Cluster configuration: parsing and validation.

Monitoring configurations reach the extension as raw dicts shaped by
``extension/activationSchema.json``. The tenant validates against that schema, but the
extension also runs from ``dt-sdk run`` with a hand-written ``activation.json``, and a
schema can drift from the code that reads it. Everything is normalised and checked once
here so the client layer can trust its inputs, and so ``fastcheck`` can reject a broken
configuration before a single request leaves the ActiveGate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# The scaffold required the API key to be a UUID, because that is the shape Cohesity shows when
# a key is created. Ticket 07 relaxed it to "non-empty, no whitespace", which is what the
# scaffold's own note recommended: the UUID shape is a guess about key format across 6.8-7.4,
# and rejecting a valid key outright is a worse failure than the mis-paste it was catching -
# the mis-paste shows up as a 401 with an actionable message, a rejected key shows up as an
# extension that will not start and a format claim nobody can check.
#
# Whitespace is still refused, because it is never part of a credential and is the one paste
# error a check can catch without knowing the format: a trailing newline or a wrapped line
# produces a header the cluster rejects for a reason the operator cannot see.
API_KEY_FORBIDDEN = re.compile(r"\s")

# The Dynatrace credential vault is resolved by the EEC, not by this extension. By the time an
# endpoint dict reaches this module the vault entry's values have already been substituted into
# the activation config, so there is no credential-vault API to call here and no SDK helper for
# one - `dynatrace_extension` has none. Everything below is about reading what the EEC injected.
#
# What the EEC does NOT do is use the same field names in both modes, and that is completely
# invisible from activationSchema.json. The vendor NetApp ONTAP 3.0.8 extension - the only
# verified example of this pattern - reads its credential like this:
#
#     if endpoint.get("useCredentialVault", False):
#         user = endpoint["username"]     # vault mode
#     else:
#         user = endpoint["user"]         # inline mode - DIFFERENT KEY NAME
#     password = endpoint["password"]     # same key in both modes
#
# So one half of the credential arrives under a different name depending on the mode and the
# other half does not. Cohesity's credential is a single API key rather than a pair, so there is
# no username half to read - but the same trap applies to the secret itself, and we cannot read
# the schema to find out which name it lands under.
#
# NetApp only proves `referencedType: USERNAME_PASSWORD`. This extension asks for TOKEN, which is
# what an API key semantically is, but TOKEN is *unverified*: we have not been able to upload the
# extension and observe what actually arrives. So rather than hard-coding one field name, probe
# the plausible ones in order and take the first non-empty value:
#
#   token    - the name `referencedType: TOKEN` implies.
#   password - what USERNAME_PASSWORD verifiably populates. This is also the documented fallback:
#              if TOKEN is rejected at upload time, switch the schema to USERNAME_PASSWORD, put
#              the API key in the password half and ignore the username half. No code change is
#              needed for that switch, because this probe already covers it.
#   apiKey   - the EEC may simply write the resolved value back into the property that declared
#              the reference, which here is the inline secret's own name.
#
# Probing rather than picking is deliberate: guessing wrong hard-codes a silent empty credential,
# and an empty credential presents as a 401 that sends someone to rotate a perfectly good key.
VAULT_SECRET_FIELDS = ("token", "password", "apiKey")

# How many protection groups the per-group runs fan-out may ask in one poll.
#
# It lives here rather than in the client because it is the one dial an operator has over what
# that fan-out costs, and it has to be turnable without a rebuild: the customer cluster answers
# HTTP 500 on two endpoints and times out on a third, so "how many requests may one poll spend
# on a cluster that is already struggling" is a site decision, not a constant.
#
# 20 rather than the 10 v0.1.6 shipped. The fan-out rotates (see the client), so the cap is not
# a coverage limit any more - it is a per-poll request budget, and the number of polls it takes
# to see every group is the group count divided by it. On 42 groups, 20 is full coverage every
# two to three polls; 10 was three to five, which is 15-25 minutes of blindness per group.
DEFAULT_RUNS_FANOUT_GROUPS = 20

# The host link (ticket 16), and the two numbers that keep it from being a cardinality bomb.
#
# The bridge metric carries ONE SERIES PER PROTECTED OBJECT. That is precisely the cardinality
# ticket 04 ruled out of v1, and it is not a theoretical worry: one SQL protection group on the
# customer's own cluster holds 856 objects, and a production Cohesity protects tens of
# thousands. The mapping it carries is also *configuration* rather than telemetry - which VM a
# job backs up changes weekly, not every five minutes - so paying for it per poll per object is
# the wrong shape of cost. See the README and ticket 16 for the successor design.
#
# So it is OFF unless somebody asks for it, and when it is on it is capped twice: by how many
# groups may be asked per poll (which reuses the runs fan-out budget, so the one dial an
# operator already has over "this cluster is struggling" governs both), and by how many objects
# may be emitted per poll regardless of how few groups that took.
DEFAULT_COLLECT_HOST_LINK = False

# 200 objects is a few hundred series - the same order as the rest of this extension's ingest,
# which is what makes it a safe default to turn on. Raising it is a deliberate decision about
# cardinality, and the extension says so in a diagnostic every time the cap truncates rather
# than quietly publishing half a mapping.
DEFAULT_HOST_LINK_OBJECTS = 200

# Alerts are OFF until asked for. Not because they are expensive - they are cheap - but
# because they are the first thing this extension sends that is prose written by the
# cluster rather than a number it measured, and prose is the only thing that could carry a
# name out of the protected estate. Opting in is the point at which somebody has decided
# that is acceptable. DEFAULT_ALERT_DESCRIPTIONS is the narrower switch for the one field
# that can carry such a name.
DEFAULT_COLLECT_ALERTS = False
DEFAULT_ALERT_DESCRIPTIONS = True
DEFAULT_ALERT_LOOKBACK_HOURS = 24
# 250, not 100: the first real poll against the customer cluster returned exactly 100 in a
# 24h window, i.e. it hit the cap on the first try, and a cap that binds silently is how a
# cluster in a bad state gets quieter in Dynatrace rather than louder.
DEFAULT_MAX_ALERTS = 250

DEFAULTS = {
    "port": 443,
    "useCredentialVault": False,
    "verifyTls": True,
    "collectStorageDomains": True,
    "collectViews": True,
    "collectProtection": True,
    "collectHostLink": DEFAULT_COLLECT_HOST_LINK,
    "intervalMinutes": 5,
    "requestTimeoutSeconds": 30,
    "maxRunFanoutGroups": DEFAULT_RUNS_FANOUT_GROUPS,
    "maxHostLinkObjects": DEFAULT_HOST_LINK_OBJECTS,
    "collectAlerts": DEFAULT_COLLECT_ALERTS,
    "alertDescriptions": DEFAULT_ALERT_DESCRIPTIONS,
    "alertLookbackHours": DEFAULT_ALERT_LOOKBACK_HOURS,
    "maxAlerts": DEFAULT_MAX_ALERTS,
    "fixtureDir": "",
}


class ConfigError(ValueError):
    """A monitoring configuration endpoint that cannot be polled as written."""


@dataclass(frozen=True)
class ClusterConfig:
    """One Cohesity cluster: where it is, how to authenticate, and what to collect from it."""

    name: str
    host: str
    port: int = 443
    api_key: str = ""
    # Which of the two auth modes the monitoring configuration chose, and - when it chose the
    # vault - which vault entry and which injected field the key was actually read out of. The
    # last one is kept because "the vault resolved to nothing" and "the cluster refused the key"
    # need different people to fix them, and only the extension can tell them apart.
    use_credential_vault: bool = False
    credential_vault_id: str = ""
    api_key_field: str = "apiKey"
    verify_tls: bool = True
    ca_cert_path: str = ""
    # One toggle per feature set in extension.yaml, same names, same meaning. Ticket 06
    # collapsed the two independent switches this extension used to have: these are now the
    # single source of truth for what is collected, and the feature sets mirror them rather
    # than gate separately. `collectNodes` is gone entirely - Node is not a v1 entity and the
    # toggle switched nothing, which is worse than no toggle at all.
    collect_storage_domains: bool = True
    collect_views: bool = True
    collect_protection: bool = True
    # Opt-in, and the only collection that is. See DEFAULT_COLLECT_HOST_LINK: it is the
    # one thing this extension emits whose series count scales with the size of the
    # protected estate rather than with the number of Cohesity objects being monitored.
    collect_host_link: bool = DEFAULT_COLLECT_HOST_LINK
    interval_minutes: int = 5
    request_timeout_seconds: int = 30
    # The per-poll request budget for the per-group runs fan-out - see
    # :data:`DEFAULT_RUNS_FANOUT_GROUPS`. Not a coverage limit: the fan-out rotates, so a
    # smaller number means every group is still seen, just less often.
    max_run_fanout_groups: int = DEFAULT_RUNS_FANOUT_GROUPS
    # The hard ceiling on bridge-metric series per poll. Truncation is reported as a WARN
    # diagnostic, never silent: half a lookup table is indistinguishable from half an
    # estate going unprotected, and nothing else in the product would say which it was.
    max_host_link_objects: int = DEFAULT_HOST_LINK_OBJECTS
    # Cluster alerts as log events. See DEFAULT_COLLECT_ALERTS for why this is opt-in.
    collect_alerts: bool = DEFAULT_COLLECT_ALERTS
    # The redaction switch. Off means the alert still arrives - id, name, severity,
    # category, state, timestamps - without the free-form sentence, which is the only
    # field that can name a datastore, a share or a VM.
    alert_descriptions: bool = DEFAULT_ALERT_DESCRIPTIONS
    alert_lookback_hours: int = DEFAULT_ALERT_LOOKBACK_HOURS
    max_alerts: int = DEFAULT_MAX_ALERTS
    # Replay mode. When set, every response is read from recorded JSON in this directory and no
    # request leaves the ActiveGate. It is one setting rather than a code path so that "we got
    # credentials" is a configuration change, not a rewrite - and so that the code above the
    # client cannot tell the two apart.
    fixture_dir: str = ""

    @property
    def base_url(self) -> str:
        return f"https://{self.host}:{self.port}"

    @property
    def replay(self) -> bool:
        return bool(self.fixture_dir)

    @property
    def key(self) -> str:
        """Stable identity for scheduling state, so repointing a config starts a fresh clock."""
        return f"{self.name}|{self.host}:{self.port}"

    @property
    def auth_source(self) -> str:
        """Where the API key came from, in words, for errors that must not misdirect.

        A rejected credential and an unresolved credential produce the same 401 on the wire and
        have completely different fixes - one is a Cohesity admin rotating a key, the other is a
        Dynatrace admin fixing a vault entry. Naming the source in every message is what stops a
        support call from starting in the wrong product.
        """
        if self.use_credential_vault:
            entry = self.credential_vault_id or "(none selected)"
            return f"credential vault entry {entry} (injected as '{self.api_key_field}')"
        return "the inline 'apiKey' property"

    @classmethod
    def from_dict(cls, raw: dict, index: int = 0) -> ClusterConfig:
        """Build a validated config from one raw endpoint dict.

        Raises:
            ConfigError: if a required value is missing or out of range.
        """
        if not isinstance(raw, dict):
            msg = f"cluster #{index + 1} is not an object"
            raise ConfigError(msg)

        label = _text(raw, "name") or f"cluster-{index + 1}"

        host = _text(raw, "host")
        if not host:
            msg = f"{label}: 'host' is required - the hostname, VIP or IP of the Cohesity cluster"
            raise ConfigError(msg)

        use_vault = _bool(raw, "useCredentialVault")
        api_key, api_key_field = resolve_api_key(raw, use_credential_vault=use_vault)

        config = cls(
            name=label,
            host=host,
            port=_int(raw, "port", label, minimum=1, maximum=65535),
            api_key=api_key,
            use_credential_vault=use_vault,
            credential_vault_id=_text(raw, "credentialVaultId"),
            api_key_field=api_key_field,
            verify_tls=_bool(raw, "verifyTls"),
            ca_cert_path=_text(raw, "caCertPath"),
            collect_storage_domains=_bool(raw, "collectStorageDomains"),
            collect_views=_bool(raw, "collectViews"),
            collect_protection=_bool(raw, "collectProtection"),
            collect_host_link=_bool(raw, "collectHostLink"),
            interval_minutes=_int(raw, "intervalMinutes", label, minimum=1, maximum=1440),
            request_timeout_seconds=_int(raw, "requestTimeoutSeconds", label, minimum=1, maximum=300),
            max_run_fanout_groups=_int(raw, "maxRunFanoutGroups", label, minimum=1, maximum=500),
            max_host_link_objects=_int(
                raw, "maxHostLinkObjects", label, minimum=1, maximum=10000
            ),
            collect_alerts=_bool(raw, "collectAlerts"),
            alert_descriptions=_bool(raw, "alertDescriptions"),
            alert_lookback_hours=_int(raw, "alertLookbackHours", label, minimum=1, maximum=168),
            max_alerts=_int(raw, "maxAlerts", label, minimum=1, maximum=1000),
            fixture_dir=_text(raw, "fixtureDir"),
        )
        config._validate_auth()
        config._validate_tls()
        return config

    def _validate_auth(self) -> None:
        """Refuse anything but exactly one usable auth mode, before a request is ever built."""
        self._validate_auth_mode()

        if not self.api_key:
            if self.replay:
                # Replay never sends a header anywhere, so demanding a credential for it would
                # only teach people to paste a fake one - and a fake credential in a monitoring
                # configuration outlives the reason it was put there.
                return
            raise ConfigError(self._missing_credential_message())
        if API_KEY_FORBIDDEN.search(self.api_key):
            # Never echo the value: it is a credential, and the length alone is enough to tell
            # a truncated paste from a username that was put in the wrong field.
            msg = (
                f"{self.name}: the API key from {self.auth_source} contains whitespace, which no "
                f"Cohesity API key does. Surrounding whitespace is trimmed, so this is whitespace "
                f"*inside* the value - a paste that wrapped across lines "
                f"(got {len(self.api_key)} characters)"
            )
            raise ConfigError(msg)

    def _validate_auth_mode(self) -> None:
        """Exactly one of the two modes, and the field that mode needs actually filled in.

        The activation schema's preconditions already hide the field belonging to the mode that
        is switched off, but preconditions only shape the UI. A hand-written ``activation.json``
        for ``dt-sdk run``, or a configuration written through the settings API, can set both or
        neither - and "both" is the dangerous one, because it looks configured while silently
        sending whichever the code happens to prefer.
        """
        if self.use_credential_vault:
            if not self.credential_vault_id:
                msg = (
                    f"{self.name}: 'Use credential vault' is on but no vault credential is "
                    f"selected. Pick one under 'Select vault credentials', or turn the switch "
                    f"off and paste the key inline - exactly one of the two has to be set."
                )
                raise ConfigError(msg)
            # No check for an inline apiKey alongside vault mode, deliberately: 'apiKey' is one
            # of the fields the EEC may inject the *resolved vault value* into (see
            # VAULT_SECRET_FIELDS), so a populated apiKey in vault mode is more likely to be the
            # vault working than an operator setting both. Refusing it would break the very
            # configuration this feature exists to support.
            return
        if self.credential_vault_id:
            msg = (
                f"{self.name}: a vault credential is selected but 'Use credential vault' is off, "
                f"so the inline key is what would be sent and the vault entry would be ignored. "
                f"Turn the switch on to use the vault entry, or clear the selection."
            )
            raise ConfigError(msg)

    def _missing_credential_message(self) -> str:
        """Why no key arrived - and the two answers point at two different products."""
        if self.use_credential_vault:
            # The EEC resolves and injects the vault entry before the extension runs, so an empty
            # value here can only be a vault-side problem. Saying so matters: the alternative
            # failure (an empty header) shows up on the cluster as a 401, and a 401 is what sends
            # somebody off to rotate a Cohesity API key that was never wrong.
            return (
                f"{self.name}: credential vault entry {self.credential_vault_id} reached the "
                f"extension empty - none of {', '.join(VAULT_SECRET_FIELDS)} carried a value. "
                f"The ActiveGate resolves the vault entry and injects it into the configuration "
                f"before this extension sees it, so this is a vault problem, NOT a bad API key: "
                f"do not rotate the key in Cohesity. Check that the credential still exists under "
                f"Settings > Credential vault, that it holds a token, and that its scope lets "
                f"this ActiveGate group read it."
            )
        return (
            f"{self.name}: 'apiKey' is required. Create one in the Cohesity UI under "
            f"Settings > Access Management > API Keys; username and password are not used, "
            f"because that flow returns a token that expires after 24 hours. Alternatively turn "
            f"'Use credential vault' on and select a stored credential instead."
        )

    def _validate_tls(self) -> None:
        # A CA path with verification off is not a harmless leftover: it reads as "we trust
        # this CA" while the connection actually trusts anything that answers.
        if self.ca_cert_path and not self.verify_tls:
            msg = (
                f"{self.name}: a CA certificate path is set but TLS verification is off. "
                f"Turn verification on to use the CA, or clear the path."
            )
            raise ConfigError(msg)

    @property
    def enabled_collections(self) -> tuple[str, ...]:
        """The optional collections switched on, for logging what an interval will actually do.

        Cluster-level metrics are not listed because they are not optional: they carry the
        cluster id and name that every other entity's dimensions are namespaced against.
        """
        names = []
        if self.collect_storage_domains:
            names.append("storage_domains")
        if self.collect_views:
            names.append("views")
        if self.collect_protection:
            names.append("protection")
        if self.collect_host_link:
            names.append("host_link")
        if self.collect_alerts:
            names.append("alerts")
        return tuple(names)


def load_clusters(activation_config) -> tuple[list[ClusterConfig], list[str]]:
    """Parse every cluster, keeping the good ones and the reasons the others were dropped.

    A single mistyped cluster must not stop the rest of the configuration from running, so
    failures are returned rather than raised.
    """
    raw_endpoints = []
    if activation_config is not None:
        try:
            raw_endpoints = activation_config.get("endpoints") or []
        except (AttributeError, TypeError):
            raw_endpoints = []

    configs: list[ClusterConfig] = []
    errors: list[str] = []
    for index, raw in enumerate(raw_endpoints):
        try:
            configs.append(ClusterConfig.from_dict(raw, index))
        except ConfigError as exception:
            errors.append(str(exception))
    return configs, errors


def resolve_api_key(raw: dict, *, use_credential_vault: bool) -> tuple[str, str]:
    """The API key for one endpoint, and the field name it was read out of.

    Two modes, and they do not read the same field - see :data:`VAULT_SECRET_FIELDS` for why
    that is a trap rather than a detail. Inline mode reads ``apiKey`` and nothing else. Vault
    mode probes, because the field the EEC injects a ``referencedType: TOKEN`` credential into
    is not documented and this extension has not yet been uploaded to observe it.

    Returns ``("", <last probed field>)`` when nothing resolved; the caller turns that into a
    message that points at the vault rather than at the cluster.
    """
    if not use_credential_vault:
        return _text(raw, "apiKey"), "apiKey"
    for field in VAULT_SECRET_FIELDS:
        value = _text(raw, field)
        if value:
            return value, field
    return "", VAULT_SECRET_FIELDS[0]


def _text(raw: dict, prop: str, *, strip: bool = True) -> str:
    value = raw.get(prop, DEFAULTS.get(prop, ""))
    if value is None:
        return ""
    value = str(value)
    return value.strip() if strip else value


def _bool(raw: dict, prop: str) -> bool:
    value = raw.get(prop, DEFAULTS.get(prop, False))
    if value is None:
        return bool(DEFAULTS.get(prop, False))
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


def _int(raw: dict, prop: str, label: str, *, minimum: int, maximum: int) -> int:
    value = raw.get(prop, DEFAULTS.get(prop))
    if value is None:
        # An explicit null, which is what a *nullable* schema property carries on a monitoring
        # configuration written before that property existed. A new REQUIRED property would
        # break in-place config upgrades outright, so new settings are nullable - and that
        # means the default has to live here, in code, rather than only in the schema.
        # `_bool` and `_text` already did this; `_int` raised "must be a whole number, got
        # None" and took the whole cluster's configuration down with it.
        value = DEFAULTS.get(prop)
    try:
        number = int(value)
    except (TypeError, ValueError):
        msg = f"{label}: '{prop}' must be a whole number, got {value!r}"
        raise ConfigError(msg) from None
    if not minimum <= number <= maximum:
        msg = f"{label}: '{prop}' must be between {minimum} and {maximum}, got {number}"
        raise ConfigError(msg)
    return number
