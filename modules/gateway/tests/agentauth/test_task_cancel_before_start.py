"""Actual acceptance, SQS and bootstrap fencing for cancellation before start."""
# ruff: noqa: F811
import json
import uuid
from datetime import timedelta

import boto3
import pytest

from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.task_delivery import TaskDelivery
from src.tasks.task_commands import TaskCommands
from tests.tasks.test_store import NOW, _request, client, store  # noqa: F401


def cancel(repository, req):
    service = TaskCommands(repository)
    service.admit(task_id=req.task_id, command_id=str(uuid.uuid4()), kind="cancel", payload={"reason": "cancel immediately"},
        principal=req.canonical_principal, tenant=req.tenant, expires_at=NOW+timedelta(minutes=5))
    return service


def test_accepted_queued_cancel_before_bootstrap_terminalizes_and_drains(store, monkeypatch):
    req = _request()
    store.accept(req)
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", store.table_name)
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="task-cancel-test")["QueueUrl"]
    sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(req.envelope))
    commands = cancel(store, req)
    assert commands.cancel_unstarted(req.task_id)
    task = store.read_task(req.task_id)
    assert task["state"] == "cancelled" and task["runtime_not_started"]
    assert task["child_exit"]["confirmed"] is False
    delivery = TaskDelivery(store=BootstrapStore(table_name=store.authority_table_name, dynamodb_client=store._client),
        sqs=sqs, queue_url=queue, clock=lambda: NOW.timestamp(), allow_task_api=True)
    assert delivery.acquire("pod-never-started") is None
    assert delivery.read("pod-never-started")["state"] == "acknowledged"
    assert not sqs.receive_message(QueueUrl=queue).get("Messages")
    assert commands.cancel_unstarted(req.task_id)  # No second capacity release.
    with pytest.raises(Exception):
        store.bind_runtime_attempt(task_id=req.task_id, invocation_id=req.invocation_id, generation=1,
            runtime_attempt_id=str(uuid.uuid4()), expected_version=task["version"])


def test_started_attempt_keeps_cooperative_cancellation(store):
    req = _request()
    store.accept(req)
    store.bind_runtime_attempt(task_id=req.task_id, invocation_id=req.invocation_id, generation=1,
        runtime_attempt_id=str(uuid.uuid4()), expected_version=store.read_task(req.task_id)["version"])
    commands = cancel(store, req)
    assert not commands.cancel_unstarted(req.task_id)
    assert store.read_task(req.task_id)["state"] == "cancel_requested"


from tests.agentauth.test_task_runtime import runtime  # noqa: E402,F401


def test_bootstrapped_but_unstarted_cancellation_releases_execution_slots(runtime):
    from types import SimpleNamespace

    from src.agentauth.bootstrap import BootstrapRefusedError
    service, pod, body, delivery = runtime
    service.bootstrap(body=body, pod=pod, delivery=delivery)
    task = service.repository.read_task(body["task_id"])
    req = SimpleNamespace(task_id=task["task_id"], canonical_principal=task["scope"]["canonical_principal"], tenant=task["scope"]["tenant"])
    commands = cancel(service.repository, req)
    assert commands.cancel_unstarted(req.task_id)
    grant = service._grant(req.tenant, task["invocation_id"], int(task["generation"]))
    assert grant["execution_capacity_released"] and grant["runtime_start_cancelled"]
    for key in grant["execution_capacity_keys"]:
        assert service.repository._get_authority(key, "ACTIVE")["active_count"] == 0
    with pytest.raises(BootstrapRefusedError):
        service.bootstrap(body=body, pod=pod, delivery=delivery)


def test_attempt_racing_terminal_cancel_is_refused_by_actual_store(store, monkeypatch):
    from src.tasks.store import TaskStateConflictError
    req = _request()
    store.accept(req)
    commands = cancel(store, req)
    transact = store._client.transact_write_items
    raced = []
    def race(**kwargs):
        if any("runtime_start_cancelled" in action.get("Update", {}).get("UpdateExpression", "") for action in kwargs["TransactItems"]):
            monkeypatch.setattr(store._client, "transact_write_items", transact)
            with pytest.raises(TaskStateConflictError):
                store.bind_runtime_attempt(task_id=req.task_id, invocation_id=req.invocation_id, generation=1,
                    runtime_attempt_id=str(uuid.uuid4()), expected_version=store.read_task(req.task_id)["version"])
            raced.append(True)
        return transact(**kwargs)
    monkeypatch.setattr(store._client, "transact_write_items", race)
    assert commands.cancel_unstarted(req.task_id)
    assert raced and store.read_task(req.task_id).get("runtime_attempt_id") is None


@pytest.mark.asyncio
async def test_no_child_terminal_proof_releases_real_admission_hold(store):
    from types import SimpleNamespace

    import fakeredis.aioredis

    from src.agentauth.task_budget import TaskBudget
    from src.agentauth.task_budget_settlement import settle_task_admission
    from src.budget.reservations import ReservationStore
    ledger = ReservationStore(redis_url=None, ttl_seconds=86400, clock=lambda: NOW.timestamp(),
        client=fakeredis.aioredis.FakeRedis(decode_responses=True))
    budget = TaskBudget(BootstrapStore(table_name=store.authority_table_name, dynamodb_client=store._client),
        reservations=ledger, qualification_id="cancel-test", clock=lambda: NOW)
    hold = await budget.reserve_admission(tenant="tenant-a", principal="svc-principal-1", idempotency_key="cancelled",
        max_usd=1, request_digest="a"*64)
    req = _request(budget_reservation=hold)
    store.accept(req)
    target = budget._target(scope="qualification:cancel-test", cap=25)
    assert (await ledger.snapshot(target)).total_usd == 1
    commands = cancel(store, req)
    assert commands.cancel_unstarted(req.task_id)
    identity = SimpleNamespace(task_id=req.task_id, invocation_id=req.invocation_id, generation=1, runtime_attempt_id=None)
    assert await settle_task_admission(store, identity, budget=budget)
    assert (await ledger.snapshot(target)).total_usd == 0
    await ledger.close()
