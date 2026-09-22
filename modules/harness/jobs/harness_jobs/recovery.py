"""Crash recovery and cancellation: settle abandoned work, honour cancellation requests.

Issue #5527 (w6-04), EPIC #4910, Wave 6.

## The property this module exists for

**An operation whose executor died does not stay stuck forever.**

When a worker process is killed mid-execution, the operation's lease eventually expires.
At that point a recovery sweep finds it, decides whether any provider call may have
happened, and settles the operation to the correct terminal state -- either retrying
from scratch (when the prior attempt provably changed nothing), or recording the outcome
as unknown and retaining the reserved budget (when we cannot establish that).

The sweep is idempotent: running it twice on the same set of expired leases settles
nothing extra. "Already settled" is always an acceptable outcome.

## Cancellation

A cancellation request is a side-channel on the operation row, not a state value:
an operation that is RUNNING and has a cancellation pending is *both*, and the executor
needs both facts at its next safe point.

`request_cancellation` writes the request. `check_cancel_requested` reads it, for an
executor to poll between provider calls. The executor's response to a true result:
stop work at the next safe point, close its lease, and retain the budget if a provider
call may have occurred.

## What this module does not do

It does not call providers. The sweep's `reconcile_call` argument is the caller's hook
to ask the provider, which is why this module holds no credential. Every result comes in
through a typed interface and is committed here.

It does not decide whether a principal may speak for an operation (that is #5529's),
and it does not release or retain budget directly (that is the caller's, via the
`BudgetDisposition` returned by `execution.reconcile`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from uuid import uuid4

from .execution import (
    BudgetDisposition,
    CallOutcome,
    CallStage,
    audit,
    disposition_for,
    read_call,
    reconcile,
)
from .execution_plan import PlanProgress, confirmed_plan_progress
from .identity import TERMINAL_STATES, ContractViolation, OperationState
from .leases import (
    DEFAULT_MAX_EXECUTION_ATTEMPTS,
    ExecutionLease,
    close,
    fence_expired_lease,
    lock_lease,
    release,
)
from .store import Connection

__all__ = [
    "CancellationRecord",
    "RecoveryReport",
    "SweepResult",
    "check_cancel_requested",
    "request_cancellation",
    "sweep_expired_leases",
    "sweep_unresolved_calls",
]


@dataclass(frozen=True)
class CancellationRecord:
    """A recorded cancellation request, for an executor reading its own safe-point."""

    operation_id: str
    requested_by: str
    reason: str | None


@dataclass(frozen=True)
class SweepResult:
    """What one operation's recovery settled."""

    operation_id: str
    action: str
    """Recovery outcome, including deferred reconciliation and lost-claim skips."""
    detail: str | None = None
    budget_disposition: str | None = None
    call_dispositions: tuple[tuple[str, BudgetDisposition], ...] = ()


@dataclass(frozen=True)
class RecoveryReport:
    """What one recovery sweep pass settled."""

    retried: int
    """Operations re-offered for execution (prior attempt provably changed nothing)."""
    retained: int
    """Operations with a terminal outcome written: unknown, succeeded, failed, or
    cancelled.  Budget disposition depends on the call outcomes; callers should check
    ``results`` for the per-operation detail."""
    skipped: int
    """Operations whose lease could not be taken over (active holder, already closed,
    or operation absent)."""
    results: tuple[SweepResult, ...]
    deferred: int = 0


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def request_cancellation(
    connection: Connection,
    *,
    operation_id: str,
    principal,
    reason: str | None = None,
) -> bool:
    """Atomically request cancellation for the authenticated principal's tenant.

    Only trusted service composition supplies ResolvedPrincipal, never request JSON.
    Missing and foreign operations both return False and create no audit in a foreign
    tenant. The first authenticated cancellation wins; the actor cannot be supplied
    independently of the verified principal.
    """
    from .identity import OperationRefused, ResolvedPrincipal

    if not isinstance(principal, ResolvedPrincipal) or not principal.may_provision:
        raise OperationRefused("workspace:provision is required")
    async with connection.transaction():
        row = await connection.fetchrow(
            "SELECT operation_id FROM harness_operations WHERE operation_id=$1 "
            "AND org_id=$2 AND workspace_id=$3 FOR UPDATE",
            operation_id,
            principal.org_id,
            principal.workspace_id,
        )
        if row is None:
            return False
        tag = await connection.execute(
            "UPDATE harness_operations SET cancel_requested_at=clock_timestamp(), "
            "cancel_requested_by=$2, cancel_reason=$3 WHERE operation_id=$1 "
            "AND org_id=$4 AND workspace_id=$5 AND cancel_requested_at IS NULL "
            "AND state <> ALL($6::text[])",
            operation_id,
            principal.subject,
            reason,
            principal.org_id,
            principal.workspace_id,
            [s.value for s in TERMINAL_STATES],
        )
        written = _rows_affected(tag) > 0
        await audit(
            connection,
            operation_id=operation_id,
            org_id=principal.org_id,
            workspace_id=principal.workspace_id,
            event="cancel.request",
            actor=principal.subject,
            allowed=written,
        )
        return written


