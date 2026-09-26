"""Exercise owner-only Task reads through actual human Activity routes."""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.activity import routes, task_readthrough
from src.tasks import authz, errors
from src.tasks.read_store import InMemoryTaskStore, TaskRecord

TASK = "tsk_12345678-1234-4123-8123-123456789abc"
INVOCATION = "682cfdbe-006f-4ac3-b23f-56175ddca882"
PRINCIPAL = "human:12345678-1234-4123-8123-123456789abc"
URL = "/me/agent-invocations/" + INVOCATION


@pytest.fixture
def bridge(monkeypatch, regular_user):
    record = TaskRecord(
        TASK,
        INVOCATION,
        regular_user.org_id,
        PRINCIPAL,
        "agent-task-claude-developer",
        "running",
        4,
        "2026-09-26T00:00:00Z",
        "2026-09-26T00:01:00Z",
        "2026-09-26T01:00:00Z",
        execution_health="unknown",
        recovery_required=True,
        latest_sequence=3,
        oldest_sequence=1,
    )
    store = InMemoryTaskStore()
    store.tasks[TASK] = record
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "true")
    authenticate = MagicMock(return_value=(regular_user, frozenset({authz.SCOPE_READ})))
    monkeypatch.setattr(authz, "authenticate", authenticate)
    caller = AsyncMock(return_value=authz.Caller(PRINCIPAL, regular_user.org_id, frozenset({authz.SCOPE_READ})))
    monkeypatch.setattr(authz, "resolve_caller", caller)
    monkeypatch.setattr(task_readthrough, "get_store", lambda: store)
    monkeypatch.setattr(routes, "resolve_canonical_user_id", AsyncMock(return_value=PRINCIPAL[6:]))
    monkeypatch.setattr(routes, "get_cost_by_run_ids", AsyncMock(return_value={}))
    service = MagicMock()
    service.get_invocation.return_value = None
    app = FastAPI()
    app.include_router(routes.router)
    app.dependency_overrides[routes.get_current_user] = lambda: regular_user
    app.dependency_overrides[routes.get_db] = lambda: None
    app.dependency_overrides[routes.get_activity_service] = lambda: service
    with TestClient(app) as client:
        yield client, store, caller, authenticate, service


def test_exact_identity_and_unknown_liveness(bridge):
    client, *_ = bridge
    response = client.get(URL)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["invocation_id"] == INVOCATION and body["task_id"] == TASK
    assert body["source_type"] == "task" and body["status"] == "in_progress"
    assert body["liveness"] == "unverifiable" and body["transcript_status"] == "pending"
    assert body["transcript_key"] is None
    assert body["task_snapshot"]["execution_health"] == "unknown"
    assert body["task_snapshot"]["recovery_required"] is True
    assert client.get(URL + "/transcript").status_code == 404


@pytest.mark.parametrize("status,mapped", [("completed", "complete"), ("failed", "failed"), ("cancelled", "aborted")])
def test_terminal_report_is_labelled_and_fenced(bridge, status, mapped):
    client, store, *_ = bridge
    store.tasks[TASK] = replace(store.tasks[TASK], status=status, result={"report": {"text": "```injected"}})
    body = client.get(URL).json()
    assert body["status"] == mapped and body["liveness"] == "unverifiable"
    assert body["transcript_status"] == "available"
    report = client.get(URL + "/transcript")
    assert report.status_code == 200
    assert "not a full native agent transcript" in report.text
    assert "````json" in report.text


@pytest.mark.parametrize("field,value", [("owner_principal_id", "human:other"), ("tenant_id", "other")])
def test_foreign_task_hidden(bridge, field, value):
    client, store, *_ = bridge
    store.tasks[TASK] = replace(store.tasks[TASK], **{field: value})
    assert client.get(URL + "?tenant_id=other&user_id=other").status_code == 404
    assert client.get(URL + "/transcript").status_code == 404


@pytest.mark.parametrize("principal,scopes,status", [("svc-owner", {authz.SCOPE_READ}, 404), (PRINCIPAL, set(), 403)])
def test_service_and_scope_refusals(bridge, principal, scopes, status):
    client, _, caller, *_ = bridge
    caller.return_value = authz.Caller(principal, "org-tenant-001", frozenset(scopes))
    assert client.get(URL).status_code == status


def test_disabled_and_malformed_never_authenticate(bridge, monkeypatch):
    client, _, _, authenticate, _ = bridge
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "false")
    assert client.get(URL).status_code == 503
    assert client.get("/me/agent-invocations/malformed").status_code == 404
    authenticate.assert_not_called()


def test_policy_revocation_and_storage_failure(bridge, monkeypatch):
    client, store, *_ = bridge
    monkeypatch.setattr(store, "require_policy", MagicMock(side_effect=errors.disallowed_scope("revoked")))
    assert client.get(URL).status_code == 403
    store.fail = True
    assert client.get(URL).status_code == 503


def test_stale_binding_hidden(bridge, monkeypatch):
    client, store, *_ = bridge
    monkeypatch.setattr(store, "resolve_invocation", lambda **kw: (TASK, 2))
    assert client.get(URL).status_code == 404


def test_unknown_status_fails_closed(bridge):
    client, store, *_ = bridge
    store.tasks[TASK] = replace(store.tasks[TASK], status="invented")
    assert client.get(URL).status_code == 503


def test_legacy_row_independent_of_task_flag(bridge, monkeypatch):
    client, store, _, authenticate, service = bridge
    from starlette.requests import Request

    service.get_invocation.return_value = task_readthrough.detail(store.tasks[TASK], Request({"type": "http", "headers": []}))
    monkeypatch.setenv("ADP_TASK_API_READ_ENABLED", "false")
    assert client.get(URL).status_code == 200
    authenticate.assert_not_called()


@pytest.mark.parametrize("field", ["result", "error"])
def test_malformed_report_returns_dependency_failure(bridge, field):
    client, store, *_ = bridge
    store.tasks[TASK] = replace(store.tasks[TASK], status="completed", **{field: "corrupt"})
    assert client.get(URL).status_code == 503
    assert client.get(URL + "/transcript").status_code == 503


def test_store_initialization_failure_returns_503(bridge, monkeypatch):
    from botocore.exceptions import NoCredentialsError

    client, *_ = bridge
    monkeypatch.setattr(task_readthrough, "get_store", MagicMock(side_effect=NoCredentialsError()))
    assert client.get(URL).status_code == 503
