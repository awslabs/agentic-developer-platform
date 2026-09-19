"""Persisted queue assignments, retries and competing gateways using Moto AWS APIs."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import boto3
import pytest
from botocore.exceptions import BotoCoreError
from moto import mock_aws

from src.agentauth.bootstrap import BootstrapStore, envelope_digest
from src.agentauth.task_delivery import TaskDelivery, TaskDeliveryError


@pytest.fixture
def tasks():
    with mock_aws():
        ddb = boto3.client("dynamodb", region_name="us-east-1")
        ddb.create_table(
            TableName="authority",
            BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
        )
        store = BootstrapStore(table_name="authority", dynamodb_client=ddb)
        sqs = boto3.client("sqs", region_name="us-east-1")
        queue = sqs.create_queue(QueueName="protected-tasks")["QueueUrl"]
        envelopes = [{"message_id": f"run-{n}", "tenant_id": f"tenant-{n}", "task": "protected work"} for n in (1, 2)]
        for envelope in envelopes:
            ddb.put_item(
                TableName="authority",
                Item={
                    "pk": {"S": "INVOCATION#" + envelope["message_id"]},
                    "sk": {"S": "DISPATCH"},
                    "envelope_digest": {"S": envelope_digest(envelope)},
                    "tenant_id": {"S": envelope["tenant_id"]},
                },
            )
            sqs.send_message(QueueUrl=queue, MessageBody=json.dumps(envelope))
        now = [1000]
        delivery = TaskDelivery(store=store, sqs=sqs, queue_url=queue, clock=lambda: now[0])
        yield SimpleNamespace(delivery=delivery, ddb=ddb, sqs=sqs, queue=queue, now=now, envelopes=envelopes)


def test_one_task_per_pod_and_no_second_task_after_ack(tasks, monkeypatch):
    receive = Mock(wraps=tasks.sqs.receive_message)
    monkeypatch.setattr(tasks.sqs, "receive_message", receive)
    first = tasks.delivery.acquire("pod-one")
    assert json.loads(first) == tasks.envelopes[0]
    assert tasks.delivery.acquire("pod-one") == first
    assert receive.call_count == 1
    tasks.delivery.maintain("pod-one", acknowledge=True)
    tasks.delivery.maintain("pod-one", acknowledge=True)
    assert tasks.delivery.read("pod-one")["state"] == "acknowledged"
    assert "receipt" not in tasks.delivery.read("pod-one")
    with pytest.raises(TaskDeliveryError, match="finished"):
        tasks.delivery.acquire("pod-one")
    assert json.loads(tasks.delivery.acquire("pod-two")) == tasks.envelopes[1]
    assert receive.call_count == 2


def test_bootstrap_cannot_substitute_a_task_or_digest(tasks):
    tasks.delivery.acquire("pod-one")
    tasks.delivery.acquire("pod-two")
    digest = envelope_digest(tasks.envelopes[0])
    tasks.delivery.require_assignment("pod-one", "run-1", digest)
    for pod, run, proof in [("pod-two", "run-1", digest), ("pod-one", "run-2", envelope_digest(tasks.envelopes[1])), ("pod-one", "run-1", "0" * 64)]:
        with pytest.raises(TaskDeliveryError, match="wrong_assignment"):
            tasks.delivery.require_assignment(pod, run, proof)


def test_concurrent_acquire_has_one_durable_winner(tasks, monkeypatch):
    put = tasks.ddb.put_item
    winners = []

    def competing(**kwargs):
        monkeypatch.setattr(tasks.ddb, "put_item", put)
        winners.append(tasks.delivery.acquire("pod-one"))
        return put(**kwargs)

    monkeypatch.setattr(tasks.ddb, "put_item", competing)
    with pytest.raises(TaskDeliveryError, match="busy"):
        tasks.delivery.acquire("pod-one")
    assert len(winners) == 1
    assert tasks.delivery.acquire("pod-one") == winners[0]
    assert json.loads(tasks.delivery.acquire("pod-two")) == tasks.envelopes[1]


def test_lost_commit_response_does_not_release_a_durably_assigned_message(tasks, monkeypatch):
    put = tasks.ddb.put_item
    release = Mock(wraps=tasks.sqs.change_message_visibility)
    monkeypatch.setattr(tasks.sqs, "change_message_visibility", release)

    def lost_response(**kwargs):
        result = put(**kwargs)
        if kwargs["Item"]["state"] == {"S": "assigned"}:
            monkeypatch.setattr(tasks.ddb, "put_item", put)
            raise BotoCoreError()
        return result

    monkeypatch.setattr(tasks.ddb, "put_item", lost_response)
    with pytest.raises(TaskDeliveryError, match="unavailable"):
        tasks.delivery.acquire("pod-one")
    release.assert_not_called()
    assert json.loads(tasks.delivery.acquire("pod-one")) == tasks.envelopes[0]
    assert json.loads(tasks.delivery.acquire("pod-two")) == tasks.envelopes[1]


def test_late_receiver_cannot_overwrite_a_new_reservation(tasks, monkeypatch):
    receive = tasks.sqs.receive_message
    winners = []

    def delayed(**kwargs):
        first = receive(**kwargs)
        tasks.now[0] += 31
        monkeypatch.setattr(tasks.sqs, "receive_message", receive)
        winners.append(tasks.delivery.acquire("pod-one"))
        return first

    monkeypatch.setattr(tasks.sqs, "receive_message", delayed)
    with pytest.raises(TaskDeliveryError, match="assignment_failed"):
        tasks.delivery.acquire("pod-one")
    assert json.loads(winners[0]) == tasks.envelopes[1]
    assert tasks.delivery.acquire("pod-one") == winners[0]
    assert json.loads(tasks.delivery.acquire("pod-two")) == tasks.envelopes[0]


def test_expired_receipt_cannot_ack_extend_or_change_tasks(tasks, monkeypatch):
    tasks.delivery.acquire("pod-one")
    tasks.now[0] += 300
    delete = Mock(wraps=tasks.sqs.delete_message)
    extend = Mock(wraps=tasks.sqs.change_message_visibility)
    monkeypatch.setattr(tasks.sqs, "delete_message", delete)
    monkeypatch.setattr(tasks.sqs, "change_message_visibility", extend)
    for acknowledge in (False, True):
        with pytest.raises(TaskDeliveryError, match="lease_expired"):
            tasks.delivery.maintain("pod-one", acknowledge=acknowledge)
    with pytest.raises(TaskDeliveryError, match="lease_expired"):
        tasks.delivery.acquire("pod-one")
    with pytest.raises(TaskDeliveryError, match="lease_expired"):
        tasks.delivery.require_assignment("pod-one", "run-1", envelope_digest(tasks.envelopes[0]))
    delete.assert_not_called()
    extend.assert_not_called()


def test_heartbeat_extends_only_server_retained_queue_and_receipt(tasks, monkeypatch):
    tasks.delivery.acquire("pod-one")
    row = tasks.delivery.read("pod-one")
    extend = Mock(wraps=tasks.sqs.change_message_visibility)
    monkeypatch.setattr(tasks.sqs, "change_message_visibility", extend)
    tasks.now[0] += 120
    tasks.delivery.maintain("pod-one", acknowledge=False)
    extend.assert_called_once_with(QueueUrl=tasks.queue, ReceiptHandle=row["receipt"], VisibilityTimeout=300)
    assert tasks.delivery.read("pod-one")["lease_until"] == 1420


def test_ack_journal_cannot_be_reactivated_after_lost_response(tasks, monkeypatch):
    tasks.delivery.acquire("pod-one")
    delete = tasks.sqs.delete_message

    def lost_ack(**kwargs):
        delete(**kwargs)
        raise BotoCoreError()

    monkeypatch.setattr(tasks.sqs, "delete_message", lost_ack)
    with pytest.raises(TaskDeliveryError, match="unavailable"):
        tasks.delivery.maintain("pod-one", acknowledge=True)
    assert tasks.delivery.read("pod-one")["state"] == "acking"
    tasks.now[0] += 21
    with pytest.raises(TaskDeliveryError, match="finished"):
        tasks.delivery.maintain("pod-one", acknowledge=False)
    monkeypatch.setattr(tasks.sqs, "delete_message", delete)
    tasks.delivery.maintain("pod-one", acknowledge=True)
    assert tasks.delivery.read("pod-one")["state"] == "acknowledged"


def test_empty_assignment_is_stable_without_another_receive(tasks, monkeypatch):
    receive = Mock(return_value={})
    monkeypatch.setattr(tasks.sqs, "receive_message", receive)
    assert tasks.delivery.acquire("pod-one") is None
    assert tasks.delivery.acquire("pod-one") is None
    receive.assert_called_once()


def test_unprovisioned_envelope_is_not_delivered(tasks):
    tasks.ddb.delete_item(TableName="authority", Key={"pk": {"S": "INVOCATION#run-1"}, "sk": {"S": "DISPATCH"}})
    with pytest.raises(TaskDeliveryError, match="assignment_failed"):
        tasks.delivery.acquire("pod-one")
    assert tasks.delivery.read("pod-one")["state"] == "receiving"


def test_already_bootstrapped_pod_cannot_acquire_an_unrelated_task(tasks, monkeypatch):
    tasks.ddb.put_item(TableName="authority", Item={"pk": {"S": "POD#pod-one"}, "sk": {"S": "BINDING"}, "invocation_id": {"S": "old-run"}})
    receive = Mock(wraps=tasks.sqs.receive_message)
    monkeypatch.setattr(tasks.sqs, "receive_message", receive)
    with pytest.raises(TaskDeliveryError, match="finished"):
        tasks.delivery.acquire("pod-one")
    receive.assert_not_called()


@pytest.mark.parametrize("delay,refusal", [(300, "lease_expired"), (21, "busy")])
def test_slow_journal_write_cannot_use_a_stale_effect_lease(tasks, monkeypatch, delay, refusal):
    tasks.delivery.acquire("pod-one")
    save = tasks.delivery.save

    def delayed(*args):
        result = save(*args)
        tasks.now[0] += delay
        return result

    monkeypatch.setattr(tasks.delivery, "save", delayed)
    delete = Mock(wraps=tasks.sqs.delete_message)
    monkeypatch.setattr(tasks.sqs, "delete_message", delete)
    with pytest.raises(TaskDeliveryError, match=refusal):
        tasks.delivery.maintain("pod-one", acknowledge=True)
    delete.assert_not_called()
