from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from src.agentauth.task_tool_policy import TaskToolPolicyError, freeze_tools, persona_tools
from src.agentauth.task_tool_routes import authorize_tool, router
from src.tasks.store import _protected_grant_digest


@pytest.fixture
def authority():
    uid = "00000000-0000-4000-8000-000000000001"
    identity = SimpleNamespace(
        task_id="tsk_" + uid, invocation_id=uid, generation=1, runtime_attempt_id=uid, tenant="tenant", canonical_principal="principal"
    )
    grant = {
        key: "fixture"
        for key in (
            "tenant",
            "canonical_principal",
            "task_id",
            "invocation_id",
            "generation",
            "request_digest",
            "persona",
            "input",
            "model_binding",
            "limits",
            "capabilities",
        )
    }
    grant["tool_grants"] = ["cyber.triage", "cyber.result", "cyber.cancel_jobs"]
    task = {key: getattr(identity, key) for key in ("task_id", "invocation_id", "generation", "runtime_attempt_id")}
    task.update(
        scope={"tenant": "tenant", "canonical_principal": "principal"},
        version=7,
        persona="investigator",
        state="running",
        deadline_at="2099-01-01T00:00:00Z",
        input_payload={"inputs": {"sample": "owned"}},
        tool_grants=list(grant["tool_grants"]),
        grant_digest=_protected_grant_digest(grant),
    )
    policy = {"status": "active", "allowed_personas": ["investigator"], "allowed_tools": list(grant["tool_grants"])}
    return SimpleNamespace(
        identity=identity,
        task=task,
        grant=grant,
        policy=policy,
        repo=SimpleNamespace(read_task=Mock(return_value=task), _get_authority=Mock(return_value=grant)),
        policies=SimpleNamespace(get=Mock(return_value=policy)),
        env={"ADP_TASK_PERSONA_TOOLS": '{"investigator":["cyber.triage","cyber.result","cyber.cancel_jobs"]}'},
    )


def check(a, tool="cyber.triage", cleanup=False):
    return authorize_tool(a.repo, a.policies, a.identity, tool, cleanup=cleanup, env=a.env)


def test_intersection_permission_returns_only_scoped_snapshot(authority):
    result = check(authority)
    assert result["identity"]["canonical_principal"] == "principal"
    assert result["task"]["version"] == 7
    assert set(result["identity"]) == {"task_id", "invocation_id", "generation", "runtime_attempt_id", "tenant", "canonical_principal"}
    assert result["task"]["input_payload"] == {"inputs": {"sample": "owned"}}
    assert "grant_digest" not in result["task"]
    assert "run_credential" not in str(result)


@pytest.mark.parametrize("layer", ["live", "frozen", "persona", "identity", "deadline", "status"])
def test_each_authority_layer_is_required(authority, layer):
    if layer == "live":
        authority.policy["allowed_tools"] = []
    elif layer == "frozen":
        authority.task["tool_grants"] = []
    elif layer == "persona":
        authority.env["ADP_TASK_PERSONA_TOOLS"] = "{}"
    elif layer == "identity":
        authority.task["scope"]["tenant"] = "other"
    elif layer == "deadline":
        authority.task["deadline_at"] = "2000-01-01T00:00:00Z"
    else:
        authority.task["state"] = "cancel_requested"
    with pytest.raises(HTTPException):
        check(authority)


def test_added_live_permission_cannot_expand_existing_task(authority):
    authority.policy["allowed_tools"].append("cyber.dynamic")
    authority.env["ADP_TASK_PERSONA_TOOLS"] = '{"investigator":["cyber.dynamic"]}'
    with pytest.raises(HTTPException):
        check(authority, "cyber.dynamic")


def test_cleanup_survives_revocation_but_cannot_start_work(authority):
    authority.policy["status"] = "disabled"
    authority.task["state"] = "cancel_requested"
    authority.task["deadline_at"] = "2000-01-01T00:00:00Z"
    authority.env["ADP_TASK_PERSONA_TOOLS"] = "{}"
    assert check(authority, "cyber.cancel_jobs", cleanup=True)["task"]["state"] == "cancel_requested"
    authority.policies.get.assert_not_called()
    assert check(authority, "other.cancel_jobs", cleanup=True)["task"]["task_id"] == authority.identity.task_id
    for tool in ("cyber.triage", "other.start"):
        with pytest.raises(HTTPException):
            check(authority, tool, cleanup=True)


