# ruff: noqa: F811
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agentauth import task_dispatch_routes
from src.agentauth.task_agent_runtime import get_task_agent_runtime as get_agent_runtime
from src.agentauth.task_dispatch_routes import router, work_store
from src.agentauth.task_work import TaskWorkStore, work_shard
from tests.tasks.test_store import AUTHORITY_TABLE, NOW, TABLE, _request, client, store  # noqa: F401

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "modules/agent-factory/webhook-ingress/lambda"))
sys.path.insert(0, str(ROOT / "scripts/task-api"))
from _schema import Registry, validate  # noqa: E402
from common import task_dispatch, task_publisher  # noqa: E402

TASK = "tsk_3d5f8a10-2b4c-4e6f-9a81-7c3e5d9f1b20"
DISPATCH = "b5e9835b-fc24-4231-96f2-e8b8ca3681be"
INVOCATION = "5e7a9c31-4d6f-4813-ba25-9c1e3f5a7d40"
TENANT = "tenant-a"
SCHEMAS = Registry(ROOT / "docs/task-api/contracts/v1/schemas")
ADAPTER_SCHEMA = SCHEMAS.docs["internal-adapters.schema.json"]


def envelope():
    return _request(task_id=TASK, invocation_id=INVOCATION, dispatch_id=DISPATCH).envelope


def assert_schema(value, definition):
    errors = validate(
        value, ADAPTER_SCHEMA["$defs"][definition], SCHEMAS,
        "internal-adapters.schema.json",
    )
    assert errors == []


@pytest.fixture
def seam(monkeypatch, client, store):
    dynamodb = client
    store.accept(_request(task_id=TASK, invocation_id=INVOCATION, dispatch_id=DISPATCH))
    clock = [NOW.timestamp()]
    store = TaskWorkStore(
        dynamodb_client=dynamodb, table_name=TABLE, authority_table_name=AUTHORITY_TABLE, clock=lambda: clock[0],
    )
    env = {
        "ADP_TASK_API_ADMISSION_ENABLED": "true", "ADP_TASK_API_RECOVERY_ENABLED": "true",
        "ADP_TASK_DISPATCH_PRODUCER_ROLES": "dispatch-role",
        "ADP_TASK_RECOVERY_PRODUCER_ROLES": "recovery-role",
        "WEBHOOK_EVENTS_TABLE": "requests", "AGENT_AUTHORITY_TABLE": "authority",
    }
    runtime = SimpleNamespace(env=env, store=SimpleNamespace(client=dynamodb, table="authority"))
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    app.dependency_overrides[work_store] = lambda: store

    async def authenticate(proof, identity, *, allowed_roles):
        assert proof == "fixture-proof"
        return next(iter(allowed_roles))

    monkeypatch.setattr(task_dispatch_routes, "verify_producer", authenticate)
    with TestClient(app) as client:
        yield SimpleNamespace(client=client, store=store, dynamodb=dynamodb, clock=clock)

def test_closed_routes_reject_caller_task_or_tenant_fields(seam):
    response = seam.client.post("/internal/v1/tasks/dispatch/claim", json={
        "schema_version": "1.0", "dispatch_id": DISPATCH,
        "task_id": TASK, "producer_proof": "fixture-proof",
    })
    assert response.status_code == 422
    assert seam.client.post("/internal/v1/agent/task-dispatch/claim", json={}).status_code == 404


def test_dispatch_and_recovery_authentication_use_separate_allowlists(seam, monkeypatch):
    calls = []

    async def authenticate(proof, identity, *, allowed_roles):
        calls.append((identity, allowed_roles))
        return "ok"

    monkeypatch.setattr(task_dispatch_routes, "verify_producer", authenticate)
    dispatch = seam.client.post("/internal/v1/tasks/dispatch/claim", json={
        "schema_version": "1.0", "dispatch_id": DISPATCH, "producer_proof": "fixture-proof",
    })
    assert dispatch.status_code == 200
    recovery = seam.client.post("/internal/v1/tasks/recovery/claim", json={
        "schema_version": "1.0", "shard": work_shard(TASK), "cursor": None,
        "limit": 100, "producer_proof": "fixture-proof",
    })
    assert recovery.status_code == 200
    assert calls == [(DISPATCH, {"dispatch-role"}), (work_shard(TASK), {"recovery-role"})]


