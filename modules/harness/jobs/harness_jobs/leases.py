"""Execution leases: who may act on an operation right now, and who is fenced out.

Issue #5527 (w6-04), EPIC #4910, Wave 6.

## The property this module exists for

**At most one executor is entitled to act on an operation at a time, that entitlement
expires on its own, and an executor whose entitlement has lapsed cannot write
anything.**

The first two halves are ordinary. The third is the one that is usually missed, and it
is the whole reason this module is not a lock table.

Expiry alone creates a window. A worker holding a 60-second lease stalls -- a long GC
pause, a lost network, a frozen hypervisor -- and its lease lapses. A second worker
legitimately takes over, which is required for liveness: the alternative is an operation
wedged until a human clears it. Now the first worker wakes up. From inside, nothing
happened; it still believes it holds the lease, and it is about to report that the
operation succeeded. `store.transition` would accept that report, because the stale
worker's `version` may well still be current.

So each grant carries a **fence token** that only ever increases, and every write a
worker makes must present the token it was granted. A worker presenting a token below
the one the row now records is stale *by definition* -- not by inference from a clock,
which is the distinction that matters, because two machines' clocks disagree and a row's
own counter does not. Its write matches zero rows and changes nothing.

This is the same discipline as `harness_operations.version` and
`harness_dispatch_outbox.claim_generation`: show the state you read, or your write is
stale. It is a *third* counter rather than a reuse of either, because the three fence
different things over different lifetimes, and the outbox's in particular is released
the moment an envelope is handed over -- before the provider call that this one has to
cover.

## Where the rule comes from, and what is ours

The fencing *rule*, the lease ceiling and ownership-on-release are published shapes:
`superplane_contracts.leases` (#5043, U8) owns `is_fenced_out`,
`DEFAULT_LEASE_DURATION`,
`MAX_LEASE_DURATION` and `authorize_release`. They are duplicated here rather than
imported, for the reason `identity.py` duplicates `REQUIRED_PERMISSION`: this package is
installed independently and must not require the contracts package on `sys.path`.
`tests/test_lease_contract_agreement.py` is what stops the two spellings drifting.

Storage, the clock and the driver are ours -- the contract says so explicitly
(`reconciliation.py`: "the driver that grants one is B's and is not built here").

## Why the database's clock, and only the database's

Every expiry comparison uses `clock_timestamp()` evaluated by PostgreSQL inside the
statement that depends on it. No Python process reads its own clock to decide whether
a lease is live.

`clock_timestamp()` rather than `now()`: `now()` returns the transaction start time in
PostgreSQL, so a check inside a long transaction sees a frozen snapshot of "now" from
when BEGIN was issued. A lease that expired one second after the transaction started
would still pass `expires_at > now()` ten seconds later inside the same transaction.
`clock_timestamp()` is the actual wall-clock time at the moment the statement runs.

The contract's `grant()` takes `now` as a parameter so that "a lease that expired
according to the *holder's* clock is not a fact the holder gets to assert". Here the
database is that receiver, and it is the only one: if a worker computed its own expiry,
two workers with skewed clocks could each conclude they held the lease, which is the
overlap the whole module exists to prevent. Passing `now` in from Python would
reintroduce it through the seam that was built to close it.

## What a lease is not

It is not permission to spend, and it is not an approval. `acquire` refuses an operation
that did not pass the admission gate (#5526) -- the same `EXISTS` on a spendable
consumption row that `outbox.claim` applies, and for the same reason: a control the
maintained entry point can route around is not a control. It is also not report
*authority* -- whether a principal may speak for an operation is #5529's (w6-06)
question. A lease answers only "is this worker the current executor".
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from uuid import uuid4

from .admission import DELIVERABLE_RESERVATION_STATES
from .execution_plan import PlanProgress, confirmed_plan_progress
from .identity import TERMINAL_STATES, ContractViolation, OperationState
from .store import Connection

__all__ = [
    "DEFAULT_LEASE_DURATION",
    "DEFAULT_MAX_CONCURRENT_OPERATIONS",
    "DEFAULT_MAX_EXECUTION_ATTEMPTS",
    "MAX_LEASE_DURATION",
    "ExecutionLease",
    "ExpiredLeaseTakeover",
    "LeaseRefused",
    "LeaseRefusal",
    "acquire",
    "close",
    "fence_expired_lease",
    "fenced_update",
    "is_fenced_out",
    "read_lease",
    "release",
    "renew",
]

# Duplicated from `superplane_contracts.leases`, with a drift test. See the module
# docstring for why duplicated rather than imported.
#
# The ceiling exists because an unbounded requested duration turns the expiry property
# off: a worker asking for a 30-day lease has taken the operation hostage, which is the
# state a lease is supposed to make impossible.
DEFAULT_LEASE_DURATION = timedelta(seconds=60)
MAX_LEASE_DURATION = timedelta(minutes=15)

# How many operations one tenant may have under execution at once.
#
# A cap rather than none, because the executor pool is shared: a tenant that opened a
# thousand operations would occupy every worker and starve every other tenant. That is a
# tenant-isolation failure reachable without any authorization bug, purely by using the
# platform enthusiastically.
#
# Applied per (org, workspace) rather than per org, matching the granularity every other
# table here is scoped at.
DEFAULT_MAX_CONCURRENT_OPERATIONS = 10

# How many times one operation may be leased for execution before it is refused.
#
# Bounded because the recovery sweep re-offers an operation whose lease lapsed, and an
# operation that crashes its worker every time would otherwise be re-executed forever --
# each attempt potentially making a provider call, so an unbounded retry is unbounded
# spend. Counted at grant time (like `harness_dispatch_outbox.attempts`) so a worker
# that dies before recording anything still consumed an attempt; counting completed
# attempts instead would make a reliably-crashing operation immortal.
DEFAULT_MAX_EXECUTION_ATTEMPTS = 5


class LeaseRefusal(str, Enum):
    """Why an acquisition was refused. Distinguished because the answers differ.

    A caller that saw only "refused" would have to retry all of them, and two of these
    must never be retried: a terminal operation is finished, and an exhausted one has
    already had every attempt it is allowed. Retrying those is how a settled operation
    gets re-executed.
    """

    NO_SUCH_OPERATION = "no_such_operation"
    """No operation with that id, or not visible to the acting tenant."""

    NOT_ADMITTED = "not_admitted"
    """No spendable approval consumption. The operation was never paid for."""

    TERMINAL = "terminal"
    """The operation already has an outcome. Do not retry."""

    CANCEL_REQUESTED = "cancel_requested"
    """A cancellation is pending. Starting work now would be starting work to stop
    it."""

    RECOVERY_REQUIRED = "recovery_required"
    """Prior execution evidence requires recovery before another grant."""

    HELD = "held"
    """Another worker holds the lease. Expiry requires recovery before retry."""

    CLOSED = "closed"
    """The lease is closed: the operation is settled or attempts are exhausted."""

    ATTEMPTS_EXHAUSTED = "attempts_exhausted"
    """Every permitted execution attempt has been used. Do not retry."""

    TENANT_AT_CAPACITY = "tenant_at_capacity"
    """This tenant already holds its maximum concurrent executions. Retry later."""


class LeaseRefused(PermissionError):
    """An acquisition, renewal or release was refused.

    `PermissionError` rather than a bare `RuntimeError` to match `OperationRefused`'s
    choice in `identity.py`: the caller is being told it may not do this, not that
    something broke.
    """

    def __init__(self, reason: LeaseRefusal, message: str) -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class ExecutionLease:
    """A granted lease. The token is what a holder must present on every write.

    Frozen: a holder that could mutate its own `fence_token` or `expires_at` could
    manufacture entitlement it was not granted, and the value's whole purpose is to be
    checked against the row rather than trusted.

    Carries the tenant because the executor needs it to scope its own reads, and the
    executor must not re-derive it from anything the caller supplied -- these values
    came off the operation row, server-resolved, which is what `identity.py` guarantees
    about them at admission.
    """

    operation_id: str
    org_id: str
    workspace_id: str
    holder: str
    fence_token: int
    attempt_id: str
    expires_at: datetime
    acquired_at: datetime
    runtime_deadline: datetime
    attempts: int
    max_attempts: int = DEFAULT_MAX_EXECUTION_ATTEMPTS

    def __post_init__(self) -> None:
        # Mirrors the contract's `Lease.__post_init__`. A naive datetime cannot be
        # compared against an aware one without guessing a zone, and a guess produces a
        # lease that expires hours early or late.
        for field in ("expires_at", "acquired_at", "runtime_deadline"):
            value = getattr(self, field)
            if value.tzinfo is None:
                raise ContractViolation(f"lease {field} must be timezone-aware")
        if self.fence_token < 1:
            raise ContractViolation("fence_token must be a positive integer")

    def is_expired(self, now: datetime) -> bool:
        """True when this lease has lapsed as of `now`.

        Present to match the contract's shape, and deliberately **not** used by this
        module to make any decision: every enforcement comparison happens in SQL against
        the database's `clock_timestamp()`. A holder asking whether its own lease has
        expired is
        asking for a hint about when to renew, which is a different act from the
        database deciding whether to accept a write.
        """
        if now.tzinfo is None:
            raise ContractViolation("now must be timezone-aware")
        return now >= self.expires_at

    def runtime_exceeded(self, now: datetime) -> bool:
        """True when this attempt has outlived its approved runtime ceiling."""
        if now.tzinfo is None:
            raise ContractViolation("now must be timezone-aware")
        return now >= self.runtime_deadline


def is_fenced_out(observed_token: int, highest_seen_token: int) -> bool:
    """True when work stamped `observed_token` must be refused as stale.

    Strictly less than, not less-or-equal: the current holder's own token equals the
    highest seen and must keep working. Duplicated from
    `superplane_contracts.leases.is_fenced_out` with a drift test; see the module
    docstring.

    Provided for a receiver that has already read both values -- for example an executor
    validating a report it was handed. The enforcement inside this package does not call
    it, because expressing the same comparison as a SQL predicate is what makes it
    atomic
    with the write it guards: a Python-side check has a window between the check and the
    UPDATE, and a `WHERE fence_token = $1` does not.
    """
    return observed_token < highest_seen_token


_LEASE_COLUMNS = """
    operation_id, org_id, workspace_id, holder, fence_token, attempt_id,
    expires_at, acquired_at, runtime_deadline, attempts, max_attempts
