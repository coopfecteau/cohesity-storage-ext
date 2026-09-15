"""Configuration parsing: defaults, validation, and partial-failure behaviour."""

from __future__ import annotations

import pytest

from cohesity_storage.config import (
    VAULT_SECRET_FIELDS,
    ClusterConfig,
    ConfigError,
    load_clusters,
    resolve_api_key,
)

VALID_API_KEY = "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"


def raw(**overrides) -> dict:
    endpoint = {
        "name": "cohesity-prod",
        "host": "10.20.30.40",
        "apiKey": VALID_API_KEY,
    }
    endpoint.update(overrides)
    return endpoint


class FakeActivationConfig:
    """Stands in for the SDK ActivationConfig, which proxies get() to the active context."""

    def __init__(self, endpoints):
        self._endpoints = endpoints

    def get(self, key, default=None):
        return {"endpoints": self._endpoints}.get(key, default)


class TestDefaults:
    def test_defaults_are_applied(self):
        config = ClusterConfig.from_dict(raw())

        assert config.port == 443
        assert config.verify_tls is True
        assert config.interval_minutes == 5
        assert config.request_timeout_seconds == 30
        assert config.base_url == "https://10.20.30.40:443"

    def test_every_optional_collection_is_on_by_default(self):
        config = ClusterConfig.from_dict(raw())

        assert config.enabled_collections == ("storage_domains", "nodes", "protection")

    def test_explicit_values_win(self):
        config = ClusterConfig.from_dict(
            raw(port=8443, intervalMinutes=60, requestTimeoutSeconds=120, collectProtection=False)
        )

        assert config.port == 8443
        assert config.interval_minutes == 60
        assert config.request_timeout_seconds == 120
        assert config.enabled_collections == ("storage_domains", "nodes")

    def test_booleans_survive_the_string_forms_a_hand_written_activation_json_produces(self):
        config = ClusterConfig.from_dict(raw(verifyTls="false", collectNodes="true"))

        assert config.verify_tls is False
        assert config.collect_nodes is True


class TestValidation:
    def test_host_is_required(self):
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(raw(host=""))

        assert "'host' is required" in str(raised.value)

    def test_a_missing_api_key_explains_that_username_and_password_are_not_used(self):
        # The 24-hour session token from the username/password flow is the trap this points at.
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(raw(apiKey=""))

        assert "'apiKey' is required" in str(raised.value)
        assert "24 hours" in str(raised.value)

    def test_a_key_that_is_not_a_uuid_is_accepted(self):
        # The scaffold demanded a UUID. That was a guess about key format across 6.8-7.4, and
        # rejecting a valid key outright is a worse failure than the mis-paste it prevented: a
        # bad key shows up as a 401 with an actionable message, a rejected one shows up as an
        # extension that will not start at all.
        config = ClusterConfig.from_dict(raw(apiKey="AbCd1234-not-a-uuid-but-a-real-key"))

        assert config.api_key == "AbCd1234-not-a-uuid-but-a-real-key"

    def test_surrounding_whitespace_is_trimmed_rather_than_rejected(self):
        config = ClusterConfig.from_dict(raw(apiKey="  0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0\n"))

        assert config.api_key == "0f1e2d3c-4b5a-6978-8796-a5b4c3d2e1f0"

    def test_whitespace_inside_the_key_is_still_rejected(self):
        # The one paste error catchable without knowing the format: a key that wrapped.
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(raw(apiKey="0f1e2d3c-4b5a\n6978-8796-a5b4c3d2e1f0"))

        assert "whitespace" in str(raised.value)

    def test_the_rejection_never_echoes_the_key(self):
        # Fastcheck messages surface in the monitoring configuration UI and in extension logs.
        secret = "still a credential"
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(raw(apiKey=secret))

        assert secret not in str(raised.value)
        assert str(len(secret)) in str(raised.value)

    def test_a_ca_path_with_verification_off_is_rejected(self):
        # Otherwise the configuration reads as "we trust this CA" while trusting anything.
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(raw(verifyTls=False, caCertPath="/opt/dynatrace/cohesity-ca.pem"))

        assert "TLS verification is off" in str(raised.value)

    def test_verification_off_without_a_ca_path_is_allowed(self):
        config = ClusterConfig.from_dict(raw(verifyTls=False))

        assert config.verify_tls is False
        assert config.ca_cert_path == ""

    def test_out_of_range_port_is_rejected(self):
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(raw(port=70000))

        assert "between 1 and 65535" in str(raised.value)

    def test_non_numeric_interval_is_rejected(self):
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(raw(intervalMinutes="hourly"))

        assert "whole number" in str(raised.value)


