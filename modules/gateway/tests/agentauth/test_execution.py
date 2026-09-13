"""Live execution state validation (#5028 AC2, AC5, AC6).

Every test here answers one question: *can a credential that verifies perfectly
still be refused?* That is the property `evaluate_execution_state` exists for, so
the file is organized by what changed between minting and use rather than by
function.

No AWS here by design — `execution.py` is the pure half of the check, so each
refusal below is reachable without a table. The request contract for the store
that feeds it is asserted in `test_store.py`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.agentauth.execution import (
    ExecutionRecord,
    ExecutionStateError,
    ExecutionStatus,
    evaluate_execution_state,
)

NOW = datetime(2026, 9, 13, 12, 0, 0, tzinfo=UTC)
TENANT = "org-tenant-001"
INVOCATION = "inv-developer-7"


def record(**overrides) -> ExecutionRecord:
    kwargs = {
        "invocation_id": INVOCATION,
        "tenant_id": TENANT,
        "current_attempt": 1,
        "status": ExecutionStatus.ACTIVE,
        "current_credential_epoch": 1,
        "min_acceptable_credential_epoch": 1,
        "flow_id": "flow-42",
    }
    kwargs.update(overrides)
    return ExecutionRecord(**kwargs)


def evaluate(**overrides):
    kwargs = {
        "record": record(),
        "invocation_id": INVOCATION,
        "attempt": 1,
        "tenant_id": TENANT,
        "credential_epoch": 1,
        "now": NOW,
    }
    kwargs.update(overrides)
    return evaluate_execution_state(**kwargs)


def refusal(**overrides) -> str:
    """Evaluate expecting a refusal, and return its audit reason."""
    with pytest.raises(ExecutionStateError) as excinfo:
        evaluate(**overrides)
    return excinfo.value.reason


class TestLegitimateExecution:
    def test_active_current_attempt_and_epoch_is_accepted(self):
        """The baseline. If this fails, every refusal below proves nothing."""
        assert evaluate().invocation_id == INVOCATION

    def test_returns_the_stored_record_not_the_presented_claims(self):
        """Callers must read tenant and flow from the store, not the credential.

        The credential's own claims are attacker-adjacent (they are whatever was
        minted for a run whose code the agent authors); the store's copy is
        protected. Returning the record is what lets the policy prefer it.
        """
        stored = record(flow_id="flow-authoritative", repo="org/real-repo")
        result = evaluate(record=stored)
        assert result.flow_id == "flow-authoritative"
        assert result.repo == "org/real-repo"

    def test_principal_is_invocation_and_current_attempt(self):
        assert record(current_attempt=3).principal == f"{INVOCATION}#3"


class TestFailClosedOnAbsence:
    def test_missing_record_is_refused(self):
        """The inverse of the usual default.

        "No record, no restriction" would make an authority-store outage an
        authorization bypass — the store is the authority on which executions
        exist, so silence from it cannot mean permission.
        """
        assert refusal(record=None) == "execution_not_found"


class TestIdentityAndTenant:
    def test_record_for_a_different_invocation_is_refused(self):
        assert refusal(record=record(invocation_id="inv-someone-else")) == "execution_identity_mismatch"

    def test_cross_tenant_credential_is_refused(self):
        assert refusal(tenant_id="org-tenant-999") == "execution_tenant_mismatch"

    def test_empty_tenant_is_refused_rather_than_treated_as_wildcard(self):
        """An absent tenant must not compare equal to anything."""
        assert refusal(record=record(tenant_id=""), tenant_id="") == "execution_tenant_mismatch"

    def test_tenant_is_checked_before_status(self):
        """Otherwise a refusal reason leaks another tenant's run lifecycle.

        With status first, probing a cross-tenant invocation would return
        "cancelled" for a cancelled run and "tenant mismatch" for a live one —
        an oracle for the existence and state of runs the caller cannot see.
        """
        cancelled_elsewhere = record(tenant_id="org-tenant-999", status=ExecutionStatus.CANCELLED)
        assert refusal(record=cancelled_elsewhere) == "execution_tenant_mismatch"


class TestStatus:
    @pytest.mark.parametrize(
        "status",
        [
            ExecutionStatus.PENDING,
            ExecutionStatus.COMPLETED,
            ExecutionStatus.CANCELLED,
            ExecutionStatus.REVOKED,
        ],
    )
    def test_only_active_may_act(self, status):
        assert refusal(record=record(status=status)) == f"execution_not_active:{status.value}"

    def test_cancellation_refuses_an_unexpired_credential(self):
        """AC6's revocation requirement, at the check that enforces it.

        The credential is unchanged and unexpired; only the store moved. This is
        the whole reason revocation cannot be expressed in the token.
        """
        assert "execution_not_active" in refusal(record=record(status=ExecutionStatus.CANCELLED))

    def test_audit_reason_names_the_specific_status(self):
        """One opaque refusal for the caller, a specific reason for the operator."""
        assert refusal(record=record(status=ExecutionStatus.REVOKED)).endswith("revoked")


class TestAttemptSupersession:
    def test_credential_from_a_superseded_attempt_is_refused(self):
        """The retry case: attempt 1's pod is replaced, its token still verifies."""
        assert refusal(record=record(current_attempt=2), attempt=1) == "execution_attempt_superseded"

    def test_attempt_ahead_of_the_store_is_refused(self):
        """Attempts are created by trusted dispatch, which writes before the pod runs.

        So an attempt the store has not seen cannot have been dispatched, whatever
        the credential says.
        """
        assert refusal(attempt=2) == "execution_attempt_unknown"

    def test_current_attempt_is_accepted_at_any_number(self):
        assert evaluate(record=record(current_attempt=5), attempt=5).current_attempt == 5