"""


def _lease(row: object) -> ExecutionLease:
    data = dict(row)  # type: ignore[call-overload]
    return ExecutionLease(
        operation_id=data["operation_id"],
        org_id=data["org_id"],
        workspace_id=data["workspace_id"],
        holder=data["holder"],
        fence_token=data["fence_token"],
        attempt_id=data["attempt_id"],
        expires_at=data["expires_at"],
        acquired_at=data["acquired_at"],
        runtime_deadline=data["runtime_deadline"],
        attempts=data["attempts"],
        max_attempts=data["max_attempts"],
    )


def _duration_seconds(duration: timedelta, *, what: str) -> int:
    if duration <= timedelta(0):
        raise ContractViolation(f"{what} must be positive")
    if duration > MAX_LEASE_DURATION:
        raise ContractViolation(
            f"{what} exceeds the maximum of {MAX_LEASE_DURATION}; an unbounded lease "
            "turns off the expiry property that makes takeover possible"
        )
    return int(duration.total_seconds())


async def acquire(
    connection: Connection,
    *,
    operation_id: str,
    holder: str,
    attempt_id: str,
    duration: timedelta = DEFAULT_LEASE_DURATION,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_OPERATIONS,
    max_attempts: int = DEFAULT_MAX_EXECUTION_ATTEMPTS,
) -> ExecutionLease:
    """Atomically grant and audit authority through the trusted service API.

    Refusals for existing operations are attributed to their stored tenant. Unknown
    IDs produce no operation audit. No other tenant's holder or token is copied into
    refusal evidence. An enclosing service transaction may roll back both grant and
    audit; without one this function commits before returning or raising.
    """
    from .execution import audit

    refusal = None
    async with connection.transaction():
        try:
            lease = await _acquire(
                connection,
                operation_id=operation_id,
                holder=holder,
                attempt_id=attempt_id,
                duration=duration,
                max_concurrent=max_concurrent,
                max_attempts=max_attempts,
            )
        except LeaseRefused as exc:
            refusal = exc
            scope = await connection.fetchrow(
                "SELECT org_id, workspace_id FROM harness_operations "
                "WHERE operation_id=$1",
                operation_id,
            )
            if scope is not None:
                await audit(
                    connection,
                    operation_id=operation_id,
                    org_id=scope["org_id"],
                    workspace_id=scope["workspace_id"],
                    event="lease.acquire",
                    actor=holder,
                    allowed=False,
                    attempt_id=attempt_id,
                    detail=exc.reason.value,
                )
        else:
            await audit(
                connection,
                operation_id=operation_id,
                org_id=lease.org_id,
                workspace_id=lease.workspace_id,
                event="lease.acquire",
                actor=lease.holder,
                allowed=True,
                attempt_id=lease.attempt_id,
                fence_token=lease.fence_token,
            )
    if refusal is not None:
        raise refusal
    return lease


async def _acquire(
    connection: Connection,
    *,
    operation_id: str,
    holder: str,
    attempt_id: str,
    duration: timedelta = DEFAULT_LEASE_DURATION,
    max_concurrent: int = DEFAULT_MAX_CONCURRENT_OPERATIONS,
    max_attempts: int = DEFAULT_MAX_EXECUTION_ATTEMPTS,
) -> ExecutionLease:
    """Take the execution lease for `operation_id`, or raise `LeaseRefused`.

    Must be called inside a transaction: the capacity check and the grant have to be one
    atomic act, and the advisory lock below is transaction-scoped.

    ## The sequence, and why it is in this order

    1. **Read the operation, locking its row.** The tenant comes off this row rather
    than
       from an argument -- an `org_id` a caller supplied is a value a caller chose, and
       the lease's tenant is what the per-tenant cap is enforced on. `identity.py` makes
       these columns unconstructable from caller input at admission; reading them back
       here is what keeps that true at execution.
    2. **Refuse a terminal or cancel-requested operation.** Before anything is counted
    or
       granted, because both mean no work should start: one is finished, and the other
       is about to be stopped. Starting an attempt on a cancel-requested operation is
       spending money to immediately undo it.
    3. **Refuse an operation that did not pass the admission gate.** The same `EXISTS`
    on
       a spendable `harness_approval_consumption` row that `outbox.claim` applies.
       Stated as a predicate here too rather than trusted from delivery, because this
       function is reachable by the recovery sweep and by a worker that was handed an
       envelope earlier, neither of which re-runs the claim -- and a reservation that
       has since been released or retained must not be executed against.
    4. **Take a per-tenant advisory lock, then count.** See below.
    5. **Grant**, incrementing the fence token.

    ## Why the advisory lock

    The cap is a `COUNT` followed by an `INSERT`, and that pair has the classic race:
    two workers acquiring for *different operations* of the same tenant both count `N`
    and both insert, giving `N + 2` against a cap of `N + 1`. Row locks cannot help,
    because the rows being counted are not the row being written -- there is no single
    row to lock.

    `pg_advisory_xact_lock` keyed on the tenant serializes the pair per tenant, which is
    the narrowest scope that closes the race: two tenants never contend, and one
    tenant's concurrent acquisitions queue briefly. Transaction-scoped, so it is
    released by commit or rollback and cannot be leaked by a process that dies holding
    it -- which a session-scoped lock can.

    `hashtextextended` over the tenant pair with a fixed seed gives the `bigint` the
    lock API takes. A hash collision between two tenants costs them serialization
    against each other, not correctness: the cap is still counted per tenant by the
    `WHERE` clause.

    ## Why the fence token is incremented by the database

    `fence_token = fence_token + 1` inside the granting statement. Not read-then-write:
    that has the same window, and a duplicated fence token is a stale worker accepted as
    current. The `ON CONFLICT DO UPDATE` form means the first acquisition creates the
    row at token 1 and every later one advances it, so the counter is per-operation
    monotonic without a sequence that could be shared or reset.
    """
    if not operation_id or not operation_id.strip():
        raise ContractViolation("operation_id must be a non-empty string")
    if not holder or not holder.strip():
        raise ContractViolation("lease holder must be a non-empty string")
    if not attempt_id or not attempt_id.strip():
        raise ContractViolation("attempt_id must be a non-empty string")
    if int(max_concurrent) < 1:
        raise ContractViolation("max_concurrent must be at least 1")
    if int(max_attempts) < 1:
        raise ContractViolation("max_attempts must be at least 1")
    seconds = _duration_seconds(duration, what="lease duration")

    # (1) The operation row, locked. `FOR UPDATE` so a concurrent acquisition for the
    # SAME operation queues here rather than racing the upsert below -- the upsert's own
    # conflict handling would serialize them, but the capacity count between the two
    # would not, and a caller refused for capacity by its own sibling is a confusing
    # failure to debug.
    operation = await connection.fetchrow(
        """
        SELECT org_id, workspace_id, state, cancel_requested_at, cleanup_required
          FROM harness_operations
         WHERE operation_id = $1
           FOR UPDATE
        """,
        operation_id,
    )
    if operation is None:
        raise LeaseRefused(
            LeaseRefusal.NO_SUCH_OPERATION,
            f"no operation {operation_id!r}; nothing to lease",
        )
    row = dict(operation)  # type: ignore[call-overload]
    org_id = row["org_id"]
    workspace_id = row["workspace_id"]

    # (2) Terminal and cancel-requested, before anything is granted.
    if OperationState(row["state"]) in TERMINAL_STATES:
        raise LeaseRefused(
            LeaseRefusal.TERMINAL,
            f"operation {operation_id} is {row['state']}; a settled operation must not "
            "be executed again",
        )
    if row["cancel_requested_at"] is not None or row["cleanup_required"]:
        raise LeaseRefused(
            LeaseRefusal.CANCEL_REQUESTED,
            f"operation {operation_id} has a pending cancellation; starting an attempt "
            "now would be spending to immediately undo it",
        )

    # Only recovery may relinquish an expired holder. A verified successful prefix
    # can continue under a new bounded attempt; uncertain or complete work cannot.
    if (
        await connection.fetchval(
            "SELECT EXISTS(SELECT 1 FROM harness_provider_call_intent WHERE "
            "operation_id=$1)",
            operation_id,
        )
        and await confirmed_plan_progress(connection, operation_id)
        is not PlanProgress.PREFIX
    ):
        raise LeaseRefused(
            LeaseRefusal.RECOVERY_REQUIRED, "Provider history requires recovery"
        )

    # (3) The admission control, restated as a predicate here.
    payable = await connection.fetchval(
        """
        SELECT EXISTS (
            SELECT 1 FROM harness_approval_consumption
             WHERE operation_id = $1 AND reservation_state = ANY($2::text[])
        )
        """,
        operation_id,
        list(DELIVERABLE_RESERVATION_STATES),
    )
    if not payable:
        raise LeaseRefused(
            LeaseRefusal.NOT_ADMITTED,
            f"operation {operation_id} has no spendable approval consumption; it was "
            "never admitted through the approval gate, or its reservation has been "
            "released or retained",
        )

    # The approved runtime ceiling for this attempt. Read from the consumption row
    # rather than taken as a parameter: the limit that matters is the one a human
    # approved, and a caller-supplied ceiling would let the executor grant itself more
    # runtime than the envelope allows.
    max_runtime = await connection.fetchval(
        """
        SELECT max_runtime_seconds FROM harness_approval_consumption
         WHERE operation_id = $1 AND reservation_state = ANY($2::text[])
        """,
        operation_id,
        list(DELIVERABLE_RESERVATION_STATES),
    )

    # (4) Serialize the capacity check per tenant, then count. Excludes this operation,
    # so re-acquiring a lapsed lease for work already in flight is never refused for
    # capacity -- it occupies a slot it already held.
    await connection.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended($1, 0))",
        f"harness-jobs-lease:{org_id}/{workspace_id}",
    )
    live = await connection.fetchval(
        """
        SELECT count(*) FROM harness_operation_leases
         WHERE org_id = $1 AND workspace_id = $2
           AND closed_at IS NULL AND holder IS NOT NULL
           AND expires_at > clock_timestamp()
           AND runtime_deadline > clock_timestamp()
           AND operation_id <> $3
        """,
        org_id,
        workspace_id,
        operation_id,
    )
    if int(live) >= int(max_concurrent):
        raise LeaseRefused(
            LeaseRefusal.TENANT_AT_CAPACITY,
            f"tenant {org_id}/{workspace_id} already holds {live} concurrent "
            f"executions, at the limit of {max_concurrent}",
        )

    # The first grant establishes the durable ceiling. A restarted scheduler's
    # defaults cannot silently raise or lower an operation's established policy.
    persisted_maximum = await connection.fetchval(
        "SELECT max_attempts FROM harness_operation_leases WHERE operation_id=$1",
        operation_id,
    )
    if persisted_maximum is not None:
        max_attempts = persisted_maximum

    # (5) Grant. One statement, so the token advance, the attempt increment and the
    # holder stamp cannot come apart.
    granted = await connection.fetchrow(
        f"""
        INSERT INTO harness_operation_leases (
            operation_id, org_id, workspace_id, fence_token, holder, attempt_id,
            expires_at, acquired_at, runtime_deadline, attempts, max_attempts
        )
        VALUES (
            $1, $2, $3, 1, $4, $5,
            clock_timestamp() + ($6 || ' seconds')::interval, clock_timestamp(),
            clock_timestamp() + ($7 || ' seconds')::interval, 1, $8
        )
        ON CONFLICT (operation_id) DO UPDATE
           SET fence_token = harness_operation_leases.fence_token + 1,
               holder = EXCLUDED.holder,
               attempt_id = EXCLUDED.attempt_id,
               expires_at = EXCLUDED.expires_at,
               acquired_at = EXCLUDED.acquired_at,
               runtime_deadline = EXCLUDED.runtime_deadline,
               attempts = harness_operation_leases.attempts + 1,
               updated_at = now()
         WHERE harness_operation_leases.closed_at IS NULL
           AND harness_operation_leases.attempts < $8
           AND harness_operation_leases.holder IS NULL
        RETURNING {_LEASE_COLUMNS}
        """,
        operation_id,
        org_id,
        workspace_id,
        holder,
        attempt_id,
        str(seconds),
        str(int(max_runtime)),
        int(max_attempts),
    )
    if granted is None:
        # Zero rows means the ON CONFLICT predicate failed. Re-read to say WHICH of the
        # three reasons it was, because the caller's correct response differs: `HELD` is
        # retryable after the lease lapses, and the other two never are.
        raise await _diagnose_refusal(
            connection, operation_id=operation_id, max_attempts=int(max_attempts)
        )
    return _lease(granted)


async def _diagnose_refusal(
    connection: Connection, *, operation_id: str, max_attempts: int
) -> LeaseRefused:
    """Turn a zero-row grant into the specific reason it was refused.

    A second read rather than a more elaborate `RETURNING`: the grant statement's job is
    to be atomic, and making it also report why it declined would mean an `ON CONFLICT`
    that updates unconditionally and then a CHECK on what it produced -- which is a
    write on a row this call was refused permission to touch.

    Its answer could in principle be stale by the time it returns, which is why it only
    ever produces a *diagnosis*: the refusal itself already happened, atomically, above.
    """
    current = await connection.fetchrow(
        """
        SELECT holder, expires_at, attempts, closed_at, closed_reason
          FROM harness_operation_leases
         WHERE operation_id = $1
        """,
        operation_id,
    )
    if current is None:
        # The INSERT did not fire and there is no row: only reachable if the row was
        # deleted between the upsert and this read (an operation cascade-deleted
        # underneath us).
        return LeaseRefused(
            LeaseRefusal.NO_SUCH_OPERATION,
            f"lease row for {operation_id} disappeared during acquisition",
        )
    data = dict(current)  # type: ignore[call-overload]
    if data["closed_at"] is not None:
        recorded = data["closed_reason"] or "no reason recorded"
        return LeaseRefused(
            LeaseRefusal.CLOSED,
            f"lease for {operation_id} is closed: {recorded}",
        )
    if int(data["attempts"]) >= max_attempts:
        return LeaseRefused(
            LeaseRefusal.ATTEMPTS_EXHAUSTED,
            f"operation {operation_id} has used all {max_attempts} permitted execution "
            "attempts; further attempts would be unbounded provider spend",
        )
    return LeaseRefused(
        LeaseRefusal.HELD,
        f"operation {operation_id} is leased by {data['holder']!r} until "
        f"{data['expires_at']}",
    )


async def renew(
    connection: Connection, lease: ExecutionLease, *, duration: timedelta | None = None
) -> ExecutionLease:
    """Extend a held lease's expiry, keeping the same fence token.

    The token does **not** advance on renewal, and that is the point of renewal existing
    at all: a long-running attempt needs to keep its entitlement alive without
    invalidating the writes it has already made under that token. A renewal that bumped
    the token would fence the holder out of its own in-flight work.

    `runtime_deadline` is deliberately **not** extended. A worker that could renew its
    way past the approved runtime ceiling would make the ceiling unenforceable, and the
    wedged-but-healthy worker -- renewing happily, never finishing -- is exactly the
    case the ceiling exists for. Once the deadline passes, renewal is refused and the
    recovery sweep may take the operation over.

    Refuses rather than returning a flag: a caller that ignored a failed renewal would
    carry on believing it holds a lease it does not, which is the stale-holder state
    this module exists to make impossible.
    """
    seconds = _duration_seconds(
        duration if duration is not None else DEFAULT_LEASE_DURATION,
        what="lease duration",
    )
    renewed = await connection.fetchrow(
        f"""
        UPDATE harness_operation_leases
           SET expires_at = clock_timestamp() + ($4 || ' seconds')::interval,
               updated_at = now()
         WHERE operation_id = $1
           AND holder = $2 AND fence_token = $3 AND closed_at IS NULL
           AND expires_at > clock_timestamp()
           AND runtime_deadline > clock_timestamp()
        RETURNING {_LEASE_COLUMNS}
        """,
        lease.operation_id,
        lease.holder,
        lease.fence_token,
        str(seconds),
    )
    if renewed is None:
        raise LeaseRefused(
            LeaseRefusal.HELD,
            f"{lease.holder!r} could not renew operation {lease.operation_id} at fence "
            f"token {lease.fence_token}: the lease has lapsed, been granted to another "
            "worker, exceeded its approved runtime, or been closed. This worker is no "
            "longer the executor and must stop.",
        )
    return _lease(renewed)


async def release(connection: Connection, lease: ExecutionLease) -> bool:
    """Release only a live, uncancelled attempt with no provider-call history.

    Effect-bearing attempts must settle or reconcile; clearing their holder would
    permit a second mutation. Lock operation then lease so cancellation and intent
    writes cannot race the absence check.
    """
    async with connection.transaction():
        if not await lock_lease(connection, lease):
            return False
        safe = await connection.fetchval(
            "SELECT cancel_requested_at IS NULL AND NOT cleanup_required AND "
            "NOT EXISTS(SELECT 1 FROM harness_provider_call_intent WHERE "
            "operation_id=$1) "
            "FROM harness_operations WHERE operation_id=$1",
            lease.operation_id,
        )
        if not safe:
            return False
        tag = await connection.execute(
            "UPDATE harness_operation_leases SET holder=NULL, expires_at=NULL, "
            "acquired_at=NULL, runtime_deadline=NULL, attempt_id=NULL, "
            "updated_at=now() "
            "WHERE operation_id=$1 AND holder=$2 AND fence_token=$3",
            lease.operation_id,
            lease.holder,
            lease.fence_token,
        )
        return _rows_affected(tag) > 0


async def close(
    connection: Connection,
    *,
    operation_id: str,
    reason: str,
    fence_token: int | None,
    holder: str | None = None,
) -> bool:
    """Close a held lease only with both holder and token, or an unheld lease only.

    Administrative closure cannot retire an active successor. Recovery uses its own
    finite claim and presents the same holder/token proof as any executor.
    """
    if fence_token is not None and (not isinstance(holder, str) or not holder.strip()):
        raise ContractViolation("A token-based close requires the lease holder")
    if fence_token is None:
        tag = await connection.execute(
            """
            INSERT INTO harness_operation_leases (
                operation_id, org_id, workspace_id, fence_token,
                closed_at, closed_reason
            )
            SELECT operation_id, org_id, workspace_id, 0, clock_timestamp(), $2
              FROM harness_operations WHERE operation_id = $1
            ON CONFLICT (operation_id) DO UPDATE
               SET closed_at = clock_timestamp(), closed_reason = $2, updated_at = now()
             WHERE harness_operation_leases.closed_at IS NULL
               AND harness_operation_leases.holder IS NULL
            """,
            operation_id,
            reason,
        )
    else:
        tag = await connection.execute(
            """
            UPDATE harness_operation_leases
               SET closed_at = clock_timestamp(), closed_reason = $3,
                   closed_holder=holder, closed_attempt_id=attempt_id,
                   holder = NULL, expires_at = NULL, acquired_at = NULL,
                   runtime_deadline = NULL, attempt_id = NULL, updated_at = now()
             WHERE operation_id = $1 AND closed_at IS NULL
               AND holder = $4 AND fence_token = $2
               AND expires_at > clock_timestamp()
               AND runtime_deadline > clock_timestamp()
            """,
            operation_id,
            int(fence_token),
            reason,
            holder,
        )
    return _rows_affected(tag) > 0


@dataclass(frozen=True)
class ExpiredLeaseTakeover:
    operation_id: str
    fence_token: int
    attempts: int
    cancel_requested: bool
    lease: ExecutionLease


async def fence_expired_lease(
    connection: Connection,
    *,
    operation_id: str,
    recovery_principal=None,
) -> ExpiredLeaseTakeover | None:
    """Claim recovery for 60 seconds without consuming an execution attempt.

    The holder stays occupied while provider observations are in flight. If recovery
    crashes, its finite lease can itself be recovered. Every later write must still
    present this claim; a slow observer never grants authority over a successor.
    """
    from .identity import OperationRefused, ResolvedPrincipal

    if recovery_principal is not None and (
        not isinstance(recovery_principal, ResolvedPrincipal)
        or "workspace:recover" not in recovery_principal.permissions
    ):
        raise OperationRefused("authenticated workspace:recover principal required")
    holder = "recovery:" + str(uuid4())
    async with connection.transaction():
        # A provider hook holds this same operation-specific advisory lock across
        # I/O. Recovery cannot observe absence or authorize retry while it is in flight.
        if not await connection.fetchval(
            "SELECT pg_try_advisory_xact_lock(hashtextextended($1, 0))",
            f"harness-provider-dispatch:{operation_id}",
        ):
            return None
        # All paths that touch operation and lease lock in this order.
        operation = await connection.fetchrow(
            "SELECT cancel_requested_at,org_id,workspace_id FROM harness_operations "
            "WHERE operation_id=$1 FOR UPDATE",
            operation_id,
        )
        if operation is None:
            return None
        if recovery_principal is not None and (
            operation["org_id"],
            operation["workspace_id"],
        ) != (recovery_principal.org_id, recovery_principal.workspace_id):
            raise OperationRefused("recovery operation outside authenticated scope")
        row = await connection.fetchrow(
            f"""
            UPDATE harness_operation_leases
               SET fence_token = fence_token + 1, holder = $2, attempt_id = $2,
                   acquired_at = clock_timestamp(),
                   expires_at = clock_timestamp() + interval '60 seconds',
                   runtime_deadline = clock_timestamp() + interval '60 seconds',
                   updated_at = now()
             WHERE operation_id = $1 AND holder IS NOT NULL AND closed_at IS NULL
               AND (expires_at <= clock_timestamp()
                    OR runtime_deadline <= clock_timestamp())
            RETURNING {_LEASE_COLUMNS}
            """,
            operation_id,
            holder,
        )
        if row is None:
            return None
        lease = _lease(row)
        if recovery_principal is not None:
            await connection.execute(
                "INSERT INTO harness_recovery_claim_bindings "
                "(operation_id,fence_token,org_id,workspace_id,"
                "holder,attempt_id,subject) "
                "VALUES($1,$2,$3,$4,$5,$6,$7)",
                lease.operation_id,
                lease.fence_token,
                lease.org_id,
                lease.workspace_id,
                lease.holder,
                lease.attempt_id,
                recovery_principal.subject,
            )
        from .execution import audit

        await audit(
            connection,
            operation_id=lease.operation_id,
            org_id=lease.org_id,
            workspace_id=lease.workspace_id,
            event="recovery.claim",
            actor=lease.holder,
            allowed=True,
            attempt_id=lease.attempt_id,
            fence_token=lease.fence_token,
        )
        return ExpiredLeaseTakeover(
            operation_id,
            lease.fence_token,
            lease.attempts,
            operation["cancel_requested_at"] is not None,
            lease,
        )


async def lock_lease(connection: Connection, lease: ExecutionLease) -> bool:
    """Lock and verify a tenant-bound live claim inside the caller's transaction."""
    if not connection.is_in_transaction():
        raise ContractViolation("lock_lease requires a transaction")
    operation = await connection.fetchrow(
        "SELECT operation_id FROM harness_operations WHERE operation_id=$1 "
        "AND org_id=$2 AND workspace_id=$3 FOR UPDATE",
        lease.operation_id,
        lease.org_id,
        lease.workspace_id,
    )
    if operation is None:
        return False
    row = await connection.fetchrow(
        """
        SELECT 1 FROM harness_operation_leases
         WHERE operation_id=$1 AND org_id=$2 AND workspace_id=$3
           AND holder=$4 AND fence_token=$5 AND attempt_id=$6
           AND closed_at IS NULL AND expires_at > clock_timestamp()
           AND runtime_deadline > clock_timestamp()
        FOR UPDATE
        """,
        lease.operation_id,
        lease.org_id,
        lease.workspace_id,
        lease.holder,
        lease.fence_token,
        lease.attempt_id,
    )
    return row is not None


