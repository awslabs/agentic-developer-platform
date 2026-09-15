"""The actual Lambda writer's records must bootstrap in the gateway."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import boto3
import pytest
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapStore, envelope_digest
from src.agentauth.workload import VerifiedPod

_path = Path(__file__).resolve().parents[3] / "agent-factory/webhook-ingress/lambda/common/agent_authority.py"
_spec = importlib.util.spec_from_file_location("adp_human_dispatch_contract", _path)
webhook = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = webhook
_spec.loader.exec_module(webhook)


@pytest.fixture
def store(monkeypatch):
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        monkeypatch.setenv("AGENT_AUTHORITY_TABLE", "authority")
        yield BootstrapStore(table_name="authority", dynamodb_client=ddb)


def event():
    return webhook.VerifiedHumanEvent.from_verified_webhook(
        body=b'{"sender":{"type":"User"},"comment":{"id":1}}',
        event_type="issue_comment",
        resolved=SimpleNamespace(user_kind="human", user_id="human"),
        sender={"type": "User"},
        tenant_id="tenant",
        repo="org/repo",
    )


def envelope():
    return {
        "message_id": "original-random-id",
        "tenant_id": "tenant",
        "persona": "developer",
        "arrived_at": "2026-09-13T00:00:00Z",
        "source_ref": {"repo": "org/repo", "issue": 42},
        "correlation": {"parent_invocation_id": "worker-controlled"},
        "payload": {"comment": {"id": 1}},
    }


def test_actual_human_writer_bootstraps_and_ignores_advisory_parent(store):
    now = datetime.now(UTC)
    final = webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client, now=now)
    pod = VerifiedPod("pod-uid", "worker", "adp-agents", "agent-scaledjob-sa", "10.1.2.3")
    record = store.bind(invocation_id=final["message_id"], digest=envelope_digest(final), pod=pod, now=now)
    assert record.workload_binding == "pod-uid"
    assert record.parent_principal is None
    assert record.flow_id == event().reference_id
    grant = store.live_grant(invocation_id=record.invocation_id, tenant_id="tenant", attempt=1, now=now)
    assert grant.authority.human_id == "human"
    assert "credential" not in final


def test_webhook_retry_preserves_identity_digest_and_authority_expiry(store):
    now = datetime.now(UTC)
    first = webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client, now=now)
    second_input = {**envelope(), "message_id": "another-random-id", "arrived_at": "2026-09-14T00:00:00Z"}
    second = webhook.provision_human_dispatch(envelope=second_input, event=event(), client=store.client, now=now + timedelta(hours=2))
    assert first == second
    grant = store.authority.load_grant(principal=f"{first['message_id']}#1", tenant_id="tenant")
    assert grant.expires_at == (now + timedelta(days=7)).replace(microsecond=0)


def test_changed_dispatch_intent_cannot_reuse_human_event(store):
    webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client)
    changed = envelope()
    changed["source_ref"] = {"repo": "org/repo", "issue": 999}
    with pytest.raises(webhook.AuthorityProvisionError):
        webhook.provision_human_dispatch(envelope=changed, event=event(), client=store.client)


def test_revoked_grant_does_not_publish_again_on_retry(store):
    final = webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client)
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{final['message_id']}#1"}},
        UpdateExpression="SET revoked = :true",
        ExpressionAttributeValues={":true": {"BOOL": True}},
    )
    with pytest.raises(webhook.AuthorityProvisionError):
        webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client)


def test_expired_human_event_cannot_extend_itself_by_redelivery(store):
    now = datetime.now(UTC)
    webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client, now=now)
    with pytest.raises(webhook.AuthorityProvisionError):
        webhook.provision_human_dispatch(envelope=envelope(), event=event(), client=store.client, now=now + timedelta(days=8))


@pytest.mark.parametrize("user_kind,sender_type", [("bot", "Bot"), ("service", "User"), ("human", "Bot")])
def test_worker_or_service_claim_cannot_create_human_event(user_kind, sender_type):
    with pytest.raises(webhook.AuthorityProvisionError):
        webhook.VerifiedHumanEvent.from_verified_webhook(
            body=b"body",
            event_type="issue_comment",
            resolved=SimpleNamespace(user_kind=user_kind, user_id="claimed-human"),
            sender={"type": sender_type},
            tenant_id="tenant",
            repo="org/repo",
        )


@pytest.fixture
def child_dispatch(store):
    from src.agentauth.bootstrap import issue_bound_credential
    from src.agentauth.composition import build_authorization_service
    from src.agentauth.dispatch import DispatchRequest, DispatchService
    from src.agentauth.run_credential import CREDENTIAL_KEY_ENV

    env = {CREDENTIAL_KEY_ENV: "gateway-test-key-not-shared-with-workers"}
    original = envelope()
    original["persona"] = "operations"
    original["source_ref"]["installation_id"] = 123
    final = webhook.provision_human_dispatch(envelope=original, event=event(), client=store.client)
    pod = VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
    record = store.bind(invocation_id=final["message_id"], digest=envelope_digest(final), pod=pod, now=datetime.now(UTC))
    credential = issue_bound_credential(record, now=datetime.now(UTC), env=env)["credential"]
    store.client.create_table(
        TableName="events",
        BillingMode="PAY_PER_REQUEST",
        KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
        AttributeDefinitions=[{"AttributeName": "event_id", "AttributeType": "S"}, {"AttributeName": "arrived_at", "AttributeType": "S"}],
    )
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="dispatch.fifo", Attributes={"FifoQueue": "true"})["QueueUrl"]
    policy = build_authorization_service(
        authority_table=store.table, events_table="events", dynamodb_client=store.client, dynamodb_resource=resource, env=env
    )
    service = DispatchService(store=store, policy=policy, queue_url=queue, events_table="events", sqs=sqs)
    body = DispatchRequest(
        persona="developer", target={"repo": "org/repo", "issue": 42}, request_id="same-request", reason="Implement the approved story"
    )
    return SimpleNamespace(service=service, body=body, credential=credential, invocation=final["message_id"], sqs=sqs, queue=queue)


def send_child(context, body=None):
    return context.service.dispatch(body=body or context.body, credential_token=context.credential, workload_binding="pod-a")


def test_authorized_dispatch_queues_one_bootstrappable_child_and_recovers_retry(store, child_dispatch):
    import json

    first = send_child(child_dispatch)
    assert send_child(child_dispatch) == first
    messages = child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]
    assert len(messages) == 1
    child_envelope = json.loads(messages[0]["Body"])
    record = store.bind(
        invocation_id=first["invocation_id"],
        digest=envelope_digest(child_envelope),
        pod=VerifiedPod("pod-b", "worker-b", "adp-agents", "agent-scaledjob-sa", "10.0.1.3"),
        now=datetime.now(UTC),
    )
    assert record.parent_principal == f"{child_dispatch.invocation}#1"
    assert record.repo == "org/repo"
    assert child_envelope["actor"]["kind"] == "service"
    assert child_envelope["actor"]["user_id"] != "human"
    grant = store.live_grant(invocation_id=record.invocation_id, tenant_id="tenant", attempt=1, now=datetime.now(UTC))
    assert {str(a) for a in grant.allowed_actions} == {"monitor", "dispatch"}
    assert {str(a) for a in grant.delegable_actions} == {"monitor"}
    parent = store.authority.load_grant(principal=f"{child_dispatch.invocation}#1", tenant_id="tenant")
    assert store.authority.active_dispatch_count(grant_id=parent.grant_id, tenant_id="tenant") == 1


def test_dispatch_exact_intent_conflict_and_scope_refused_before_queue(child_dispatch):
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.dispatch import DispatchRequest
    from src.agentauth.policy import PolicyError

    send_child(child_dispatch)
    with pytest.raises(PolicyError, match="different content"):
        send_child(child_dispatch, child_dispatch.body.model_copy(update={"reason": "changed instructions"}))
    for target in ({"repo": "another/repo", "issue": 42}, {"repo": "org/repo", "issue": 99}):
        with pytest.raises(BootstrapRefusedError):
            send_child(child_dispatch, DispatchRequest(persona="developer", target=target, request_id="outside"))
    assert len(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]) == 1


def test_dispatch_full_capacity_still_recovers_an_existing_outcome(child_dispatch):
    from src.agentauth.policy import PolicyError

    first = send_child(child_dispatch)
    send_child(child_dispatch, child_dispatch.body.model_copy(update={"request_id": "second"}))
    assert send_child(child_dispatch) == first
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch, child_dispatch.body.model_copy(update={"request_id": "third"}))
    assert exc.value.status_code == 429


def test_dispatch_lost_send_response_reuses_same_sqs_dedup_id(child_dispatch, monkeypatch):
    from botocore.exceptions import BotoCoreError

    from src.agentauth.policy import PolicyError

    actual_send = child_dispatch.sqs.send_message

    def lose_response(**kwargs):
        actual_send(**kwargs)
        raise BotoCoreError()

    monkeypatch.setattr(child_dispatch.sqs, "send_message", lose_response)
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch)
    assert exc.value.status_code == 503
    monkeypatch.setattr(child_dispatch.sqs, "send_message", actual_send)
    send_child(child_dispatch)
    assert len(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]) == 1


def test_ambiguous_send_is_not_reissued_after_dedup_window(child_dispatch, monkeypatch):
    from botocore.exceptions import BotoCoreError

    from src.agentauth.policy import PolicyError

    def fail(**kwargs):
        raise BotoCoreError()

    monkeypatch.setattr(child_dispatch.sqs, "send_message", fail)
    with pytest.raises(PolicyError):
        send_child(child_dispatch)
    child_dispatch.service.now = lambda: datetime.now(UTC) + timedelta(seconds=241)
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch)
    assert exc.value.status_code == 409


def test_revoked_parent_blocks_queued_child_bootstrap(store, child_dispatch):
    import json

    from src.agentauth.bootstrap import BootstrapRefusedError

    first = send_child(child_dispatch)
    child_envelope = json.loads(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue)["Messages"][0]["Body"])
    store.client.update_item(
        TableName=store.table,
        Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{child_dispatch.invocation}#1"}},
        UpdateExpression="SET revoked = :true",
        ExpressionAttributeValues={":true": {"BOOL": True}},
    )
    with pytest.raises(BootstrapRefusedError):
        store.bind(
            invocation_id=first["invocation_id"],
            digest=envelope_digest(child_envelope),
            pod=VerifiedPod("pod-b", "worker-b", "adp-agents", "agent-scaledjob-sa", "10.0.1.3"),
            now=datetime.now(UTC),
        )
    assert store.authority.load_execution(invocation_id=first["invocation_id"], tenant_id="tenant").workload_binding is None


def test_real_dispatch_http_contract_creates_child(store, child_dispatch, monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from src.agentauth.routes import AgentRuntime, get_agent_runtime, router

    pod = VerifiedPod("pod-a", "worker-a", "adp-agents", "agent-scaledjob-sa", "10.0.1.2")
    runtime = AgentRuntime(
        store=store, workloads=SimpleNamespace(verify=lambda token: pod), env=child_dispatch.service.policy._env, dispatcher=child_dispatch.service
    )
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_agent_runtime] = lambda: runtime
    monkeypatch.setattr("src.agentauth.routes.verify_internal_or_irsa", AsyncMock())
    headers = {
        "X-Caller-Identity": "shared-worker-role",
        "X-Adp-Workload-Token": "verified-by-tokenreview-in-other-tests",
        "X-Adp-Run-Credential": child_dispatch.credential,
    }
    client = TestClient(app)
    result = client.post("/internal/v1/agent/dispatch", json=child_dispatch.body.model_dump(), headers=headers)
    assert result.status_code == 202
    assert result.json()["status"] == "accepted"
    assert result.headers["cache-control"] == "no-store"
    forged = {**child_dispatch.body.model_dump(), "parent_invocation_id": "forged-other-run"}
    assert client.post("/internal/v1/agent/dispatch", json=forged, headers=headers).status_code == 422
    assert len(child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue, MaxNumberOfMessages=10)["Messages"]) == 1


def test_dispatch_reservation_rechecks_grant_atomically(store, child_dispatch, monkeypatch):
    from src.agentauth.policy import PolicyError

    actual = store.client.transact_write_items

    def revoke_before_commit(**kwargs):
        store.client.update_item(
            TableName=store.table,
            Key={"pk": {"S": "TENANT#tenant"}, "sk": {"S": f"GRANT#{child_dispatch.invocation}#1"}},
            UpdateExpression="SET revoked = :true",
            ExpressionAttributeValues={":true": {"BOOL": True}},
        )
        return actual(**kwargs)

    monkeypatch.setattr(store.client, "transact_write_items", revoke_before_commit)
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch)
    assert exc.value.status_code == 409
    assert not child_dispatch.sqs.receive_message(QueueUrl=child_dispatch.queue).get("Messages")
    assert store.client.scan(TableName="events", Select="COUNT")["Count"] == 0


def test_committed_reservation_lost_response_is_recovered(store, child_dispatch, monkeypatch):
    from botocore.exceptions import BotoCoreError

    actual = store.client.transact_write_items

    def lose_response(**kwargs):
        actual(**kwargs)
        raise BotoCoreError()

    monkeypatch.setattr(store.client, "transact_write_items", lose_response)
    first = send_child(child_dispatch)
    assert send_child(child_dispatch) == first
    assert store.client.scan(TableName="events", Select="COUNT")["Count"] == 1


def test_total_dispatch_budget_survives_reservation_release(store, child_dispatch):
    from src.agentauth.policy import PolicyError

    for index in range(4):
        result = send_child(child_dispatch, child_dispatch.body.model_copy(update={"request_id": str(index)}))
        row = store._read("TENANT#tenant", f"EXEC#{result['invocation_id']}")
        store.authority.release_dispatch(grant_id=row["parent_grant_id"]["S"], tenant_id="tenant", reservation_id=row["dispatch_reservation_id"]["S"])
    with pytest.raises(PolicyError) as exc:
        send_child(child_dispatch, child_dispatch.body.model_copy(update={"request_id": "over-total-budget"}))
    assert exc.value.status_code == 409
