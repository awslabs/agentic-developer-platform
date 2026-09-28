"""Real HTTP validation and durable tool journal; IAM/attempt proof are fixtures."""

# ruff: noqa: F811
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agentauth import task_tool_routes as routes
from src.agentauth.task_tool_policy import codex_tool_name
from tests.agentauth.test_task_tool_receipts import journal  # noqa: F401
from tests.tasks.test_store import client, store  # noqa: F401

PATH = "/internal/v1/agent/task/tool-operation"
JOURNAL_FACTORY = routes.tool_journal


@pytest.fixture
def api(journal, monkeypatch):
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.require_agent_transport] = lambda: None
    monkeypatch.setattr(routes, "authenticate_task_attempt", AsyncMock(return_value=journal.identity))
    monkeypatch.setattr(routes, "tool_journal", lambda identity: journal.service)
    identity = journal.identity
    body = {
        "schema_version": "1.0",
        "action": "claim",
        "attempt": {
            "run": {"task_id": identity.task_id, "invocation_id": identity.invocation_id, "generation": identity.generation},
            "runtime_attempt_id": identity.runtime_attempt_id,
        },
        "turn_id": journal.claim["turn_id"],
        "call_id": "call_1",
        "tool": journal.claim["tool"],
        "arguments": {"number": 1},
    }
    with TestClient(app) as http:
        yield http, body, journal


def test_http_claim_settle_keeps_owner_out_of_public_receipts_and_replays(api):
    http, body, journal = api
    first = http.post(PATH, json=body)
    assert first.status_code == 200
    claim = first.json()
    assert claim["created"] is True
    owner = claim["owner_token"]
    assert "owner_token" not in claim["receipt"]
    assert "arguments_json" not in claim["receipt"]
    replay = http.post(PATH, json=body).json()
    assert replay["created"] is False and "owner_token" not in replay
    settled = http.post(
        PATH,
        json={
            "schema_version": "1.0",
            "action": "settle",
            "attempt": body["attempt"],
            "call_id": "call_1",
            "owner_token": owner,
            "status": "confirmed",
            "content": "verified result",
        },
    )
    assert settled.status_code == 200
    assert settled.json()["receipt"]["content"] == "verified result"
    assert owner not in settled.text
    assert journal.service.read(journal.identity.task_id, "call_1")["operation_status"] == "confirmed"


@pytest.mark.parametrize(
    "field,value", [("repository", "other/repo"), ("endpoint", "https://other.invalid"), ("cleanup", True), ("owner_token", "injected")]
)
def test_claim_schema_refuses_authority_or_transport_overrides(api, field, value):
    http, body, journal = api
    assert http.post(PATH, json={**body, field: value}).status_code == 422
    assert journal.service.read(journal.identity.task_id, "call_1") is None


def test_claim_requires_authenticated_attempt_and_current_permission(api):
    http, body, journal = api
    other = deepcopy(body)
    other["attempt"]["run"]["generation"] += 1
    assert http.post(PATH, json=other).status_code == 404
    journal.policy["allowed_tools"] = []
    assert http.post(PATH, json=body).status_code == 403
    assert journal.service.read(journal.identity.task_id, "call_1") is None


def test_unverified_transport_cannot_reach_tool_journal(api):
    _, body, journal = api
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as http:
        assert http.post(PATH, json=body).status_code == 403
    assert journal.service.read(journal.identity.task_id, "call_1") is None


def test_settlement_cannot_inject_success_before_claim(api):
    http, body, journal = api
    response = http.post(
        PATH,
        json={
            "schema_version": "1.0",
            "action": "settle",
            "attempt": body["attempt"],
            "call_id": "call_1",
            "owner_token": body["attempt"]["runtime_attempt_id"],
            "status": "confirmed",
            "content": "forged",
        },
    )
    assert response.status_code == 409
    assert journal.service.read(journal.identity.task_id, "call_1") is None


def test_permission_names_have_stable_distinct_bounded_sdk_identifiers():
    for permission in ["repository.read_change", "a_b.c", "a.b_c", "a" * 48 + "." + "b" * 64]:
        name = codex_tool_name(permission)
        assert name.startswith("adp_")
        assert len(name) <= 64
    assert codex_tool_name("a_b.c") != codex_tool_name("a.b_c")
    assert codex_tool_name("repository.read_change") == "adp_75ac58ef0e809a16d800561c0d85bc1bbbd842ae3416488c2b846848"