async def settle_unheld_cancellation(
    connection, ledger, *, operation_id, principal, reason
):
    """Finish queued cancellation through the maintained fence/withdraw/budget path.

    The committed cancellation already prevents new acquisition. The fence rechecks
    holder and effect history under the operation lock, closes the lease and records
    the terminal result in the outbox-classification transaction. Ledger calls happen
    only after commit and can be retried using the stored reservation identity.
    """
    from .admission import Reservation, cancel_before_dispatch
    from .leases import close

    row = await connection.fetchrow(
        "SELECT o.job_id,o.attempt_id,c.reservation_id,c.reservation_state "
        "FROM harness_operations o JOIN harness_approval_consumption c "
        "USING(operation_id) "
        "WHERE o.operation_id=$1 AND o.org_id=$2 AND o.workspace_id=$3 "
        "AND o.cancel_requested_at IS NOT NULL "
        "AND NOT EXISTS(SELECT 1 FROM harness_operation_leases l WHERE "
        "l.operation_id=o.operation_id AND l.holder IS NOT NULL)",
        operation_id,
        principal.org_id,
        principal.workspace_id,
    )
    if row is None:
        return False
    reservation = Reservation(row["reservation_id"], row["job_id"], row["attempt_id"])

    class UnheldFence:
        async def fence(self, **_):
            locked = await connection.fetchrow(
                "SELECT state FROM harness_operations WHERE operation_id=$1 "
                "AND org_id=$2 AND workspace_id=$3 AND cancel_requested_at "
                "IS NOT NULL FOR UPDATE",
                operation_id,
                principal.org_id,
                principal.workspace_id,
            )
            if locked is None or await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM harness_operation_leases "
                "WHERE operation_id=$1 AND holder IS NOT NULL)",
                operation_id,
            ):
                return False
            effects = await connection.fetchval(
                "SELECT EXISTS(SELECT 1 FROM harness_provider_call_intent "
                "WHERE operation_id=$1)",
                operation_id,
            )
            state = OperationState.UNKNOWN if effects else OperationState.CANCELLED
            await close(
                connection,
                operation_id=operation_id,
                reason="unheld cancellation",
                fence_token=None,
            )
            await connection.execute(
                "UPDATE harness_operations SET state=$2, cleanup_required=$3, "
                "version=version+1, updated_at=now() WHERE operation_id=$1 "
                "AND state <> ALL($4::text[])",
                operation_id,
                state.value,
                effects,
                [s.value for s in TERMINAL_STATES],
            )
            await audit(
                connection,
                operation_id=operation_id,
                org_id=principal.org_id,
                workspace_id=principal.workspace_id,
                event="cancel.unheld",
                actor=principal.subject,
                allowed=True,
                detail=state.value,
            )
            # Historical effects are conservatively retained even if the queue has
            # no delivery record. This also repairs older unsafe lease releases.
            return not effects and row["reservation_state"] != "retained"

    await cancel_before_dispatch(
        connection,
        ledger,
        UnheldFence(),
        operation_id=operation_id,
        job_id=row["job_id"],
        attempt_id=row["attempt_id"],
        reservation=reservation,
        reason=reason or "authenticated cancellation",
    )
    return True


