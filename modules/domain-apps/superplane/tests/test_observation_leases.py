"""Leases: lock acquire/release without a domain table grant.

Issue #5043 (U8), EPIC #4910. Design item 4 of the story's scope list.

The monitor coordinates today by writing `reconcile_locks` directly, which is
exactly the grant U15 withdraws. Withdrawing it without giving the monitor another
way to serialize its work would not make the monitor safer — it would make it
concurrent with itself. So the contract has to express "I hold the right to
reconcile this scope, for a while" without the holder touching a table.

Three properties are asserted, and the third is the one usually missed:

* **expiry** — a lease is time-bounded, so a monitor that dies mid-reconcile does
  not hold the scope until an operator clears it;
* **ownership on release** — a lease cannot be released by whoever asks;
* **fencing** — expiry alone leaves a window in which the previous holder still
  believes it holds the lease (its clock, its network, a long GC pause) while a
  new holder legitimately does. Both then act. The monotonically increasing
  fence token is what actually prevents the overlap; expiry only bounds it.

Every time value is passed in rather than read from the clock, so the receiver's
clock decides expiry — a lease that expired according to the *holder's* clock is
not a fact the holder gets to assert — and so every branch is testable without
patching time.
"""

from __future__ import annotations

from datetime import timedelta

import _contracts_path  # noqa: F401  (imported for its sys.path side effect)
import pytest
from conftest import OBSERVED_AT, lease_duration
from superplane_contracts import (
    DEFAULT_LEASE_DURATION,
    MAX_LEASE_DURATION,
    ContractViolation,
    Lease,
    LeaseRequest,
    authorize_release,
    grant,
    is_fenced_out,
)


class TestLeaseRequest:
    """A request cannot ask for an unbounded or nameless hold."""

    def test_default_duration_is_bounded_and_short(self) -> None:
        request = LeaseRequest(scope="reconcile:ws-w1", holder="monitor-1")
        assert request.duration == DEFAULT_LEASE_DURATION
        assert request.duration <= MAX_LEASE_DURATION

    def test_blank_scope_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="scope"):
            LeaseRequest(scope="  ", holder="monitor-1")

    def test_blank_holder_is_refused(self) -> None:
        """An anonymous holder cannot be checked on release."""
        with pytest.raises(ContractViolation, match="holder"):
            LeaseRequest(scope="reconcile:ws-w1", holder="")

    def test_zero_or_negative_duration_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="positive"):
            LeaseRequest(
                scope="reconcile:ws-w1", holder="monitor-1", duration=timedelta(0)
            )
        with pytest.raises(ContractViolation, match="positive"):
            LeaseRequest(
                scope="reconcile:ws-w1",
                holder="monitor-1",
                duration=lease_duration(-5),
            )

    def test_duration_above_the_ceiling_is_refused(self) -> None:
        """An unbounded request would switch the expiry property off.

        A submitter asking for a 30-day lease has functionally taken the table
        lock the withdrawal was meant to remove.
        """
        with pytest.raises(ContractViolation, match="exceeds the maximum"):
            LeaseRequest(
                scope="reconcile:ws-w1",
                holder="monitor-1",
                duration=MAX_LEASE_DURATION + timedelta(seconds=1),
            )

    def test_duration_exactly_at_the_ceiling_is_allowed(self) -> None:
        """The ceiling is inclusive, so the boundary is not off by one."""
        request = LeaseRequest(
            scope="reconcile:ws-w1", holder="monitor-1", duration=MAX_LEASE_DURATION
        )
        assert request.duration == MAX_LEASE_DURATION


class TestGrant:
    """Granting sets expiry from the receiver's clock and advances the fence."""

    def _request(self, holder: str = "monitor-1") -> LeaseRequest:
        return LeaseRequest(
            scope="reconcile:ws-w1", holder=holder, duration=lease_duration(60)
        )

    def test_expiry_is_the_receivers_now_plus_the_duration(self) -> None:
        lease = grant(self._request(), now=OBSERVED_AT)
        assert lease.expires_at == OBSERVED_AT + lease_duration(60)

    def test_first_grant_starts_the_fence_at_one(self) -> None:
        """Tokens start at 1, so 0 is never a valid held token.

        That makes "no lease has ever been granted for this scope" distinguishable
        from "a lease exists at token 0".
        """
        assert grant(self._request(), now=OBSERVED_AT).fence_token == 1

    def test_each_grant_advances_the_fence(self) -> None:
        first = grant(self._request(), now=OBSERVED_AT)
        second = grant(
            self._request("monitor-2"),
            now=OBSERVED_AT,
            previous_token=first.fence_token,
        )
        assert second.fence_token == first.fence_token + 1

    def test_token_is_not_chosen_by_the_requester(self) -> None:
        """`LeaseRequest` has no token field.

        A requester-chosen token is not a fence — it is a field the requester sets
        to whatever gets its work accepted. Asserted as an absence, because the
        absence is the design decision.
        """
        assert not hasattr(self._request(), "fence_token")

    def test_naive_now_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            grant(self._request(), now=OBSERVED_AT.replace(tzinfo=None))