async def read_lease(
    connection: Connection, *, operation_id: str
) -> ExecutionLease | None:
    """The current lease, or `None` when it is free, closed or absent.

    Returns `None` for all three rather than distinguishing them, because a *reader*
    that wants to know whether it may act gets that answer from `acquire`, atomically.
    This is for status and audit surfaces, which display a holder rather than decide.
    """
    row = await connection.fetchrow(
        f"""
        SELECT {_LEASE_COLUMNS}
          FROM harness_operation_leases
         WHERE operation_id = $1 AND holder IS NOT NULL AND closed_at IS NULL
        """,
        operation_id,
    )
    return None if row is None else _lease(row)


async def fenced_update(
    connection: Connection,
    lease: ExecutionLease,
    statement: str,
    *extra: object,
) -> bool:
    """Run a statement requiring the lease this holder claims. Returns whether it hit.

    Every write an executor makes goes through here, so the ownership predicate is
    written once. `outbox._owned_update` exists for the same reason and the argument is
    the same one: a second spelling of the predicate is a second place to forget it, and
    forgetting it is the whole defect.

    The statement must take `$1` = operation_id and `$2` = fence_token and carry its own
    `WHERE ... fence_token = $2` -- the predicate cannot be added from out here, because
    where it belongs depends on the statement's shape.

    Before running it, this re-checks that the lease is still held by this holder at
    this token. The check is done by locking the lease row with `FOR UPDATE`, making
    the ownership verification and the subsequent write atomic: any concurrent release,
    takeover or expiry-clearing update will block until this transaction completes, so
    the state observed by the check is the state the write acts on.

    A plain `SELECT EXISTS` is not enough: another connection could release the lease
    (preserving the fence token) between the check and the execute, and the write would
    then proceed against an unheld row whose token still matches. `FOR UPDATE` closes
    that window.

    Also checks `runtime_deadline > clock_timestamp()`: an expired runtime whose lease
    is still live must not be able to write -- the deadline is what the recovery sweep
    uses to detect a healthy-but-wedged worker that has exceeded its approved runtime.
    """
    async with connection.transaction():
        if not await lock_lease(connection, lease):
            return False
        tag = await connection.execute(
            statement, lease.operation_id, lease.fence_token, *extra
        )
        return _rows_affected(tag) > 0


def _rows_affected(tag: object) -> int:
    """Rows touched, from the driver's command tag (`UPDATE <n>`).

    Duplicated from `outbox._rows_affected` rather than imported across modules,
    matching how this package already keeps its modules independent. An unparseable tag
    is treated as zero -- the conservative answer, since claiming to have written a row
    this call may not have touched is the error with consequences.
    """
    if not isinstance(tag, str):
        return 0
    parts = tag.strip().split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0