async def check_cancel_requested(
    connection: Connection,
    lease: ExecutionLease,
) -> CancellationRecord | None:
    """Read the cancellation request for the lease holder's own operation.

    Returns `None` when no cancellation has been requested. Returns the request when one
    exists, so the executor can act on it at its next safe point.

    Requires a live lease: the executor must still be entitled to act on the operation
    to read its own cancel state. An expired or released holder is no longer the
    executor and has nothing to poll.
    """
    if not isinstance(lease, ExecutionLease):
        raise ContractViolation("lease must be an ExecutionLease")

    row = await connection.fetchrow(
        """
        SELECT o.cancel_requested_by, o.cancel_reason
          FROM harness_operations o
          JOIN harness_operation_leases l ON l.operation_id = o.operation_id
         WHERE o.operation_id = $1
           AND l.holder = $2 AND l.fence_token = $3
           AND l.closed_at IS NULL AND l.expires_at > clock_timestamp()
           AND l.runtime_deadline > clock_timestamp()
           AND o.org_id = $4 AND o.workspace_id = $5 AND l.attempt_id = $6
           AND o.cancel_requested_at IS NOT NULL
        """,
        lease.operation_id,
        lease.holder,
        lease.fence_token,
        lease.org_id,
        lease.workspace_id,
        lease.attempt_id,
    )
    if row is None:
        return None
    return CancellationRecord(
        operation_id=lease.operation_id,
        requested_by=row["cancel_requested_by"],
        reason=row["cancel_reason"],
    )


# ---------------------------------------------------------------------------
# Sweep: expired leases
# ---------------------------------------------------------------------------

# A provider-observer callable: given the idempotency key for a call the sweep found
# in `intended` state, ask the provider what happened and return a (CallOutcome,
# detail, provider_ref) triple. The sweep never calls a provider itself; this hook is
# where the credential boundary is.
ProviderObserver = Callable[
    [str, str, str, str],  # idempotency_key, provider, operation_kind, target
    Awaitable[tuple[CallOutcome, str | None, str | None]],
]

# A timed-out hook may refuse cancellation. Retain a bounded number of tasks until
# they actually exit, preventing duplicate observation of that intent in this loop.
# Hooks receive only provider identifiers, never the recovery database connection.
_MAX_OBSERVER_TASKS = 50
_observer_tasks: dict[tuple[asyncio.AbstractEventLoop, str], asyncio.Task] = {}


async def _observe_with_deadline(observer, key, provider, kind, target, seconds):
    identity = (asyncio.get_running_loop(), key)
    previous = _observer_tasks.get(identity)
    if previous is not None and previous.done():
        _observer_tasks.pop(identity)
        previous = None
    if previous is not None or len(_observer_tasks) >= _MAX_OBSERVER_TASKS:
        raise TimeoutError("Provider observation still active or capacity exhausted")

    async def observe():
        return await observer(key, provider, kind, target)

    task = asyncio.create_task(observe())
    _observer_tasks[identity] = task

    def completed(finished):
        if _observer_tasks.get(identity) is finished:
            _observer_tasks.pop(identity)
        # Observe late failures without publishing a late provider result.
        if not finished.cancelled():
            finished.exception()

    task.add_done_callback(completed)
    try:
        done, _ = await asyncio.wait({task}, timeout=seconds)
        if not done:
            raise TimeoutError("Provider observation deadline exceeded")
        if task.cancelled():
            # Cancellation inside the child is an inconclusive observation. Parent
            # cancellation still propagates directly from asyncio.wait above.
            raise TimeoutError("Provider observer cancelled itself")
        return task.result()
    finally:
        if not task.done():
            # Do not await cancellation: a broken hook may suppress it indefinitely.
            task.cancel()


def _validate_observation_timeout(seconds: float) -> None:
    if (
        isinstance(seconds, bool)
        or not isinstance(seconds, int | float)
        or not 0 < seconds <= 30
    ):
        raise ContractViolation(
            "observation_timeout_seconds must be finite and in (0, 30]"
        )


