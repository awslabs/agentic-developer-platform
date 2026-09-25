"""Server-observed dead-pod recovery over the real Task Dynamo transactions."""

# ruff: noqa: F811
import asyncio
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import boto3
import pytest

from src.agentauth import task_execution_recovery as recovery
from src.agentauth.bootstrap import BootstrapStore
from src.agentauth.task_delivery import TaskDelivery
from src.tasks.records import task_ops_partition, task_partition, task_work_partition, work_shard
from src.tasks.store import _serialize
from tests.agentauth.test_task_runtime import _settlement_fixture, runtime  # noqa: F401
from tests.tasks.test_store import NOW, client, store  # noqa: F401


@pytest.mark.parametrize("mode", ["success", "ack_cas_failure", "budget_retry"])
def test_no_send_recovery_keeps_child_exit_unknown_and_drains_exact_message(runtime, monkeypatch, mode):
    service, pod, _ = _settlement_fixture(runtime)
    repository = service.repository
    identity = service.authenticate_settlement(pod=pod)
    work = repository._get(task_work_partition(identity.task_id), "RECONCILE")
    assert work["work_kind"] == "execution"  # Written with the attempt transaction.
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", repository.table_name)
    observed = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    monkeypatch.setattr(recovery, "terminated_workload", lambda *args, **kwargs: observed)
    budget_calls = []

    async def budget(repo, bound):
        budget_calls.append(bound.task_id)
        return not (mode == "budget_retry" and len(budget_calls) == 1)

    monkeypatch.setattr(recovery, "settle_task_admission", budget)
    now = datetime.now(UTC)
    claimed = repository.claim_due_work(shard=work_shard(identity.task_id), now=now)
    claim = next(item for item in claimed if item["kind"] == "execution")
    result = asyncio.run(
        recovery.recover_execution(repository, SimpleNamespace(workloads=None), work_id=claim["work_id"], lease_token=claim["lease_token"])
    )
    assert result == ("unknown", "failed")  # Real queue acknowledgment remains outstanding.
    task = repository.read_task(identity.task_id)
    assert task["child_exit"]["confirmed"] is False
    assert task["error"]["provider_outcome"] == "not_started"
    assert task["error"]["total_usd"] == 0
    assert task["stop_evidence"]["workload_terminated"] is True
    assert task["server_workload_terminated"] is True
    assert repository.resolve_work(claim["work_id"]).get("task_due")
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="execution-recovery")["QueueUrl"]
    envelope = repository.resolve_work(task["dispatch_id"])["envelope"]
    sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(envelope))
    delivery = TaskDelivery(
        store=BootstrapStore(table_name=repository.authority_table_name, dynamodb_client=repository._client),
        sqs=sqs,
        queue_url=queue,
        clock=lambda: NOW.timestamp(),
        allow_task_api=True,
    )
    if mode == "ack_cas_failure":
        from botocore.exceptions import ClientError

        from src.agentauth.task_delivery import TaskDeliveryError

        original_write = repository._client.transact_write_items

        def reject_ack(**kwargs):
            item = kwargs["TransactItems"][0].get("Put", {}).get("Item", {})
            if item.get("state") == {"S": "acknowledged"}:
                raise ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
            return original_write(**kwargs)

        monkeypatch.setattr(repository._client, "transact_write_items", reject_ack)
        with pytest.raises(TaskDeliveryError):
            delivery.acquire("new-drainer")
        assert delivery.read("new-drainer")["state"] == "acking"
        assert repository.read_task(identity.task_id)["queue_ack_status"] == "pending"
        assert repository.resolve_work(claim["work_id"]).get("task_due")
        return
    assert delivery.acquire("new-drainer") is None
    assert delivery.read("new-drainer")["state"] == "acknowledged"
    assert repository.read_task(identity.task_id)["queue_ack_status"] == "confirmed"
    assert not sqs.receive_message(QueueUrl=queue).get("Messages")
    result = asyncio.run(
        recovery.recover_execution(repository, SimpleNamespace(workloads=None), work_id=claim["work_id"], lease_token=claim["lease_token"])
    )
    assert result == ("confirmed", "failed")
    assert "task_due" not in repository.resolve_work(claim["work_id"])
    assert len(budget_calls) == 2


