"""Dynamic metadata refresh never grants policy and never disables static tools."""

from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from tools_facade.server import create_app

CORE = {"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}
KNOWLEDGE = {"name": "knowledge___search", "description": "Knowledge", "inputSchema": {"type": "object"}}
HEADERS = {"Authorization": "Bearer fixture-human-token"}


@pytest.mark.parametrize("headers", [
    {"x-adp-native-test-capability": "untrusted"},
    {"x-adp-native-test-operation": "untrusted"},
    {"x-adp-native-test-capability": "untrusted", "x-adp-native-test-operation": "untrusted"},
])
def test_asserted_native_headers_do_not_bypass_the_native_authorizer(headers):
    authority = Authority()
    client = TestClient(create_app(gateway=authority, agentcore=SimpleNamespace()))
    response = client.post("/agent/tools/call", headers={**HEADERS, **headers}, json={
        "tool": "knowledge___search", "arguments": {"query": "private"},
        "request_id": "00000000-0000-4000-8000-000000000001",
    })
    assert response.status_code == 403
    assert authority.calls == []


class Authority:
    def __init__(self):
        self.ready = False
        self.changed = False
        self.calls = []

    def decision(self):
        user = "other" if self.changed else "fixture-human"
        return {
            "schema_version": "1.0",
            "allowed": True,
            "receipt_id": "fixture-receipt",
            "principal": {
                "user_id": user,
                "root_human_id": user,
                "tenant_id": "fixture-tenant",
                "team_id": "fixture-team",
            },
        }

    async def list_tools(self, headers):
        self.calls.append("list")
        return {
            **self.decision(),
            "tools": [CORE, *([KNOWLEDGE] if self.ready else [])],
            "dynamic_catalogue_unavailable": not self.ready,
        }

    async def authorize(self, tool, arguments, headers):
        self.calls.append("authorize")
        if not self.ready:
            raise HTTPException(403, "tool_undeployed")
        return self.decision()


@pytest.mark.parametrize("failure", [False, True])
def test_refresh_reads_authority_again_and_preserves_core_on_discovery_failure(failure):
    authority = Authority()
    calls = []

    async def discover(decision, *, require_search):
        calls.append(require_search)
        if failure:
            raise httpx.ConnectError("fixture unavailable")
        authority.ready = True
        return {}

    client = TestClient(create_app(gateway=authority, agentcore=SimpleNamespace(discover=discover)))
    response = client.get("/agent/tools", headers=HEADERS)
    assert response.status_code == 200
    names = [tool["name"] for tool in response.json()["tools"]]
    assert names == [CORE["name"], *([] if failure else [KNOWLEDGE["name"]])]
    assert response.json()["dynamic_catalogue_status"] == ("unavailable" if failure else "ready")
    assert calls == [False] and authority.calls == ["list", "list"]


def test_verified_metadata_is_reloaded_when_discovery_fails_after_publication():
    authority = Authority()

    async def discover(decision, **kwargs):
        authority.ready = True
        raise ValueError("another target returned invalid discovery metadata")

    client = TestClient(create_app(gateway=authority, agentcore=SimpleNamespace(discover=discover)))
    response = client.get("/agent/tools", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["dynamic_catalogue_status"] == "ready"
    assert [tool["name"] for tool in response.json()["tools"]] == [CORE["name"], KNOWLEDGE["name"]]
    assert authority.calls == ["list", "list"]


def test_identity_cannot_change_during_metadata_refresh():
    authority = Authority()

    async def discover(decision, **kwargs):
        authority.ready = authority.changed = True

    response = TestClient(create_app(gateway=authority, agentcore=SimpleNamespace(discover=discover))).get(
        "/agent/tools", headers=HEADERS
    )
    assert response.status_code == 403


@pytest.mark.parametrize("tool", [
    "knowledge___search", "atlassian___search_jira", "atlassian___get_jira_issue",
    "github___get_user", "jira___get_issue", "slack___get_user",
])
def test_direct_dynamic_call_refreshes_before_call_authorization(tool):
    authority = Authority()
    events = []

    async def discover(decision, **kwargs):
        events.append("discover")
        authority.ready = True

    async def invoke(tool, arguments, decision):
        events.append("invoke")
        return {
            "jsonrpc": "2.0",
            "result": {
                "content": [{"type": "text", "text": "own-sentinel"}],
                "_meta": {"adp_receipt_id": decision.receipt_id},
            },
        }

    client = TestClient(create_app(gateway=authority, agentcore=SimpleNamespace(discover=discover, invoke=invoke)))
    response = client.post(
        "/agent/tools/call",
        headers=HEADERS,
        json={
            "tool": tool,
            "arguments": {"query": "sentinel"},
            "request_id": "00000000-0000-4000-8000-000000000001",
        },
    )
    assert response.status_code == 200 and "own-sentinel" in response.text
    assert response.json()["request_id"] == "00000000-0000-4000-8000-000000000001"
    expected = ["list", "list", "authorize"]
    if not tool.startswith("knowledge___"):
        expected.insert(0, "authorize")
    assert authority.calls == expected and events == ["discover", "invoke"]


@pytest.mark.parametrize("changed", ["revoked", "different_owner"])
def test_dynamic_refresh_does_not_grant_a_revoked_or_changed_identity(changed):
    authority = Authority()
    events = []
    original = authority.decision

    def decision():
        result = original()
        if authority.ready and changed == "revoked":
            result["allowed"] = False
        return result

    authority.decision = decision

    async def discover(decision, **kwargs):
        authority.ready = True
        authority.changed = changed == "different_owner"

    async def invoke(*args):
        events.append("invoke")

    client = TestClient(create_app(gateway=authority, agentcore=SimpleNamespace(discover=discover, invoke=invoke)))
    response = client.post("/agent/tools/call", headers=HEADERS, json={
        "tool": "atlassian___search_jira", "arguments": {"jql": "updated >= -1d"},
        "request_id": "00000000-0000-4000-8000-000000000001",
    })
    assert response.status_code == 403
    assert authority.calls == ["authorize", "list", "list"] and events == []


@pytest.mark.parametrize("reason", ["tool_not_allowed", "scope_not_granted", "tool_not_consented"])
def test_policy_denials_do_not_trigger_metadata_refresh(reason):
    authority = Authority()

    async def refused(tool, arguments, headers):
        authority.calls.append("authorize")
        raise HTTPException(403, reason)

    authority.authorize = refused
    response = TestClient(create_app(gateway=authority, agentcore=SimpleNamespace())).post(
        "/agent/tools/call", headers=HEADERS, json={
            "tool": "atlassian___search_jira", "arguments": {"jql": "updated >= -1d"},
            "request_id": "00000000-0000-4000-8000-000000000001",
        },
    )
    assert response.status_code == 403 and response.json()["detail"] == reason
    assert authority.calls == ["authorize"]


def test_unavailable_catalogue_refresh_is_bounded_and_never_dispatches():
    authority = Authority()
    events = []

    async def discover(decision, **kwargs):
        events.append("discover")
        raise httpx.ConnectError("target unavailable")

    async def invoke(*args):
        events.append("invoke")

    client = TestClient(create_app(gateway=authority, agentcore=SimpleNamespace(discover=discover, invoke=invoke)))
    response = client.post(
        "/agent/tools/call", headers=HEADERS, json={
            "tool": "atlassian___search_jira", "arguments": {"jql": "updated >= -1d"},
            "request_id": "00000000-0000-4000-8000-000000000001",
        },
    )
    assert response.status_code == 403
    assert authority.calls == ["authorize", "list", "list", "authorize"]
    assert events == ["discover"]