async def sweep_expired_leases(
    connection: Connection,
    *,
    observe_call: ProviderObserver | None = None,
    max_operations: int = 50,
    max_attempts: int = DEFAULT_MAX_EXECUTION_ATTEMPTS,
    max_reconcile_attempts: int = 3,
    observation_timeout_seconds: float = 30,
) -> RecoveryReport:
    """Settle operations whose executor lease has lapsed.

    Finds held, open leases that have expired and takes each over using
    `fence_expired_lease`, which advances the fence token without consuming an
    execution attempt. For each operation, decides:

    - Cancellation requested: settle as CANCELLED and close the lease permanently.
    - No provider calls, attempts remaining: release the lease so the next executor
      can acquire it (retry). The dead worker is fenced out by the token advance.
    - No provider calls, attempts exhausted: settle as FAILED and close permanently.
    - Unresolved provider calls: reconcile each via `observe_call` (or mark UNKNOWN),
      settle the operation as UNKNOWN/FAILED/SUCCEEDED as appropriate, and close.

    `observe_call` is the credential-bearing hook: it must not be this module. When it
    is `None`, every unresolved call is marked UNKNOWN and budget is retained.

    The attempt ceiling comes from the first grant's durable lease policy. The
    legacy max_attempts argument is validated but cannot override that policy.
    Idempotent: running it twice without new expiries settles nothing extra.
    """
    if not isinstance(max_operations, int) or max_operations < 1:
        raise ContractViolation("max_operations must be a positive integer")

    if connection.is_in_transaction():
        raise ContractViolation("Recovery must own its transaction boundaries")
    if type(max_attempts) is not int or max_attempts < 1:
        raise ContractViolation("max_attempts must be a positive integer")
    if type(max_reconcile_attempts) is not int or not 1 <= max_reconcile_attempts <= 10:
        raise ContractViolation("max_reconcile_attempts must be between 1 and 10")
    _validate_observation_timeout(observation_timeout_seconds)
    expired = await connection.fetch(
        """
        SELECT operation_id FROM harness_operation_leases
         WHERE holder IS NOT NULL AND closed_at IS NULL
           AND (expires_at <= clock_timestamp()
                    OR runtime_deadline <= clock_timestamp())
         ORDER BY LEAST(expires_at, runtime_deadline)
         LIMIT $1
        """,
        max_operations,
    )

    results: list[SweepResult] = []
    retried = retained = skipped = deferred = 0

    for row in expired:
        operation_id: str = row["operation_id"]
        result = await _recover_one(
            connection,
            operation_id=operation_id,
            observe_call=observe_call,
            max_attempts=max_attempts,
            max_reconcile_attempts=max_reconcile_attempts,
            observation_timeout_seconds=observation_timeout_seconds,
        )
        results.append(result)
        if result.action == "retried":
            retried += 1
        elif result.action == "deferred":
            deferred += 1
        elif result.action in ("unknown", "cancelled", "failed", "succeeded"):
            retained += 1
        else:
            skipped += 1

    return RecoveryReport(
        retried=retried,
        retained=retained,
        skipped=skipped,
        results=tuple(results),
        deferred=deferred,
    )


async def _settle_operation(
    connection: Connection,
    *,
    operation_id: str,
    state: OperationState,
    detail: str,
) -> bool:
    """Move an operation to a terminal state, but only if it is not already terminal.

    Recovery needs to write an outcome without knowing the current version -- the
    version-checked `store.transition` is for callers who read the record first and
    present their read version, which is not the sweep's pattern. This UPDATE is
    conditioned on ``state NOT IN (terminal states)`` instead: it is idempotent and
    does not need a version.

    Returns True if the row was updated, False if it was already terminal.
    """
    terminal_values = [s.value for s in TERMINAL_STATES]
    tag = await connection.execute(
        """
        UPDATE harness_operations
           SET state = $2, detail = $3, version = version + 1, updated_at = now()
         WHERE operation_id = $1
           AND state NOT IN (SELECT unnest($4::text[]))
        """,
        operation_id,
        state.value,
        detail,
        terminal_values,
    )
    return _rows_affected(tag) > 0


async def _recovery_audit(connection, lease, event, allowed=True, detail=None):
    await audit(
        connection,
        operation_id=lease.operation_id,
        org_id=lease.org_id,
        workspace_id=lease.workspace_id,
        event=event,
        actor=lease.holder,
        allowed=allowed,
        attempt_id=lease.attempt_id,
        fence_token=lease.fence_token,
        detail=detail,
    )


async def _reserve_reconciliation(connection, key, maximum):
    """Persist the attempt and exponential delay BEFORE observer I/O."""
    row = await connection.fetchrow(
        "SELECT reconcile_attempts, reconcile_after FROM harness_provider_call_intent "
        "WHERE idempotency_key=$1 AND stage='intended' "
        "AND (reconcile_after IS NULL OR reconcile_after <= clock_timestamp()) "
        "FOR UPDATE",
        key,
    )
    if row is None:
        return None
    if row["reconcile_attempts"] >= maximum:
        return (row["reconcile_attempts"], True)
    attempt = row["reconcile_attempts"] + 1
    await connection.execute(
        "UPDATE harness_provider_call_intent SET reconcile_attempts=$2, "
        "reconcile_after=clock_timestamp()+($3 * interval '1 second') "
        "WHERE idempotency_key=$1",
        key,
        attempt,
        30 * 2 ** (attempt - 1),
    )
    return attempt, False


