"""The protected authority store's DynamoDB request contract (#5028 AC2, AC4, AC7).

These tests assert the *wire request*, not just the return value. That is the
point: the security properties of this store are properties of the request shape
—

- ``ConsistentRead=True``, or an authorization read can serve a pre-revocation
  value and the revocation bound becomes unstateable;
- a condition expression on every mutation, or "check then write" is a race;
- keys built from a verified tenant, so no lookup can match another tenant's item.

A mock returning canned values would pass while the request silently lost its
condition. ``botocore.stub.Stubber`` validates each request against the real
service model and fails if the expected parameters do not match exactly, so it is
the only fake that can hold this contract. (``moto`` is declared in
``pyproject.toml`` but not importable in this environment, and would not assert
the request shape anyway.)
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import ANY, Stubber

from src.agentauth.execution import ExecutionRecord, ExecutionStatus
from src.agentauth.grants import AgentAction, TargetRelationship
from src.agentauth.store import (
    AgentAuthorityStore,
    AuthorityStoreError,
    DispatchLimitReachedError,
    DispatchReservationConflictError,
    EpochRotationConflictError,
    ExecutionAlreadyExistsError,
    ExecutionTransitionConflictError,
    GrantNotFoundError,
)

TABLE = "adp-test-agent-authority"
TENANT = "org-tenant-001"
INVOCATION = "inv-developer-7"
PRINCIPAL = "inv-coordinator#1"
NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)

EXEC_KEY = {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": f"EXEC#{INVOCATION}"}}
GRANT_KEY = {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": f"GRANT#{PRINCIPAL}"}}


@pytest.fixture
def client():
    # No credentials are configured in this environment and none are needed:
    # Stubber intercepts before any request is signed or sent.
    return boto3.client(
        "dynamodb",
        region_name="us-east-1",
        aws_access_key_id="testing",
        aws_secret_access_key="testing",
    )


@pytest.fixture
def stub(client):
    stubber = Stubber(client)
    stubber.activate()
    yield stubber
    stubber.deactivate()


@pytest.fixture
def store(client):
    return AgentAuthorityStore(table_name=TABLE, dynamodb_client=client)


def exec_item(**overrides) -> dict:
    item = {
        "pk": {"S": f"TENANT#{TENANT}"},
        "sk": {"S": f"EXEC#{INVOCATION}"},
        "invocation_id": {"S": INVOCATION},
        "tenant_id": {"S": TENANT},
        "current_attempt": {"N": "1"},
        "status": {"S": "active"},
        "current_credential_epoch": {"N": "2"},
        "min_acceptable_credential_epoch": {"N": "2"},
    }
    item.update(overrides)
    return item


def grant_item(**overrides) -> dict:
    item = {
        "pk": {"S": f"TENANT#{TENANT}"},
        "sk": {"S": f"GRANT#{PRINCIPAL}"},
        "grant_id": {"S": "grant-coordinator-1"},
        "tenant_id": {"S": TENANT},
        "principal": {"S": PRINCIPAL},
        "authority_kind": {"S": "gate_decision"},
        "authority_reference_id": {"S": "decision-abc"},
        "authority_human_id": {"S": "human-operator-1"},
        "authority_org_id": {"S": TENANT},
        "allowed_actions": {"SS": ["monitor", "pause"]},
        "target_relationships": {"SS": ["flow_node"]},
        "flow_id": {"S": "flow-42"},
        "revocation_epoch": {"N": "4"},
        "max_dispatch_concurrency": {"N": "3"},
    }
    item.update(overrides)
    return item


def client_error(code: str) -> dict:
    return {"Error": {"Code": code, "Message": code}}


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestConsistentReads:
    """Every authorization read must be strongly consistent.

    Asserted per read path rather than once, because the flag is passed at each
    call site's `_get` invocation and a future caller could add a read that
    forgets it.
    """

    def test_load_execution_uses_a_consistent_read(self, store, stub):
        stub.add_response(
            "get_item",
            {"Item": exec_item()},
            {"TableName": TABLE, "Key": EXEC_KEY, "ConsistentRead": True},
        )
        assert store.load_execution(invocation_id=INVOCATION, tenant_id=TENANT) is not None
        stub.assert_no_pending_responses()

    def test_load_grant_uses_a_consistent_read(self, store, stub):
        stub.add_response(
            "get_item",
            {"Item": grant_item()},
            {"TableName": TABLE, "Key": GRANT_KEY, "ConsistentRead": True},
        )
        assert store.load_grant(principal=PRINCIPAL, tenant_id=TENANT) is not None
        stub.assert_no_pending_responses()

    def test_dispatch_count_uses_a_consistent_read(self, store, stub):
        stub.add_response(
            "get_item",
            {"Item": {"in_flight": {"N": "2"}}},
            {
                "TableName": TABLE,
                "Key": {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": "RESV#grant-1"}},
                "ConsistentRead": True,
            },
        )
        assert store.active_dispatch_count(grant_id="grant-1", tenant_id=TENANT) == 2
        stub.assert_no_pending_responses()


class TestKeyConstruction:
    def test_execution_and_grant_are_distinct_items_in_one_tenant_partition(self, store, stub):
        """The prefixes are what keep three item kinds from colliding, and the
        tenant partition is what keeps one tenant's authority off another's."""
        stub.add_response(
            "get_item",
            {"Item": exec_item()},
            {"TableName": TABLE, "Key": EXEC_KEY, "ConsistentRead": True},
        )
        stub.add_response(
            "get_item",
            {"Item": grant_item()},
            {"TableName": TABLE, "Key": GRANT_KEY, "ConsistentRead": True},
        )
        store.load_execution(invocation_id=INVOCATION, tenant_id=TENANT)
        store.load_grant(principal=PRINCIPAL, tenant_id=TENANT)
        stub.assert_no_pending_responses()

    def test_missing_arguments_do_not_reach_dynamodb(self, store, stub):
        """A key built from an empty tenant would address a real partition
        (`TENANT#`). Refusing before the call means no such partition is ever read.

        No stubbed responses are queued, so any request here fails the test.
        """
        assert store.load_execution(invocation_id=INVOCATION, tenant_id="") is None
        assert store.load_execution(invocation_id="", tenant_id=TENANT) is None
        assert store.load_grant(principal=PRINCIPAL, tenant_id="") is None
        assert store.load_grant(principal="", tenant_id=TENANT) is None
        stub.assert_no_pending_responses()


class TestAbsenceVersusFailure:
    """`None` and "the store is down" must not be conflated.

    Both refuse, but only one is an operational alarm — and if a read failure
    returned `None`, the policy could not tell a nonexistent grant from an
    unreachable authority.
    """

    def test_absent_execution_returns_none(self, store, stub):
        stub.add_response("get_item", {}, {"TableName": TABLE, "Key": EXEC_KEY, "ConsistentRead": True})
        assert store.load_execution(invocation_id=INVOCATION, tenant_id=TENANT) is None

    def test_absent_grant_returns_none(self, store, stub):
        stub.add_response("get_item", {}, {"TableName": TABLE, "Key": GRANT_KEY, "ConsistentRead": True})
        assert store.load_grant(principal=PRINCIPAL, tenant_id=TENANT) is None

    def test_read_failure_raises_rather_than_returning_none(self, store, stub):
        stub.add_client_error("get_item", service_error_code="InternalServerError")
        with pytest.raises(AuthorityStoreError):
            store.load_execution(invocation_id=INVOCATION, tenant_id=TENANT)

    def test_access_denied_raises_rather_than_returning_none(self, store, stub):
        """The shape of the intended worker refusal.

        A worker role holds no grant on this table, so its read fails with
        AccessDenied — which must surface as an error, never as "no such grant"
        and never as an unrestricted default.
        """
        stub.add_client_error("get_item", service_error_code="AccessDeniedException")
        with pytest.raises(AuthorityStoreError):
            store.load_grant(principal=PRINCIPAL, tenant_id=TENANT)


class TestDeserializationFailsClosed:
    def _load_exec(self, store, stub, item):
        stub.add_response("get_item", {"Item": item}, {"TableName": TABLE, "Key": EXEC_KEY, "ConsistentRead": True})
        return store.load_execution(invocation_id=INVOCATION, tenant_id=TENANT)

    def test_stored_fields_round_trip(self, store, stub):
        record = self._load_exec(
            store,
            stub,
            exec_item(
                current_attempt={"N": "3"},
                workload_binding={"S": "pod-uid-aaa"},
                flow_id={"S": "flow-42"},
                repo={"S": "org/repo"},
            ),
        )
        assert (record.current_attempt, record.status) == (3, ExecutionStatus.ACTIVE)
        assert record.workload_binding == "pod-uid-aaa"
        assert record.repo == "org/repo"

    def test_unknown_status_is_not_treated_as_active(self, store, stub):
        """A future status value, or a corrupted field, must not authorize work."""
        record = self._load_exec(store, stub, exec_item(status={"S": "some_new_state"}))
        assert record.status is ExecutionStatus.CANCELLED

    def test_missing_status_is_not_treated_as_active(self, store, stub):
        item = exec_item()
        del item["status"]
        assert self._load_exec(store, stub, item).status is ExecutionStatus.CANCELLED

    def test_missing_epoch_floor_defaults_to_current_not_to_one(self, store, stub):
        """Defaulting the floor to 1 would accept every historical epoch."""
        item = exec_item(current_credential_epoch={"N": "5"})
        del item["min_acceptable_credential_epoch"]
        record = self._load_exec(store, stub, item)
        assert record.min_acceptable_credential_epoch == 5

    def test_unreadable_overlap_deadline_closes_the_window(self, store, stub):
        """An overlap with no enforceable deadline is an unbounded tolerance, so the
        floor is raised to the current epoch and the old epoch stops verifying."""
        record = self._load_exec(
            store,
            stub,
            exec_item(
                current_credential_epoch={"N": "5"},
                min_acceptable_credential_epoch={"N": "4"},
                epoch_overlap_expires_at={"S": "not-a-timestamp"},
            ),
        )
        assert record.min_acceptable_credential_epoch == 5
        assert record.epoch_overlap_expires_at is None

    def test_valid_overlap_deadline_is_parsed_and_preserved(self, store, stub):
        record = self._load_exec(
            store,
            stub,
            exec_item(
                current_credential_epoch={"N": "5"},
                min_acceptable_credential_epoch={"N": "4"},
                epoch_overlap_expires_at={"S": "2026-09-13T12:00:30Z"},
            ),
        )
        assert record.min_acceptable_credential_epoch == 4
        assert record.epoch_overlap_expires_at == NOW + timedelta(seconds=30)

    def _load_grant(self, store, stub, item):
        stub.add_response("get_item", {"Item": item}, {"TableName": TABLE, "Key": GRANT_KEY, "ConsistentRead": True})
        return store.load_grant(principal=PRINCIPAL, tenant_id=TENANT)

    def test_grant_actions_and_relationships_round_trip(self, store, stub):
        grant = self._load_grant(store, stub, grant_item())
        assert grant.allowed_actions == frozenset({AgentAction.MONITOR, AgentAction.PAUSE})
        assert grant.target_relationships == frozenset({TargetRelationship.FLOW_NODE})
        assert grant.max_dispatch_concurrency == 3

    def test_unknown_action_is_dropped_not_honoured(self, store, stub):
        """ "A permission I do not understand" is "a permission I do not have"."""
        grant = self._load_grant(store, stub, grant_item(allowed_actions={"SS": ["monitor", "launch_missiles"]}))
        assert grant.allowed_actions == frozenset({AgentAction.MONITOR})

    def test_unknown_relationship_is_dropped(self, store, stub):
        grant = self._load_grant(store, stub, grant_item(target_relationships={"SS": ["flow_node", "sibling_ish"]}))
        assert grant.target_relationships == frozenset({TargetRelationship.FLOW_NODE})

    def test_absent_action_set_is_empty_not_missing(self, store, stub):
        """DynamoDB cannot store an empty set, so absence *is* the empty encoding."""
        item = grant_item()
        del item["allowed_actions"]
        assert self._load_grant(store, stub, item).allowed_actions == frozenset()

    def test_unparseable_expiry_refuses_the_grant_rather_than_widening_it(self, store, stub):
        """`expires_at=None` reads as "never expires", so a corrupted expiry must
        not deserialize — it goes down the malformed-record path and returns None."""
        assert self._load_grant(store, stub, grant_item(expires_at={"S": "31st of Never"})) is None

    def test_malformed_grant_returns_none_rather_than_raising(self, store, stub):
        """A 500 here would tell the caller its target exists. Refusal must be
        indistinguishable from "no such grant"."""
        assert self._load_grant(store, stub, grant_item(allowed_actions={"SS": ["pause"]}, delegable_actions={"SS": ["monitor"]})) is None

    def test_revoked_flag_survives_the_round_trip(self, store, stub):
        assert self._load_grant(store, stub, grant_item(revoked={"BOOL": True})).revoked is True


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------


class TestDispatchReservation:
    """Concurrency must be claimed atomically AND accounted per reservation.

    Atomicity alone is not enough, and the first version of this store got that
    wrong. A bare counter incremented by a conditional ADD does stop two callers
    both observing 2-of-3 — but with no record of *who* holds each slot, a
    duplicate release decrements the shared counter and frees a slot another child
    is still occupying, so the ceiling quietly admits an extra dispatch.

    So each reservation is an identified record written in the same transaction as
    the counter. These tests pin both halves, and the transaction, into the
    request: the enforcement is the pair, not either item alone.
    """

    RESV_ITEM_SK = "RESV#grant-1#resv-child-1"
    COUNTER_SK = "RESV#grant-1"

    def _expected_reserve(self, *, ceiling: str = "3") -> dict:
        return {
            "TransactItems": [
                {
                    "Put": {
                        "TableName": TABLE,
                        "Item": {
                            "pk": {"S": f"TENANT#{TENANT}"},
                            "sk": {"S": self.RESV_ITEM_SK},
                            "grant_id": {"S": "grant-1"},
                            "reservation_id": {"S": "resv-child-1"},
                            "state": {"S": "held"},
                            "created_at": ANY,
                            "updated_at": ANY,
                        },
                        "ConditionExpression": "attribute_not_exists(sk)",
                    }
                },
                {
                    "Update": {
                        "TableName": TABLE,
                        "Key": {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": self.COUNTER_SK}},
                        "UpdateExpression": "ADD in_flight :one SET updated_at = :now",
                        "ConditionExpression": "attribute_not_exists(in_flight) OR in_flight < :ceiling",
                        "ExpressionAttributeValues": {
                            ":one": {"N": "1"},
                            ":ceiling": {"N": ceiling},
                            ":now": ANY,
                        },
                    }
                },
            ]
        }

    def _reserve(self, store):
        return store.reserve_dispatch(grant_id="grant-1", tenant_id=TENANT, reservation_id="resv-child-1", max_concurrency=3)

    def _cancelled(self, *reasons: str) -> dict:
        """A TransactionCanceledException with positional per-item reason codes."""
        return {
            "Error": {"Code": "TransactionCanceledException", "Message": "cancelled"},
            "CancellationReasons": [{"Code": reason} for reason in reasons],
        }

    def test_reservation_writes_the_slot_and_the_counter_in_one_transaction(self, store, stub):
        """Neither item alone is a correct reservation.

        A counter incremented without its record is a leaked slot nothing can ever
        release; a record without the increment is an unbounded ceiling. One
        transaction is what makes both impossible.
        """
        stub.add_response("transact_write_items", {}, self._expected_reserve())
        stub.add_response(
            "get_item",
            {"Item": {"in_flight": {"N": "3"}}},
            {
                "TableName": TABLE,
                "Key": {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": self.COUNTER_SK}},
                "ConsistentRead": True,
            },
        )
        assert self._reserve(store) == 3
        stub.assert_no_pending_responses()

    def test_a_duplicate_reservation_maps_to_a_conflict_not_a_limit(self, store):
        """Index 0 of CancellationReasons is the reservation record."""
        exc = ClientError(self._cancelled("ConditionalCheckFailed", "None"), "TransactWriteItems")
        with pytest.raises(DispatchReservationConflictError):
            store._raise_reservation_failure(exc, grant_id="grant-1", reservation_id="resv-child-1")

    def test_a_full_ceiling_maps_to_a_limit_not_a_conflict(self, store):
        """Index 1 is the counter, so this is genuine backpressure to retry later."""
        exc = ClientError(self._cancelled("None", "ConditionalCheckFailed"), "TransactWriteItems")
        with pytest.raises(DispatchLimitReachedError):
            store._raise_reservation_failure(exc, grant_id="grant-1", reservation_id="resv-child-1")

    def test_a_cancellation_for_another_reason_is_not_reported_as_a_limit(self, store):
        """Reporting a throttle as "budget exhausted" would tell the caller its
        budget is spent when it is not, silently shrinking the effective ceiling."""
        exc = ClientError(self._cancelled("TransactionConflict", "None"), "TransactWriteItems")
        with pytest.raises(AuthorityStoreError):
            store._raise_reservation_failure(exc, grant_id="grant-1", reservation_id="resv-child-1")

    def test_unreadable_cancellation_reasons_still_fail_closed(self, store):
        """A caller that cannot parse the reasons must not raise a second error
        while handling the first, and must not fall through to success."""
        exc = ClientError({"Error": {"Code": "TransactionCanceledException", "Message": "cancelled"}}, "TransactWriteItems")
        with pytest.raises(AuthorityStoreError):
            store._raise_reservation_failure(exc, grant_id="grant-1", reservation_id="resv-child-1")

    def test_zero_budget_is_refused_without_a_write(self, store, stub):
        """A grant conveying no dispatch budget performs no write at all."""
        with pytest.raises(DispatchLimitReachedError):
            store.reserve_dispatch(grant_id="grant-1", tenant_id=TENANT, reservation_id="resv-child-1", max_concurrency=0)
        stub.assert_no_pending_responses()

    def test_a_reservation_without_an_id_is_refused_before_any_write(self, store, stub):
        """An unidentified reservation is the old broken counter by another name,
        so it must be impossible to make rather than defaulted."""
        with pytest.raises(AuthorityStoreError):
            store.reserve_dispatch(grant_id="grant-1", tenant_id=TENANT, reservation_id="", max_concurrency=3)
        stub.assert_no_pending_responses()

    def test_store_failure_is_not_mistaken_for_a_limit(self, store, stub):
        """A throttle or outage must alarm, not read as "budget exhausted"."""
        stub.add_client_error("transact_write_items", service_error_code="ProvisionedThroughputExceededException")
        with pytest.raises(AuthorityStoreError):
            self._reserve(store)

    def test_release_marks_the_reservation_and_decrements_together(self, store, stub):
        """The state transition and the decrement must not be separable: a released
        record whose counter was not decremented leaks budget permanently."""
        stub.add_response(
            "transact_write_items",
            {},
            {
                "TransactItems": [
                    {
                        "Update": {
                            "TableName": TABLE,
                            "Key": {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": self.RESV_ITEM_SK}},
                            "UpdateExpression": "SET #st = :released, updated_at = :now",
                            "ConditionExpression": "attribute_exists(sk) AND #st = :held",
                            "ExpressionAttributeNames": {"#st": "state"},
                            "ExpressionAttributeValues": {
                                ":released": {"S": "released"},
                                ":held": {"S": "held"},
                                ":now": ANY,
                            },
                        }
                    },
                    {
                        "Update": {
                            "TableName": TABLE,
                            "Key": {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": self.COUNTER_SK}},
                            "UpdateExpression": "ADD in_flight :minus SET updated_at = :now",
                            "ConditionExpression": "in_flight > :zero",
                            "ExpressionAttributeValues": {
                                ":minus": {"N": "-1"},
                                ":zero": {"N": "0"},
                                ":now": ANY,
                            },
                        }
                    },
                ]
            },
        )
        store.release_dispatch(grant_id="grant-1", tenant_id=TENANT, reservation_id="resv-child-1")
        stub.assert_no_pending_responses()

    def test_a_duplicate_release_does_not_free_another_childs_slot(self, store, stub):
        """The exact over-dispatch this design replaces.

        The held-state condition cancels the whole transaction, so the counter is
        untouched. Idempotent by state, not by swallowing a decrement.
        """
        stub.add_client_error("transact_write_items", service_error_code="TransactionCanceledException")
        stub.add_response(
            "get_item",
            {"Item": {"state": {"S": "released"}, "grant_id": {"S": "grant-1"}, "reservation_id": {"S": "resv-child-1"}}},
            {
                "TableName": TABLE,
                "Key": {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": "RESV#grant-1#resv-child-1"}},
                "ConsistentRead": True,
            },
        )
        store.release_dispatch(grant_id="grant-1", tenant_id=TENANT, reservation_id="resv-child-1")
        stub.assert_no_pending_responses()

    def test_releasing_a_reservation_never_made_is_a_no_op(self, store, stub):
        """So a confused caller cannot manufacture budget out of nothing."""
        stub.add_client_error("transact_write_items", service_error_code="TransactionCanceledException")
        stub.add_response(
            "get_item",
            {},
            {
                "TableName": TABLE,
                "Key": {"pk": {"S": f"TENANT#{TENANT}"}, "sk": {"S": "RESV#grant-1#never-existed"}},
                "ConsistentRead": True,
            },
        )
        store.release_dispatch(grant_id="grant-1", tenant_id=TENANT, reservation_id="never-existed")
        stub.assert_no_pending_responses()

    def test_a_release_without_an_id_is_refused_before_any_write(self, store, stub):
        with pytest.raises(AuthorityStoreError):
            store.release_dispatch(grant_id="grant-1", tenant_id=TENANT, reservation_id="")
        stub.assert_no_pending_responses()

    def test_release_store_failure_still_raises(self, store, stub):
        """An outage must not be reported as completed cleanup."""
        stub.add_client_error("transact_write_items", service_error_code="InternalServerError")
        with pytest.raises(AuthorityStoreError):
            store.release_dispatch(grant_id="grant-1", tenant_id=TENANT, reservation_id="resv-child-1")

    def test_missing_arguments_raise_rather_than_reporting_zero_in_flight(self, store, stub):
        """0 means "budget fully available". Returning it for "I could not build a
        key" would turn a bug into an unlimited dispatch allowance."""
        with pytest.raises(AuthorityStoreError):
            store.active_dispatch_count(grant_id="", tenant_id=TENANT)
        with pytest.raises(AuthorityStoreError):
            store.active_dispatch_count(grant_id="grant-1", tenant_id="")
        stub.assert_no_pending_responses()

    def test_absent_counter_reads_as_zero(self, store, stub):
        """No reservations yet is genuinely zero in flight."""
        stub.add_response("get_item", {}, {"TableName": TABLE, "Key": ANY, "ConsistentRead": True})
        assert store.active_dispatch_count(grant_id="grant-1", tenant_id=TENANT) == 0


