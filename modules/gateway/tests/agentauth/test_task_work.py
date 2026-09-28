# ruff: noqa: F811
"""Cross-story tests: real T1 acceptance feeds real T3 publication/recovery."""

import pytest

from src.agentauth.task_work import TaskWorkError, TaskWorkStore, work_shard
from tests.tasks.test_store import AUTHORITY_TABLE, NOW, TABLE, _request, client, store  # noqa: F401


@pytest.fixture
def accepted(client, store):
    request = _request()
    store.accept(request)
    clock = [NOW.timestamp()]
    adapter = TaskWorkStore(dynamodb_client=client, table_name=TABLE, authority_table_name=AUTHORITY_TABLE, clock=lambda: clock[0])
    return request, adapter, clock


def test_acceptance_dispatch_contract(accepted):
    request, adapter, _ = accepted
    bound = adapter.resolve(request.dispatch_id, expected_kind="dispatch")
    assert bound.envelope == request.envelope
    assert bound.tenant_id == request.tenant
    assert bound.work["work_kind"] == {"S": "dispatch"}


def test_duplicate_claim_refused(accepted):
    request, adapter, _ = accepted
    adapter.claim_publication(request.dispatch_id)
    with pytest.raises(TaskWorkError):
        adapter.claim_publication(request.dispatch_id)


def test_expired_lease_cannot_settle_new_claim(accepted):
    request, adapter, clock = accepted
    first = adapter.claim_publication(request.dispatch_id)
    clock[0] += 46
    second = adapter.claim_publication(request.dispatch_id)
    assert first.work["publication_lease_token"] != second.work["publication_lease_token"]
    with pytest.raises(TaskWorkError, match="stale_lease"):
        adapter.settle_publication(
            dispatch_id=request.dispatch_id,
            lease_token=first.work["publication_lease_token"]["S"],
            publication_outcome="confirmed",
            sqs_message_id="old",
        )
    assert adapter.task_status(request.task_id) == "accepted"


def test_unknown_send_reuses_same_envelope(accepted):
    request, adapter, _ = accepted
    first = adapter.claim_publication(request.dispatch_id)
    adapter.settle_publication(
        dispatch_id=request.dispatch_id, lease_token=first.work["publication_lease_token"]["S"], publication_outcome="unknown", sqs_message_id=None
    )
    second = adapter.claim_publication(request.dispatch_id)
    assert second.envelope == first.envelope == request.envelope


def test_recovery_and_publication_leases_independent(accepted):
    request, adapter, _ = accepted
    claimed, cursor = adapter.claim_recovery(shard=work_shard(request.task_id))
    assert cursor is None
    assert len(claimed) == 1
    dispatch = adapter.claim_publication(request.dispatch_id)
    recovery_token = claimed[0].work["recovery_lease_token"]["S"]
    assert recovery_token != dispatch.work["publication_lease_token"]["S"]
    adapter.settle_publication(
        dispatch_id=request.dispatch_id,
        lease_token=dispatch.work["publication_lease_token"]["S"],
        publication_outcome="confirmed",
        sqs_message_id="real-send",
    )
    status, _ = adapter.settle_recovery(
        work_id=request.dispatch_id, lease_token=recovery_token, evidence_kind="publication", observed=True, observed_at=NOW
    )
    assert status == "confirmed"
    assert adapter.task_status(request.task_id) == "queued"


def test_boolean_observation_does_not_create_send_evidence(accepted):
    request, adapter, _ = accepted
    claimed, _ = adapter.claim_recovery(shard=work_shard(request.task_id))
    with pytest.raises(TaskWorkError):
        adapter.settle_recovery(
            work_id=request.dispatch_id,
            lease_token=claimed[0].work["recovery_lease_token"]["S"],
            evidence_kind="publication",
            observed=True,
            observed_at=NOW,
        )
    assert adapter.task_status(request.task_id) == "accepted"


def test_revoked_locator_refused(accepted, client):
    request, adapter, _ = accepted
    client.update_item(
        TableName=AUTHORITY_TABLE,
        Key={"pk": {"S": "TASK_WORK_ID#" + request.dispatch_id}, "sk": {"S": "BINDING"}},
        UpdateExpression="SET binding_state = :state",
        ExpressionAttributeValues={":state": {"S": "revoked"}},
    )
    with pytest.raises(TaskWorkError):
        adapter.claim_publication(request.dispatch_id)


def test_cross_shard_cursor_refused(accepted):
    from src.agentauth.task_work import encode_cursor

    request, adapter, _ = accepted
    with pytest.raises(TaskWorkError, match="invalid_cursor"):
        adapter.claim_recovery(shard="v1#00", cursor=encode_cursor({"event_id": "x", "arrived_at": "x", "task_due": "x", "task_work_shard": "v1#01"}))