async def _recover_one(
    connection: Connection,
    *,
    operation_id: str,
    observe_call: ProviderObserver | None,
    max_attempts: int,
    max_reconcile_attempts: int,
    observation_timeout_seconds: float,
) -> SweepResult:
    """Hold a finite recovery claim and persist bounded provider observation retries."""
    takeover = await fence_expired_lease(connection, operation_id=operation_id)
    skipped = SweepResult(operation_id, "skipped", "recovery claim absent or lost")
    if takeover is None:
        return skipped
    lease = takeover.lease
    rows = await connection.fetch(
        "SELECT idempotency_key FROM harness_provider_call_intent "
        "WHERE operation_id=$1 ORDER BY created_at, idempotency_key",
        operation_id,
    )
    calls = []
    for row in rows:
        key = row["idempotency_key"]
        call = await read_call(connection, idempotency_key=key)
        if call.stage is CallStage.INTENDED:
            async with connection.transaction():
                if not await lock_lease(connection, lease):
                    await _recovery_audit(
                        connection, lease, "recovery.lost_claim", False
                    )
                    return skipped
                reserved = await _reserve_reconciliation(
                    connection, key, max_reconcile_attempts
                )
                if reserved is None:
                    calls.append(call)
                    continue
                await _recovery_audit(connection, lease, "recovery.observe_started")
            attempt, exhausted = reserved
            outcome, detail, provider_ref = CallOutcome.UNKNOWN, None, None
            if observe_call is not None and not exhausted:
                try:
                    outcome, detail, provider_ref = await _observe_with_deadline(
                        observe_call,
                        call.idempotency_key,
                        call.provider,
                        call.operation_kind,
                        call.target,
                        observation_timeout_seconds,
                    )
                    if not isinstance(outcome, CallOutcome):
                        raise ContractViolation("Observer must return CallOutcome")
                except Exception:  # noqa: BLE001
                    outcome, detail, provider_ref = CallOutcome.UNKNOWN, None, None
            async with connection.transaction():
                if not await lock_lease(connection, lease):
                    await _recovery_audit(
                        connection, lease, "recovery.lost_claim", False
                    )
                    return skipped
                if outcome is CallOutcome.UNKNOWN and attempt < max_reconcile_attempts:
                    await _recovery_audit(connection, lease, "recovery.deferred")
                else:
                    call, disposition = await reconcile(
                        connection,
                        idempotency_key=key,
                        outcome=outcome,
                        detail=detail,
                        provider_ref=provider_ref,
                    )
                    await _recovery_audit(
                        connection,
                        lease,
                        "recovery.reconciled",
                        detail=disposition.value,
                    )
        calls.append(call)

    dispositions = tuple((c.idempotency_key, disposition_for(c)) for c in calls)
    kinds = {d.value for _, d in dispositions}
    budget = next(iter(kinds)) if len(kinds) == 1 else "mixed" if kinds else "release"
    async with connection.transaction():
        if not await lock_lease(connection, lease):
            await _recovery_audit(connection, lease, "recovery.lost_claim", False)
            return skipped
        operation = await connection.fetchrow(
            "SELECT cancel_requested_at IS NOT NULL AS cancelled, cleanup_required "
            "FROM harness_operations WHERE operation_id=$1",
            operation_id,
        )
        cancelled = operation["cancelled"]
        if cancelled and any(
            d is not BudgetDisposition.RELEASE for _, d in dispositions
        ):
            await connection.execute(
                "UPDATE harness_operations SET cleanup_required=true "
                "WHERE operation_id=$1",
                operation_id,
            )
        if any(c.stage is CallStage.INTENDED for c in calls):
            await _recovery_audit(connection, lease, "recovery.deferred")
            return SweepResult(
                operation_id,
                "deferred",
                "provider reconciliation scheduled",
                "retain",
                dispositions,
            )
        if operation["cleanup_required"]:
            state = OperationState.UNKNOWN
        elif cancelled:
            state = (
                OperationState.CANCELLED
                if all(d is BudgetDisposition.RELEASE for _, d in dispositions)
                else OperationState.UNKNOWN
            )
        elif not calls and takeover.attempts < lease.max_attempts:
            await _recovery_audit(connection, lease, "recovery.retry")
            if not await release(connection, lease):
                raise ContractViolation("Recovery claim lost while locked")
            return SweepResult(
                operation_id,
                "retried",
                "lease released; ready for next attempt",
                "retain",
            )
        elif not calls:
            state = OperationState.FAILED
        elif all(c.outcome is CallOutcome.SUCCEEDED for c in calls):
            # Every recorded call succeeded. Settle SUCCEEDED only when this is
            # confirmed to be the *complete* admitted plan. A partial confirmed
            # prefix defers so the next executor can continue the remaining steps
            # using the same stable idempotency keys (already-stable keys are
            # returned idempotently; only the unrecorded steps are new work).
            # Absent plan falls back to UNKNOWN.
            plan_result = await _check_all_plan_steps_succeeded(
                connection, operation_id, calls
            )
            if plan_result is _PLAN_PREFIX:
                if await _release_prefix_claim(connection, operation_id):
                    await _recovery_audit(connection, lease, "recovery.retry")
                    return SweepResult(
                        operation_id,
                        "retried",
                        "partial plan prefix confirmed; ready for bounded continuation",
                        "retain",
                        dispositions,
                    )
                plan_result = OperationState.UNKNOWN
            state = plan_result  # SUCCEEDED or UNKNOWN
        elif all(d is BudgetDisposition.RELEASE for _, d in dispositions):
            state = OperationState.FAILED
        else:
            state = OperationState.UNKNOWN
        detail = (
            "budget "
            + {
                "settle": "settled",
                "release": "released",
                "retain": "retained",
                "mixed": "mixed; see per-call dispositions",
            }[budget]
        )
        if state is OperationState.UNKNOWN and not cancelled:
            detail += "; workflow completion unconfirmed"
        if cancelled and state is OperationState.UNKNOWN:
            detail += "; cancellation requires cleanup of recorded provider effects"
        await _settle_operation(
            connection, operation_id=operation_id, state=state, detail=detail
        )
        await _recovery_audit(connection, lease, "recovery.settled", detail=state.value)
        if not await close(
            connection,
            operation_id=operation_id,
            reason="recovery: " + state.value,
            fence_token=lease.fence_token,
            holder=lease.holder,
        ):
            raise ContractViolation("Recovery claim expired before close")
    return SweepResult(operation_id, state.value, detail, budget, dispositions)


