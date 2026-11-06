"""Exercise user-access registration without contacting AWS or GitHub."""

import json
import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "register-github-app.sh"
RETURN_URL = "https://dashboard.example.test/api/auth/connections/oauth/return"
CALLBACK = "https://bedrock-agentcore.us-east-1.amazonaws.com/identities/oauth2/callback/test"

FAKE_AWS = '''#!/usr/bin/env python3
import json
import os
import sys

path = os.environ["STATE_PATH"]
with open(path) as stream:
    state = json.load(stream)
service, action = sys.argv[1:3]
arguments = sys.argv[3:]

def option(name):
    return arguments[arguments.index(name) + 1]

def missing():
    sys.stderr.write("ResourceNotFoundException")
    sys.exit(2)

if service == "secretsmanager":
    if action == "get-secret-value":
        if option("--secret-id") == "adp/dev/github-app/adp-agent-platform-meta":
            value = state.get("manifest")
        else:
            value = state["previous" if "--version-stage" in arguments else "current"]
        if value is None:
            missing()
        print(json.dumps(value))
    elif action in ("create-secret", "put-secret-value"):
        reference = option("--secret-string")
        assert reference.startswith("file://")
        with open(reference[7:]) as stream:
            value = json.load(stream)
        if action == "put-secret-value":
            state["previous"] = state["current"]
        state["current"] = value
        state["calls"].append(action)
elif service == "bedrock-agentcore-control":
    if action == "get-workload-identity":
        if not state["workload"]:
            missing()
        print(state["workload"])
    elif action == "create-workload-identity":
        state["workload"] = option("--allowed-resource-oauth2-return-urls")
        state["calls"].append(action)
    elif action == "get-oauth2-credential-provider":
        if state["provider"] is None:
            missing()
        print("GithubOauth2" if option("--query") == "credentialProviderVendor" else os.environ["CALLBACK"])
    elif action in ("create-oauth2-credential-provider", "update-oauth2-credential-provider"):
        if action == "update-oauth2-credential-provider" and os.environ.get("FAKE_FAIL_UPDATE") == "true":
            sys.stderr.write("secret-sentinel")
            sys.exit(2)
        reference = option("--oauth2-provider-config-input")
        assert reference.startswith("file://")
        with open(reference[7:]) as stream:
            config = json.load(stream)["githubOauth2ProviderConfig"]
        assert config["clientSecretConfig"] == {"secretId": "adp/dev/connections/oauth/github", "jsonKey": "client_secret"}
        assert config["clientSecretSource"] == "EXTERNAL"
        state["provider"] = config["clientId"]
        state["calls"].append(action)
    else:
        sys.exit(3)
else:
    sys.exit(3)
with open(path, "w") as stream:
    json.dump(state, stream)
'''


@pytest.fixture
def fake_aws(tmp_path):
    aws = tmp_path / "aws"
    aws.write_text(FAKE_AWS)
    aws.chmod(0o755)
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"workload": None, "provider": None, "current": None, "previous": None, "calls": []}))
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "STATE_PATH": str(state_path),
        "CALLBACK": CALLBACK,
        "GH_APP_CLIENT_ID": "Iv_example123",
        "GH_APP_CLIENT_SECRET": "secret-sentinel",
        "ADP_OAUTH_RETURN_URL": RETURN_URL,
    }

    gh = tmp_path / "gh"
    gh.write_text("#!/bin/sh\nexit 88\n")
    gh.chmod(0o755)

    def run(*args, environment_override=None, app_id="12345"):
        result = subprocess.run(
            ["bash", str(SCRIPT), "--user-access", *(["--app-id", app_id] if app_id else []), *args],
            env={**environment, **(environment_override or {})}, capture_output=True, text=True, check=False,
        )
        assert "secret-sentinel" not in result.stdout + result.stderr
        assert "manifest-secret" not in result.stdout + result.stderr
        return result, json.loads(state_path.read_text())

    return run, state_path


def test_creates_provider_and_callback_without_printing_secrets(fake_aws):
    run, _ = fake_aws
    result, state = run()
    assert result.returncode == 0, result.stderr
    assert CALLBACK in result.stdout
    assert state["workload"] == RETURN_URL
    assert state["provider"] == "Iv_example123"
    assert state["current"] == {"client_id": "Iv_example123", "client_secret": "secret-sentinel", "app_id": "12345"}
    assert state["calls"] == ["create-workload-identity", "create-secret", "create-oauth2-credential-provider"]


def test_replaces_provider_and_restores_previous_version(fake_aws):
    run, state_path = fake_aws
    original = {"client_id": "legacy", "client_secret": "old-secret"}
    state_path.write_text(json.dumps({
        "workload": RETURN_URL, "provider": "legacy", "current": original, "previous": None, "calls": [],
    }))
    result, state = run()
    assert result.returncode == 0, result.stderr
    assert state["previous"] == original
    assert state["current"]["app_id"] == "12345"
    assert state["calls"] == ["put-secret-value", "update-oauth2-credential-provider"]
    result, state = run()
    assert result.returncode == 0, result.stderr
    assert state["calls"] == ["put-secret-value", "update-oauth2-credential-provider", "update-oauth2-credential-provider"]
    result, state = run("--rollback")
    assert result.returncode == 0, result.stderr
    assert state["provider"] == "legacy"
    assert state["current"] == original
    assert state["previous"]["app_id"] == "12345"
    assert "old-secret" not in result.stdout + result.stderr


