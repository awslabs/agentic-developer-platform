"""Lease grant/release for the observation contract — issue #5056 (U15).

The receiving half of `superplane_contracts.leases`. That module holds the rules
(expiry, ownership-on-release, fencing); this one holds the storage and the clock,
which is the whole reason the rules could not live together with them.

Why this exists at all: the monitor currently serializes its work by writing to
`reconcile_locks`, and U15 withdraws that grant. Withdrawing it without giving the
monitor another way to serialize would not make the monitor safer — it would make
it concurrent with itself. So acquire/release becomes an authenticated API call,
and the receiver, not the caller, decides who holds what and until when.

The clock is the receiver's throughout. A lease that expired according to the
*holder's* clock is not a fact the holder gets to assert, and `now` is passed to
the contract's functions rather than read inside them so that stays true.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.observation import ObservationLease

from superplane_contracts import (
    ContractViolation,
    Lease,
    LeaseRequest,
    Submitter,
    authorize_release,
    grant,
)


class LeaseUnavailable(Exception):
    """A lease request the receiver will not satisfy (409 at the route)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


# Refusal reasons. "Held by another caller" is deliberately not distinguished from
# "you may not have this" — a caller learning *who* holds a scope learns about
# another tenant's monitoring topology.
_HELD = "lease scope is currently held"
_NOT_HELD = "lease not held by this caller"


@dataclass(frozen=True)
class LeaseGrant:
    """A granted lease plus the fence token the holder must present with work."""

    scope: str
    holder: str
    expires_at: datetime
    fence_token: int


def scope_for(resource_type: str, resource_id: str) -> str:
    """The lease scope key for a (resource_type, resource_id) pair.

    Keyed by the pair rather than by cluster because not every lease scope is a
    cluster: the budget monitor holds `("budget_monitor", "global")`, for which no
    cluster row exists. A cluster-keyed lease table could not represent it.
    """
    return f"{resource_type}/{resource_id}"


def holder_for(submitter: Submitter, instance_id: str) -> str:
    """The stored holder identity for a caller.

    Namespaced by the *authenticated* `submitter_id` rather than taken from the
    request, because `authorize_release` only checks that the presented holder
    matches the recorded one. If callers chose their own holder string outright,
    any authenticated submitter could release another's lease by naming it — and
    freeing a lease someone still holds recreates exactly the concurrent reconcile
    the lease prevents.

    `instance_id` distinguishes replicas of the same submitter (the monitor runs
    two), so one replica cannot release its sibling's lease either.
    """
    suffix = (instance_id or "").strip() or "default"
    return f"{submitter.submitter_id}:{suffix}"


async def acquire(
    db: AsyncSession,
    *,
    submitter: Submitter,
    scope: str,
    instance_id: str,
    duration: timedelta,
    now: datetime | None = None,
) -> LeaseGrant:
    """Acquire or renew a lease, returning its fence token.

    Raises `ContractViolation` for a request the contract rejects (blank scope or
    holder, non-positive duration, or a duration above the 15-minute ceiling) and
    `LeaseUnavailable` when the scope is validly held by someone else.

    Re-entrant for the current holder: an unexpired lease held by the same holder
    is renewed, which is what a monitor's periodic extension needs. Renewal still
    advances the fence token, so a token is never reused across grants.

    The holder identity is derived from the authenticated submitter (see
    `holder_for`) rather than accepted from the caller, so a caller cannot claim
    to be a different holder.
    """
    holder = holder_for(submitter, instance_id)
    clock = now if now is not None else datetime.now(UTC)
    if clock.tzinfo is None:
        raise ContractViolation("now must be timezone-aware")

    # Constructed before any storage access so the contract's own validation
    # (duration ceiling, blank fields) refuses a bad request before it can touch
    # a row.
    request = LeaseRequest(scope=scope, holder=holder, duration=duration)

    row = await db.get(ObservationLease, scope, with_for_update=True)
    if row is None:
        row = ObservationLease(scope=scope, fence_token=0, acquire_count=0)
        db.add(row)
    else:
        expires_at = _as_utc(row.expires_at)
        held_by_other = (
            row.holder is not None
            and row.holder != holder
            and expires_at is not None
            and clock < expires_at
        )
        if held_by_other:
            raise LeaseUnavailable(_HELD)

    lease = grant(request, clock, previous_token=row.fence_token or 0)
    row.holder = lease.holder
    row.last_holder = lease.holder
    row.expires_at = lease.expires_at
    row.fence_token = lease.fence_token
    row.acquire_count = (row.acquire_count or 0) + 1
    await db.commit()
    return LeaseGrant(
        scope=lease.scope,
        holder=lease.holder,
        expires_at=lease.expires_at,
        fence_token=lease.fence_token,
    )


async def release(
    db: AsyncSession,
    *,
    submitter: Submitter,
    scope: str,
    instance_id: str,
    fence_token: int,
    now: datetime | None = None,
) -> None:
    """Release a lease held by this authenticated caller.

    Refuses a release by anyone other than the recorded holder, and refuses a
    release stamped with a superseded fence token — a late release from a previous
    holder must not free the lease the current holder legitimately took.

    The row is retained with its fence token; only the holder is cleared. Deleting
    it (which is what the old `DELETE FROM reconcile_locks` did) would reset the
    token sequence and let a stalled previous holder's token compare equal to a
    newly granted one, which is the overlap the fence exists to prevent.
    """
    clock = now if now is not None else datetime.now(UTC)
    holder = holder_for(submitter, instance_id)
    row = await db.get(ObservationLease, scope, with_for_update=True)
    if row is None or row.holder is None:
        raise LeaseUnavailable(_NOT_HELD)

    recorded = Lease(
        scope=scope,
        holder=row.holder,
        expires_at=_as_utc(row.expires_at) or clock,
        fence_token=row.fence_token or 1,
    )
    decision = authorize_release(recorded, holder)
    if not decision.granted:
        raise LeaseUnavailable(decision.reason or _NOT_HELD)
    if fence_token != recorded.fence_token:
        # Same identity, superseded grant. Refused rather than honoured, because a
        # release from an old grant would free a lease its holder still holds.
        raise LeaseUnavailable(_NOT_HELD)

    row.holder = None
    row.expires_at = None
    await db.commit()


async def highest_fence_token(db: AsyncSession, scope: str) -> int:
    """The highest token ever issued for `scope`, or 0 if never granted.

    This is what a receiver compares incoming work against via the contract's
    `is_fenced_out`.
    """
    row = await db.get(ObservationLease, scope)
    return (row.fence_token or 0) if row is not None else 0


def _as_utc(value: datetime | None) -> datetime | None:
    """Attach UTC to a naive stored timestamp.

    SQLite (the test backend) returns naive datetimes even for timezone-aware
    columns. Stored instants are UTC by construction, so attaching UTC restores a
    correct comparison instead of assuming the process's local zone.
    """
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