# ---------------------------------------------------------------------------
# Trusted writes
# ---------------------------------------------------------------------------


def record(**overrides) -> ExecutionRecord:
    kwargs = {
        "invocation_id": INVOCATION,
        "tenant_id": TENANT,
        "current_attempt": 1,
        "status": ExecutionStatus.ACTIVE,
        "current_credential_epoch": 1,
        "min_acceptable_credential_epoch": 1,
    }
    kwargs.update(overrides)
    return ExecutionRecord(**kwargs)


class TestPutExecution:
    def test_new_execution_is_written_guarded_against_overwrite(self, store, stub):
        """`attribute_not_exists(sk)` is the whole protection here: without it, a
        fresh pod could supersede a live authorized attempt by naming its run."""
        stub.add_response(
            "put_item",
            {},
            {
                "TableName": TABLE,
                "Item": {
                    **EXEC_KEY,
                    "invocation_id": {"S": INVOCATION},
                    "tenant_id": {"S": TENANT},
                    "current_attempt": {"N": "1"},
                    "status": {"S": "active"},
                    "current_credential_epoch": {"N": "1"},
                    "min_acceptable_credential_epoch": {"N": "1"},
                    "flow_id": {"S": "flow-42"},
                },
                "ConditionExpression": "attribute_not_exists(sk)",
            },
        )
        store.put_execution(record=record(flow_id="flow-42"))
        stub.assert_no_pending_responses()

    def test_overwriting_an_existing_execution_is_refused(self, store, stub):
        stub.add_client_error("put_item", service_error_code="ConditionalCheckFailedException")
        with pytest.raises(ExecutionAlreadyExistsError):
            store.put_execution(record=record())

    def test_optional_fields_are_omitted_rather_than_written_empty(self, store, stub):
        """An empty-string `workload_binding` would compare unequal to every
        presented binding *and* be non-None, i.e. it would bind the execution to
        nothing and refuse everyone."""
        stub.add_response(
            "put_item",
            {},
            {
                "TableName": TABLE,
                "Item": {
                    **EXEC_KEY,
                    "invocation_id": {"S": INVOCATION},
                    "tenant_id": {"S": TENANT},
                    "current_attempt": {"N": "1"},
                    "status": {"S": "active"},
                    "current_credential_epoch": {"N": "1"},
                    "min_acceptable_credential_epoch": {"N": "1"},
                },
                "ConditionExpression": "attribute_not_exists(sk)",
            },
        )
        store.put_execution(record=record())
        stub.assert_no_pending_responses()


