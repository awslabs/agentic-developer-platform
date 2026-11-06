"""Crew tools reuse the live capability, pod, lease and human binding."""

import json
from dataclasses import replace

import httpx
import pytest
from httpx import AsyncClient

from src.agentauth import chat_tools
from tests.agentauth import test_chat_crew_model as fixtures
from tests.internal.test_tools_authorize import BODY
from tests.internal.test_tools_authorize import decision_api as decision_api_fixture

client = fixtures.client
runtime = fixtures.runtime
store = fixtures.store
sts = fixtures.sts
retained_input_table = fixtures.retained_input_table
mailbox = fixtures.mailbox
bound = fixtures.bound
decision_api = decision_api_fixture
pytestmark = pytest.mark.parametrize("mailbox", ["persistent"], indirect=True)


def proofs(bound):
    return {
        "x-adp-chat-capability": bound.token,
        "x-adp-chat-run-id": "run-user",
        "x-adp-chat-session-id": "session-a",
        "x-agent-runid": bound.run_id,
        "x-adp-workload-token": "sandbox-token",
    }


@pytest.fixture
def relay(bound, monkeypatch):
    bound.client._transport.app.include_router(chat_tools.router)
    requests = []

    def upstream(request):
        requests.append(request)
        return httpx.Response(200, json={"tools": [{"name": "websearch___search"}]})

    monkeypatch.setattr(chat_tools.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=httpx.MockTransport(upstream), **kwargs))
    monkeypatch.setattr(chat_tools, "runtime", lambda: (bound.runtime[1], bound.runtime[0]))
    return requests


async def request_tools(bound, **changes):
    return await bound.client.post(
        "/v1/chat/data/tools",
        headers={
            "Authorization": f"Bearer {bound.token}",
            "x-adp-workload-token": "sandbox-token",
            "x-agent-runid": bound.run_id,
        },
        json={"run_id": "run-user", "session_id": "session-a", "method": "GET", "path": "/tools", **changes},
    )


async def test_discovery_and_call_relay_only_original_proofs(bound, relay):
    response = await request_tools(bound)
    assert response.status_code == 200, response.text
    assert response.json() == {"status": 200, "body": {"tools": [{"name": "websearch___search"}]}}
    response = await request_tools(bound, method="POST", path="/tools/call", body={"tool": "websearch___search", "arguments": {"query": "example"}})
    assert response.status_code == 200
    assert len(relay) == 2
    assert str(relay[0].url) == chat_tools.FACADE_URL
    assert str(relay[1].url) == chat_tools.FACADE_URL + "/call"
    assert json.loads(relay[1].content)["arguments"] == {"query": "example"}
    for forwarded in relay:
        for name, value in proofs(bound).items():
            assert forwarded.headers[name] == value
        assert "authorization" not in forwarded.headers
        assert "x-adp-run-credential" not in forwarded.headers


@pytest.mark.parametrize("path", ["/tools/../internal", "https://attacker.invalid/tools", "/tools/%2e%2e/internal", "/tools#fragment"])
async def test_relay_cannot_escape_the_tools_routes(bound, relay, path):
    assert (await request_tools(bound, path=path)).status_code == 400
    assert not relay


@pytest.mark.parametrize("recover", [False, True])
@pytest.mark.parametrize("change", ["user", "tenant", "pod", "session", "lease", "revoked"])
async def test_foreign_or_stale_binding_cannot_reach_facade(bound, relay, change, recover):
    route = {"method": "POST", "path": "/tools/operations/recover", "body": {}} if recover else {}
    if change == "pod":
        bound.state["pod"] = replace(bound.state["pod"], uid="other-pod")
    elif change == "session":
        assert (await request_tools(bound, session_id="session-other", **route)).status_code == 404
        assert not relay
        return
    elif change == "revoked":
        assert bound.crew.revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=int(bound.lease["generation"]))
    else:
        field, value = {"user": ("user_id", "other-human"), "tenant": ("tenant_id", "other-tenant"), "lease": ("lease_generation", 99)}[change]
        row = bound.crew.read_run(bound.run_id)
        bound.registry.update_item(
            Key={"event_id": bound.run_id, "arrived_at": row["arrived_at"]},
            UpdateExpression="SET #field = :value",
            ExpressionAttributeNames={"#field": field},
            ExpressionAttributeValues={":value": value},
        )
    response = await request_tools(bound, **route)
    assert response.status_code == 404, response.text
    assert not relay