# ---------------------------------------------------------------------------
# Sweep: unresolved provider calls
# ---------------------------------------------------------------------------


async def _orphan_still_owned(connection, operation_id, fence_token):
    await connection.fetchrow(
        "SELECT operation_id FROM harness_operations WHERE operation_id=$1 FOR UPDATE",
        operation_id,
    )
    claim = await connection.fetchrow(
        "SELECT *, (holder IS NOT NULL AND closed_at IS NULL "
        "AND expires_at > clock_timestamp() "
        "AND runtime_deadline > clock_timestamp()) AS live "
        "FROM harness_operation_leases WHERE operation_id=$1 FOR UPDATE",
        operation_id,
    )
    return (not claim and fence_token is None) or (
        claim and not claim["live"] and claim["fence_token"] == fence_token
    )


async def sweep_unresolved_calls(
    connection: Connection,
    *,
    observe_call: ProviderObserver,
    max_calls: int = 50,
    max_reconcile_attempts: int = 3,
    observation_timeout_seconds: float = 30,
) -> int:
    """Reconcile orphaned intents with durable attempt/backoff and successor fencing.

    The operation advisory lock (`harness-provider-dispatch:<id>`) is acquired before
    the first transaction and released only after the fenced write commits, so a live
    executor cannot interleave its own provider I/O while recovery is observing the same
    intent. This is the same lock `execute_provider` holds across provider I/O; the two
    cannot run concurrently for the same operation.
    """
    if not callable(observe_call):
        raise ContractViolation("observe_call must be callable")
    if type(max_calls) is not int or max_calls < 1:
        raise ContractViolation("max_calls must be a positive integer")
    if type(max_reconcile_attempts) is not int or not 1 <= max_reconcile_attempts <= 10:
        raise ContractViolation("max_reconcile_attempts must be between 1 and 10")
    _validate_observation_timeout(observation_timeout_seconds)
    if connection.is_in_transaction():
        raise ContractViolation("Recovery must own its transaction boundaries")
    rows = await connection.fetch(
        """
        SELECT i.*, (SELECT fence_token FROM harness_operation_leases
                      WHERE operation_id=i.operation_id) AS current_fence
          FROM harness_provider_call_intent i
          LEFT JOIN harness_operation_leases l ON l.operation_id=i.operation_id
           AND l.holder IS NOT NULL AND l.closed_at IS NULL
           AND l.expires_at > clock_timestamp()
           AND l.runtime_deadline > clock_timestamp()
         WHERE i.stage='intended' AND l.operation_id IS NULL
           AND (i.reconcile_after IS NULL OR i.reconcile_after <= clock_timestamp())
         ORDER BY i.created_at LIMIT $1
        """,
        max_calls,
    )
    settled = 0
    for row in rows:
        operation_id = row["operation_id"]
        actor = "recovery-orphan:" + str(uuid4())

        async def record(event, allowed=True, _row=row, _actor=actor):
            await audit(
                connection,
                operation_id=_row["operation_id"],
                org_id=_row["org_id"],
                workspace_id=_row["workspace_id"],
                event=event,
                actor=_actor,
                allowed=allowed,
                attempt_id=_row["attempt_id"],
                fence_token=_row["current_fence"],
            )

        # Hold the same advisory lock that execute_provider holds across provider I/O.
        # This prevents a concurrent executor from interleaving a mutation while we
        # are observing. Session-level (not xact-level) so it survives transaction
        # boundaries; released explicitly after the fenced write commits.
        dispatch_key = f"harness-provider-dispatch:{operation_id}"
        lock_held = await connection.fetchval(
            "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", dispatch_key
        )
        if not lock_held:
            # A provider hook is active for this operation; skip until it finishes.
            await record("recovery.orphan_skipped_dispatch_lock", False)
            continue

        try:
            async with connection.transaction():
                if not await _orphan_still_owned(
                    connection, operation_id, row["current_fence"]
                ):
                    await record("recovery.orphan_refused", False)
                    continue
                reserved = await _reserve_reconciliation(
                    connection, row["idempotency_key"], max_reconcile_attempts
                )
                if reserved is None:
                    continue
                await record("recovery.orphan_observe_started")
            attempt, exhausted = reserved
            outcome, detail, provider_ref = CallOutcome.UNKNOWN, None, None
            if not exhausted:
                try:
                    outcome, detail, provider_ref = await _observe_with_deadline(
                        observe_call,
                        row["idempotency_key"],
                        row["provider"],
                        row["operation_kind"],
                        row["target"],
                        observation_timeout_seconds,
                    )
                    if not isinstance(outcome, CallOutcome):
                        raise ContractViolation("Observer must return CallOutcome")
                except Exception:  # noqa: BLE001
                    outcome, detail, provider_ref = CallOutcome.UNKNOWN, None, None
            async with connection.transaction():
                if not await _orphan_still_owned(
                    connection, operation_id, row["current_fence"]
                ):
                    await record("recovery.orphan_refused", False)
                    continue
                current = await connection.fetchrow(
                    "SELECT reconcile_attempts, stage"
                    " FROM harness_provider_call_intent"
                    " WHERE idempotency_key=$1 FOR UPDATE",
                    row["idempotency_key"],
                )
                if (
                    current["stage"] != "intended"
                    or current["reconcile_attempts"] != attempt
                ):
                    await record("recovery.orphan_refused", False)
                    continue
                if outcome is CallOutcome.UNKNOWN and attempt < max_reconcile_attempts:
                    await record("recovery.orphan_deferred")
                    continue
                await reconcile(
                    connection,
                    idempotency_key=row["idempotency_key"],
                    outcome=outcome,
                    detail=detail,
                    provider_ref=provider_ref,
                )
                await record("recovery.orphan_reconciled")
                settled += 1
                # After reconciling this call, check whether every call for this
                # operation is now settled. If so, finalize the operation durably so
                # it does not remain pending behind a closed/unheld lease.
                await _finalize_if_all_calls_settled(
                    connection, operation_id, row["current_fence"], actor
                )
        finally:
            await connection.execute(
                "SELECT pg_advisory_unlock(hashtextextended($1, 0))", dispatch_key
            )
    return settled