def test_failed_provider_update_can_restore_previous_grant_without_leaking_secret(fake_aws):
    run, state_path = fake_aws
    original = {"client_id": "legacy", "client_secret": "old-secret"}
    state_path.write_text(json.dumps({
        "workload": RETURN_URL, "provider": "legacy", "current": original, "previous": None, "calls": [],
    }))
    failure, state = run(environment_override={"FAKE_FAIL_UPDATE": "true"})
    assert failure.returncode != 0
    assert state["previous"] == original
    assert state["provider"] == "legacy"
    restored, state = run("--rollback", "--app-id", "", environment_override={"GH_APP_CLIENT_SECRET": ""})
    assert restored.returncode == 0, restored.stderr
    assert state["provider"] == "legacy"
    assert state["current"] == original
    assert "old-secret" not in restored.stdout + restored.stderr


def test_manifest_created_app_can_register_replace_and_rollback_without_exposing_credentials(fake_aws):
    run, state_path = fake_aws
    manifest = {"app_id": "12345", "app_slug": "sample-app", "client_id": "Iv_manifest", "client_secret": "manifest-secret"}
    state_path.write_text(json.dumps({
        "workload": RETURN_URL, "provider": "legacy", "current": {"client_id": "legacy", "client_secret": "old-secret"},
        "previous": None, "calls": [], "manifest": manifest,
    }))
    without_inputs = {"GH_APP_CLIENT_ID": "", "GH_APP_CLIENT_SECRET": ""}
    result, state = run("--from-manifest", environment_override=without_inputs)
    assert result.returncode == 0, result.stderr
    assert CALLBACK in result.stdout
    assert state["current"] == {"client_id": "Iv_manifest", "client_secret": "manifest-secret", "app_id": "12345"}
    assert state["provider"] == "Iv_manifest"
    assert state["previous"] == {"client_id": "legacy", "client_secret": "old-secret"}
    assert state["calls"] == ["put-secret-value", "update-oauth2-credential-provider"]
    restored, state = run("--rollback", environment_override=without_inputs)
    assert restored.returncode == 0, restored.stderr
    assert state["current"] == {"client_id": "legacy", "client_secret": "old-secret"}
    assert state["provider"] == "legacy"
    assert "old-secret" not in result.stdout + result.stderr + restored.stdout + restored.stderr


def test_manifest_created_app_creates_provider_without_manual_client_inputs(fake_aws):
    run, state_path = fake_aws
    state = json.loads(state_path.read_text())
    state["manifest"] = {"app_id": "12345", "client_id": "Iv_manifest", "client_secret": "manifest-secret"}
    state_path.write_text(json.dumps(state))
    result, state = run("--from-manifest", environment_override={"GH_APP_CLIENT_ID": "", "GH_APP_CLIENT_SECRET": ""}, app_id=None)
    assert result.returncode == 0, result.stderr
    assert CALLBACK in result.stdout
    assert state["calls"] == ["create-workload-identity", "create-secret", "create-oauth2-credential-provider"]
    assert state["current"]["client_secret"] == "manifest-secret"


@pytest.mark.parametrize("manifest,app_id", [
    (None, "12345"),
    ({"app_id": "54321", "client_id": "Iv_manifest", "client_secret": "manifest-secret"}, "12345"),
    ({"app_id": "12345", "client_id": "Iv_manifest", "client_secret": ""}, "12345"),
    ({"app_id": "12345", "client_id": "Iv_manifest", "client_secret": "PLACEHOLDER_SET_BY_REGISTER_SCRIPT"}, "12345"),
    ({"app_id": "12345", "client_id": "bad client", "client_secret": "manifest-secret"}, "12345"),
])
def test_manifest_metadata_must_be_complete_and_match_expected_app(fake_aws, manifest, app_id):
    run, state_path = fake_aws
    state = json.loads(state_path.read_text())
    state["manifest"] = manifest
    state_path.write_text(json.dumps(state))
    result, state = run("--app-id", app_id, "--from-manifest")
    assert result.returncode != 0
    assert state["calls"] == []
    assert state["current"] is None


def test_manifest_mode_rejects_rollback(fake_aws):
    run, _ = fake_aws
    result, state = run("--from-manifest", "--rollback")
    assert result.returncode != 0
    assert state["calls"] == []


def test_refuses_existing_provider_without_secret(fake_aws):
    run, state_path = fake_aws
    state_path.write_text(json.dumps({
        "workload": RETURN_URL, "provider": "legacy", "current": None, "previous": None, "calls": [],
    }))
    failure, state = run()
    assert failure.returncode != 0
    assert state["calls"] == []


@pytest.mark.parametrize("args,environment_override", [
    (("--rollback",), {}),
    ((), {"GH_APP_CLIENT_SECRET": ""}),
    (("--return-url", "http://insecure.test/api/auth/connections/oauth/return"), {}),
])
def test_missing_rollback_or_invalid_setup_fails_without_writing(fake_aws, args, environment_override):
    run, _ = fake_aws
    result, state = run(*args, environment_override=environment_override)
    assert result.returncode != 0
    assert state["calls"] == []
    assert state["current"] is None
