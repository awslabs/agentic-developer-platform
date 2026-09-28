"""Lock acquire/release expressed as a contract operation, not a table grant.

Issue #5043 (U8), EPIC #4910.

## Why leases appear in an observation contract at all

Because the monitor currently coordinates through a table. It takes a lock by
writing to `reconcile_locks` directly, which means the monitor needs write access
to a domain table — and U15's job is to withdraw exactly that grant. Withdrawing
it without giving the monitor another way to serialize its work would not make the
monitor safer; it would make it concurrent with itself.

So the contract has to express "I hold the right to reconcile this scope, for a
while" without the holder touching a table. That is a lease.

## The two properties that make a lease safe, and the one that is usually missed

* **Expiry.** A lease is time-bounded, so a monitor that dies mid-reconcile does
  not hold the scope forever. A lock without expiry needs an operator to clear it.
* **Ownership on release.** Release requires the holder's identity, so a lease
  cannot be released by whoever asks — otherwise any submitter could free another's
  lease and induce the concurrency the lease existed to prevent.
* **Fencing.** This is the one usually missed. Expiry alone creates a window where
  the *previous* holder still believes it holds the lease (its clock, its network,
  its long GC pause) while a new holder legitimately has it. Both then act. So each
  grant carries a monotonically increasing `fence_token`, and a receiver must
  refuse work stamped with a token lower than the highest it has seen for that
  scope. Expiry bounds the damage; the fence token is what actually prevents the
  overlap.

`is_fenced_out` is the check a receiver applies, and it lives here rather than
upstream because it is the *rule*, not the storage.

## What this module is not

No storage, no clock authority, no server. Times are passed in rather than read
from `datetime.now()` so a receiver's clock is the one that decides expiry — a
lease that expired according to the *holder's* clock is not a fact the holder gets
to assert. That also makes every branch here testable without patching time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .health import ContractViolation

# Default lease duration, and the ceiling a request may ask for. A ceiling exists
# because an unbounded requested duration turns the expiry property off: a
# submitter asking for a 30-day lease has functionally taken the table lock the
# withdrawal was meant to remove.
DEFAULT_LEASE_DURATION = timedelta(seconds=60)
MAX_LEASE_DURATION = timedelta(minutes=15)


@dataclass(frozen=True)
class LeaseRequest:
    """A request to hold a named scope for a bounded period."""

    scope: str
    holder: str
    duration: timedelta = DEFAULT_LEASE_DURATION

    def __post_init__(self) -> None:
        if not self.scope or not self.scope.strip():
            raise ContractViolation("lease scope must be a non-empty string")
        if not self.holder or not self.holder.strip():
            raise ContractViolation("lease holder must be a non-empty string")
        if self.duration <= timedelta(0):
            raise ContractViolation("lease duration must be positive")
        if self.duration > MAX_LEASE_DURATION:
            raise ContractViolation(
                f"lease duration exceeds the maximum of {MAX_LEASE_DURATION}"
            )


@dataclass(frozen=True)
class Lease:
    """A granted lease: who holds what, until when, at which fence token."""

    scope: str
    holder: str
    expires_at: datetime
    fence_token: int

    def __post_init__(self) -> None:
        if self.expires_at.tzinfo is None:
            # A naive expiry cannot be compared against a receiver's aware clock
            # without guessing a zone, and guessing produces a lease that expires
            # hours early or late.
            raise ContractViolation("lease expires_at must be timezone-aware")
        if self.fence_token < 1:
            raise ContractViolation("fence_token must be a positive integer")

    def is_expired(self, now: datetime) -> bool:
        """True when this lease has expired as of the receiver's `now`."""
        if now.tzinfo is None:
            raise ContractViolation("now must be timezone-aware")
        return now >= self.expires_at


@dataclass(frozen=True)
class LeaseDecision:
    """Outcome of an acquire or release. `lease` is set only when granted."""

    granted: bool
    lease: Lease | None = None
    reason: str = ""


def grant(request: LeaseRequest, now: datetime, previous_token: int = 0) -> Lease:
    """Grant a lease, advancing the fence token past `previous_token`.

    The receiver supplies `previous_token` (the highest it has issued for this
    scope) and `now`. The token advances here rather than being chosen by the
    requester, because a requester-chosen token is not a fence — it is a field the
    requester can set to whatever gets its work accepted.
    """
    if now.tzinfo is None:
        raise ContractViolation("now must be timezone-aware")
    return Lease(
        scope=request.scope,
        holder=request.holder,
        expires_at=now + request.duration,
        fence_token=previous_token + 1,
    )


def authorize_release(lease: Lease, holder: str) -> LeaseDecision:
    """Authorize a release request against the lease's recorded holder.

    Refuses a release by anyone other than the holder. Without this, freeing
    another monitor's lease is an unauthenticated way to create the concurrent
    reconcile the lease prevents — a denial-of-correctness rather than a
    denial-of-service, and harder to notice.
    """
    if holder != lease.holder:
        return LeaseDecision(granted=False, reason="lease not held by this caller")
    return LeaseDecision(granted=True, lease=lease)


def is_fenced_out(observed_token: int, highest_seen_token: int) -> bool:
    """True when work stamped `observed_token` must be refused as stale.

    Strictly less than, not less-or-equal: the current holder's own token equals
    the highest seen and must keep working. This is the check that closes the
    expiry window described in the module docstring, and it is the reason a
    receiver can trust a lease without trusting the holder's clock.
    """
    return observed_token < highest_seen_token