async def _finalize_if_all_calls_settled(connection, operation_id, fence_token, actor):
    """Settle an operation whose every provider call is now reconciled.

    Called after each call reconciliation inside a transaction. If any call is still
    in `intended` stage the operation is not yet settleable; we skip and the sweep
    will pick it up again on a future pass when the remaining calls are reconciled.

    Only writes a terminal state when the lease is unheld or held at the recorded
    fence token — the same condition _orphan_still_owned checks. This prevents a
    successor worker from being closed by a slow recovery pass.
    """
    # Check whether all calls for this operation are settled.
    pending = await connection.fetchval(
        "SELECT EXISTS(SELECT 1 FROM harness_provider_call_intent "
        "WHERE operation_id=$1 AND stage='intended')",
        operation_id,
    )
    if pending:
        return  # Other calls still unresolved; deferred.

    if not await _orphan_still_owned(connection, operation_id, fence_token):
        return

    all_rows = await connection.fetch(
        "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
        operation_id,
    )
    from .execution import _call_from_row as _cfr

    calls = [_cfr(r) for r in all_rows]
    if not calls:
        return  # No calls at all: leave for sweep_expired_leases to handle.

    dispositions = [(c.idempotency_key, disposition_for(c)) for c in calls]
    kinds = {d.value for _, d in dispositions}
    budget = next(iter(kinds)) if len(kinds) == 1 else "mixed"

    operation = await connection.fetchrow(
        "SELECT cancel_requested_at IS NOT NULL AS cancelled, cleanup_required "
        "FROM harness_operations WHERE operation_id=$1",
        operation_id,
    )
    released = all(d is BudgetDisposition.RELEASE for _, d in dispositions)
    if operation["cancelled"] and not released:
        await connection.execute(
            "UPDATE harness_operations SET cleanup_required=true WHERE operation_id=$1",
            operation_id,
        )
    if operation["cleanup_required"]:
        state = OperationState.UNKNOWN
    elif operation["cancelled"]:
        state = OperationState.CANCELLED if released else OperationState.UNKNOWN
    elif released:
        state = OperationState.FAILED
    elif all(c.outcome is CallOutcome.SUCCEEDED for c in calls):
        state = await _check_all_plan_steps_succeeded(connection, operation_id, calls)
        if state is _PLAN_PREFIX:
            if await _release_prefix_claim(connection, operation_id):
                await audit(
                    connection,
                    operation_id=operation_id,
                    org_id=all_rows[0]["org_id"],
                    workspace_id=all_rows[0]["workspace_id"],
                    event="recovery.orphan_retry",
                    actor=actor,
                    allowed=True,
                    detail="confirmed prefix; budget retained",
                )
                return
            state = OperationState.UNKNOWN
    else:
        state = OperationState.UNKNOWN

    detail = "budget " + {
        "settle": "settled",
        "release": "released",
        "retain": "retained",
        "mixed": "mixed; see per-call dispositions",
    }.get(budget, "retained")
    if state is OperationState.UNKNOWN:
        detail += "; workflow completion unconfirmed"

    await _settle_operation(
        connection, operation_id=operation_id, state=state, detail=detail
    )
    # The operation/lease locks and dispatch advisory lock are held. Fence and
    # clear an expired holder before using the unheld close path.
    await connection.execute(
        "UPDATE harness_operation_leases SET fence_token=fence_token+1, holder=NULL, "
        "expires_at=NULL, acquired_at=NULL, runtime_deadline=NULL, attempt_id=NULL "
        "WHERE operation_id=$1 AND closed_at IS NULL",
        operation_id,
    )
    await close(
        connection,
        operation_id=operation_id,
        reason="orphan recovery: " + state.value,
        fence_token=None,
    )
    await audit(
        connection,
        operation_id=operation_id,
        org_id=all_rows[0]["org_id"],
        workspace_id=all_rows[0]["workspace_id"],
        event="recovery.orphan_settled",
        actor=actor,
        allowed=True,
        detail=state.value,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


# Sentinel: all recorded calls succeeded but the plan has more steps remaining.
_PLAN_PREFIX = object()


async def _check_all_plan_steps_succeeded(connection, operation_id, calls):
    progress = await confirmed_plan_progress(connection, operation_id)
    return {
        PlanProgress.COMPLETE: OperationState.SUCCEEDED,
        PlanProgress.PREFIX: _PLAN_PREFIX,
        PlanProgress.UNKNOWN: OperationState.UNKNOWN,
    }[progress]


async def _release_prefix_claim(connection, operation_id):
    """Recovery-only handoff, under the already held operation and lease locks.

    Call only after verifying a successful prefix and checking cancellation. Never
    reopen a closed lease or increase its persisted attempt ceiling.
    """
    tag = await connection.execute(
        "UPDATE harness_operation_leases SET fence_token=fence_token+1, holder=NULL, "
        "expires_at=NULL, acquired_at=NULL, runtime_deadline=NULL, attempt_id=NULL, "
        "updated_at=now() WHERE operation_id=$1 AND closed_at IS NULL "
        "AND attempts < max_attempts",
        operation_id,
    )
    return _rows_affected(tag) > 0


def _rows_affected(tag: object) -> int:
    """Rows touched, from the driver's command tag (`UPDATE <n>`)."""
    if not isinstance(tag, str):
        return 0
    parts = tag.strip().split()
    if not parts:
        return 0
    try:
        return int(parts[-1])
    except ValueError:
        return 0