class TestLoadClusters:
    def test_one_broken_cluster_does_not_drop_the_others(self):
        configs, errors = load_clusters(
            FakeActivationConfig([raw(name="good"), raw(name="broken", host=""), raw(name="also-good")])
        )

        assert [config.name for config in configs] == ["good", "also-good"]
        assert len(errors) == 1
        assert "broken" in errors[0]

    def test_missing_endpoints_is_not_an_error(self):
        configs, errors = load_clusters(FakeActivationConfig([]))

        assert configs == []
        assert errors == []

    def test_cluster_key_distinguishes_same_name_on_different_hosts(self):
        first = ClusterConfig.from_dict(raw(name="cohesity", host="10.20.30.40"))
        second = ClusterConfig.from_dict(raw(name="cohesity", host="10.20.30.41"))

        assert first.key != second.key


class TestReplayMode:
    """Replay is a configuration switch, so its rules belong with the other configuration rules."""

    def test_a_fixture_directory_turns_replay_on(self):
        config = ClusterConfig.from_dict(raw(fixtureDir="/opt/dynatrace/cohesity-fixtures"))

        assert config.replay is True
        assert config.fixture_dir == "/opt/dynatrace/cohesity-fixtures"

    def test_the_default_is_a_real_cluster(self):
        assert ClusterConfig.from_dict(raw()).replay is False

    def test_replay_does_not_demand_a_credential_it_will_never_send(self):
        # Requiring one here would only teach people to paste a fake credential into a
        # monitoring configuration, where it outlives the reason it was put there.
        config = ClusterConfig.from_dict(raw(apiKey="", fixtureDir="/tmp/fixtures"))

        assert config.api_key == ""

    def test_a_real_cluster_still_demands_a_credential(self):
        with pytest.raises(ConfigError):
            ClusterConfig.from_dict(raw(apiKey="", fixtureDir=""))


VAULT_ID = "CREDENTIALS_VAULT-0123456789ABCDEF"


def vault_raw(**overrides) -> dict:
    """An endpoint as it arrives *after* the EEC has resolved and injected a vault entry.

    Note there is no 'apiKey' here. The extension never calls the credential vault - the
    ActiveGate substitutes the values into the activation config first - so the only thing a test
    can meaningfully stand in for is the shape of what comes out the other side.
    """
    endpoint = {
        "name": "cohesity-prod",
        "host": "10.20.30.40",
        "useCredentialVault": True,
        "credentialVaultId": VAULT_ID,
        "token": VALID_API_KEY,
    }
    endpoint.update(overrides)
    return endpoint


