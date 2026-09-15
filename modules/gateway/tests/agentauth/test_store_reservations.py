"""Exercise persisted dispatch-reservation accounting against Moto."""

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from src.agentauth.store import (
    AgentAuthorityStore,
    AuthorityStoreError,
    DispatchLimitReachedError,
    DispatchReservationConflictError,
)


@pytest.fixture
def persisted():
    with mock_aws():
        client = boto3.client(
            "dynamodb",
            region_name="us-east-1",
            aws_access_key_id="testing",
            aws_secret_access_key="testing",
        )
        client.create_table(
            TableName="authority-reservations-test",
            KeySchema=[
                {"AttributeName": "pk", "KeyType": "HASH"},
                {"AttributeName": "sk", "KeyType": "RANGE"},
            ],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        yield AgentAuthorityStore(table_name="authority-reservations-test", dynamodb_client=client)


# ---------------------------------------------------------------------------
# Dispatch reservations, against persisted state (#5028)
#
# The Stubber tests in test_store.py assert the request shape. These assert the
# resulting *accounting*, which is where the original counter-only design was
# wrong: it produced correct-looking requests and still allowed over-dispatch,
# because a duplicate release decremented a shared counter and freed a slot
# another child was holding. Only replaying the sequence against real DynamoDB
# semantics shows that.
# ---------------------------------------------------------------------------

CEILING = 2


def reserve(store, reservation_id, *, ceiling=CEILING):
    return store.reserve_dispatch(grant_id="grant-a", tenant_id="tenant-a", reservation_id=reservation_id, max_concurrency=ceiling)


def release(store, reservation_id):
    store.release_dispatch(grant_id="grant-a", tenant_id="tenant-a", reservation_id=reservation_id)


def in_flight(store):
    return store.active_dispatch_count(grant_id="grant-a", tenant_id="tenant-a")


def test_reservations_accumulate_up_to_the_ceiling(persisted):
    store = persisted
    assert reserve(store, "child-1") == 1
    assert reserve(store, "child-2") == 2
    assert in_flight(store) == 2


def test_the_ceiling_refuses_the_next_distinct_reservation(persisted):
    """Backpressure, and it must not have consumed a slot on the way out."""
    store = persisted
    reserve(store, "child-1")
    reserve(store, "child-2")
    with pytest.raises(DispatchLimitReachedError):
        reserve(store, "child-3")
    assert in_flight(store) == 2


def test_a_retried_dispatch_cannot_consume_a_second_slot(persisted):
    """Same unit of work, dispatched twice. The second is already-admitted work,
    not new work, so it must be refused distinctly from a full ceiling."""
    store = persisted
    reserve(store, "child-1")
    with pytest.raises(DispatchReservationConflictError):
        reserve(store, "child-1")
    assert in_flight(store) == 1


def test_a_duplicate_release_cannot_free_another_childs_slot(persisted):
    """The over-dispatch regression, end to end.

    Under the counter-only design this sequence left in_flight at 0 with child-2
    still running, so the next reserve succeeded and two children ran against a
    ceiling of... one remaining slot. Here the second release for child-1 is a
    no-op, so child-2's slot stays held.
    """
    store = persisted
    reserve(store, "child-1")
    reserve(store, "child-2")

    release(store, "child-1")
    assert in_flight(store) == 1

    release(store, "child-1")
    assert in_flight(store) == 1, "a duplicate release freed a slot it did not hold"


def test_a_released_slot_is_reusable_by_new_work(persisted):
    """The ceiling must be a concurrency limit, not a lifetime quota."""
    store = persisted
    reserve(store, "child-1")
    reserve(store, "child-2")
    release(store, "child-1")
    assert reserve(store, "child-3") == 2


def test_a_released_reservation_id_cannot_be_reclaimed(persisted):
    """Re-claiming a retired ID would make the release/re-reserve pair
    unaccountable — the record is the audit trail for the slot."""
    store = persisted
    reserve(store, "child-1")
    release(store, "child-1")
    with pytest.raises(DispatchReservationConflictError):
        reserve(store, "child-1")
    assert in_flight(store) == 0


def test_releasing_a_reservation_never_made_does_not_manufacture_budget(persisted):
    store = persisted
    reserve(store, "child-1")
    release(store, "phantom-child")
    assert in_flight(store) == 1


def test_releases_cannot_drive_the_counter_below_zero(persisted):
    """A negative counter silently raises the effective ceiling."""
    store = persisted
    reserve(store, "child-1")
    release(store, "child-1")
    release(store, "child-1")
    release(store, "phantom-child")
    assert in_flight(store) == 0


def test_reservations_are_scoped_per_grant(persisted):
    """One grant exhausting its budget must not throttle another."""
    store = persisted
    reserve(store, "child-1")
    reserve(store, "child-2")
    assert store.reserve_dispatch(grant_id="grant-b", tenant_id="tenant-a", reservation_id="child-1", max_concurrency=CEILING) == 1
    assert in_flight(store) == 2


def test_reservations_are_scoped_per_tenant(persisted):
    """Same grant ID and reservation ID under a different tenant is different work."""
    store = persisted
    reserve(store, "child-1")
    assert store.reserve_dispatch(grant_id="grant-a", tenant_id="tenant-b", reservation_id="child-1", max_concurrency=CEILING) == 1
    assert in_flight(store) == 1


@pytest.mark.parametrize("reason", [None, "TransactionConflict", "ProvisionedThroughputExceeded", "ValidationError"])
def test_failed_release_stays_retryable_until_slot_is_confirmed_released(persisted, monkeypatch, reason):
    store = persisted
    reserve(store, "child-1")
    reserve(store, "child-2")
    transact = store._client.transact_write_items
    error = {"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"}}
    if reason:
        error["CancellationReasons"] = [{"Code": reason}, {"Code": "None"}]

    def fail_transaction(**kwargs):
        raise ClientError(error, "TransactWriteItems")

    monkeypatch.setattr(store._client, "transact_write_items", fail_transaction)
    with pytest.raises(AuthorityStoreError):
        release(store, "child-1")
    assert in_flight(store) == 2

    # Once the conflict/outage clears, retry releases exactly this child's slot.
    monkeypatch.setattr(store._client, "transact_write_items", transact)
    release(store, "child-1")
    release(store, "child-1")
    assert in_flight(store) == 1


def test_unreadable_reservation_cannot_confirm_release(persisted, monkeypatch):
    store = persisted
    reserve(store, "child-1")

    def fail_transaction(**kwargs):
        raise ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"}}, "TransactWriteItems")

    def fail_read(**kwargs):
        raise ClientError({"Error": {"Code": "InternalServerError", "Message": "unavailable"}}, "GetItem")

    monkeypatch.setattr(store._client, "transact_write_items", fail_transaction)
    monkeypatch.setattr(store._client, "get_item", fail_read)
    with pytest.raises(AuthorityStoreError):
        release(store, "child-1")


def test_held_reservation_with_missing_counter_reports_inconsistent_state(persisted):
    store = persisted
    reserve(store, "child-1")
    store._client.delete_item(TableName=store.table_name, Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "RESV#grant-a"}})
    with pytest.raises(AuthorityStoreError):
        release(store, "child-1")
    reservation = store._client.get_item(
        TableName=store.table_name, Key={"pk": {"S": "TENANT#tenant-a"}, "sk": {"S": "RESV#grant-a#child-1"}}, ConsistentRead=True
    )["Item"]
    assert reservation["state"] == {"S": "held"}
