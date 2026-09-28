"""Exercise persisted authority transitions and stale requests against Moto."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from moto import mock_aws

from src.agentauth.execution import ExecutionRecord, ExecutionStateError, ExecutionStatus, evaluate_execution_state
from src.agentauth.store import (
    AgentAuthorityStore,
    AuthorityStoreError,
    EpochRotationConflictError,
    ExecutionTransitionConflictError,
)

NOW = datetime(2026, 9, 13, 12, tzinfo=UTC)
RECORD = ExecutionRecord(
    invocation_id="run-a",
    tenant_id="tenant-a",
    current_attempt=1,
    status=ExecutionStatus.ACTIVE,
    current_credential_epoch=2,
    min_acceptable_credential_epoch=1,
    workload_binding="pod-a",
)
IDENTITY = {"invocation_id": "run-a", "tenant_id": "tenant-a"}
KEY = {"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "EXEC#run-a"}}


@pytest.fixture
def persisted():
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        client.create_table(
            TableName="authority-lifecycle-test",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[{"AttributeName": "pk", "AttributeType": "S"}, {"AttributeName": "sk", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        store = AgentAuthorityStore(table_name="authority-lifecycle-test", dynamodb_client=client)
        store.put_execution(record=RECORD)
        yield store, client


def validate(record, epoch, *, now=NOW):
    return evaluate_execution_state(record=record, **IDENTITY, attempt=1, credential_epoch=epoch, now=now, presented_workload_binding="pod-a")


@pytest.mark.parametrize("deadline", [None, {"S": ""}, {"N": "1"}, {"S": "malformed"}])
def test_absent_or_invalid_deadline_never_keeps_old_epoch_live(persisted, deadline):
    store, client = persisted
    if deadline is not None:
        client.update_item(
            TableName=store.table_name,
            Key=KEY,
            UpdateExpression="SET epoch_overlap_expires_at = :deadline",
            ExpressionAttributeValues={":deadline": deadline},
        )
    loaded = store.load_execution(**IDENTITY)
    with pytest.raises(ExecutionStateError, match="credential_epoch_superseded"):
        validate(loaded, 1)
    assert validate(loaded, 2).current_attempt == 1


def test_pure_evaluator_also_closes_overlap_with_no_deadline():
    with pytest.raises(ExecutionStateError, match="credential_epoch_superseded"):
        validate(RECORD, 1)


@pytest.mark.parametrize("field", ["current_attempt", "current_credential_epoch"])
@pytest.mark.parametrize("value", [None, {"S": "1"}, {"N": "0"}, {"N": "-1"}, {"N": "1.5"}])
def test_missing_or_invalid_version_cannot_restore_first_attempt(persisted, field, value):
    store, client = persisted
    args = {"TableName": store.table_name, "Key": KEY}
    if value is None:
        client.update_item(**args, UpdateExpression=f"REMOVE {field}")
    else:
        client.update_item(**args, UpdateExpression=f"SET {field} = :value", ExpressionAttributeValues={":value": value})
    with pytest.raises(AuthorityStoreError):
        store.load_execution(**IDENTITY)


def test_delayed_activation_cannot_resurrect_cancelled_execution(persisted):
    store, _ = persisted
    store.put_execution(record=replace(RECORD, status=ExecutionStatus.PENDING), expect_absent=False)
    store.set_execution_status(**IDENTITY, status=ExecutionStatus.CANCELLED, expected_attempt=1, expected_status=ExecutionStatus.PENDING)
    with pytest.raises(ExecutionTransitionConflictError):
        store.set_execution_status(**IDENTITY, status=ExecutionStatus.ACTIVE, expected_attempt=1, expected_status=ExecutionStatus.PENDING)
    assert store.load_execution(**IDENTITY).status is ExecutionStatus.CANCELLED


def test_duplicate_completion_is_idempotent_but_cannot_complete_new_attempt(persisted):
    store, _ = persisted
    transition = {**IDENTITY, "status": ExecutionStatus.COMPLETED, "expected_attempt": 1, "expected_status": ExecutionStatus.ACTIVE}
    store.set_execution_status(**transition)
    store.set_execution_status(**transition)
    assert store.load_execution(**IDENTITY).status is ExecutionStatus.COMPLETED
    store.put_execution(record=replace(RECORD, current_attempt=2, workload_binding="pod-b"), expect_absent=False)
    with pytest.raises(ExecutionTransitionConflictError):
        store.set_execution_status(**transition)
    loaded = store.load_execution(**IDENTITY)
    assert (loaded.current_attempt, loaded.status) == (2, ExecutionStatus.ACTIVE)


def test_activation_needs_bootstrap_binding_and_remains_idempotent(persisted):
    store, _ = persisted
    transition = {**IDENTITY, "status": ExecutionStatus.ACTIVE, "expected_attempt": 1, "expected_status": ExecutionStatus.PENDING}
    store.put_execution(record=replace(RECORD, status=ExecutionStatus.PENDING, workload_binding=None), expect_absent=False)
    with pytest.raises(ExecutionTransitionConflictError):
        store.set_execution_status(**transition)
    store.put_execution(record=replace(RECORD, status=ExecutionStatus.PENDING), expect_absent=False)
    store.set_execution_status(**transition)
    store.set_execution_status(**transition)
    assert store.load_execution(**IDENTITY).status is ExecutionStatus.ACTIVE


@pytest.mark.parametrize("attempt,binding", [(1, "pod-a"), (2, "pod-a"), (1, "pod-b")])
def test_renewal_for_old_attempt_or_workload_cannot_rotate_replacement(persisted, attempt, binding):
    store, _ = persisted
    store.put_execution(record=replace(RECORD, current_attempt=2, workload_binding="pod-b"), expect_absent=False)
    with pytest.raises(EpochRotationConflictError):
        store.rotate_credential_epoch(**IDENTITY, expected_epoch=2, expected_attempt=attempt, workload_binding=binding, overlap_seconds=30, now=NOW)
    assert store.load_execution(**IDENTITY).current_credential_epoch == 2


def test_valid_rotation_has_bounded_overlap_and_preserves_attempt_and_binding(persisted):
    store, _ = persisted
    assert (
        store.rotate_credential_epoch(**IDENTITY, expected_epoch=2, expected_attempt=1, workload_binding="pod-a", overlap_seconds=86_400, now=NOW)
        == 3
    )
    loaded = store.load_execution(**IDENTITY)
    assert (loaded.current_attempt, loaded.workload_binding) == (1, "pod-a")
    assert validate(loaded, 2, now=NOW + timedelta(seconds=29))
    with pytest.raises(ExecutionStateError, match="credential_epoch_superseded"):
        validate(loaded, 2, now=NOW + timedelta(seconds=30))
    assert validate(loaded, 3, now=NOW + timedelta(seconds=30))


def test_cancelled_execution_cannot_rotate_even_with_current_attempt_and_binding(persisted):
    store, _ = persisted
    store.set_execution_status(**IDENTITY, status=ExecutionStatus.CANCELLED, expected_attempt=1, expected_status=ExecutionStatus.ACTIVE)
    with pytest.raises(EpochRotationConflictError):
        store.rotate_credential_epoch(**IDENTITY, expected_epoch=2, expected_attempt=1, workload_binding="pod-a", overlap_seconds=30, now=NOW)
    assert store.load_execution(**IDENTITY).current_credential_epoch == 2
