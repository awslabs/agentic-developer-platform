"""Native funding retains ordinary Atlassian policy and connection checks."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from src.agentauth.native_test_atlassian_runtime import NativeAtlassianRuntime
from src.agentauth.native_test_facade_admission import NativeFacadeAdmission
from src.internal import tool_adapter_authority
from src.internal import tools_authorize as routes
from tests.internal.test_native_test_tools_gate import native_tools  # noqa: F401
from tests.internal.test_tools_authorize import HUMAN, bearer_identity, decision_api  # noqa: F401


@pytest.fixture
def atlassian(native_tools, monkeypatch):  # noqa: F811
    f = native_tools
    f.designate()
    bearer_identity(monkeypatch)
    runtime = f.client.app.state.native_test_runtime
    runtime.enrollment = SimpleNamespace(store=f.store, _effect_runtime=object.__new__(NativeAtlassianRuntime))
    native = NativeFacadeAdmission(enrollment=runtime.enrollment, budget=object())
    runtime.tool_admission = native
    tools = ["atlassian.get_jira_issue", "atlassian.create_jira_issue"]
    f.policy.update(
        consent_mode="explicit",
        allowed_tools=tools,
        consented_tools=tools[:],
        tool_max_cost_usd={tool: "1" for tool in tools},
        revision=1,
    )
    monkeypatch.setattr(routes, "gateway_catalogue", lambda: {"tools": [{"name": tool} for tool in tools]})
    selected = AsyncMock(return_value=({"id": "owned", "revision": "one", "site_id": "site-one"}, ["write:jira:agent-interface"]))
    monkeypatch.setattr(tool_adapter_authority, "current_atlassian_read", selected)
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_MEDIATOR_ENABLED", "true")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_WRITES_ENABLED", "true")
    prepared = AsyncMock(
        return_value={
            "schema_version": "1.0",
            "allowed": True,
            "receipt_id": "original-native-reservation",
            "principal": {"user_id": HUMAN, "root_human_id": HUMAN, "tenant_id": "tenant-one", "team_id": "team-one"},
            "native_admission": {"offline": True},
        }
    )
    monkeypatch.setattr(native, "prepare", prepared)
    operation = str(uuid4())
    headers = {"x-adp-user-authorization": "Bearer signed-login", "x-adp-native-test-capability": "offline", "x-adp-native-test-operation": operation}
    return SimpleNamespace(f=f, prepare=prepared, selected=selected, operation=operation, headers=headers)


def call(a, tool):
    return a.f.client.post(
        "/internal/v1/tools/authorize",
        json={
            "schema_version": "1.0",
            "tool": tool,
            "arguments": {"cloudId": "site-one"},
            "request_id": str(uuid4()),
        },
        headers=a.headers,
    )


@pytest.mark.parametrize("tool", ["atlassian___get_jira_issue", "atlassian___create_jira_issue"])
def test_native_call_keeps_connection_and_write_approval_without_a_second_budget(atlassian, tool):
    a = atlassian
    response = call(a, tool)
    assert response.status_code == 200, response.text
    reply = response.json()
    assert reply["receipt_id"] == "original-native-reservation"
    assert reply["atlassian_connection"] == {"id": "owned", "revision": "one", "site_id": "site-one"}
    if tool.endswith("create_jira_issue"):
        assert reply["atlassian_approval"]["consent_source"] == "explicit"
    assert a.prepare.await_args.kwargs["operation_id"] == a.operation
    assert json.loads(a.prepare.await_args.kwargs["body"])["tool"] == tool
    a.f.budget.assert_not_awaited()
    a.f.rate.assert_awaited_once()


@pytest.mark.parametrize("fault", ["consent", "connection", "mediator", "entitlement", "policy"])
def test_native_budget_cannot_override_current_user_tool_authority(atlassian, monkeypatch, fault):
    a = atlassian
    if fault == "consent":
        a.f.policy["consented_tools"] = ["atlassian.get_jira_issue"]
    elif fault == "connection":
        a.selected.side_effect = HTTPException(403, "offline disconnected")
    elif fault == "mediator":
        monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_MEDIATOR_ENABLED", "false")
    elif fault == "entitlement":
        monkeypatch.setattr(routes, "effective_entitlement", AsyncMock(return_value=(False, None)))
    else:
        a.f.policy["status"] = "revoked"
    response = call(a, "atlassian___create_jira_issue")
    assert response.status_code == 403, response.text
    a.prepare.assert_not_awaited()
    a.f.budget.assert_not_awaited()
