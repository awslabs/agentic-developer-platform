"""HTTP contract, scope and verified-attempt routing over real T1 transactions."""

import os

os.environ.setdefault("BG_TOKEN_SECRET_KEY", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=")
import uuid
from datetime import timedelta
from types import SimpleNamespace

import pytest
from . import test_store as t1_fixtures
from fastapi import FastAPI
from fastapi.testclient import TestClient
from .test_store import NOW, _request
from .test_task_commands import final_body, running

from src.agentauth.routes import require_agent_transport
from src.shared.database import get_db
from src.tasks import authz
from src.tasks import command_routes as routes
from src.tasks.task_commands import TaskCommands

client = t1_fixtures.client
store = t1_fixtures.store


@pytest.fixture
def api(store, monkeypatch):
    req = _request()
    store.accept(req)
    identity = running(store, req)
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "true")
    caller = authz.Caller(principal_id=req.canonical_principal, tenant_id=req.tenant, scopes=frozenset({"adp-tasks/input", "adp-tasks/cancel"}))
    monkeypatch.setattr(authz, "authenticate", lambda request: (SimpleNamespace(expires_at=NOW + timedelta(minutes=5)), caller.scopes))

    async def resolve(*args):
        return caller

    monkeypatch.setattr(authz, "resolve_caller", resolve)
    monkeypatch.setattr(authz, "authorize_task", lambda *args: None)
    monkeypatch.setattr(routes, "get_store", lambda: SimpleNamespace(repository=store))
    from src.agentauth import task_runtime_routes

    async def verified(request):
        return identity

    monkeypatch.setattr(task_runtime_routes, "authenticate_task_attempt", verified)
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[require_agent_transport] = lambda: None
    return TestClient(app), req, identity, caller


def test_http_message_duplicate_and_cancel_preserve_receipts(api, store):
    web, req, identity, _ = api
    body = {"schema_version": "1.0", "command_id": str(uuid.uuid4()), "text": "Additional facts"}
    url = f"/v1/tasks/{req.task_id}/messages"
    first = web.post(url, json=body)
    assert first.status_code == 202, first.text
    assert first.json()["status"] == "accepted"
    assert web.post(url, json=body).json() == first.json()
    response = web.post(f"/v1/tasks/{req.task_id}/cancel", json={"schema_version": "1.0", "command_id": str(uuid.uuid4())})
    assert response.status_code == 202, response.text
    assert store.read_task(req.task_id)["state"] == "cancel_requested"
    assert web.post(url, json=body | {"command_id": str(uuid.uuid4())}).status_code == 409


def test_authority_fields_and_duplicate_json_keys_are_rejected(api, store):
    web, req, *_ = api
    url = f"/v1/tasks/{req.task_id}/messages"
    body = {"schema_version": "1.0", "command_id": str(uuid.uuid4()), "text": "approve this", "grant": "escalate"}
    assert web.post(url, json=body).status_code == 400
    assert web.post(url, content='{"text":"a","text":"b"}', headers={"Content-Type": "application/json"}).status_code == 400
    assert TaskCommands(store).commands(req.task_id) == []


def test_scope_is_required_before_writing(api, store, monkeypatch):
    web, req, identity, caller = api

    async def resolve(*args):
        return authz.Caller(principal_id=caller.principal_id, tenant_id=caller.tenant_id, scopes=frozenset({"adp-tasks/read"}))

    monkeypatch.setattr(authz, "resolve_caller", resolve)
    response = web.post(f"/v1/tasks/{req.task_id}/messages", json={"schema_version": "1.0", "command_id": str(uuid.uuid4()), "text": "x"})
    assert response.status_code == 403, response.text
    assert TaskCommands(store).commands(req.task_id) == []


def test_control_body_cannot_change_verified_attempt(api):
    web, req, identity, _ = api
    binding = {
        "run": {key: getattr(identity, key) for key in ("task_id", "invocation_id", "generation")},
        "runtime_attempt_id": identity.runtime_attempt_id,
    }
    body = {"schema_version": "1.0", "attempt": binding, "last_receipt_cursor": None}
    assert web.post("/internal/v1/agent/task/control", json=body).status_code == 200
    body["attempt"]["runtime_attempt_id"] = str(uuid.uuid4())
    assert web.post("/internal/v1/agent/task/control", json=body).status_code == 404


def test_finalization_requires_actual_typed_exit_evidence(api):
    web, req, identity, _ = api
    body = final_body(identity)
    body["child_exit"]["confirmed"] = False
    assert web.post("/internal/v1/agent/task/finalize", json=body).status_code == 400
    body["child_exit"].update(confirmed=True, exit_code=None, signal=None)
    assert web.post("/internal/v1/agent/task/finalize", json=body).status_code == 409


def test_canonical_internal_requests_validate_without_translation():
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    catalog = json.loads((root / "docs/task-api/contracts/v1/fixtures/valid/internal-adapter-catalog.json").read_text())
    for name, model in [("control", routes.Control), ("finalize", routes.Finalize), ("settlement", routes.Settlement)]:
        model.model_validate(catalog[name]["request"])


def test_explicit_null_and_numeric_boolean_are_not_contract_values(api):
    web, req, identity, _ = api
    message = {"schema_version": "1.0", "command_id": str(uuid.uuid4()), "text": "facts", "reply_to": None}
    assert web.post(f"/v1/tasks/{req.task_id}/messages", json=message).status_code == 400
    body = final_body(identity)
    body["child_exit"]["confirmed"] = 1
    assert web.post("/internal/v1/agent/task/finalize", json=body).status_code == 400
