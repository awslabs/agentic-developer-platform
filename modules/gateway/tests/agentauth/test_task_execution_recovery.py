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