def test_frozen_grants_are_digest_bound(authority):
    changed = deepcopy(authority.grant)
    changed["tool_grants"].append("cyber.dynamic")
    assert _protected_grant_digest(changed) != _protected_grant_digest(authority.grant)


def test_missing_configuration_means_no_tools_not_wildcard():
    assert persona_tools("investigator", {}) == frozenset()
    assert freeze_tools("investigator", {"allowed_tools": ["cyber.triage"]}, {}) == ()
    assert freeze_tools(
        "investigator", {"allowed_tools": ["cyber.triage", "other.read"]}, {"ADP_TASK_PERSONA_TOOLS": '{"investigator":["cyber.triage"]}'}
    ) == ("cyber.triage",)


@pytest.mark.parametrize("config", ['{"investigator":["cyber.*"]}', '{"investigator":"cyber.triage"}', "[]", "not-json"])
def test_malformed_configuration_fails_closed(config):
    with pytest.raises(TaskToolPolicyError):
        persona_tools("investigator", {"ADP_TASK_PERSONA_TOOLS": config})


def test_tool_authorization_requires_verified_transport():
    app = FastAPI()
    app.include_router(router)
    uid = "00000000-0000-4000-8000-000000000001"
    with TestClient(app) as client:
        response = client.post(
            "/internal/v1/agent/task/tool-authorize",
            json={
                "schema_version": "1.0",
                "attempt": {"run": {"task_id": "tsk_" + uid, "invocation_id": uid, "generation": 1}, "runtime_attempt_id": uid},
                "tool": "cyber.triage",
            },
        )
    assert response.status_code == 403


@pytest.mark.parametrize("tools", [["cyber.*"], ["cyber.triage", "cyber.triage"], ["https://endpoint"], "cyber.triage"])
def test_policy_writer_rejects_invalid_allowed_tools(tools):
    from src.agentauth.task_service_policy import TaskServicePolicyError, _validate_policy

    with pytest.raises(TaskServicePolicyError):
        _validate_policy({"status": "active", "allowed_tools": tools})


def test_legacy_grant_digest_remains_supported_without_tools(authority):
    from src.tasks.records import payload_digest

    grant = deepcopy(authority.grant)
    del grant["tool_grants"]
    assert _protected_grant_digest(grant) == payload_digest(grant)


def test_model_only_task_can_cleanup_without_domain_grants(authority):
    authority.task["tool_grants"] = []
    authority.grant["tool_grants"] = []
    authority.task["grant_digest"] = _protected_grant_digest(authority.grant)
    authority.policy["status"] = "disabled"
    authority.env["ADP_TASK_PERSONA_TOOLS"] = "{}"
    assert check(authority, "cyber.cancel_jobs", cleanup=True)["task"]["tool_grants"] == []
    authority.policies.get.assert_not_called()
    with pytest.raises(HTTPException):
        check(authority, "cyber.triage", cleanup=True)


def test_cleanup_exception_cannot_borrow_another_task_identity(authority):
    authority.task["scope"]["canonical_principal"] = "different-principal"
    with pytest.raises(HTTPException):
        check(authority, "cyber.cancel_jobs", cleanup=True)


def test_repository_binding_is_current_protected_authority_not_task_prose(authority):
    from tests.agentauth.test_task_repository_policy import BINDING

    authority.policy["repositories"] = {"application": deepcopy(BINDING)}
    authority.grant["repository_binding"] = {"alias": "application", "binding": deepcopy(BINDING)}
    authority.task["grant_digest"] = _protected_grant_digest(authority.grant)
    authority.task["input_payload"]["inputs"]["repository"] = "attacker/repo"
    assert check(authority)["task"]["repository_binding"]["binding"] == BINDING
    authority.policy["repositories"]["application"]["repository_id"] = "999"
    with pytest.raises(HTTPException, match="repository authority"):
        check(authority)


def test_repository_grant_tampering_is_refused(authority):
    from tests.agentauth.test_task_repository_policy import BINDING

    authority.grant["repository_binding"] = {"alias": "application", "binding": deepcopy(BINDING)}
    with pytest.raises(HTTPException, match="grant refused"):
        check(authority)