def test_route_factory_uses_frozen_tool_permissions_for_sdk_name_binding(api, monkeypatch):
    from src.tasks.store import _serialize

    http, body, journal = api
    monkeypatch.setattr(routes, "tool_journal", JOURNAL_FACTORY)
    monkeypatch.setattr(routes, "get_task_agent_runtime", lambda: object())
    monkeypatch.setattr(routes, "task_runtime", lambda runtime: SimpleNamespace(repository=journal.store))
    monkeypatch.setattr(routes, "TaskServicePolicyStore", lambda **kwargs: SimpleNamespace(get=lambda **kwargs: journal.policy))
    monkeypatch.setenv("ADP_TASK_PERSONA_TOOLS", '{"agent-task-investigator":["repository.read_change"]}')
    model = deepcopy(journal.model)
    model["responses_response"]["output"][0]["name"] = codex_tool_name(body["tool"])
    journal.store._client.put_item(TableName=journal.store.table_name, Item=_serialize(model))
    result = http.post(PATH, json=body)
    assert result.status_code == 200
    assert result.json()["created"] is True


@pytest.mark.parametrize("scenario", ["success", "lost_claim", "lost_settlement", "tool_unknown", "wrong_receipt"])
def test_worker_claim_execute_settle_with_real_http_journal(api, scenario):
    import sys
    import uuid
    from pathlib import Path
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[4] / "modules/agent-factory/agent-worker-image"))
    from lib.task_host import TaskHost
    from lib.task_protocol import validate_child_frame
    from lib.task_run_client import TaskRunClientError

    http, body, journal = api
    effects = []
    lost = False

    class WorkerClient:
        def tool_operation(self, request):
            nonlocal lost
            response = http.post(PATH, json=request)
            if response.status_code != 200:
                raise TaskRunClientError("journal refused")
            result = response.json()
            if scenario == "wrong_receipt":
                result["receipt"]["request_digest"] = "f" * 64
            if not lost and (
                (scenario == "lost_claim" and request["action"] == "claim") or (scenario == "lost_settlement" and request["action"] == "settle")
            ):
                lost = True
                raise TaskRunClientError("fixture lost response")
            return result

        def tool(self, name, request):
            effects.append(name)
            if scenario == "tool_unknown":
                raise TaskRunClientError("private-provider-detail")
            return {
                "schema_version": "1.0",
                "task_id": journal.identity.task_id,
                "operation_id": request["operation_id"],
                "operation_status": "confirmed",
                "result": {"verified": True},
                "artifact": {
                    "artifact_id": "art_" + str(uuid.uuid4()),
                    "content_type": "application/json",
                    "content_sha256": "a" * 64,
                    "byte_length": 18,
                },
            }

    worker = TaskHost(client=WorkerClient())
    assignment = SimpleNamespace(task_id=journal.identity.task_id)
    frame = {
        "protocol_version": 1,
        "type": "tool.request",
        "request_id": str(uuid.uuid4()),
        "task_id": assignment.task_id,
        "tool": body["tool"],
        "payload": body["arguments"],
        "model_call": {"turn_id": body["turn_id"], "call_id": body["call_id"]},
    }
    validate_child_frame(frame, assignment.task_id)
    if scenario in {"lost_claim", "wrong_receipt"}:
        with pytest.raises(TaskRunClientError):
            worker._cyber(assignment, body["attempt"], frame)
        assert effects == []
        if scenario == "lost_claim":
            assert worker._cyber(assignment, body["attempt"], frame)["operation_status"] == "unknown"
        return
    first = worker._cyber(assignment, body["attempt"], frame)
    assert first["operation_status"] == ("confirmed" if scenario == "success" else "unknown")
    again = worker._cyber(assignment, body["attempt"], {**frame, "request_id": str(uuid.uuid4())})
    assert again["operation_status"] == ("unknown" if scenario == "tool_unknown" else "confirmed")
    assert len(effects) == 1
    assert "owner_token" not in str(first) + str(again)
    assert "private-provider-detail" not in str(first) + str(again)
    if again["operation_status"] == "confirmed":
        assert again["result"] == {"verified": True}
        assert again["content"] == journal.service.read(assignment.task_id, "call_1")["content"]