def test_model_claim_blocks_zero_cost_recovery(runtime):
    service, pod, _ = _settlement_fixture(runtime)
    identity = service.authenticate_settlement(pod=pod)
    service.repository._client.put_item(
        TableName=service.repository.table_name,
        Item=_serialize({"event_id": task_ops_partition(identity.task_id), "arrived_at": "MODEL#existing", "operation_status": "unknown"}),
    )
    assert recovery.finalize_no_send(service.repository, identity, observed_at="2026-09-24T12:00:00Z") is False
    assert service.repository.read_task(identity.task_id)["state"] == "accepted"


def test_model_claim_race_invalidates_termination_snapshot(runtime, monkeypatch):
    from src.tasks.errors import TaskApiError

    service, pod, _ = _settlement_fixture(runtime)
    identity = service.authenticate_settlement(pod=pod)
    repository = service.repository
    original = repository._client.query

    def race(**kwargs):
        result = original(**kwargs)
        repository._client.update_item(
            TableName=repository.table_name,
            Key=_serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
            UpdateExpression="ADD #version :one",
            ExpressionAttributeNames={"#version": "version"},
            ExpressionAttributeValues=_serialize({":one": 1}),
        )
        return result

    monkeypatch.setattr(repository._client, "query", race)
    with pytest.raises(TaskApiError):
        recovery.finalize_no_send(repository, identity, observed_at="2026-09-24T12:00:00Z")
    assert repository.read_task(identity.task_id)["state"] == "accepted"


@pytest.mark.parametrize("alter", ["uid", "running", "missing", "namespace"])
def test_termination_requires_exact_existing_terminal_pod(alter):
    pod = {
        "metadata": {"uid": "owner"},
        "spec": {"serviceAccountName": "worker", "restartPolicy": "Never"},
        "status": {"phase": "Failed", "containerStatuses": [{"state": {"terminated": {"finishedAt": "2026-09-24T12:00:00Z"}}}]},
    }
    if alter == "uid":
        pod["metadata"]["uid"] = "other"
    if alter == "running":
        pod["status"]["phase"] = "Running"

    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            return {"items": [] if alter == "missing" else [pod]}

    retention = SimpleNamespace(
        namespace="adp-agents", service_account="worker", _headers=lambda: {}, client=SimpleNamespace(get=lambda *args, **kwargs: Response())
    )
    assert (
        recovery.terminated_workload(
            SimpleNamespace(exit_retention=retention), uid="owner", namespace="other" if alter == "namespace" else "adp-agents"
        )
        is None
    )


def test_retained_pod_is_released_after_durable_stop_even_when_provider_is_unknown(runtime, monkeypatch):
    from src.tasks.records import task_authority_partition, task_run_grant_sort_key

    service, pod, _ = _settlement_fixture(runtime)
    repository = service.repository
    identity = service.authenticate_settlement(pod=pod)
    repository._client.update_item(
        TableName=repository.authority_table_name,
        Key=_serialize(
            {
                "pk": task_authority_partition(identity.tenant),
                "sk": task_run_grant_sort_key(invocation_id=identity.invocation_id, generation=identity.generation),
            }
        ),
        UpdateExpression="SET workload_name = :name",
        ExpressionAttributeValues=_serialize({":name": "retained-worker"}),
    )
    operation = {"event_id": task_ops_partition(identity.task_id), "arrived_at": "MODEL#unknown", "operation_status": "unknown", "reserved_usd": "1"}
    repository._client.put_item(TableName=repository.table_name, Item=_serialize(operation))
    monkeypatch.setattr(recovery, "terminated_workload", lambda *args, **kwargs: "2026-09-24T12:00:00Z")

    async def pending(*args):
        return False

    monkeypatch.setattr(recovery, "settle_task_admission", pending)
    releases = []
    platform = SimpleNamespace(workloads=SimpleNamespace(exit_retention=SimpleNamespace(release=lambda **kwargs: releases.append(kwargs))))
    claim = next(item for item in repository.claim_due_work(shard=work_shard(identity.task_id), now=datetime.now(UTC)) if item["kind"] == "execution")
    assert asyncio.run(recovery.recover_execution(repository, platform, work_id=claim["work_id"], lease_token=claim["lease_token"])) == (
        "unknown",
        "failed",
    )
    task = repository.read_task(identity.task_id)
    assert task["error"]["provider_outcome"] == "unknown" and task["error"]["total_usd"] is None
    assert task["child_exit"]["confirmed"] is False and task["server_workload_terminated"] is True
    assert releases == [{"name": "retained-worker", "uid": pod.uid, "invocation_id": identity.invocation_id, "tenant_id": identity.tenant}]
    assert repository._get(task_ops_partition(identity.task_id), "MODEL#unknown") == operation
    assert repository.resolve_work(claim["work_id"]).get("task_due")


