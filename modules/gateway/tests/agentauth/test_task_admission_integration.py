"""Lambda -> gateway HTTP -> admission -> DynamoDB/Redis integration.

Real ingress signing/byte preservation, proof binding, producer-role allowlist,
admission, task policy, strict budget scripts, acceptance and dispatch resolution
run together. External IAM/ST​S/Cognito/model readiness use explicit fixtures;
DynamoDB and Redis run against moto/fakeredis. This is not live AWS qualification.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import boto3
import fakeredis.aioredis
import httpx
import pytest
from botocore.credentials import Credentials
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.agentauth import task_admission_routes as route
from src.agentauth import task_dispatch_routes, work_routes
from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.task_admission import TaskAdmission
from src.agentauth.task_agent_runtime import get_task_agent_runtime as get_agent_runtime
from src.agentauth.task_budget import TaskBudget
from src.agentauth.task_service_policy import TaskServicePolicyStore
from src.agentauth.task_work import TaskWorkStore
from src.budget.reservations import ReservationStore
from src.shared.database import get_db
from src.tasks import authz
from src.tasks.records import idempotency_partition
from tests.tasks import test_store as storage_tests

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "modules/agent-factory/webhook-ingress/lambda"))
from common import task_dispatch  # noqa: E402
from task_api import admit_client, handler  # noqa: E402

ROLE = "arn:aws:iam::111122223333:role/task-ingress"


@pytest.fixture
def client():
    yield from storage_tests.client.__wrapped__()


@pytest.fixture
def store(client):
    return storage_tests.store.__wrapped__(client)


@pytest.fixture
def flow(client, store, monkeypatch):
    policies = TaskServicePolicyStore(table_name=store.authority_table_name, client=client, clock=lambda: storage_tests.NOW)
    client.delete_item(TableName=store.authority_table_name, Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "TASK_POLICY#svc-principal-1"}})
    policies.put(
        tenant_id="tenant-a",
        canonical_principal_id="svc-principal-1",
        expected_version=0,
        updated_by="fixture",
        policy={
            "status": "active",
            "allowed_personas": ["agent-task-investigator"],
            "task_scopes": ["submit"],
            "model_policy_version": "1",
            "limits": {"max_duration_minutes": 30, "max_turns": 8, "max_output_tokens_per_turn": 4096, "max_usd_per_task": 1},
        },
    )
    reservations = ReservationStore(
        redis_url=None, ttl_seconds=86400, clock=lambda: storage_tests.NOW.timestamp(), client=fakeredis.aioredis.FakeRedis(decode_responses=True)
    )
    budget = TaskBudget(
        BootstrapStore(table_name=store.authority_table_name, dynamodb_client=client),
        reservations=reservations,
        qualification_id="http-integration",
        clock=lambda: storage_tests.NOW,
    )
    model_calls = []

    async def model(*args, **kwargs):
        model_calls.append(kwargs)
        return storage_tests._request().model_binding

    service = TaskAdmission(store, policies=policies, budget=budget, model_resolver=model, clock=lambda: storage_tests.NOW)
    monkeypatch.setattr(route, "get_admission", lambda: service)

    from src.auth import caller_provenance

    monkeypatch.setattr(
        caller_provenance, "get_settings", lambda: SimpleNamespace(trust_apigw_headers=True, apigw_provenance_secret="task-test-edge-provenance")
    )

    def authenticate(request):
        assert request.headers["Authorization"] == "Bearer caller-access-token"
        return SimpleNamespace(), frozenset({"adp-tasks/submit"})

    async def resolve(context, scopes, db):
        return authz.Caller(tenant_id="tenant-a", principal_id="svc-principal-1", scopes=scopes)

    monkeypatch.setattr(authz, "authenticate", authenticate)
    monkeypatch.setattr(authz, "resolve_caller", resolve)
    sts_calls = []

    class StsClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, headers, content):
            sts_calls.append(headers)
            assert url == "https://sts.us-east-1.amazonaws.com/"
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                content=b"<GetCallerIdentityResponse><GetCallerIdentityResult><Arn>arn:aws:sts::111122223333:assumed-role/task-ingress/fixture</Arn></GetCallerIdentityResult></GetCallerIdentityResponse>",
            )

    monkeypatch.setattr(work_routes.httpx, "AsyncClient", StsClient)
    monkeypatch.setattr(admit_client, "_frozen_credentials", lambda: Credentials("testing", "testing", "testing").get_frozen_credentials())
    monkeypatch.setenv("ADP_TASK_API_ADMISSION_ENABLED", "true")
    monkeypatch.setenv("ADP_TASK_ADMISSION_PRODUCER_ROLES", ROLE)
    monkeypatch.setenv("ADP_TASK_ADMIT_ENDPOINT", "https://fixture.execute-api.us-east-1.amazonaws.com/dev")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    app = FastAPI()
    app.dependency_overrides[get_db] = lambda: None
    app.include_router(route.router)
    app.include_router(task_dispatch_routes.router)
    runtime = SimpleNamespace(env={"ADP_TASK_API_ADMISSION_ENABLED": "true", "ADP_TASK_DISPATCH_PRODUCER_ROLES": ROLE})
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    work = TaskWorkStore(
        dynamodb_client=client,
        table_name=store.table_name,
        authority_table_name=store.authority_table_name,
        clock=lambda: storage_tests.NOW.timestamp(),
    )
    app.dependency_overrides[task_dispatch_routes.work_store] = lambda: work
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="tasks.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
    monkeypatch.setenv("ADP_TASK_GATEWAY_ENDPOINT", "https://fixture.execute-api.us-east-1.amazonaws.com/dev")
    monkeypatch.setenv("SUBMIT_QUEUE_URL", queue)
    captured = []
    fault = {"tamper": False, "lose_response": False}
    with TestClient(app) as web:

        class Bridge:
            def open(self, request, timeout):
                body = request.data
                captured.append(body)
                if fault["tamper"]:
                    body = body.replace(b"inspect", b"changed")
                headers = dict(request.header_items())
                assert headers["Authorization"].startswith("AWS4-HMAC-SHA256")
                headers["X-Caller-Identity"] = fault.get("edge_identity", ROLE)
                headers["X-Adp-Edge-Provenance"] = fault.get("edge_provenance", "task-test-edge-provenance")
                path = request.full_url.split("amazonaws.com/dev", 1)[1]
                response = web.post(path, content=body, headers=headers)
                if fault["lose_response"]:
                    fault["lose_response"] = False
                    raise OSError("response lost after gateway returned")

                class Response(io.BytesIO):
                    pass

                result = Response(response.content)
                result.status = response.status_code
                return result

        monkeypatch.setattr(admit_client.urllib.request, "build_opener", lambda *args: Bridge())
        yield SimpleNamespace(
            store=store,
            service=service,
            policies=policies,
            web=web,
            reservations=reservations,
            budget=budget,
            captured=captured,
            sts_calls=sts_calls,
            model_calls=model_calls,
            fault=fault,
            sqs=sqs,
            queue=queue,
        )
        web.portal.call(reservations.close)


def submit(raw, key="http-task"):
    return handler.handle_task_submit(
        {
            "httpMethod": "POST",
            "resource": "/v1/tasks",
            "body": raw,
            "isBase64Encoded": False,
            "headers": {"Authorization": "Bearer caller-access-token", "Idempotency-Key": key},
            "requestContext": {"requestId": "http-fixture"},
        },
        None,
    )


def test_lambda_gateway_acceptance_and_dispatch_preserve_exact_input(flow):
    raw = ' { "schema_version":"1.0", "persona":"agent-task-investigator", "instructions":"inspect é", "inputs":{"large":1e200} }\n'
    first = submit(raw)
    assert first["statusCode"] == 202, first
    receipt = json.loads(first["body"])
    task = flow.store.read_task(receipt["task_id"])
    assert task["input_payload"] == json.loads(raw)
    work = flow.store.resolve_work(task["dispatch_id"], expected_kind="dispatch")
    assert work["envelope"]["task_id"] == receipt["task_id"]
    assert raw.encode() in flow.captured[0]
    publication = task_dispatch.publish_dispatch(task["dispatch_id"])
    assert publication["publication_outcome"] == "confirmed", publication
    assert publication["settled"] is True
    messages = flow.sqs.receive_message(QueueUrl=flow.queue, MaxNumberOfMessages=10)["Messages"]
    assert len(messages) == 1
    assert json.loads(messages[0]["Body"]) == work["envelope"]
    assert flow.store.read_task(receipt["task_id"])["state"] == "queued"
    replay = submit(raw)
    assert replay["statusCode"] == 202, replay
    assert json.loads(replay["body"])["task_id"] == receipt["task_id"]
    assert json.loads(replay["body"])["idempotent_replay"] is True
    assert len(flow.model_calls) == 1
    target = flow.budget._target(scope="qualification:http-integration", cap=25)
    assert flow.web.portal.call(flow.reservations.snapshot, target).total_usd == 1


def test_tampered_proof_never_reaches_admission(flow):
    flow.fault["tamper"] = True
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 400, response
    assert flow.model_calls == []
    assert flow.sts_calls == []


def test_lost_http_response_replays_committed_acceptance(flow):
    raw = '{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}'
    flow.fault["lose_response"] = True
    assert submit(raw)["statusCode"] == 503
    replay = submit(raw)
    assert replay["statusCode"] == 202, replay
    assert json.loads(replay["body"])["idempotent_replay"] is True
    assert len(flow.model_calls) == 1


def test_unallowlisted_producer_cannot_admit(flow, monkeypatch):
    monkeypatch.setenv("ADP_TASK_ADMISSION_PRODUCER_ROLES", "arn:aws:iam::111122223333:role/other")
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 403, response
    assert flow.model_calls == []


def test_caller_without_submit_scope_cannot_borrow_producer_authority(flow, monkeypatch):
    monkeypatch.setattr(authz, "authenticate", lambda request: (SimpleNamespace(), frozenset({"adp-tasks/read"})))
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 403, response
    assert flow.model_calls == []


def test_changed_request_key_conflict_leaves_one_task_and_hold(flow):
    raw = '{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}'
    assert submit(raw)["statusCode"] == 202
    response = submit(raw.replace("inspect", "changed"))
    assert response["statusCode"] == 409, response
    assert len(flow.model_calls) == 1


def test_unready_model_refuses_without_accepting_or_fallback(flow):
    from src.agentauth.model_policy import ModelPolicyError

    async def unavailable(*args, **kwargs):
        raise ModelPolicyError("task_model_probe_required")

    flow.service.model_resolver = unavailable
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 503, response
    assert (
        flow.store._read_idempotency(idempotency_partition(tenant="tenant-a", canonical_principal="svc-principal-1", idempotency_key="http-task"))
        is None
    )


@pytest.mark.parametrize("edge", ["", "forged-provenance"])
def test_missing_or_forged_edge_provenance_cannot_use_valid_body_proof(flow, edge):
    flow.fault["edge_provenance"] = edge
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 403
    assert flow.model_calls == []


def test_edge_and_sts_producer_roles_must_be_the_same(flow, monkeypatch):
    other = "arn:aws:iam::111122223333:role/other-producer"
    monkeypatch.setenv("ADP_TASK_ADMISSION_PRODUCER_ROLES", ROLE + "," + other)
    flow.fault["edge_identity"] = other
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 403
    assert flow.model_calls == []


def test_disabled_edge_header_trust_does_not_fallback_to_sts_only(flow, monkeypatch):
    from src.auth import caller_provenance

    monkeypatch.setattr(
        caller_provenance, "get_settings", lambda: SimpleNamespace(trust_apigw_headers=False, apigw_provenance_secret="task-test-edge-provenance")
    )
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 403
    assert flow.model_calls == []


def test_scoped_producer_needs_no_generic_internal_agent_grant(flow, monkeypatch):
    from src.agentauth import routes

    async def generic_forbidden(*args, **kwargs):
        raise AssertionError("Task producer must not enter generic internal authorization")

    monkeypatch.setattr(routes, "verify_internal_or_irsa", generic_forbidden)
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 202
    assert len(flow.model_calls) == 1


def test_uploaded_artifact_admission_preserves_closed_dispatch_reference(flow):
    artifact_id = "art_12345678-1234-4234-8234-123456789abc"
    flow.store.create_artifact_binding(
        artifact_id=artifact_id,
        tenant="tenant-a",
        canonical_principal="svc-principal-1",
        version=1,
        content_sha256="a" * 64,
        content_type="text/plain",
        size_bytes=40,
    )
    raw = json.dumps({"schema_version": "1.0", "persona": "agent-task-investigator", "instructions": "inspect", "artifact_ids": [artifact_id]})
    response = submit(raw)
    assert response["statusCode"] == 202, response
    task = flow.store.read_task(json.loads(response["body"])["task_id"])
    work = flow.store.resolve_work(task["dispatch_id"], expected_kind="dispatch")
    assert work["envelope"]["input_ref"]["artifact_refs"] == [{"artifact_id": artifact_id, "version": 1, "content_sha256": "a" * 64}]
    replay = submit(raw)
    assert replay["statusCode"] == 202
    assert json.loads(replay["body"])["task_id"] == task["task_id"]
    target = flow.budget._target(scope="qualification:http-integration", cap=25)
    assert flow.web.portal.call(flow.reservations.snapshot, target).total_usd == 1


def test_artifact_retry_after_pretransaction_failure_reuses_original_budget_hold(flow, monkeypatch, caplog):
    from datetime import timedelta

    from src.tasks.store import TaskStoreError

    artifact_id = "art_12345678-1234-4234-8234-123456789abc"
    flow.store.create_artifact_binding(
        artifact_id=artifact_id,
        tenant="tenant-a",
        canonical_principal="svc-principal-1",
        version=1,
        content_sha256="a" * 64,
        content_type="text/plain",
        size_bytes=40,
    )
    raw = json.dumps({"schema_version": "1.0", "persona": "agent-task-investigator", "instructions": "inspect", "artifact_ids": [artifact_id]})
    original = flow.store.accept

    def fail_before_transaction(request):
        raise TaskStoreError("secret-body-must-not-be-logged")

    monkeypatch.setattr(flow.store, "accept", fail_before_transaction)
    assert submit(raw)["statusCode"] == 503
    assert "TaskStoreError" in [getattr(record, "error_class", "") for record in caplog.records]
    assert "secret-body-must-not-be-logged" not in caplog.text
    target = flow.budget._target(scope="qualification:http-integration", cap=25)
    assert flow.web.portal.call(flow.reservations.snapshot, target).total_usd == 1
    monkeypatch.setattr(flow.store, "accept", original)
    flow.budget.clock = lambda: storage_tests.NOW + timedelta(seconds=121)
    response = submit(raw)
    assert response["statusCode"] == 202, response
    assert flow.web.portal.call(flow.reservations.snapshot, target).total_usd == 1


def enable_cyber_policy(flow):
    policy = flow.policies.get(tenant_id="tenant-a", canonical_principal_id="svc-principal-1")
    flow.policies.put(
        tenant_id="tenant-a",
        canonical_principal_id="svc-principal-1",
        expected_version=int(policy["version"]),
        updated_by="fixture-cyber-enrollment",
        policy={
            **{key: policy[key] for key in ("status", "task_scopes", "model_policy_version", "limits")},
            "allowed_personas": ["agent-task-investigator", "agent-task-cyber"],
            "allowed_tools": ["cyber.triage", "cyber.dynamic"],
        },
    )


def test_tools_unconfigured_admits_model_only_without_tool_grants(flow, monkeypatch):
    enable_cyber_policy(flow)
    monkeypatch.delenv("ADP_TASK_PERSONA_TOOLS", raising=False)
    response = submit('{"schema_version":"1.0","persona":"agent-task-cyber","instructions":"inspect"}')
    assert response["statusCode"] == 202, response
    task = flow.store.read_task(json.loads(response["body"])["task_id"])
    assert task["tool_grants"] == []


def test_cyber_enabled_uses_enrolled_persona_specific_resolver(flow, monkeypatch):
    enable_cyber_policy(flow)
    monkeypatch.setenv("ADP_TASK_PERSONA_TOOLS", '{"agent-task-cyber":["cyber.triage"]}')
    response = submit('{"schema_version":"1.0","persona":"agent-task-cyber","instructions":"inspect"}')
    assert response["statusCode"] == 202, response
    assert len(flow.model_calls) == 1
    assert flow.model_calls[0]["persona"] == "agent-task-cyber"
    task = flow.store.read_task(json.loads(response["body"])["task_id"])
    assert task["persona"] == "agent-task-cyber"
    assert task["tool_grants"] == ["cyber.triage"]
    grant = flow.store._get_authority("TENANT#tenant-a", f"TASK_RUN#{task['invocation_id']}#GEN#0000000001")
    assert grant["tool_grants"] == ["cyber.triage"]
    assert flow.store.resolve_work(task["dispatch_id"], expected_kind="dispatch")["envelope"]["persona"] == "agent-task-cyber"


def test_cyber_enabled_still_requires_policy_enrollment(flow, monkeypatch):
    monkeypatch.setenv("ADP_TASK_PERSONA_TOOLS", '{"agent-task-cyber":["cyber.triage"]}')
    response = submit('{"schema_version":"1.0","persona":"agent-task-cyber","instructions":"inspect"}')
    assert response["statusCode"] == 403, response
    assert flow.model_calls == []


def test_cyber_missing_exact_probe_refuses_without_task_or_hold(flow, monkeypatch):
    from src.agentauth.model_policy import ModelPolicyError

    enable_cyber_policy(flow)
    monkeypatch.setenv("ADP_TASK_PERSONA_TOOLS", '{"agent-task-cyber":["cyber.triage"]}')

    async def unproven(*args, **kwargs):
        flow.model_calls.append(kwargs)
        raise ModelPolicyError("task_model_probe_required")

    flow.service.model_resolver = unproven
    response = submit('{"schema_version":"1.0","persona":"agent-task-cyber","instructions":"inspect"}')
    assert response["statusCode"] == 503, response
    assert flow.model_calls[0]["persona"] == "agent-task-cyber"
    assert (
        flow.store._read_idempotency(idempotency_partition(tenant="tenant-a", canonical_principal="svc-principal-1", idempotency_key="http-task"))
        is None
    )
    target = flow.budget._target(scope="qualification:http-integration", cap=25)
    assert flow.web.portal.call(flow.reservations.snapshot, target) is None


def test_six_hour_policy_freezes_exact_deadline_in_task_and_run_grant(flow):
    from datetime import timedelta

    policy = flow.policies.get(tenant_id="tenant-a", canonical_principal_id="svc-principal-1")
    flow.policies.put(
        tenant_id="tenant-a",
        canonical_principal_id="svc-principal-1",
        expected_version=int(policy["version"]),
        updated_by="six-hour-fixture",
        policy={
            **{key: policy[key] for key in ("status", "task_scopes", "allowed_personas", "model_policy_version")},
            "limits": {**policy["limits"], "max_duration_minutes": 360},
        },
    )
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 202, response
    task = flow.store.read_task(json.loads(response["body"])["task_id"])
    deadline = storage_tests.NOW + timedelta(hours=6)
    assert task["deadline_at"] == deadline.strftime("%Y-%m-%dT%H:%M:%SZ")
    assert flow.model_calls[0]["deadline"] == deadline
    grant = flow.store._get_authority("TENANT#tenant-a", f"TASK_RUN#{task['invocation_id']}#GEN#0000000001")
    assert grant["limits"]["deadline_at"] == task["deadline_at"]


def test_persona_specific_model_authorization_reaches_model_binding(flow):
    current = flow.policies.get(tenant_id="tenant-a", canonical_principal_id="svc-principal-1")
    fields = {key: current[key] for key in ("status", "allowed_personas", "task_scopes", "model_policy_version", "limits")}
    fields["model_policy_versions"] = {"agent-task-investigator": "7"}
    flow.policies.put(tenant_id="tenant-a", canonical_principal_id="svc-principal-1", expected_version=1, policy=fields, updated_by="admin")
    response = submit('{"schema_version":"1.0","persona":"agent-task-investigator","instructions":"inspect"}')
    assert response["statusCode"] == 202, response
    assert flow.model_calls[0]["expected_policy_version"] == "7"
