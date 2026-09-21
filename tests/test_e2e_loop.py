"""The e2e loop's local logic: secrets handling, version rewriting, host derivation, verdicts.

Nothing here touches a tenant. The tenant-facing half is proven by running the loop; these pin
down the parts that must never regress silently - above all, that a token never reaches output.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SECRET = "not-a-real-token-0123456789abcdef"


def _load_loop():
    spec = importlib.util.spec_from_file_location("e2e_loop", REPO_ROOT / "e2e" / "loop.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["e2e_loop"] = module
    spec.loader.exec_module(module)
    return module


loop = _load_loop()


class TestEnvFileParser:
    def test_plain_quoted_and_exported_values(self):
        values = loop.parse_env_file(
            "A=plain\nB=\"double quoted\"\nC='single # not a comment'\nexport D=exported\n"
        )

        assert values == {"A": "plain", "B": "double quoted", "C": "single # not a comment", "D": "exported"}

    def test_comments_blank_lines_and_junk_are_ignored(self):
        values = loop.parse_env_file("# a comment\n\n   \nnot a pair\n=novalue\nE=1 # trailing comment\n")

        assert values == {"E": "1"}

    def test_the_first_equals_splits_and_the_rest_is_value(self):
        assert loop.parse_env_file("URL=https://x/?a=b")["URL"] == "https://x/?a=b"

    def test_crlf_and_bom_survive(self, tmp_path):
        env_file = tmp_path / "x.env"
        env_file.write_bytes(b"\xef\xbb\xbfTOKEN=abc\r\nOTHER=def\r\n")

        settings = loop.Settings(env_file)

        assert settings.get("TOKEN") == "abc"
        assert settings.get("OTHER") == "def"


class TestSettings:
    def test_the_real_environment_wins_over_the_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / "x.env"
        env_file.write_text("LOOP_TEST_VAR=from-file\n")
        monkeypatch.setenv("LOOP_TEST_VAR", "from-env")

        assert loop.Settings(env_file).get("LOOP_TEST_VAR") == "from-env"

    def test_the_file_is_used_when_the_environment_is_silent(self, tmp_path, monkeypatch):
        env_file = tmp_path / "x.env"
        env_file.write_text("LOOP_TEST_VAR=from-file\n")
        monkeypatch.delenv("LOOP_TEST_VAR", raising=False)

        assert loop.Settings(env_file).get("LOOP_TEST_VAR") == "from-file"

    def test_a_missing_variable_names_the_variable_and_file_but_no_values(self, tmp_path, monkeypatch):
        env_file = tmp_path / "x.env"
        env_file.write_text(f"SOME_OTHER_TOKEN={SECRET}\n")
        monkeypatch.delenv("LOOP_MISSING_VAR", raising=False)

        with pytest.raises(loop.ConfigError) as raised:
            loop.Settings(env_file).require("LOOP_MISSING_VAR")

        message = str(raised.value)
        assert "LOOP_MISSING_VAR" in message
        assert str(env_file) in message
        assert SECRET not in message

    def test_an_unreadable_file_is_a_config_error(self, tmp_path):
        with pytest.raises(loop.ConfigError):
            loop.Settings(tmp_path / "absent.env").get("ANYTHING")


class TestRedaction:
    def test_every_registered_secret_is_scrubbed(self):
        redact = loop.Redactor()
        redact.add(SECRET)

        assert redact(f"Authorization: Bearer {SECRET}") == "Authorization: Bearer ***"

    def test_dry_run_output_never_contains_the_token(self, capsys, monkeypatch):
        monkeypatch.setenv("DT_BEARER_TOKEN", SECRET)

        code = loop.main(["--dry-run", "--env-url", "https://example.apps.dynatrace.com"])

        out = capsys.readouterr().out
        assert code == 0
        assert SECRET not in out
        assert "Bearer ***" in out

    def test_an_http_error_body_echoing_the_token_is_scrubbed(self):
        redact = loop.Redactor()
        redact.add(SECRET)

        message = redact(loop.friendly_error("GET", "/x", 401, {"error": {"message": f"bad {SECRET}"}}))

        assert SECRET not in message
        assert "401" in message


class TestErrorHints:
    def test_an_sso_rejection_is_not_reported_as_a_missing_scope(self):
        # Seen for real against a sprint tenant: an expired OAuth bearer returns 403 with an SSO
        # message, and "add a scope" is the wrong advice for a token that cannot authenticate.
        sso = "An error occurred during SSO authentication to the Dynatrace environment."
        body = {"error": {"message": sso}}

        message = loop.friendly_error("GET", "/platform/extensions/v2/extensions", 403, body)

        assert "SSO authentication, before any scope check" in message
        assert "lacks a scope" not in message

    def test_a_genuine_missing_scope_still_says_so(self):
        body = {"error": {"message": "OAuth token is missing required scope. Use one of: [x:y:z]"}}

        message = loop.friendly_error("GET", "/api/v1/deployment/installer", 403, body)

        assert "lacks a scope" in message


class TestHosts:
    @pytest.mark.parametrize(
        ("apps", "classic"),
        [
            ("https://abc123.apps.dynatrace.com", "https://abc123.live.dynatrace.com"),
            ("https://abc123.sprint.apps.dynatracelabs.com/", "https://abc123.sprint.dynatracelabs.com"),
            ("abc123.dev.apps.dynatracelabs.com", "https://abc123.dev.dynatracelabs.com"),
        ],
    )
    def test_classic_host_is_derived_from_the_apps_host(self, apps, classic):
        assert loop.classic_url(apps) == classic

    def test_an_api_url_ending_in_api_is_trimmed(self):
        assert (
            loop.api_base_url("https://abc.sprint.dynatracelabs.com/api/")
            == "https://abc.sprint.dynatracelabs.com"
        )

    def test_auth_scheme_follows_the_token_type(self):
        assert loop.auth_scheme("dt0c01.ABC.DEF") == "Api-Token"
        assert loop.auth_scheme("dt0s16.ABC.DEF") == "Bearer"


class TestVersioning:
    YAML = (
        'name: custom:cohesity.storage\nversion: 0.1.1\nminDynatraceVersion: "1.341.0"\n  version: nested\n'
    )

    def test_dev_versions_are_in_their_own_minor_and_match_the_api_pattern(self):
        version = loop.dev_version(1_790_000_000.0)

        assert version == f"0.99.{1_790_000_000 // 60}"
        assert version.startswith(loop.DEV_VERSION_PREFIX)

    def test_only_the_top_level_version_is_rewritten(self):
        rewritten = loop.with_version(self.YAML, "0.99.5")

        assert "version: 0.99.5\n" in rewritten
        assert "  version: nested" in rewritten
        assert loop.extension_version(rewritten) == "0.99.5"

    def test_the_manifest_is_restored_byte_for_byte_even_after_a_crash(self, tmp_path):
        manifest = tmp_path / "extension.yaml"
        original = self.YAML.replace("\n", "\r\n").encode()
        manifest.write_bytes(original)

        with pytest.raises(RuntimeError), loop.temporary_version(manifest, "0.99.7"):
            assert b"version: 0.99.7" in manifest.read_bytes()
            raise RuntimeError

        assert manifest.read_bytes() == original

    def test_zip_name_matches_dt_sdk(self):
        assert loop.zip_name("custom:cohesity.storage", "0.99.1") == "custom_cohesity.storage-0.99.1.zip"


class TestMonitoringConfiguration:
    def test_every_endpoint_field_exists_in_the_activation_schema(self):
        schema = json.loads((REPO_ROOT / "extension" / "activationSchema.json").read_text(encoding="utf-8"))
        properties = schema["types"]["dynatrace.datasource.python:cohesity-cluster-endpoint"]["properties"]

        endpoint = loop.monitoring_value("0.99.1", "cohesity-e2e")["pythonRemote"]["endpoints"][0]

        assert set(endpoint) <= set(properties)

    def test_feature_sets_exist_in_the_manifest(self):
        manifest = (REPO_ROOT / "extension" / "extension.yaml").read_text(encoding="utf-8")

        for feature_set in loop.monitoring_value("0.99.1", "g")["featureSets"]:
            assert f"featureSet: {feature_set}" in manifest

    def test_tls_is_verified_against_the_instance_ca(self):
        endpoint = loop.monitoring_value("0.99.1", "g")["pythonRemote"]["endpoints"][0]

        assert endpoint["verifyTls"] is True
        assert endpoint["caCertPath"] == "/etc/cohesity-fake/ca.crt"
        assert endpoint["intervalMinutes"] == 1


class TestVerdicts:
    def test_all_21_metric_keys_come_from_the_manifest(self):
        keys = loop.expected_metric_keys(
            (REPO_ROOT / "extension" / "extension.yaml").read_text(encoding="utf-8")
        )

        assert len(keys) == 21
        assert loop.COLLECTION_SUCCESS in keys

    def test_metric_keys_match_with_or_without_a_grail_prefix(self):
        matches = loop.match_metric_keys(
            ["cohesity.cluster.io.iops", "cohesity.cluster.cpu.usage", "cohesity.cluster.memory.usage"],
            {"cohesity.cluster.io.iops", "ext:cohesity.cluster.cpu.usage"},
        )

        assert matches["cohesity.cluster.io.iops"] == "cohesity.cluster.io.iops"
        assert matches["cohesity.cluster.cpu.usage"] == "ext:cohesity.cluster.cpu.usage"
        assert matches["cohesity.cluster.memory.usage"] is None

    def test_nodes_need_one_of_each_type(self):
        ok, _ = loop.evaluate_nodes(
            [{"type": "EXT_COHESITY_CLUSTER", "n": "1"}, {"type": "EXT_COHESITY_STORAGE_DOMAIN", "n": "3"}]
        )
        assert not ok

        ok, _ = loop.evaluate_nodes([{"type": t, "n": "1"} for t in loop.NODE_TYPES])
        assert ok

    def test_edges_need_every_relationship(self):
        rows = [{"type": t, "source_type": s, "target_type": d, "n": "2"} for t, s, d in loop.EDGES]

        assert loop.evaluate_edges(rows)[0]
        assert not loop.evaluate_edges(rows[:-1])[0]

    def test_collection_success_needs_a_one(self):
        assert loop.evaluate_collection([{"best": "1"}])[0]
        assert not loop.evaluate_collection([{"best": "0"}])[0]
        assert not loop.evaluate_collection([{"best": None}])[0]