@pytest.mark.parametrize("retention_fails", [False, True])
def test_attempt_receipt_requires_native_exit_retention(monkeypatch, retention_fails):
    import uuid

    from fastapi import HTTPException
    from starlette.requests import Request

    from src.agentauth import task_runtime_routes as routes
    from src.agentauth.exit_retention import ExitRetentionError

    events = []
    identity = SimpleNamespace(pod_uid=str(uuid.uuid4()), invocation_id=str(uuid.uuid4()), tenant="tenant")

    async def authenticate(*args, **kwargs):
        return identity

    monkeypatch.setattr(routes, "_authenticate", authenticate)
    monkeypatch.setattr(routes, "task_runtime", lambda runtime: SimpleNamespace(register_attempt=lambda **kwargs: events.append("bound")))
    pod = SimpleNamespace(uid=identity.pod_uid, name="worker", namespace="adp-agents")

    def retain(**kwargs):
        assert kwargs == {"name": "worker", "uid": identity.pod_uid, "invocation_id": identity.invocation_id, "tenant_id": "tenant"}
        events.append("retained")
        if retention_fails:
            raise ExitRetentionError("unavailable")

    platform = SimpleNamespace(workloads=SimpleNamespace(verify=lambda token: pod, exit_retention=SimpleNamespace(retain=retain)))
    body = routes.AttemptBody(
        schema_version="1.0",
        task_id="tsk_" + str(uuid.uuid4()),
        invocation_id=identity.invocation_id,
        generation=1,
        runtime_attempt_id=str(uuid.uuid4()),
        protocol_version=1,
        capabilities=["input", "cancel"],
        old_attempt_invalidated=True,
    )
    request = Request({"type": "http", "headers": []})
    if retention_fails:
        with pytest.raises(HTTPException) as error:
            asyncio.run(routes.attempt(body, request, runtime=platform))
        assert error.value.status_code == 503
    else:
        assert asyncio.run(routes.attempt(body, request, runtime=platform))["operation_status"] == "confirmed"
    assert events == ["bound", "retained"]


@pytest.mark.parametrize("mode", ["success", "missing_proof", "stale_attempt"])
def test_confirmed_child_exit_drains_without_claiming_provider_success(runtime, monkeypatch, mode):
    from botocore.exceptions import ClientError

    from src.agentauth.task_delivery import TaskDeliveryError
    from src.tasks.task_commands import TaskCommands

    service, pod, _ = _settlement_fixture(runtime)
    repository = service.repository
    identity = service.authenticate_settlement(pod=pod)
    monkeypatch.setenv("WEBHOOK_EVENTS_TABLE", repository.table_name)
    model = {
        "event_id": task_ops_partition(identity.task_id),
        "arrived_at": "MODEL#unknown",
        "operation_status": "unknown",
        "reservation_status": "unknown",
    }
    repository._client.put_item(TableName=repository.table_name, Item=_serialize(model))
    TaskCommands(repository).settlement(
        identity,
        {
            "stop_evidence": {"child_exit_confirmed": True, "workload_terminated": False, "observed_at": "2026-09-24T12:00:00Z"},
            "queue_ack_status": "unknown",
        },
    )
    task = repository.read_task(identity.task_id)
    assert task["child_exit"]["confirmed"] is True
    assert not task.get("server_workload_terminated")
    assert task["error"]["provider_outcome"] == "unknown"
    envelope = repository.resolve_work(task["dispatch_id"])["envelope"]
    sqs = boto3.client("sqs", region_name="us-east-1")
    queue = sqs.create_queue(QueueName="child-exit-redelivery")["QueueUrl"]
    sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(envelope))
    delivery = TaskDelivery(
        store=BootstrapStore(table_name=repository.authority_table_name, dynamodb_client=repository._client),
        sqs=sqs,
        queue_url=queue,
        clock=lambda: NOW.timestamp(),
        allow_task_api=True,
    )
    if mode == "missing_proof":
        repository._client.update_item(
            TableName=repository.table_name,
            Key=_serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
            UpdateExpression="REMOVE child_exit",
        )
        assert delivery._cancelled_before_start(json.dumps(envelope)) is False
        assert repository.read_task(identity.task_id)["queue_ack_status"] == "unknown"
        return
    if mode == "stale_attempt":
        original = delivery._stopped_task_ack_item

        def race(receipt):
            item = original(receipt)
            repository._client.update_item(
                TableName=repository.table_name,
                Key=_serialize({"event_id": task_partition(identity.task_id), "arrived_at": "META"}),
                UpdateExpression="SET runtime_attempt_id = :new",
                ExpressionAttributeValues=_serialize({":new": "00000000-0000-4000-8000-000000000001"}),
            )
            return item

        monkeypatch.setattr(delivery, "_stopped_task_ack_item", race)
        with pytest.raises((TaskDeliveryError, ClientError)):
            delivery.acquire("child-drainer")
        assert repository.read_task(identity.task_id)["queue_ack_status"] == "unknown"
        return
    assert delivery.acquire("child-drainer") is None
    assert repository.read_task(identity.task_id)["queue_ack_status"] == "confirmed"
    assert repository.read_task(identity.task_id)["error"] == task["error"]
    assert repository._get(task_ops_partition(identity.task_id), "MODEL#unknown") == model
    assert not sqs.receive_message(QueueUrl=queue).get("Messages")