class TestRotateCredentialEpoch:
    def test_rotation_touches_only_epoch_fields(self, store, stub):
        """Attempt, status and workload binding must survive a renewal untouched:
        the command journal and control generation are keyed to the attempt, so a
        renewal that reset it would detach them."""
        stub.add_response(
            "update_item",
            {"Attributes": {"current_credential_epoch": {"N": "3"}}},
            {
                "TableName": TABLE,
                "Key": EXEC_KEY,
                "UpdateExpression": (
                    "SET current_credential_epoch = :next, "
                    "min_acceptable_credential_epoch = :floor, "
                    "epoch_overlap_expires_at = :overlap, "
                    "updated_at = :now"
                ),
                "ConditionExpression": (
                    "current_credential_epoch = :expected AND #st = :active AND current_attempt = :attempt AND workload_binding = :binding"
                ),
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {
                    ":next": {"N": "3"},
                    ":floor": {"N": "2"},
                    ":overlap": {"S": "2026-09-13T12:00:30Z"},
                    ":expected": {"N": "2"},
                    ":attempt": {"N": "1"},
                    ":binding": {"S": "pod-uid-a"},
                    ":active": {"S": "active"},
                    ":now": {"S": "2026-09-13T12:00:00Z"},
                },
                "ReturnValues": "UPDATED_NEW",
            },
        )
        assert (
            store.rotate_credential_epoch(
                invocation_id=INVOCATION,
                tenant_id=TENANT,
                expected_epoch=2,
                expected_attempt=1,
                workload_binding="pod-uid-a",
                overlap_seconds=30,
                now=NOW,
            )
            == 3
        )
        stub.assert_no_pending_responses()

    def test_floor_stays_at_the_outgoing_epoch_during_the_overlap(self, store, stub):
        """Which is what lets a request in flight at rotation finish instead of
        failing mid-task. Pinned via the `:floor` value above and here."""
        stub.add_response(
            "update_item",
            {"Attributes": {"current_credential_epoch": {"N": "8"}}},
            {
                "TableName": TABLE,
                "Key": EXEC_KEY,
                "UpdateExpression": ANY,
                "ConditionExpression": ANY,
                "ExpressionAttributeNames": ANY,
                "ExpressionAttributeValues": {
                    ":next": {"N": "8"},
                    ":floor": {"N": "7"},
                    ":overlap": ANY,
                    ":expected": {"N": "7"},
                    ":attempt": {"N": "1"},
                    ":binding": {"S": "pod-uid-a"},
                    ":active": {"S": "active"},
                    ":now": ANY,
                },
                "ReturnValues": "UPDATED_NEW",
            },
        )
        store.rotate_credential_epoch(
            invocation_id=INVOCATION,
            tenant_id=TENANT,
            expected_epoch=7,
            expected_attempt=1,
            workload_binding="pod-uid-a",
            overlap_seconds=30,
            now=NOW,
        )
        stub.assert_no_pending_responses()

    def test_concurrent_rotation_loses_rather_than_skipping_an_epoch(self, store, stub):
        """Two renewals must not both advance the epoch. The loser retries against
        the new value."""
        stub.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        with pytest.raises(EpochRotationConflictError):
            store.rotate_credential_epoch(
                invocation_id=INVOCATION,
                tenant_id=TENANT,
                expected_epoch=2,
                expected_attempt=1,
                workload_binding="pod-uid-a",
                overlap_seconds=30,
                now=NOW,
            )

    def test_cancelled_execution_cannot_renew(self, store, stub):
        """AC6: the status half of the same condition. Indistinguishable from the
        epoch conflict at the wire level, which is why the condition carries both."""
        stub.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        with pytest.raises(EpochRotationConflictError):
            store.rotate_credential_epoch(
                invocation_id=INVOCATION,
                tenant_id=TENANT,
                expected_epoch=2,
                expected_attempt=1,
                workload_binding="pod-uid-a",
                overlap_seconds=30,
                now=NOW,
            )

    def test_negative_overlap_does_not_backdate_the_window(self, store, stub):
        """A negative overlap would set a deadline in the past. Clamped to zero, so
        the worst case is "no overlap", never "a window that was never open"."""
        stub.add_response(
            "update_item",
            {"Attributes": {"current_credential_epoch": {"N": "3"}}},
            {
                "TableName": TABLE,
                "Key": EXEC_KEY,
                "UpdateExpression": ANY,
                "ConditionExpression": ANY,
                "ExpressionAttributeNames": ANY,
                "ExpressionAttributeValues": {
                    ":next": {"N": "3"},
                    ":floor": {"N": "2"},
                    ":overlap": {"S": "2026-09-13T12:00:00Z"},
                    ":expected": {"N": "2"},
                    ":attempt": {"N": "1"},
                    ":binding": {"S": "pod-uid-a"},
                    ":active": {"S": "active"},
                    ":now": {"S": "2026-09-13T12:00:00Z"},
                },
                "ReturnValues": "UPDATED_NEW",
            },
        )
        store.rotate_credential_epoch(
            invocation_id=INVOCATION,
            tenant_id=TENANT,
            expected_epoch=2,
            expected_attempt=1,
            workload_binding="pod-uid-a",
            overlap_seconds=-600,
            now=NOW,
        )
        stub.assert_no_pending_responses()