class TestCredentialEpoch:
    def test_epoch_below_the_floor_is_refused(self):
        stale = record(current_credential_epoch=3, min_acceptable_credential_epoch=3)
        assert refusal(record=stale, credential_epoch=2) == "credential_epoch_superseded"

    def test_previous_epoch_is_accepted_inside_an_open_overlap_window(self):
        """Renewal needs two epochs briefly valid, or rotation kills in-flight work."""
        rotating = record(
            current_credential_epoch=3,
            min_acceptable_credential_epoch=2,
            epoch_overlap_expires_at=NOW + timedelta(seconds=30),
        )
        assert evaluate(record=rotating, credential_epoch=2).current_credential_epoch == 3

    def test_previous_epoch_is_refused_once_the_overlap_deadline_passes(self):
        """The bound that makes the overlap a window rather than a permanent tolerance.

        The floor field still says 2. The deadline is what closes it — which is
        why the design stores a deadline instead of computing `current - 1`.
        """
        expired = record(
            current_credential_epoch=3,
            min_acceptable_credential_epoch=2,
            epoch_overlap_expires_at=NOW - timedelta(seconds=1),
        )
        assert refusal(record=expired, credential_epoch=2) == "credential_epoch_superseded"

    def test_overlap_closes_exactly_at_the_deadline(self):
        """`now == deadline` is expiry, not grace. Pinned so a later refactor to
        `>` cannot quietly extend every overlap window."""
        boundary = record(
            current_credential_epoch=3,
            min_acceptable_credential_epoch=2,
            epoch_overlap_expires_at=NOW,
        )
        assert refusal(record=boundary, credential_epoch=2) == "credential_epoch_superseded"

    def test_current_epoch_still_works_after_the_overlap_closed(self):
        """Closing the window must not refuse the epoch it rotated *to*."""
        expired = record(
            current_credential_epoch=3,
            min_acceptable_credential_epoch=2,
            epoch_overlap_expires_at=NOW - timedelta(seconds=1),
        )
        assert evaluate(record=expired, credential_epoch=3).current_credential_epoch == 3

    def test_epoch_above_what_was_issued_is_refused(self):
        """Not reachable with a genuinely minted credential — so it means forgery."""
        assert refusal(credential_epoch=99) == "credential_epoch_unknown"


class TestWorkloadBinding:
    def test_unbound_execution_ignores_the_presented_binding(self):
        """Bootstrap binding is not implemented yet (Decision 7), so today's records
        carry no binding and the check must stay inert rather than refuse everything."""
        assert evaluate(presented_workload_binding="pod-uid-anything").workload_binding is None

    def test_bound_execution_refuses_a_different_workload(self):
        """The leaked-credential case: the token names the run it was issued for,
        and only the binding can say which pod is presenting it."""
        bound = record(workload_binding="pod-uid-aaa")
        assert refusal(record=bound, presented_workload_binding="pod-uid-bbb") == "workload_binding_mismatch"

    def test_bound_execution_refuses_a_caller_presenting_no_binding(self):
        """A caller that omits the binding must not thereby skip the check."""
        bound = record(workload_binding="pod-uid-aaa")
        assert refusal(record=bound, presented_workload_binding=None) == "workload_binding_mismatch"

    def test_bound_execution_accepts_the_bound_workload(self):
        bound = record(workload_binding="pod-uid-aaa")
        assert evaluate(record=bound, presented_workload_binding="pod-uid-aaa") is bound


class TestRecordIsImmutable:
    def test_record_cannot_be_mutated_after_the_read(self):
        """It is an authorization input. Code able to edit it post-read would have
        reintroduced the tampering the protected table exists to prevent."""
        with pytest.raises(Exception):
            record().status = ExecutionStatus.ACTIVE  # type: ignore[misc]
