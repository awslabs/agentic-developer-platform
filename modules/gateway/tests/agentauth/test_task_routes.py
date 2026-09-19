"""Task acquisition and bootstrap exercised through real routes and TokenReview."""

import json
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.routes import AgentRuntime, get_agent_runtime
from src.agentauth.routes import router as bootstrap_router
from src.agentauth.task_routes import router, task_delivery
from tests.agentauth.test_bootstrap_routes import ENV, kubernetes, provision, store  # noqa: F401

HEADERS = {"X-Caller-Identity": "registered-worker", "X-Adp-Workload-Token": "pod-token"}
BASE = "/internal/v1/agent/task"


@pytest.fixture
def task_http(store, kubernetes, monkeypatch):  # noqa: F811 - shared fixtures
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="tasks")["QueueUrl"]
    envelope, _ = provision(store)
    provision(store, invocation="run-b")
    sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(envelope))
    runtime = AgentRuntime(store=store, workloads=kubernetes[0], env={**ENV, "ADP_RUN_TASKS_ENABLED": "true", "ADP_RUN_TASK_QUEUE_URL": queue})
    delivery = task_delivery(runtime)
    app = FastAPI()
    app.include_router(router)
    app.include_router(bootstrap_router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    app.dependency_overrides[task_delivery] = lambda: delivery
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    return TestClient(app), runtime, delivery, envelope, kubernetes


def test_acquire_then_bootstrap_only_the_server_assigned_envelope(task_http):
    client, runtime, delivery, envelope, _ = task_http
    body = {"invocation_id": "run-a", "envelope_digest": envelope_digest(envelope)}
    denied = client.post("/internal/v1/agent/bootstrap", headers=HEADERS, json=body)
    assert denied.status_code == 404
    assert runtime.store._read("POD#pod-a", "BINDING") is None
    acquired = client.post(BASE + "/acquire", headers=HEADERS, json={})
    assert acquired.status_code == 200
    assert set(acquired.json()) == {"body"}
    assert json.loads(acquired.json()["body"]) == envelope
    assert delivery.read("pod-a")["receipt"] not in acquired.text
    assert acquired.headers["cache-control"] == "no-store"
    accepted = client.post("/internal/v1/agent/bootstrap", headers=HEADERS, json=body)
    assert accepted.status_code == 200
    denied = client.post("/internal/v1/agent/bootstrap", headers=HEADERS, json={**body, "invocation_id": "run-b"})
    assert denied.status_code == 404


@pytest.mark.parametrize("action", ["acquire", "heartbeat", "ack"])
@pytest.mark.parametrize("field", ["pod_uid", "tenant_id", "invocation_id", "queue_url", "receipt_handle"])
def test_worker_cannot_address_another_assignment(task_http, action, field):
    client, _, delivery, _, _ = task_http
    response = client.post(BASE + "/" + action, headers=HEADERS, json={field: "victim"})
    assert response.status_code == 422
    assert delivery.read("pod-a") is None


def test_deleted_pod_after_receive_does_not_receive_private_body(task_http, monkeypatch):
    client, _, delivery, _, kube = task_http
    original = delivery.acquire

    def acquire(uid):
        result = original(uid)
        kube[1]["deleted"] = True
        return result

    monkeypatch.setattr(delivery, "acquire", acquire)
    response = client.post(BASE + "/acquire", headers=HEADERS, json={})
    assert response.status_code == 404
    assert "body" not in response.json()


@pytest.mark.parametrize("headers", [{}, {"X-Caller-Identity": "worker"}, {"X-Adp-Workload-Token": "pod-token"}])
def test_transport_and_workload_proofs_are_both_required(task_http, headers):
    client, _, delivery, _, _ = task_http
    response = client.post(BASE + "/acquire", headers=headers, json={})
    assert response.status_code in (403, 404)
    assert delivery.read("pod-a") is None


def test_task_service_feature_flag_defaults_off(task_http):
    _, runtime, _, _, _ = task_http
    runtime.env.pop("ADP_RUN_TASKS_ENABLED")
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as error:
        task_delivery(runtime)
    assert error.value.status_code == 503
