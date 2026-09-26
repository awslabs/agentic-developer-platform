"""Public IAM admission cannot supply execution commands or private ownership."""

import json
from types import SimpleNamespace
import uuid

import pytest

from validation_tools import handler
from test_operations import inputs


@pytest.fixture
def service(jobs, monkeypatch):
    store, identity, task = jobs
    attempt, request, receipt = inputs(identity)
    task["repository_binding"] = {"binding": {"validation_checks": [{"name": "unit", "image": "registry.example/check@sha256:" + "a" * 64, "timeout_seconds": 120}]}}
    proof = {"x-adp-workload-token": "fixture", "x-adp-run-credential": "fixture"}
    authority = SimpleNamespace(headers=proof, close=lambda: None,
        authorize=lambda **kwargs: SimpleNamespace(identity=identity, task=task))
    monkeypatch.setattr(handler, "TaskAuthorityClient", lambda *args, **kwargs: authority)
    monkeypatch.setattr(handler, "OperationRepository", lambda *args: store.repo)
    deliveries = []
    def invoke(**kwargs):
        deliveries.append(json.loads(kwargs["Payload"]))
        return {"StatusCode": 202}
    monkeypatch.setattr(handler.boto3, "client", lambda name: SimpleNamespace(invoke=invoke))
    monkeypatch.setenv("ADP_VALIDATION_SERVICE_ENABLED", "true")
    monkeypatch.setenv("ADP_VALIDATION_TABLE", "fixture")
    monkeypatch.setenv("ADP_VALIDATION_WORKER_ROLES", "arn:aws:iam::123456789012:role/worker")
    body = {"schema_version": "1.0", "attempt": attempt, "operation": "run",
            "operation_id": str(uuid.uuid4()), "payload": request.model_dump()}
    event = {"httpMethod": "POST", "resource": "/tools/validation", "body": json.dumps(body), "headers": proof,
        "requestContext": {"identity": {"userArn": "arn:aws:sts::123456789012:assumed-role/worker/session"}}}
    return event, body, store, identity, deliveries


def call(event):
    return handler.lambda_handler(event, SimpleNamespace(invoked_function_arn="arn:aws:lambda:us-east-1:123456789012:function:validation"))


def test_admission_and_status_redelivery_preserve_one_job(service):
    event, body, store, identity, deliveries = service
    first = call(event)
    assert first["statusCode"] == 200, first
    assert json.loads(first["body"])["phase"] == "pending"
    assert call(event)["statusCode"] == 200
    assert len(deliveries) == 1
    assert deliveries[0]["operation_id"] == body["operation_id"]
    assert "owner_token" not in first["body"] and "fixture" not in first["body"]
    assert "headers" not in store.read(identity, body["operation_id"])


@pytest.mark.parametrize("mutation", ["caller", "command", "private", "payload"])
def test_public_boundary_refuses_untrusted_inputs(service, mutation):
    event, body, _, _, deliveries = service
    if mutation == "caller":
        event["requestContext"] = {}
    elif mutation == "command":
        body["payload"]["argv"] = ["sh"]
    elif mutation == "private":
        body["kind"] = "validation-job-v1"
    else:
        body["operation"] = "cancel_jobs"
    event["body"] = json.dumps(body)
    assert call(event)["statusCode"] in {403, 422}
    assert not deliveries


def test_cleanup_stays_available_when_new_work_disabled(service, monkeypatch):
    event, body, _, _, deliveries = service
    assert call(event)["statusCode"] == 200
    monkeypatch.setenv("ADP_VALIDATION_SERVICE_ENABLED", "false")
    assert call(event)["statusCode"] == 503
    body.update(operation="cancel_jobs", payload=None)
    event["body"] = json.dumps(body)
    response = call(event)
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["pending"] == []
    assert len(deliveries) == 1
