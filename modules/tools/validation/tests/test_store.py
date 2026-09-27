"""Real DynamoDB conditional/transaction behavior through Moto; no live services."""

import copy
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import boto3
from botocore.exceptions import ClientError
from fastapi import HTTPException
from moto import mock_aws
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from adp_tools.contracts import ToolIdentity  # noqa: E402
from adp_tools.storage import OperationRepository  # noqa: E402
from validation_tools.store import ValidationJobs  # noqa: E402


@pytest.fixture
def jobs():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(TableName="validation-jobs", BillingMode="PAY_PER_REQUEST",
            KeySchema=[{"AttributeName": "event_id", "KeyType": "HASH"}, {"AttributeName": "arrived_at", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": name, "AttributeType": "S"} for name in ["event_id", "arrived_at"]])
        identity = ToolIdentity(task_id="tsk_" + str(uuid.uuid4()), invocation_id=str(uuid.uuid4()), generation=1,
                                runtime_attempt_id=str(uuid.uuid4()), tenant="tenant", canonical_principal="principal")
        task = {"task_id": identity.task_id, "invocation_id": identity.invocation_id, "generation": 1, "version": 1,
                "runtime_attempt_id": identity.runtime_attempt_id, "state": "running",
                "scope": {"tenant": identity.tenant, "canonical_principal": identity.canonical_principal}}
        repo = OperationRepository(client, "validation-jobs", lambda: SimpleNamespace(identity=identity, task=copy.deepcopy(task)))
        yield ValidationJobs(repo), identity, task


def test_redelivery_has_one_admission_and_one_execution_claim(jobs):
    store, identity, _ = jobs
    operation = str(uuid.uuid4())
    row, created = store.admit(identity, operation, {"check": "unit"})
    assert created
    replay, created = store.admit(identity, operation, {"check": "unit"})
    assert not created and replay["request_digest"] == row["request_digest"]
    claimed = store.claim(identity, operation)
    assert claimed and claimed["owner_token"]
    assert store.claim(identity, operation) is None
    result = {"status": "passed", "durationSeconds": 0.1}
    settled = store.settle(identity, operation, claimed["owner_token"], phase="completed", result=result)
    assert settled["phase"] == "completed"
    assert store.settle(identity, operation, claimed["owner_token"], phase="completed", result=result) == settled
    assert store.claim(identity, operation) is None


def test_operation_identity_cannot_be_reused_with_changed_input(jobs):
    store, identity, _ = jobs
    operation = str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    with pytest.raises(HTTPException) as failure:
        store.admit(identity, operation, {"check": "different"})
    assert failure.value.status_code == 409


def test_cancellation_fences_late_queue_delivery_and_fresh_admission(jobs):
    store, identity, _ = jobs
    operation = str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    assert store.close(identity) == []
    assert store.claim(identity, operation) is None
    assert store.read(identity, operation)["phase"] == "cancelled"
    with pytest.raises(HTTPException):
        store.admit(identity, str(uuid.uuid4()), {"check": "unit"})


def test_running_work_remains_pending_until_owner_confirms_termination(jobs):
    store, identity, _ = jobs
    operation = str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    claimed = store.claim(identity, operation)
    assert store.close(identity) == [operation]
    store.settle(identity, operation, claimed["owner_token"], phase="cancelled")
    assert store.close(identity) == []


def test_unknown_execution_retains_active_fence_and_cannot_replay(jobs):
    store, identity, _ = jobs
    operation, next_operation = str(uuid.uuid4()), str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    claimed = store.claim(identity, operation)
    store.settle(identity, operation, claimed["owner_token"], phase="unknown")
    store.admit(identity, next_operation, {"check": "unit"})
    assert store.claim(identity, next_operation) is None
    assert store.close(identity) == [operation]
    assert store.claim(identity, operation) is None


def test_wrong_owner_cannot_settle_or_release_execution(jobs):
    store, identity, _ = jobs
    operation = str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    store.claim(identity, operation)
    with pytest.raises(HTTPException) as failure:
        store.settle(identity, operation, str(uuid.uuid4()), phase="cancelled")
    assert failure.value.status_code == 403
    assert store.read(identity, operation)["phase"] == "running"


def test_cross_tenant_and_old_attempt_reads_are_refused(jobs):
    store, identity, _ = jobs
    operation = str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    for update in [{"tenant": "other"}, {"runtime_attempt_id": str(uuid.uuid4())}]:
        with pytest.raises(HTTPException):
            store.read(identity.model_copy(update=update), operation)


def test_lost_claim_ack_is_resolved_without_another_claim(jobs, monkeypatch):
    store, identity, _ = jobs
    operation = str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    original = store.repo._client.transact_write_items
    calls = []
    def lost(**kwargs):
        calls.append(kwargs)
        original(**kwargs)
        raise ClientError({"Error": {"Code": "RequestTimeout"}}, "TransactWriteItems")
    monkeypatch.setattr(store.repo._client, "transact_write_items", lost)
    assert store.claim(identity, operation)["owner_token"]
    assert store.claim(identity, operation) is None
    assert len(calls) == 1


def test_delivery_is_bounded_but_does_not_grant_an_execution_claim(jobs):
    store, identity, _ = jobs
    now = [100]
    store.clock = lambda: now[0]
    operation = str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    for _ in range(3):
        assert store.delivery(identity, operation)
        assert not store.delivery(identity, operation)
        now[0] += 6
    assert not store.delivery(identity, operation)
    assert store.read(identity, operation)["phase"] == "pending"
    assert store.claim(identity, operation)
    assert not store.delivery(identity, operation)


def test_close_racing_completion_requires_stop_settlement(jobs):
    store, identity, _ = jobs
    operation = str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    claim = store.claim(identity, operation)
    store.close(identity)
    with pytest.raises(ClientError):
        store.settle(identity, operation, claim["owner_token"], phase="completed", result={"status": "passed"})
    assert store.stopping(identity)
    assert store.read(identity, operation)["phase"] == "running"
    store.settle(identity, operation, claim["owner_token"], phase="cancelled")
    assert store.close(identity) == []


def test_missing_active_job_never_reports_clean(jobs):
    store, identity, _ = jobs
    operation = str(uuid.uuid4())
    store.admit(identity, operation, {"check": "unit"})
    store.claim(identity, operation)
    from adp_tools.storage import serialize
    store.repo._client.delete_item(TableName=store.repo.table_name,
        Key=serialize(store._key(identity.task_id, operation)))
    assert store.close(identity) == [operation]