class TestLeaseValidation:
    """A lease itself cannot be malformed."""

    def test_naive_expiry_is_refused(self) -> None:
        """A naive expiry cannot be compared against an aware clock.

        Guessing a zone produces a lease that expires hours early or late.
        """
        with pytest.raises(ContractViolation, match="timezone-aware"):
            Lease(
                scope="reconcile:ws-w1",
                holder="monitor-1",
                expires_at=OBSERVED_AT.replace(tzinfo=None),
                fence_token=1,
            )

    def test_non_positive_fence_token_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="positive integer"):
            Lease(
                scope="reconcile:ws-w1",
                holder="monitor-1",
                expires_at=OBSERVED_AT,
                fence_token=0,
            )


class TestExpiry:
    """Expiry is decided by the receiver's clock, not the holder's."""

    def _lease(self) -> Lease:
        return grant(
            LeaseRequest(
                scope="reconcile:ws-w1", holder="monitor-1", duration=lease_duration(60)
            ),
            now=OBSERVED_AT,
        )

    def test_not_expired_before_the_deadline(self) -> None:
        assert not self._lease().is_expired(OBSERVED_AT + lease_duration(59))

    def test_expired_at_the_deadline(self) -> None:
        """The boundary is inclusive: at the expiry instant the lease is gone.

        Treating the deadline as still-held would leave a one-tick window in
        which two holders both believe they hold it.
        """
        assert self._lease().is_expired(OBSERVED_AT + lease_duration(60))

    def test_expired_after_the_deadline(self) -> None:
        assert self._lease().is_expired(OBSERVED_AT + lease_duration(61))

    def test_naive_now_is_refused(self) -> None:
        with pytest.raises(ContractViolation, match="timezone-aware"):
            self._lease().is_expired(OBSERVED_AT.replace(tzinfo=None))


class TestRelease:
    """A lease is released only by its holder."""

    def _lease(self, holder: str = "monitor-1") -> Lease:
        return grant(
            LeaseRequest(scope="reconcile:ws-w1", holder=holder), now=OBSERVED_AT
        )

    def test_holder_can_release(self) -> None:
        decision = authorize_release(self._lease(), holder="monitor-1")
        assert decision.granted
        assert decision.lease is not None

    def test_another_caller_cannot_release(self) -> None:
        """Freeing another monitor's lease would induce the concurrency it prevents.

        A denial-of-correctness rather than a denial-of-service, and harder to
        notice — two reconcilers running is not an error either of them reports.
        """
        decision = authorize_release(self._lease(), holder="monitor-2")
        assert not decision.granted
        assert decision.reason == "lease not held by this caller"
        assert decision.lease is None

    def test_release_check_is_exact_not_prefix(self) -> None:
        """A holder name that merely starts the same is a different holder."""
        assert not authorize_release(self._lease(), holder="monitor-11").granted


class TestFencing:
    """The property that closes the overlap window expiry leaves open."""

    def test_stale_token_is_fenced_out(self) -> None:
        """The previous holder's work is refused once a newer lease exists.

        This is the case expiry alone cannot handle: the old holder's clock says
        it still holds the lease, and it submits work. The receiver refuses on the
        token rather than trusting either clock.
        """
        assert is_fenced_out(observed_token=1, highest_seen_token=2)

    def test_current_token_is_not_fenced_out(self) -> None:
        """Strictly-less-than, not less-or-equal.

        The current holder's token equals the highest seen, and it must keep
        working — an off-by-one here would fence out the legitimate holder
        immediately after granting it the lease.
        """
        assert not is_fenced_out(observed_token=2, highest_seen_token=2)

    def test_future_token_is_not_fenced_out(self) -> None:
        """A receiver that has not yet caught up does not refuse valid work."""
        assert not is_fenced_out(observed_token=3, highest_seen_token=2)

    def test_takeover_after_expiry_fences_the_previous_holder(self) -> None:
        """End to end: expire, re-grant, and the old token is now stale.

        The sequence a recovery actually follows — the original holder died, its
        lease lapsed, a second monitor took the scope. Anything still in flight
        from the first is refused.
        """
        first = grant(
            LeaseRequest(
                scope="reconcile:ws-w1", holder="monitor-1", duration=lease_duration(60)
            ),
            now=OBSERVED_AT,
        )
        later = OBSERVED_AT + lease_duration(61)
        assert first.is_expired(later)

        second = grant(
            LeaseRequest(
                scope="reconcile:ws-w1", holder="monitor-2", duration=lease_duration(60)
            ),
            now=later,
            previous_token=first.fence_token,
        )
        assert second.holder == "monitor-2"
        assert is_fenced_out(first.fence_token, second.fence_token)
        assert not is_fenced_out(second.fence_token, second.fence_token)


class TestNoTableGrantIsImplied:
    """The contract expresses locking without any storage surface.

    Asserted as an absence because the absence is the deliverable: U15 withdraws
    the monitor's `reconcile_locks` write grant, and a lease shape that carried a
    table name, a connection or a SQL fragment would have quietly kept the
    dependency the withdrawal removes.
    """

    def test_lease_shape_names_no_storage(self) -> None:
        lease = grant(
            LeaseRequest(scope="reconcile:ws-w1", holder="monitor-1"), now=OBSERVED_AT
        )
        fields = set(vars(lease))
        assert fields == {"scope", "holder", "expires_at", "fence_token"}
        forbidden = {"table", "connection", "dsn", "sql", "row_id"}
        assert forbidden.isdisjoint(fields)

    def test_scope_is_an_opaque_string_not_a_table_reference(self) -> None:
        """Scope is a name the two ends agree on, carrying no schema meaning."""
        assert (
            LeaseRequest(scope="anything:at:all", holder="m").scope == "anything:at:all"
        )