class TestSetExecutionStatus:
    def test_status_write_requires_the_record_to_exist(self, store, stub):
        """An execution brought into being by a status update would have no
        authority reference behind it."""
        stub.add_response(
            "update_item",
            {},
            {
                "TableName": TABLE,
                "Key": EXEC_KEY,
                "UpdateExpression": "SET #st = :status, updated_at = :now",
                "ConditionExpression": "current_attempt = :attempt AND (#st = :expected OR #st = :status)",
                "ExpressionAttributeNames": {"#st": "status"},
                "ExpressionAttributeValues": {":status": {"S": "cancelled"}, ":expected": {"S": "active"}, ":attempt": {"N": "1"}, ":now": ANY},
            },
        )
        store.set_execution_status(
            invocation_id=INVOCATION, tenant_id=TENANT, status=ExecutionStatus.CANCELLED, expected_attempt=1, expected_status=ExecutionStatus.ACTIVE
        )
        stub.assert_no_pending_responses()

    def test_status_write_on_a_missing_record_is_refused(self, store, stub):
        stub.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        with pytest.raises(ExecutionTransitionConflictError):
            store.set_execution_status(
                invocation_id=INVOCATION,
                tenant_id=TENANT,
                status=ExecutionStatus.COMPLETED,
                expected_attempt=1,
                expected_status=ExecutionStatus.ACTIVE,
            )


