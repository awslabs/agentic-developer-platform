"""Exercise the actual operational client and protected qualification boundary."""

import base64
import hashlib
import io
import json
from pathlib import Path
import time
from unittest.mock import patch
from uuid import uuid4

import pytest

from modules.tools.platform.scripts import operational_release as release
from modules.tools.platform.scripts.native_test_session import AdmissionUnavailable, canonical


@pytest.fixture
def prepared(tmp_path):
    now = int(time.time())
    config = {
        "version": 1, "scope": release.SCOPE, "source_revision": "a" * 40,
        "definition_revision": "b" * 40, "gateway_origin": "https://gateway.example.com",
        "subject_id": str(uuid4()), "user_id": str(uuid4()), "tenant_id": "tenant",
        "team_id": "fixture-team", "query": "explicit private query", "authorized_at": now - 10,
        "expires_at": now + 1800,
    }
    claims = base64.urlsafe_b64encode(canonical({"token_use": "access", "sub": config["subject_id"],
                                              "exp": now + 3600})).decode().rstrip("=")
    token = "header." + claims + ".signature"
    env = {
        "GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch", "GITHUB_REF": "refs/heads/main",
        "GITHUB_RUN_ATTEMPT": "1", "GITHUB_REPOSITORY": "aws-e/adp", "GITHUB_WORKFLOW": "Deploy tools platform",
        "GITHUB_JOB": "release", "GITHUB_RUN_ID": "1234", "ADP_TOOLS_ENVIRONMENT": "dev",
        "ADP_SOURCE_REVISION": config["source_revision"], "ADP_DEFINITION_REVISION": config["definition_revision"],
        "ADP_TOOLS_RELEASE_QUALIFICATION": "operational", "ADP_TOOLS_SMOKE_GATEWAY_URL": config["gateway_origin"],
        "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(tmp_path / "env"),
        "ADP_TOOLS_SMOKE_EVIDENCE_DIR": str(tmp_path / "evidence"),
        "ADP_TOOLS_OPERATIONAL_SMOKE_JSON": json.dumps(config), "ADP_TOOLS_SMOKE_OPERATIONAL_ACCESS_TOKEN": token,
    }
    gateway = "https://selected.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"
    outputs, targets = tmp_path / "outputs.json", tmp_path / "targets.jsonl"
    outputs.write_text(json.dumps({"gateway_url": {"value": gateway}}))
    targets.write_text(json.dumps({"targetId": "fixture", "lastSynchronizedAt": "2026-01-01T00:00:00Z"}))
    return env, config, token, gateway, outputs, targets


class Wire:
    def __init__(self, config, token, gateway, fault=None):
        self.config, self.token, self.gateway, self.fault = config, token, gateway, fault
        self.calls = []
        self.execution = str(uuid4())
        self.request_id = None

    def open(self, request, **kwargs):
        self.calls.append(request.full_url)
        assert request.get_header("Authorization") == "Bearer " + self.token
        assert not request.get_header("X-adp-native-test-capability")
        assert kwargs["timeout"] == 40
        status = 200
        if request.full_url.endswith("/auth/me"):
            value = {"user_id": self.config["subject_id"], "org_id": self.config["tenant_id"],
                     "team_id": self.config["team_id"], "is_admin": self.fault == "admin"}
        elif request.full_url.endswith("/tools?live=true"):
            value = {"tools": [{"name": release.TOOL}], "discovery": {
                "gateway_url_sha256": hashlib.sha256(self.gateway.encode()).hexdigest(),
                "tools_list_sha256": "c" * 64}}
            if self.fault == "wrong-gateway":
                value["discovery"]["gateway_url_sha256"] = "d" * 64
        elif request.full_url.endswith("/tools"):
            value = {"root_human_id": "foreign-user" if self.fault == "wrong-owner" else self.config["user_id"]}
        elif "/close?" in request.full_url:
            value = {"execution_id": self.execution, "state": "closed", "cleanup_status": "confirmed"}
            if self.fault == "cleanup":
                value["cleanup_status"] = "unknown"
        else:
            if request.data:
                body = json.loads(request.data)
                assert body["arguments"] == {"query": self.config["query"]}
                assert body["tool"] == release.TOOL
                if self.request_id:
                    assert body["request_id"] == self.request_id
                self.request_id = body["request_id"]
            value = {"request_id": self.request_id, "execution_id": self.execution, "receipt_id": "original-receipt",
                     "status": "confirmed", "result_status": "success",
                     "gateway_url_sha256": hashlib.sha256(self.gateway.encode()).hexdigest()}
            if request.full_url.endswith("/call"):
                assert request.get_header("X-adp-expected-gateway-sha256") == value["gateway_url_sha256"]
                if self.fault == "search-unknown":
                    raise OSError("private-provider-sentinel")
                if self.fault == "search-error":
                    value["result_status"] = "error"
            elif "/operations/" in request.full_url and self.fault == "foreign-operation":
                value["execution_id"] = str(uuid4())
        response = io.BytesIO(canonical(value))
        response.status = status
        return response


def setup(prepared, fault=None):
    env, config, token, gateway, outputs, targets = prepared
    wire = Wire(config, token, gateway, fault)
    def factory(config, token):
        return release.Session(config, token, opener=wire)
    env.update(release.prepare(env, session_type=factory))
    return env, config, token, outputs, targets, wire, factory