@pytest.mark.parametrize("budget_confirmed", [True, False])
def test_durable_child_exit_releases_execution_slots_without_live_pod(runtime, monkeypatch, budget_confirmed):
    from src.tasks.records import task_authority_partition, task_run_grant_sort_key
    from src.tasks.task_commands import TaskCommands

    service, pod, _ = _settlement_fixture(runtime)
    repository = service.repository
    identity = service.authenticate_settlement(pod=pod)
    # Final reporting records a real child stop; it deliberately does not invoke
    # the later settlement endpoint, reproducing the leaked execution slots.
    original_release = TaskCommands._release_execution_on_finalize
    monkeypatch.setattr(TaskCommands, "_release_execution_on_finalize", lambda *args: None)
    TaskCommands(repository).finalize(
        identity,
        {
            "schema_version": "1.0",
            "outcome": "failed",
            "final_report_id": "00000000-0000-4000-8000-000000000042",
            "child_exit": {"confirmed": True, "exit_code": 1, "signal": None, "stopped_at": "2026-09-24T12:00:00Z"},
            "result": None,
            "error": {
                "schema_version": "1.0",
                "outcome": "failed",
                "code": "process_failed",
                "message": "child stopped",
                "committed_at": "2026-09-24T12:00:00Z",
                "child_exit_confirmed": True,
                "recovery_required": False,
                "provider_outcome": "unknown",
                "total_usd": None,
            },
            "committed_result_refs": [],
        },
    )
    monkeypatch.setattr(TaskCommands, "_release_execution_on_finalize", original_release)
    original_error = repository.read_task(identity.task_id)["error"]
    pk = task_authority_partition(identity.tenant)
    sk = task_run_grant_sort_key(invocation_id=identity.invocation_id, generation=identity.generation)
    grant = repository._get_authority(pk, sk)
    assert grant["execution_capacity_released"] is False
    assert all(repository._get_authority(key, "ACTIVE")["active_count"] == 1 for key in grant["execution_capacity_keys"])

    def no_pod(*args, **kwargs):
        raise AssertionError("durable child stop must not need an existing pod")

    monkeypatch.setattr(recovery, "terminated_workload", no_pod)

    async def budget(*args):
        return budget_confirmed

    monkeypatch.setattr(recovery, "settle_task_admission", budget)
    claimed = repository.claim_due_work(shard=work_shard(identity.task_id), now=datetime.now(UTC))
    claim = next(row for row in claimed if row["kind"] == "execution")
    for _ in range(2):
        assert asyncio.run(
            recovery.recover_execution(repository, SimpleNamespace(workloads=None), work_id=claim["work_id"], lease_token=claim["lease_token"])
        ) == ("unknown", "failed")
    assert repository._get_authority(pk, sk)["execution_capacity_released"] is True
    assert all(repository._get_authority(key, "ACTIVE")["active_count"] == 0 for key in grant["execution_capacity_keys"])
    assert repository.read_task(identity.task_id)["error"] == original_error