class TestRevokeGrant:
    def test_revocation_sets_the_flag_and_bumps_the_epoch_in_one_update(self, store, stub):
        """Separately would leave a gap: the flag alone lets an already-queued
        action's epoch check still pass, and the epoch alone lets a fresh request
        still authorize."""
        stub.add_response(
            "update_item",
            {},
            {
                "TableName": TABLE,
                "Key": GRANT_KEY,
                "UpdateExpression": "SET revoked = :true, updated_at = :now ADD revocation_epoch :one",
                "ConditionExpression": "attribute_exists(sk)",
                "ExpressionAttributeValues": {":true": {"BOOL": True}, ":one": {"N": "1"}, ":now": ANY},
            },
        )
        store.revoke_grant(principal=PRINCIPAL, tenant_id=TENANT)
        stub.assert_no_pending_responses()

    def test_revoking_an_absent_grant_is_refused(self, store, stub):
        """So a caller cannot believe it revoked authority that was never there."""
        stub.add_client_error("update_item", service_error_code="ConditionalCheckFailedException")
        with pytest.raises(GrantNotFoundError):
            store.revoke_grant(principal=PRINCIPAL, tenant_id=TENANT)


class TestTableNaming:
    def test_table_name_comes_from_the_environment(self, client, monkeypatch):
        """Env indirection so dev and prod cannot share an authority table."""
        monkeypatch.setenv("AGENT_AUTHORITY_TABLE", "adp-prod-agent-authority")
        assert AgentAuthorityStore(dynamodb_client=client).table_name == "adp-prod-agent-authority"

    def test_explicit_table_name_wins_over_the_environment(self, client, monkeypatch):
        monkeypatch.setenv("AGENT_AUTHORITY_TABLE", "adp-prod-agent-authority")
        assert AgentAuthorityStore(table_name=TABLE, dynamodb_client=client).table_name == TABLE
