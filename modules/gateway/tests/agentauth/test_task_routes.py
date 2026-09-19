"""Task acquisition and bootstrap exercised through real routes and TokenReview."""

import json
from dataclasses import replace
from unittest.mock import AsyncMock

import boto3
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agentauth.bootstrap import envelope_digest
from src.agentauth.routes import AgentRuntime, get_agent_runtime
from src.agentauth.routes import router as bootstrap_router
from src.agentauth.task_routes import router, task_delivery
from tests.agentauth.test_bootstrap_routes import ENV, kubernetes, lifecycle_job, provision, store  # noqa: F401

HEADERS = {"X-Caller-Identity": "registered-worker", "X-Adp-Workload-Token": "pod-token"}
BASE = "/internal/v1/agent/task"


@pytest.fixture
def task_http(store, kubernetes, monkeypatch, report_only_db):  # noqa: F811 - shared fixtures
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
    from src.shared.database import get_db

    app.dependency_overrides[get_db] = report_only_db
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


@pytest.mark.parametrize("action", ["acquire", "heartbeat", "ack"])
@pytest.mark.parametrize("job_status", [500, 200])
def test_optional_deadline_change_does_not_refuse_a_committed_task(task_http, monkeypatch, action, job_status):
    client, runtime, delivery, envelope, kube = task_http
    state = lifecycle_job(kube)
    state["job_status"] = 200 if job_status == 500 else 500
    before = runtime.workloads.verify("pod-token")
    assert (before.deadline_at is not None) == (job_status == 500)
    if action != "acquire":
        assert client.post(BASE + "/acquire", headers=HEADERS, json={}).status_code == 200
    method = "acquire" if action == "acquire" else "maintain"
    original = getattr(delivery, method)

    def change_optional_observation(*args, **kwargs):
        result = original(*args, **kwargs)
        state["job_status"] = job_status
        return result

    monkeypatch.setattr(delivery, method, change_optional_observation)
    response = client.post(BASE + "/" + action, headers=HEADERS, json={})
    assert response.status_code == 200
    assert runtime.workloads.verify("pod-token").deadline_at != before.deadline_at
    row = delivery.read("pod-a")
    assert row["invocation_id"] == "run-a"
    assert row["state"] == ("acknowledged" if action == "ack" else "assigned")
    if action == "acquire":
        assert json.loads(response.json()["body"]) == envelope
        # A lost response still returns this exact assignment, never a new task.
        assert client.post(BASE + "/acquire", headers=HEADERS, json={}).json() == response.json()
    else:
        assert response.json() == {"accepted": True}
    if action == "ack":
        assert client.post(BASE + "/ack", headers=HEADERS, json={}).status_code == 200
        assert client.post(BASE + "/acquire", headers=HEADERS, json={}).status_code == 404


@pytest.mark.parametrize("action", ["acquire", "heartbeat", "ack"])
@pytest.mark.parametrize("field", ["uid", "name", "namespace", "service_account", "ip"])
def test_task_final_identity_fence_still_rejects_replacement(task_http, monkeypatch, action, field):
    client, runtime, delivery, _, _ = task_http
    if action != "acquire":
        assert client.post(BASE + "/acquire", headers=HEADERS, json={}).status_code == 200
    verify = runtime.workloads.verify
    changed = False

    def verified_snapshot(token):
        pod = verify(token)
        return replace(pod, **{field: "replacement"}) if changed else pod

    method = "acquire" if action == "acquire" else "maintain"
    original = getattr(delivery, method)

    def replace_identity(*args, **kwargs):
        nonlocal changed
        result = original(*args, **kwargs)
        changed = True
        return result

    monkeypatch.setattr(runtime.workloads, "verify", verified_snapshot)
    monkeypatch.setattr(delivery, method, replace_identity)
    response = client.post(BASE + "/" + action, headers=HEADERS, json={})
    assert response.status_code == 404
    assert "body" not in response.json() and "accepted" not in response.json()