@pytest.mark.parametrize("change", [
    {"ADP_TOOLS_ENVIRONMENT": "prod"}, {"GITHUB_RUN_ATTEMPT": "2"}, {"ADP_TOOLS_RELEASE_QUALIFICATION": "skip"},
    {"GITHUB_EVENT_NAME": "push"}, {"ADP_CORRELATION": "engine-run"}, {"ADP_SOURCE_REVISION": "c" * 40},
    {"ADP_DEFINITION_REVISION": "c" * 40}, {"ADP_TOOLS_SMOKE_GATEWAY_URL": "https://foreign.example.com"},
])
def test_policy_rejects_unapproved_scope_before_network(prepared, change):
    env, config, *_ = prepared
    with patch.object(release.Session, "identity") as identity:
        with pytest.raises(AdmissionUnavailable):
            release.prepare({**env, **change})
        identity.assert_not_called()


@pytest.mark.parametrize("change", [
    {"scope": "all_in"}, {"expires_at": 1}, {"expires_at": int(time.time()) + 7200},
    {"query": ""}, {"extra": "unsupported"}, {"gateway_origin": "http://gateway.example.com"},
])
def test_invalid_protected_configuration_is_refused(prepared, change):
    env, config, *_ = prepared
    with pytest.raises(AdmissionUnavailable):
        release.validate({**config, **change}, env, now=time.time())


def test_admin_login_cannot_be_used_as_fixture(prepared):
    with pytest.raises(AdmissionUnavailable):
        setup(prepared, "admin")


def test_success_uses_live_bound_ordinary_routes_and_retains_no_secrets(prepared):
    env, config, token, outputs, targets, wire, factory = setup(prepared)
    result = release.run(outputs, targets, env, session_type=factory)
    assert result["behavioral_status"] == "passed" and result["cleanup_status"] == "confirmed"
    assert result["cost_qualification"] == "not_requested" and result["governance_complete"] is False
    evidence = Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"]) / "operational-smoke.json"
    assert token not in evidence.read_text() and config["query"] not in evidence.read_text()
    assert evidence.stat().st_mode & 0o777 == 0o600
    assert sum(url.endswith("/call") for url in wire.calls) == 1
    assert sum("/close?" in url for url in wire.calls) == 1
    assert sum(url.endswith("/tools?live=true") for url in wire.calls) == 2
    with pytest.raises(AdmissionUnavailable):
        release.run(outputs, targets, env, session_type=lambda *_: pytest.fail("no replay"))


@pytest.mark.parametrize("fault", ["wrong-gateway", "wrong-owner", "search-unknown", "search-error", "cleanup", "foreign-operation"])
def test_failures_block_publication_and_close_only_original_execution(prepared, fault):
    env, _, _, outputs, targets, wire, factory = setup(prepared, fault)
    with pytest.raises(AdmissionUnavailable, match="publication refused"):
        release.run(outputs, targets, env, session_type=factory)
    report = (Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"]) / "operational-smoke.json").read_text()
    assert json.loads(report)["behavioral_status"] == "failed"
    assert "private-provider-sentinel" not in report
    assert sum(url.endswith("/call") for url in wire.calls) <= 1
    assert sum(url.endswith("/operations/recover") for url in wire.calls) <= 1
    if fault in {"search-unknown", "search-error"}:
        assert sum("/close?" in url for url in wire.calls) == 1


@pytest.mark.parametrize("fault", [None, "funding-claim", "foreign-run", "no-retention", "wrong-manifest"])
def test_only_exact_retained_operational_evidence_qualifies(prepared, fault):
    env, _, _, outputs, targets, _, factory = setup(prepared)
    result = release.run(outputs, targets, env, session_type=factory)
    directory = Path(env["ADP_TOOLS_SMOKE_EVIDENCE_DIR"])
    release_path = directory / "release.json"
    document = {"component": "tools-platform", "qualification_status": "smoke_passed_evidence_pending",
                "source_revision": env["ADP_SOURCE_REVISION"], "definition_revision": env["ADP_DEFINITION_REVISION"],
                "operational_smoke": result}
    if fault == "funding-claim":
        document["operational_smoke"]["governance_complete"] = True
    if fault == "foreign-run":
        document["operational_smoke"]["github_run_id"] = "9999"
    release.private_write(release_path, canonical(document))
    receipt = {"version": 1, "operational_smoke_sha256": result["operational_evidence_sha256"], "manifest": {
        "bucket": "adp-dev-ci-evidence-123456789012", "sha256": "d" * 64, "version_id": "immutable-version",
        "key": "artifacts/14/aws-e/adp/1234/1/release/operational-tools-smoke/" + "d" * 64 + ".manifest.json"}}
    if fault == "wrong-manifest":
        receipt["manifest"]["key"] = "foreign-key"
    if fault != "no-retention":
        release.private_write(directory / "retention.json", canonical(receipt))
    if fault:
        with pytest.raises((AdmissionUnavailable, OSError)):
            release.qualify(release_path, env)
    else:
        release.qualify(release_path, env)
        assert json.loads(release_path.read_text())["qualification_status"] == "operational_smoke_verified_evidence_retained"