async def test_crew_policy_decision_derives_human_and_records_receipt(bound, relay, decision_api):
    api, worker, policy_read, policy, budget, _ = decision_api
    policy["catalog"] = [{"name": "websearch___search", "description": "Search", "inputSchema": {"type": "object"}}]
    listing = api.post("/internal/v1/tools/authorize", headers=proofs(bound), json={"schema_version": "1.0", "action": "list"})
    assert listing.status_code == 200, listing.text
    assert [tool["name"] for tool in listing.json()["tools"]] == ["websearch___search"]
    response = api.post("/internal/v1/tools/authorize", headers=proofs(bound), json=BODY)
    assert response.status_code == 200, response.text
    decision = response.json()
    assert decision["principal"]["user_id"] == decision["principal"]["root_human_id"] == "human"
    assert decision["principal"]["team_id"] == "team-one"
    assert decision["run_id"] == bound.run_id and decision["workload_id"] == bound.state["pod"].uid
    policy_read.assert_called_with(tenant_id="tenant", owner_id="human")
    assert api.app.state.tool_activity_recorder.call_args.kwargs["run_id"] == bound.run_id
    worker.authenticate.assert_not_called()
    assert budget.await_count == 2
    policy["consented_tools"] = []
    assert api.post("/internal/v1/tools/authorize", headers=proofs(bound), json=BODY).status_code == 403
    listing = api.post("/internal/v1/tools/authorize", headers=proofs(bound), json={"schema_version": "1.0", "action": "list"})
    assert listing.json()["tools"] == []


@pytest.mark.parametrize("change", ["workload", "capability", "session", "run", "mixed", "revoked"])
async def test_facade_callback_reverifies_crew_proofs(bound, relay, decision_api, change):
    api, worker, policy_read, _, _, _ = decision_api
    headers = proofs(bound)
    if change == "revoked":
        bound.crew.revoke_for_lease(tenant_id="tenant", session_id="session-a", lease_generation=int(bound.lease["generation"]))
    else:
        field = {
            "workload": "x-adp-workload-token",
            "capability": "x-adp-chat-capability",
            "session": "x-adp-chat-session-id",
            "run": "x-agent-runid",
            "mixed": "x-adp-run-credential",
        }[change]
        headers[field] = "other"
    response = api.post("/internal/v1/tools/authorize", headers=headers, json=BODY)
    assert response.status_code in (401, 404), response.text
    policy_read.assert_not_called()
    worker.authenticate.assert_not_called()


async def test_ambiguous_tool_response_is_not_retried(bound, relay, monkeypatch):
    attempts = []

    def timeout(request):
        attempts.append(request)
        raise httpx.ReadTimeout("response lost")

    monkeypatch.setattr(chat_tools.httpx, "AsyncClient", lambda **kwargs: AsyncClient(transport=httpx.MockTransport(timeout), **kwargs))
    response = await request_tools(bound, method="POST", path="/tools/call", body={"request_id": "stable-request"})
    assert response.status_code == 200
    assert response.json()["status"] == 502
    assert len(attempts) == 1


async def test_owned_recovery_relay_keeps_payload_and_proofs_after_new_work_revocation(bound, relay, decision_api):
    api, worker, policy_read, policy, budget, _ = decision_api
    policy["consented_tools"] = []
    assert api.post("/internal/v1/tools/authorize", headers=proofs(bound), json=BODY).status_code == 403
    policy_read.reset_mock()
    budget.reset_mock()
    payload = {
        "tool": "browser___start",
        "arguments": {"url": "https://example.org"},
        "request_id": "00000000-0000-4000-8000-000000000099",
        "ttl_seconds": 2,
    }
    response = await request_tools(bound, method="POST", path="/tools/operations/recover", body=payload)
    assert response.status_code == 200 and response.json()["status"] == 200
    assert len(relay) == 1
    forwarded = relay[0]
    assert str(forwarded.url) == chat_tools.FACADE_URL + "/operations/recover"
    assert json.loads(forwarded.content) == payload
    assert all(forwarded.headers[name] == value for name, value in proofs(bound).items())
    assert "authorization" not in forwarded.headers and "x-adp-run-credential" not in forwarded.headers
    # The facade's existing recovery callback rechecks the same Crew authority,
    # bypassing only new-work consent and budget admission.
    recovered = api.post(
        "/internal/v1/tools/authorize", headers=dict(forwarded.headers), json={"schema_version": "1.0", "action": "recover", "tool": payload["tool"]}
    )
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["run_id"] == bound.run_id
    assert recovered.json()["workload_id"] == bound.state["pod"].uid
    assert recovered.json()["principal"]["user_id"] == "human"
    policy_read.assert_not_called()
    budget.assert_not_awaited()
    worker.authenticate.assert_not_called()


@pytest.mark.parametrize(
    "method,path",
    [
        ("GET", "/tools/operations/recover"),
        ("POST", "/tools/operations/recover/other"),
        ("POST", "/tools/operations/recover/"),
        ("POST", "/tools/operations/%72ecover"),
        ("POST", "/tools/operations/../recover"),
        ("POST", "https://attacker.invalid/tools/operations/recover"),
    ],
)
async def test_request_recovery_route_cannot_expand_chat_bridge(bound, relay, method, path):
    response = await request_tools(bound, method=method, path=path)
    assert response.status_code == 400 and not relay