def install_gateway_seam(monkeypatch, seam, requests_seen):
    mapping = {
        "/internal/v1/tasks/dispatch/claim": ("dispatch_claim_request", "dispatch_claim_response"),
        "/internal/v1/tasks/dispatch/settle": ("dispatch_settle_request", "dispatch_settle_response"),
        "/internal/v1/tasks/recovery/claim": ("recovery_claim_request", "recovery_claim_response"),
        "/internal/v1/tasks/recovery/settle": ("recovery_settle_request", "recovery_settle_response"),
    }

    def bridge(path, body, *, identity):
        request = {**body, "producer_proof": "fixture-proof"}
        assert_schema(request, mapping[path][0])
        requests_seen.append((path, body, identity))
        response = seam.client.post(path, json=request)
        if response.status_code != 200:
            return None
        value = response.json()
        assert_schema(value, mapping[path][1])
        return value

    monkeypatch.setattr(task_dispatch, "_call_gateway", bridge)


class Sqs:
    def __init__(self, outcomes=None):
        self.calls = []
        self.outcomes = list(outcomes or ["sqs-message-1"])

    def send_message(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return {"MessageId": outcome}


def test_actual_gateway_claim_flows_through_actual_lambda_publisher(seam, monkeypatch):
    requests_seen = []
    install_gateway_seam(monkeypatch, seam, requests_seen)
    sqs = Sqs()
    monkeypatch.setattr(task_publisher, "_sqs", sqs)
    monkeypatch.setenv("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/tasks.fifo")

    result = task_dispatch.publish_dispatch(DISPATCH)

    assert result["publication_outcome"] == "confirmed"
    assert result["settled"] is True
    assert seam.store.task_status(TASK) == "queued"
    assert json.loads(sqs.calls[0]["MessageBody"]) == envelope()
    assert sqs.calls[0]["MessageDeduplicationId"] == DISPATCH
    assert sqs.calls[0]["MessageGroupId"] == task_publisher.message_group_id(TENANT, TASK)
    assert [entry[0] for entry in requests_seen] == [
        "/internal/v1/tasks/dispatch/claim", "/internal/v1/tasks/dispatch/settle",
    ]


def test_actual_recovery_claim_flows_through_publisher_and_both_settlements(seam, monkeypatch):
    requests_seen = []
    install_gateway_seam(monkeypatch, seam, requests_seen)
    sqs = Sqs()
    monkeypatch.setattr(task_publisher, "_sqs", sqs)
    monkeypatch.setenv("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/tasks.fifo")
    response = seam.client.post("/internal/v1/tasks/recovery/claim", json={
        "schema_version": "1.0", "shard": work_shard(TASK), "cursor": None,
        "limit": 100, "producer_proof": "fixture-proof",
    })
    assert response.status_code == 200
    assert_schema(response.json(), "recovery_claim_response")

    outcome = task_dispatch._recover_one(response.json()["work"][0])

    assert outcome == "confirmed"
    assert seam.store.task_status(TASK) == "queued"
    assert [entry[0] for entry in requests_seen] == [
        "/internal/v1/tasks/dispatch/claim",
        "/internal/v1/tasks/dispatch/settle",
        "/internal/v1/tasks/recovery/settle",
    ]


def test_unknown_send_recovery_reuses_exact_body_and_id(seam, monkeypatch):
    requests_seen = []
    install_gateway_seam(monkeypatch, seam, requests_seen)
    sqs = Sqs([TimeoutError("ambiguous"), "sqs-message-retry"])
    monkeypatch.setattr(task_publisher, "_sqs", sqs)
    monkeypatch.setenv("SUBMIT_QUEUE_URL", "https://sqs.us-east-1.amazonaws.com/123/tasks.fifo")

    first = task_dispatch.publish_dispatch(DISPATCH)
    second = task_dispatch.publish_dispatch(DISPATCH)

    assert first["publication_outcome"] == "unknown"
    assert second["publication_outcome"] == "confirmed"
    assert [call["MessageBody"] for call in sqs.calls] == [sqs.calls[0]["MessageBody"]] * 2
    assert [call["MessageDeduplicationId"] for call in sqs.calls] == [DISPATCH, DISPATCH]


def test_recovery_settlement_requires_committed_publication_evidence(seam):
    claim = seam.client.post("/internal/v1/tasks/recovery/claim", json={
        "schema_version": "1.0", "shard": work_shard(TASK), "cursor": None,
        "limit": 100, "producer_proof": "fixture-proof",
    }).json()["work"][0]
    response = seam.client.post("/internal/v1/tasks/recovery/settle", json={
        "schema_version": "1.0", "work_id": DISPATCH, "lease_token": claim["lease_token"],
        "evidence": {"kind": "publication", "observed": True, "observed_at": "2026-09-24T14:42:04Z"},
        "producer_proof": "fixture-proof",
    })
    assert response.status_code == 409
    assert seam.store.task_status(TASK) == "accepted"