class TestCredentialVault:
    """The two auth modes, and the trap that they do not read the same field."""

    def test_vault_mode_reads_the_injected_secret(self):
        config = ClusterConfig.from_dict(vault_raw())

        assert config.use_credential_vault is True
        assert config.credential_vault_id == VAULT_ID
        assert config.api_key == VALID_API_KEY

    def test_inline_mode_is_unchanged_and_still_the_default(self):
        config = ClusterConfig.from_dict(raw())

        assert config.use_credential_vault is False
        assert config.credential_vault_id == ""
        assert config.api_key == VALID_API_KEY
        assert config.api_key_field == "apiKey"

    def test_vault_mode_without_a_selected_credential_is_rejected(self):
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(vault_raw(credentialVaultId=""))

        assert "no vault credential is selected" in str(raised.value)

    def test_a_selected_credential_with_the_switch_off_is_rejected_as_ambiguous(self):
        # "Both set" in the direction that actually misleads: the page shows a chosen credential
        # while the inline key is what would really be sent.
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(raw(useCredentialVault=False, credentialVaultId=VAULT_ID))

        assert "'Use credential vault' is off" in str(raised.value)

    def test_neither_mode_configured_is_rejected(self):
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(raw(apiKey="", credentialVaultId=""))

        assert "'apiKey' is required" in str(raised.value)
        assert "credential vault" in str(raised.value)

    def test_vault_mode_tolerates_a_populated_apiKey_because_that_may_be_the_injection(self):  # noqa: N802
        # 'apiKey' is one of the fields the EEC may inject the resolved value into, so refusing
        # this combination would break the feature rather than catch a mistake.
        config = ClusterConfig.from_dict(
            vault_raw(token=None, apiKey="injected-into-the-declaring-property")
        )

        assert config.api_key == "injected-into-the-declaring-property"
        assert config.api_key_field == "apiKey"

    def test_an_unresolved_vault_entry_does_not_read_as_a_bad_key(self):
        # The whole point of the distinction: an empty vault entry and a wrong API key both end
        # as a 401 on the wire, and they are fixed in different products by different people.
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(vault_raw(token=""))

        message = str(raised.value)
        assert VAULT_ID in message
        assert "vault problem, NOT a bad API key" in message
        assert "do not rotate the key in Cohesity" in message
        assert "Credential vault" in message

    def test_whitespace_in_a_vault_secret_names_the_vault_not_the_inline_property(self):
        with pytest.raises(ConfigError) as raised:
            ClusterConfig.from_dict(vault_raw(token="0f1e2d3c\n4b5a"))

        message = str(raised.value)
        assert "whitespace" in message
        assert VAULT_ID in message

    def test_replay_still_does_not_demand_a_credential_in_vault_mode(self):
        config = ClusterConfig.from_dict(vault_raw(token="", fixtureDir="/tmp/fixtures"))

        assert config.api_key == ""
        assert config.replay is True


class TestVaultFieldProbing:
    """Which field the EEC injects into is undocumented for referencedType TOKEN.

    NetApp ONTAP 3.0.8 only proves USERNAME_PASSWORD, where the secret half arrives as
    'password'. Until this extension has been uploaded and observed, every plausible name is
    probed rather than one being picked, so a wrong guess cannot silently produce an empty key.
    """

    def test_the_probe_order_is_token_then_password_then_apiKey(self):  # noqa: N802
        assert VAULT_SECRET_FIELDS == ("token", "password", "apiKey")

    @pytest.mark.parametrize("field", VAULT_SECRET_FIELDS)
    def test_any_single_injected_field_resolves(self, field):
        value, found_in = resolve_api_key({field: VALID_API_KEY}, use_credential_vault=True)

        assert value == VALID_API_KEY
        assert found_in == field

    def test_the_first_non_empty_field_wins(self):
        value, found_in = resolve_api_key(
            {"token": "", "password": "from-password", "apiKey": "from-apikey"},
            use_credential_vault=True,
        )

        assert value == "from-password"
        assert found_in == "password"

    def test_the_username_password_fallback_needs_no_code_change(self):
        # The documented fallback if TOKEN is rejected at upload time: switch the schema to
        # referencedType USERNAME_PASSWORD, put the API key in the password half, ignore the
        # username half. The probe already covers it - that is the point of probing.
        config = ClusterConfig.from_dict(
            vault_raw(token=None, username="svc-dynatrace", password=VALID_API_KEY)
        )

        assert config.api_key == VALID_API_KEY
        assert config.api_key_field == "password"

    def test_inline_mode_never_probes_the_vault_fields(self):
        # A 'password' left over from another integration must not become the Cohesity API key.
        value, found_in = resolve_api_key(
            {"password": "someone-elses-secret", "apiKey": "the-real-key"},
            use_credential_vault=False,
        )

        assert value == "the-real-key"
        assert found_in == "apiKey"

    def test_nothing_resolved_reports_empty_rather_than_guessing(self):
        value, _ = resolve_api_key({"credentialVaultId": VAULT_ID}, use_credential_vault=True)

        assert value == ""

    def test_the_auth_source_names_the_field_the_key_actually_came_from(self):
        # This log line is the only place the injected field name is observable - it is not in
        # the schema and not in the UI - so it is what a first upload will be read against.
        config = ClusterConfig.from_dict(vault_raw(token=None, password=VALID_API_KEY))

        assert VAULT_ID in config.auth_source
        assert "password" in config.auth_source
        assert "inline" in ClusterConfig.from_dict(raw()).auth_source
