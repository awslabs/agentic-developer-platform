"""Performing a provider call durably, and reconciling one whose outcome is unknown.

Issue #5527 (w6-04), EPIC #4910, Wave 6.

## The property this module exists for

**A provider call is described in this database before it is made, so "we may have
asked" is never confused with "we provably never asked."**

`leases.py` answers who may act. This answers what happens when the acting stops
mid-call.

A worker calls a provider to create something. The provider creates it. The reply is
lost -- the process is killed, the network drops, the socket times out. From inside the
worker there is no difference between that and a call that never left. From outside
there is all the difference in the world: one has a running, billing resource behind it
and the other does not.

So the intent is committed first, under the caller's fence token, keyed by the
idempotency key the call will present. After a crash the row is the only evidence the
call may exist, and the key is what lets a recovery pass ask the provider about *that*
call rather than a different one.

This is the same discipline `admission.py` applies to the ledger (`IntentStage`),
applied to the more expensive of the two external calls: a leaked ledger hold is
bookkeeping, a leaked provider resource is money.

## Why `unknown` is not `failed`

The temptation is to treat a lost reply as a failure and retry. Both directions of
collapsing the two are wrong, and they are wrong in opposite ways:

* **Calling an uncertain provision `failed` and retrying it** pays twice for capacity
  that may already exist -- and the second resource is invisible to the first's records.
* **Calling an uncertain teardown `succeeded`** reports cleanup complete while billable
  capacity keeps running, which is the leak that shows up on an invoice rather than in a
  log.

So `CallOutcome.UNKNOWN` is a real answer, `UNRESOLVED` is a terminal stage that is
*not* a failure, and budget is **retained** rather than released whenever absence has
not been established. Only a provider that positively says "no such resource" permits a
release.

## What this module does not do

It does not talk to a provider. Every entry point takes the provider's answer as an
argument, or takes a callable the caller supplies. That is not squeamishness about I/O:
this package declares zero runtime dependencies and holds no credential, and the
credential-delivery boundary is #5528's (w6-05). A module that imported an SDK here
would be the place a credential ended up.

It does not decide whether a reporter may speak for an operation -- report authority is
#5529's (w6-06). It does not release or retain budget itself; it says which of the two
is owed, and the ledger call is the caller's, because the reservation ledger is the
domain's (`__init__.py`'s ownership table).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .identity import ContractViolation, OperationState
from .leases import (
    ExecutionLease,
    LeaseRefused,
    close,
    lock_lease,
    release,
    renew,
)
from .store import Connection

__all__ = [
    "BudgetDisposition",
    "CallOutcome",
    "CallStage",
    "CancellationPending",
    "OperationExecutor",
    "OperationStatus",
    "ProviderCall",
    "ProviderCallRefused",
    "audit",
    "derive_idempotency_key",
    "observe",
    "read_audit",
    "read_call",
    "reconcile",
    "record_intent",
    "unresolved_calls",
]


class CallStage(str, Enum):
    """How far a provider call is *known* to have got.

    Mirrors `admission.IntentStage` in discipline: advanced only after the corresponding
    external effect is known to have happened, so the column may lag reality and must
    never run ahead of it. "The database says observed" implies a reply arrived; "the
    database says intended" implies nothing either way.

    Values go in a column with a CHECK constraint naming them (`schema.py`, version 4).
    """

    INTENDED = "intended"
    """Committed, and the provider has not been called -- or was, with no reply heard.

    One state for both, deliberately: after a crash they are indistinguishable from
    inside this process, and a state claiming to tell them apart would be a guess
    recorded as a fact. `reconcile` resolves the ambiguity by asking the provider.
    """

    OBSERVED = "observed"
    """A reply was received by the worker that made the call."""

    RECONCILED = "reconciled"
    """A recovery pass asked the provider afterwards and got an answer.

    Distinct from `OBSERVED` because "the caller saw this" and "we went back and asked"
    are different provenance for the same fact, and an incident review of a duplicated
    spend needs to know which one a row is.
    """

    UNRESOLVED = "unresolved"
    """The provider could not be reached to answer. Terminal, and not a failure.

    The state in which budget is retained and a human decides. It exists because "we
    asked and it said no" and "we could not ask" have opposite safe answers, and a
    vocabulary without a place for the second forces it to be written as the first.
    """


class CallOutcome(str, Enum):
    """What a provider said, including saying nothing.

    `UNKNOWN` is a first-class answer rather than an error, because the whole module
    exists for the case where it is the truthful one. See the module docstring.
    """

    SUCCEEDED = "succeeded"
    """The provider confirmed the effect. Whatever was created exists."""

    FAILED = "failed"
    """The provider refused, and established that nothing was created.

    Stronger than "the call errored": a timeout is not this. A 4xx that says the request
    was rejected before any resource was allocated is.
    """

    ABSENT = "absent"
    """The provider was asked and reported no such resource under this key.

    The only outcome that permits releasing reserved budget, because it is the only one
    that establishes absence rather than failing to establish presence.
    """

    UNKNOWN = "unknown"
    """The provider's answer was not obtained. Something may exist."""


class BudgetDisposition(str, Enum):
    """What is owed to the reservation ledger for a call, once its stage is settled.

    Named and returned rather than acted on, because the ledger belongs to the domain
    (`__init__.py`'s ownership table). This module's contribution is the *decision*, and
    it is the decision that carries the safety property.
    """

    RELEASE = "release"
    """Absence is established; the hold may be returned.

    Reachable only from `CallOutcome.ABSENT` and from `FAILED`, both of which say
    nothing was created. Never from `UNKNOWN`.
    """

    RETAIN = "retain"
    """Something may exist. Hold the budget until a human or the provider settles it.

    The conservative answer, and the default for every uncertain path. Retaining budget
    that turns out to be unnecessary costs a reservation nobody spends. Releasing budget
    for a resource that does exist costs an untracked running resource -- and the
    reservation that would have paid for it is gone, so the overspend is also invisible.
    """

    SETTLE = "settle"
    """The call succeeded; the spend is real and the envelope consumed as approved."""


class ProviderCallRefused(PermissionError):
    """A record, observation or reconciliation was refused.

    `PermissionError` for the reason `LeaseRefused` gives: the caller is being told it
    may not do this, not that something broke.
    """


class CancellationPending(ProviderCallRefused):
    """The provider effect is recorded; cancellation needs reconciliation/cleanup."""

    def __init__(self, call, disposition):
        super().__init__(
            "Cancellation arrived during dispatch; "
            "reconcile/clean up the recorded effect"
        )
        self.call = call
        self.disposition = disposition


@dataclass(frozen=True)
class ProviderCall:
    """A provider call this database knows about, at whatever stage it has reached.

    Frozen: this is evidence read out of a row, and a caller that could mutate its
    `stage` or `fence_token` could manufacture the very certainty the row exists to
    withhold.
    """

    idempotency_key: str
    operation_id: str
    org_id: str
    workspace_id: str
    job_id: str
    attempt_id: str
    fence_token: int
    provider: str
    operation_kind: str
    target: str
    stage: CallStage
    outcome: CallOutcome | None
    provider_ref: str | None
    created_at: datetime
    updated_at: datetime

    @property
    def is_settled(self) -> bool:
        """True when no further automatic action is owed on this row.

        `UNRESOLVED` counts as settled *for the sweep*: it is the state that says a
        human decides, and a sweep that kept retrying it would be overriding that
        decision on a timer.
        """
        return self.stage is not CallStage.INTENDED

    @property
    def may_have_happened(self) -> bool:
        """True when a resource may exist that nothing here has confirmed.

        The question a cleanup path asks. `INTENDED` and an `UNKNOWN` outcome both
        answer yes, and so does `UNRESOLVED` -- which is why none of the three may be
        reported as a completed teardown.
        """
        if self.stage is CallStage.INTENDED or self.stage is CallStage.UNRESOLVED:
            return True
        return self.outcome is CallOutcome.UNKNOWN


def derive_idempotency_key(operation_id: str, attempt_id: str, step: str) -> str:
    """The key a call presents to the provider, derivable by a recovering process.

    **Derived, never generated.** A random key would be unknown after a crash, which is
    precisely the moment it is needed: the provider has to be asked about the call under
    the same key it was made under, or the question is about a different call and the
    answer is worthless.

    `step` distinguishes several calls within one attempt -- an attempt that creates a
    network and then a host makes two, and they must not collide. A caller passing the
    same `step` twice in one attempt is claiming they are the same call, which is what
    the PRIMARY KEY on this value then enforces.

    Composed by joining rather than hashing so that an operator reading an unresolved
    row can see which operation, attempt and step it belongs to without a lookup.
    Nothing here is secret -- these are internal identifiers -- so there is nothing to
    hide, and a hash would trade readability during an incident for no security.
    """
    for name, value in (
        ("operation_id", operation_id),
        ("attempt_id", attempt_id),
        ("step", step),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ContractViolation(f"{name} must be a non-empty string")
        if "/" in value:
            raise ContractViolation(
                f"{name} must not contain '/': it is the separator in the derived "
                "idempotency key, and a value containing it would make two different "
                "calls derive the same key"
            )
    return f"{operation_id}/{attempt_id}/{step}"


async def record_intent(
    connection: Connection,
    lease: ExecutionLease,
    *,
    idempotency_key: str,
    provider: str,
    operation_kind: str,
    target: str,
) -> ProviderCall:
    """Record an intent under a locked tenant/attempt/holder claim.

    Internal service API: callers must commit before external I/O. Workers use
    OperationExecutor, which owns and enforces that commit boundary.
    """
    _require_lease(lease)
    async with connection.transaction():
        if not await lock_lease(connection, lease):
            raise ProviderCallRefused(
                "Provider intent requires the live tenant-bound lease"
            )
        return await _record_intent_locked(
            connection,
            lease,
            idempotency_key=idempotency_key,
            provider=provider,
            operation_kind=operation_kind,
            target=target,
        )


async def _record_intent_locked(
    connection: Connection,
    lease: ExecutionLease,
    *,
    idempotency_key: str,
    provider: str,
    operation_kind: str,
    target: str,
) -> ProviderCall:
    """Commit the intent to make a provider call. **Call this before the call.**

    Must run in a transaction that commits before the provider is contacted. A caller
    that opens a transaction, records the intent, makes the call and then commits has
    written nothing durable at the moment it matters: if the process dies mid-call the
    transaction rolls back, and the evidence that the call may have happened dies with
    it. That is the failure this function exists to prevent, so it is worth stating
    where it can be read.

    The lease is presented and re-checked here, not trusted. A worker whose lease has
    lapsed must not be able to record a new provider call: recording one is the first
    half of spending, and the second half is a call it is no longer entitled to make.

    Raises `ProviderCallRefused` when the lease is not held, and when the key is already
    recorded by a *different* attempt. The same attempt re-recording the same key gets
    its existing row back -- that is a duplicate queue delivery, and answering it
    idempotently is how the same envelope arriving twice makes one call instead of two.
    """
    _require_lease(lease)
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise ContractViolation("idempotency_key must be a non-empty string")
    for name, value in (
        ("provider", provider),
        ("operation_kind", operation_kind),
        ("target", target),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ContractViolation(f"{name} must be a non-empty string")

    existing = await read_call(connection, idempotency_key=idempotency_key)
    if existing is not None:
        if (
            existing.attempt_id == lease.attempt_id
            and existing.operation_id == lease.operation_id
            and existing.org_id == lease.org_id
            and existing.workspace_id == lease.workspace_id
            and existing.fence_token == lease.fence_token
        ):
            # Same attempt, same operation, same token, same key: a duplicate delivery
            # of work already claimed.  Before returning it idempotently, verify:
            # (a) the immutable binding (provider, operation_kind, target) matches the
            # stored row -- a replay presenting different values is a misuse of the key,
            # not a legitimate duplicate; and (b) the caller still holds a live lease at
            # the recorded fence token -- a released holder must not receive execution
            # permission merely because their attempt_id matches.
            if (
                existing.provider != provider
                or existing.operation_kind != operation_kind
                or existing.target != target
            ):
                raise ProviderCallRefused(
                    f"idempotency key {idempotency_key!r} was recorded for attempt "
                    f"{existing.attempt_id!r} with provider={existing.provider!r}, "
                    f"operation_kind={existing.operation_kind!r}, "
                    f"target={existing.target!r}; the binding is immutable and "
                    "cannot be changed by re-recording with different values"
                )
            # Verify the caller still holds the lease they recorded under. An attempt
            # that released its lease cannot replay the key to regain execution rights.
            still_live = await connection.fetchval(
                """
                SELECT EXISTS (
                    SELECT 1 FROM harness_operation_leases
                     WHERE operation_id = $1 AND holder = $2
                       AND fence_token = $3 AND closed_at IS NULL
                       AND expires_at > clock_timestamp()
                       AND runtime_deadline > clock_timestamp()
                )
                """,
                lease.operation_id,
                lease.holder,
                lease.fence_token,
            )
            if not still_live:
                raise ProviderCallRefused(
                    f"{lease.holder!r} no longer holds a live lease on "
                    f"{lease.operation_id} at fence token {lease.fence_token}; "
                    "a released, expired or runtime-exceeded holder cannot replay an "
                    "intent key to reclaim execution"
                )
            return existing
        raise ProviderCallRefused(
            f"idempotency key {idempotency_key!r} is already recorded for operation "
            f"{existing.operation_id!r}, attempt {existing.attempt_id!r}, "
            f"tenant {existing.org_id!r}/{existing.workspace_id!r}, "
            f"fence token {existing.fence_token}; "
            f"the current caller (operation {lease.operation_id!r}, "
            f"attempt {lease.attempt_id!r}, fence token {lease.fence_token}) "
            "must not reuse it"
        )

    # The lease check and the insert are one statement. Two statements would leave a
    # window in which the lease lapsed between them, and the insert would then record a
    # call under a token its holder no longer has.  `runtime_deadline` is checked here
    # for the same reason it is checked in `fenced_update` and `observe`: a worker
    # whose approved runtime has expired must not record a new provider call.
    row = await connection.fetchrow(
        """
        INSERT INTO harness_provider_call_intent (
            idempotency_key, operation_id, org_id, workspace_id, job_id, attempt_id,
            fence_token, provider, operation_kind, target, stage
        )
        SELECT $1, o.operation_id, o.org_id, o.workspace_id, o.job_id, $3, $4,
               $5, $6, $7, 'intended'
          FROM harness_operations o
          JOIN harness_operation_leases l ON l.operation_id = o.operation_id
         WHERE o.operation_id = $2
           AND l.holder = $8 AND l.fence_token = $4
           AND l.closed_at IS NULL AND l.expires_at > clock_timestamp()
           AND l.runtime_deadline > clock_timestamp()
        RETURNING *
        """,
        idempotency_key,
        lease.operation_id,
        lease.attempt_id,
        lease.fence_token,
        provider,
        operation_kind,
        target,
        lease.holder,
    )
    if row is None:
        raise ProviderCallRefused(
            f"{lease.holder!r} does not hold a live lease on {lease.operation_id} at "
            f"fence token {lease.fence_token}; a worker that has been fenced out must "
            "not record a new provider call"
        )
    return _call_from_row(row)


async def observe(
    connection: Connection,
    lease: ExecutionLease,
    *,
    idempotency_key: str,
    outcome: CallOutcome,
    detail: str | None = None,
    provider_ref: str | None = None,
) -> tuple[ProviderCall, BudgetDisposition]:
    """Record what the provider said, and what is therefore owed to the ledger.

    Called by the worker that made the call, with the reply it got. Returns the updated
    row and the budget disposition, so the caller knows whether the hold may be released
    without re-deriving that rule at each call site.

    A fenced-out worker is refused. This is the **AC-02 case that costs money**: a stale
    worker reporting `SUCCEEDED` for a call its successor is re-making would publish a
    terminal success from a process that is no longer the executor. Locking the lease
    row makes the ownership check and this write atomic with a concurrent release.

    An `UNKNOWN` outcome moves the row to `UNRESOLVED`, not to `OBSERVED`: the worker
    did not observe anything. It is the one outcome that leaves the row
    terminal-and-uncertain rather than terminal-and-known, and it retains budget.
    """
    _require_lease(lease)
    if not isinstance(outcome, CallOutcome):
        raise ContractViolation("outcome must be a CallOutcome")

    stage = (
        CallStage.UNRESOLVED if outcome is CallOutcome.UNKNOWN else CallStage.OBSERVED
    )
    async with connection.transaction():
        if not await lock_lease(connection, lease):
            raise ProviderCallRefused(
                f"{lease.holder!r} may not record an outcome for "
                f"{idempotency_key!r}: this worker no longer holds the lease"
            )
        row = await connection.fetchrow(
            """
            UPDATE harness_provider_call_intent AS i
               SET stage = $3, outcome = $4, provider_ref = $5, updated_at = now()
             WHERE i.idempotency_key = $1
               AND i.stage = 'intended'
               AND i.fence_token = $6
               AND i.operation_id = $7
               AND i.attempt_id = $8
               AND i.org_id = $9
               AND i.workspace_id = $10
               AND EXISTS (
                   SELECT 1 FROM harness_operation_leases l
                    WHERE l.operation_id = i.operation_id
                      AND l.holder = $2 AND l.fence_token = $6
                      AND l.closed_at IS NULL AND l.expires_at > clock_timestamp()
                      AND l.runtime_deadline > clock_timestamp()
               )
            RETURNING *
            """,
            idempotency_key,
            lease.holder,
            stage.value,
            _outcome_detail(outcome, detail),
            provider_ref,
            lease.fence_token,
            lease.operation_id,
            lease.attempt_id,
            lease.org_id,
            lease.workspace_id,
        )
        if row is None:
            raise ProviderCallRefused(
                f"{lease.holder!r} may not record an outcome for "
                f"{idempotency_key!r}: either the call is already settled, or this "
                "worker no longer holds the lease at the fence token the call was "
                "recorded under"
            )
        call = _call_from_row(row)
        return call, disposition_for(call)


async def reconcile(
    connection: Connection,
    *,
    idempotency_key: str,
    outcome: CallOutcome,
    detail: str | None = None,
    provider_ref: str | None = None,
) -> tuple[ProviderCall, BudgetDisposition]:
    """Settle a call by asking the provider afterwards. The recovery path.

    Takes no lease, and that is deliberate rather than an omission. The whole reason
    this function exists is that the worker holding the lease is *gone* -- requiring a
    lease would make the unrecoverable case exactly the case it cannot recover. The
    recorded `fence_token` is left untouched, so the row still says which attempt made
    the call.

    This internal primitive must not be reachable from a request path. Recovery
    verifies its current claim and appends the attributed audit event in the same
    transaction, so automatic settlement is distinct from a worker report.

    Refuses a call that is already settled, including one already `UNRESOLVED`. Asking
    again would be fine; *overwriting* a human's unresolved row on a timer would not,
    and the second is what an unconditional update would do.
    """
    if not isinstance(outcome, CallOutcome):
        raise ContractViolation("outcome must be a CallOutcome")
    stage = (
        CallStage.UNRESOLVED if outcome is CallOutcome.UNKNOWN else CallStage.RECONCILED
    )
    row = await connection.fetchrow(
        """
        UPDATE harness_provider_call_intent
           SET stage = $2, outcome = $3, provider_ref = $4, updated_at = now()
         WHERE idempotency_key = $1 AND stage = 'intended'
        RETURNING *
        """,
        idempotency_key,
        stage.value,
        _outcome_detail(outcome, detail),
        provider_ref,
    )
    if row is None:
        raise ProviderCallRefused(
            f"provider call {idempotency_key!r} is not awaiting reconciliation: it is "
            "absent, or already settled -- and an already-unresolved row must not be "
            "overwritten by a sweep, because that state records a decision to "
            "involve a human"
        )
    call = _call_from_row(row)
    return call, disposition_for(call)


def disposition_for(call: ProviderCall) -> BudgetDisposition:
    """What is owed to the ledger for a settled call. The safety rule, in one place.

    Written once and read by both `observe` and `reconcile` so the two cannot disagree.
    Two spellings of this rule is two answers to "may this budget be released", and the
    wrong one is not a bookkeeping error -- it is either a duplicated provision or a
    leaked resource whose reservation has been given back.
    """
    if call.stage is CallStage.INTENDED:
        # Nothing is owed yet, and nothing may be released: the call may be in flight.
        return BudgetDisposition.RETAIN
    if call.outcome is CallOutcome.SUCCEEDED:
        return BudgetDisposition.SETTLE
    if call.outcome in (CallOutcome.FAILED, CallOutcome.ABSENT):
        # Both establish that nothing was created -- `FAILED` because the provider
        # rejected the request before allocating, `ABSENT` because it was asked and said
        # there is no such resource. Only established absence permits a release.
        return BudgetDisposition.RELEASE
    return BudgetDisposition.RETAIN


async def read_call(
    connection: Connection, *, idempotency_key: str
) -> ProviderCall | None:
    """One recorded provider call, or None."""
    row = await connection.fetchrow(
        "SELECT * FROM harness_provider_call_intent WHERE idempotency_key = $1",
        idempotency_key,
    )
    return None if row is None else _call_from_row(row)


async def unresolved_calls(
    connection: Connection, *, limit: int = 50
) -> tuple[ProviderCall, ...]:
    """Calls that may have happened and have never been resolved, oldest first.

    The enumerable half of the reconciliation contract, for provider calls -- the same
    obligation `admission.list_interrupted_admissions` meets for ledger holds. Without
    an enumerable set, recovery is not difficult, it is undefined: there is nothing to
    iterate.

    `intended` only. `unresolved` is excluded because it records a decision to involve a
    human, and re-sweeping it would override that decision on a timer.

    Unscoped by tenant, like `list_interrupted_admissions`: an operator reconciling
    provider calls is asking about the harness's own obligations, and the tenant of a
    row is part of the answer rather than an input to the question. **Not to be exposed
    on a request path.**
    """
    bounded = max(1, min(int(limit), 200))
    rows = await connection.fetch(
        """
        SELECT * FROM harness_provider_call_intent
         WHERE stage = 'intended'
         ORDER BY created_at, idempotency_key
         LIMIT $1
        """,
        bounded,
    )
    return tuple(_call_from_row(row) for row in rows)


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def audit(
    connection: Connection,
    *,
    operation_id: str,
    org_id: str,
    workspace_id: str,
    event: str,
    actor: str,
    allowed: bool,
    attempt_id: str | None = None,
    fence_token: int | None = None,
    detail: str | None = None,
) -> None:
    """Append one execution event. Never updates, never deletes.

    **The refusals are the valuable half.** A fenced-out worker's attempt to publish
    success leaves no trace in any other table, because refusing it correctly means
    changing nothing -- so without this the most security-relevant event in the system
    is the one with no record. `allowed` is required rather than defaulted for that
    reason: a caller has to say which kind of event this was.

    `detail` is free text and carries **no credential, connection string or vault
    handle.** This is the function most tempted to "log everything for debugging", and
    it is called by the executor, which is the process that holds the credential. The
    absence is the contract.

    Returns nothing. There is no read-back to check and no id a caller needs; making the
    audit write look like it produces a value invites treating a failure to audit as
    recoverable, and it is not -- it rides in the caller's transaction and fails with
    it.
    """
    for name, value in (
        ("operation_id", operation_id),
        ("org_id", org_id),
        ("workspace_id", workspace_id),
        ("event", event),
        ("actor", actor),
    ):
        if not isinstance(value, str) or not value.strip():
            raise ContractViolation(f"{name} must be a non-empty string")
    if not isinstance(allowed, bool):
        raise ContractViolation("allowed must be an explicit bool")
    await connection.execute(
        """
        INSERT INTO harness_execution_audit (
            operation_id, org_id, workspace_id, attempt_id, fence_token,
            event, actor, allowed, detail
        ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)
        """,
        operation_id,
        org_id,
        workspace_id,
        attempt_id,
        None if fence_token is None else int(fence_token),
        event,
        actor,
        allowed,
        detail,
    )


async def read_audit(
    connection: Connection,
    *,
    operation_id: str,
    org_id: str,
    workspace_id: str,
    limit: int = 100,
) -> tuple[dict[str, object], ...]:
    """One operation's execution history, in order, scoped to its tenant.

    **Tenant-scoped in the WHERE clause**, matching `store.get`: an operation belonging
    to another tenant returns an empty tuple, the same answer as one that does not
    exist. A distinguishable "exists but forbidden" would confirm another tenant's
    operation to a caller who should not learn it -- and here it would also disclose how
    many execution attempts that operation took.

    Returns plain dicts rather than a dataclass. This is display data for a status
    surface, the column set is the schema's, and a dataclass would be a second place to
    edit every time an event field is added without making any caller safer.
    """
    bounded = max(1, min(int(limit), 500))
    rows = await connection.fetch(
        """
        SELECT id, operation_id, org_id, workspace_id, attempt_id, fence_token,
               event, actor, allowed, detail, recorded_at
          FROM harness_execution_audit
         WHERE operation_id = $1 AND org_id = $2 AND workspace_id = $3
         ORDER BY id
         LIMIT $4
        """,
        operation_id,
        org_id,
        workspace_id,
        bounded,
    )
    return tuple(dict(row) for row in rows)


# ---------------------------------------------------------------------------
# Trusted service execution runtime
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationStatus:
    """The current state of an operation, as seen by its executor.

    Returned by `OperationExecutor.status`. Carries what a worker needs to make
    its next decision: whether to proceed, stop, or report back.

    Frozen: the worker must not mutate its view of the operation's state.
    """

    operation_id: str
    state: OperationState
    """The last recorded state of the operation (e.g. RUNNING, SUCCEEDED)."""
    cancel_requested: bool
    """True when a cancellation request is pending."""
    detail: str | None
    cleanup_required: bool = False


class OperationExecutor:
    """Trusted service runtime. Never lend this object to a worker process.

    The factory lends a fresh connection per call; workers never supply connections
    or transaction boundaries. Provider I/O starts only after the intent transaction
    and audit have committed and the connection has returned to the factory. The
    provider hook is injected by the trusted service, not by a worker request.
    """

    def __init__(
        self,
        lease: ExecutionLease,
        *,
        connect: Callable[[], AbstractAsyncContextManager[Connection]],
        provider_call: Callable[
            [ProviderCall], Awaitable[tuple[CallOutcome, str | None, str | None]]
        ]
        | None = None,
    ) -> None:
        if not isinstance(lease, ExecutionLease):
            raise ContractViolation("OperationExecutor requires an ExecutionLease")
        if not callable(connect):
            raise ContractViolation("A trusted connection factory is required")
        self._lease, self._connect, self._provider_call = lease, connect, provider_call

    @property
    def lease(self) -> ExecutionLease:
        return self._lease

    @property
    def operation_id(self) -> str:
        return self._lease.operation_id

    @property
    def org_id(self) -> str:
        return self._lease.org_id

    @property
    def workspace_id(self) -> str:
        return self._lease.workspace_id

    @asynccontextmanager
    async def _connection(self):
        async with self._connect() as connection:
            if connection.is_in_transaction():
                raise ContractViolation(
                    "Executor requires a connection without an open transaction"
                )
            yield connection

    async def _audit(self, connection, event, allowed, detail=None):
        await audit(
            connection,
            operation_id=self.operation_id,
            org_id=self.org_id,
            workspace_id=self.workspace_id,
            event=event,
            actor=self._lease.holder,
            allowed=allowed,
            attempt_id=self._lease.attempt_id,
            fence_token=self._lease.fence_token,
            detail=detail,
        )

    async def status(self) -> OperationStatus | None:
        async with self._connection() as connection, connection.transaction():
            if not await lock_lease(connection, self._lease):
                return None
            row = await connection.fetchrow(
                "SELECT state, detail, cleanup_required, "
                "cancel_requested_at IS NOT NULL AS cancelled "
                "FROM harness_operations WHERE operation_id=$1",
                self.operation_id,
            )
            return OperationStatus(
                self.operation_id,
                OperationState(row["state"]),
                row["cancelled"],
                row["detail"],
                row["cleanup_required"],
            )

    async def cancel_requested(self) -> bool:
        status = await self.status()
        if status is None:
            raise ProviderCallRefused(
                "Executor lease is no longer live; stop execution"
            )
        return status.cancel_requested

    async def cancel(self, *, reason: str | None = None) -> bool:
        """Request cancellation of this live owned operation at its next safe point."""
        from .identity import REQUIRED_PERMISSION, ResolvedPrincipal
        from .recovery import request_cancellation

        async with self._connection() as connection, connection.transaction():
            live = await lock_lease(connection, self._lease)
            written = False
            if live:
                written = await request_cancellation(
                    connection,
                    operation_id=self.operation_id,
                    principal=ResolvedPrincipal(
                        self.org_id,
                        self.workspace_id,
                        self._lease.holder,
                        frozenset({REQUIRED_PERMISSION}),
                    ),
                    reason=reason,
                )
            await self._audit(connection, "cancel", live)
        if not live:
            raise ProviderCallRefused("Executor lease is no longer live")
        return written

    async def _record(
        self, *, idempotency_key, provider, operation_kind, target, fresh=False
    ):
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    if not await lock_lease(connection, self._lease):
                        raise ProviderCallRefused("Executor lease is no longer live")
                    from .identity import TERMINAL_STATES

                    row = await connection.fetchrow(
                        "SELECT state, cancel_requested_at FROM harness_operations "
                        "WHERE operation_id=$1",
                        self.operation_id,
                    )
                    if (
                        row["cancel_requested_at"] is not None
                        or OperationState(row["state"]) in TERMINAL_STATES
                    ):
                        raise ProviderCallRefused(
                            "Cancelled or terminal operations cannot call providers"
                        )
                    existing = await read_call(
                        connection, idempotency_key=idempotency_key
                    )
                    if fresh and existing is not None:
                        raise ProviderCallRefused(
                            "Provider intent already exists; "
                            "reconcile instead of repeating the call"
                        )
                    call = await record_intent(
                        connection,
                        self._lease,
                        idempotency_key=idempotency_key,
                        provider=provider,
                        operation_kind=operation_kind,
                        target=target,
                    )
                    await self._audit(connection, "record_intent", True)
            except ProviderCallRefused:
                await self._audit(connection, "record_intent.refused", False)
                raise
        return call

    async def record_intent(
        self, *, idempotency_key: str, provider: str, operation_kind: str, target: str
    ) -> ProviderCall:
        """Return only after durable commit; an ambient transaction is refused."""
        return await self._record(
            idempotency_key=idempotency_key,
            provider=provider,
            operation_kind=operation_kind,
            target=target,
        )

    async def execute_provider(
        self, *, idempotency_key: str, provider: str, operation_kind: str, target: str
    ) -> tuple[ProviderCall, BudgetDisposition]:
        """Serialize trusted provider I/O against recovery before committing intent.

        The dedicated connection owns a session lock, so short intent/observation
        transactions still commit independently. The lock is released only after the
        hook has returned, including cancellation, rather than when its lease expires.
        Providers must also honor the stable operation-step idempotency key across
        transport/process failures; uncertain intents are never automatically reissued.
        """
        async with self._connection() as dispatch:
            key = f"harness-provider-dispatch:{self.operation_id}"
            held = await dispatch.fetchval(
                "SELECT pg_try_advisory_lock(hashtextextended($1, 0))", key
            )
            if not held:
                raise ProviderCallRefused("Provider dispatch already in flight")

            @asynccontextmanager
            async def bound_connection():
                yield dispatch

            runtime = OperationExecutor(
                self._lease, connect=bound_connection, provider_call=self._provider_call
            )
            try:
                return await runtime._execute_provider(
                    idempotency_key=idempotency_key,
                    provider=provider,
                    operation_kind=operation_kind,
                    target=target,
                )
            finally:
                await dispatch.execute(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", key
                )

    async def _execute_provider(
        self, *, idempotency_key: str, provider: str, operation_kind: str, target: str
    ) -> tuple[ProviderCall, BudgetDisposition]:
        """Commit intent, invoke the trusted hook once, then record its observation.

        Hook failures are uncertain outcomes. A repeated intent never reissues the
        side effect automatically; crash recovery observes the original key instead.
        """
        if self._provider_call is None:
            raise ContractViolation(
                "Trusted composition did not provide a provider hook"
            )
        call = await self._record(
            idempotency_key=idempotency_key,
            provider=provider,
            operation_kind=operation_kind,
            target=target,
            fresh=True,
        )
        cancelled = await self._cancel_before_dispatch(call)
        if cancelled is not None:
            raise CancellationPending(*cancelled)
        try:
            outcome, detail, provider_ref = await self._provider_call(call)
            if not isinstance(outcome, CallOutcome):
                raise ContractViolation("Provider hook must return CallOutcome")
        except Exception:  # noqa: BLE001
            outcome = CallOutcome.UNKNOWN
        if outcome is CallOutcome.UNKNOWN:
            # Intent remains recoverable: a transport failure is not a terminal
            # decision to abandon reconciliation. Never serialize exception details.
            async with self._connection() as connection, connection.transaction():
                live = await lock_lease(connection, self._lease)
                if live:
                    await connection.execute(
                        "UPDATE harness_operations SET cleanup_required=true "
                        "WHERE operation_id=$1 AND cancel_requested_at IS NOT NULL",
                        self.operation_id,
                    )
                await self._audit(connection, "provider.uncertain", live)
            if not live:
                raise ProviderCallRefused("Executor lease is no longer live")
            if await self.cancel_requested():
                raise CancellationPending(call, BudgetDisposition.RETAIN)
            return call, BudgetDisposition.RETAIN
        result = await self.observe(
            idempotency_key=idempotency_key,
            outcome=outcome,
            detail=detail,
            provider_ref=provider_ref,
        )
        if await self.cancel_requested():
            raise CancellationPending(*result)
        return result

    async def observe(
        self,
        *,
        idempotency_key: str,
        outcome: CallOutcome,
        detail: str | None = None,
        provider_ref: str | None = None,
    ) -> tuple[ProviderCall, BudgetDisposition]:
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    if not await lock_lease(connection, self._lease):
                        raise ProviderCallRefused("Executor lease is no longer live")
                    result = await observe(
                        connection,
                        self._lease,
                        idempotency_key=idempotency_key,
                        outcome=outcome,
                        detail=detail,
                        provider_ref=provider_ref,
                    )
                    if result[1] is not BudgetDisposition.RELEASE:
                        await connection.execute(
                            "UPDATE harness_operations SET cleanup_required=true "
                            "WHERE operation_id=$1 AND cancel_requested_at IS NOT NULL",
                            self.operation_id,
                        )
                    await self._audit(connection, "observe", True, result[1].value)
            except ProviderCallRefused:
                await self._audit(connection, "observe.refused", False)
                raise
        return result

    async def _cancel_before_dispatch(self, call):
        """Persist known absence if cancellation wins before invoking the hook."""
        async with self._connection() as connection, connection.transaction():
            if not await lock_lease(connection, self._lease):
                await self._audit(connection, "provider.dispatch_refused", False)
                raise ProviderCallRefused("Executor lease is no longer live")
            cancelled = await connection.fetchval(
                "SELECT cancel_requested_at IS NOT NULL FROM harness_operations "
                "WHERE operation_id=$1",
                self.operation_id,
            )
            if not cancelled:
                return None
            result = await observe(
                connection,
                self._lease,
                idempotency_key=call.idempotency_key,
                outcome=CallOutcome.ABSENT,
                detail="cancelled before provider invocation",
            )
            await self._audit(
                connection,
                "provider.cancelled_before_dispatch",
                True,
                BudgetDisposition.RELEASE.value,
            )
            await self._settle(connection, OperationState.CANCELLED, None)
            return result

    async def settle(self, *, state: OperationState, detail: str | None = None) -> bool:
        """Attest whole-workflow completion, with all effects resolved atomically.

        Only the trusted workflow driver knows whether every required step ran.
        Recovery cannot infer that fact from the subset of calls recorded at a crash.
        UNKNOWN is the only terminal result permitted with unresolved effects.
        """
        from .identity import TERMINAL_STATES

        if state not in TERMINAL_STATES:
            raise ContractViolation("settle requires a terminal state")
        async with self._connection() as connection, connection.transaction():
            return await self._settle(connection, state, detail)

    async def _settle(self, connection, state, detail):
        from .identity import TERMINAL_STATES

        if not await lock_lease(connection, self._lease):
            await self._audit(connection, "settle", False)
            return False
        operation = await connection.fetchrow(
            "SELECT cancel_requested_at IS NOT NULL AS requested, cleanup_required "
            "FROM harness_operations WHERE operation_id=$1",
            self.operation_id,
        )
        if operation["requested"] and state is OperationState.SUCCEEDED:
            await self._audit(connection, "settle.cancel_refused", False)
            return False
        rows = await connection.fetch(
            "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1",
            self.operation_id,
        )
        calls = [_call_from_row(row) for row in rows]
        dispositions = [disposition_for(call) for call in calls]
        uncertain = any(call.may_have_happened for call in calls) or (
            BudgetDisposition.RETAIN in dispositions
        )
        existing = any(d is not BudgetDisposition.RELEASE for d in dispositions)
        if state is not OperationState.UNKNOWN and (
            uncertain
            or operation["cleanup_required"]
            or (state is OperationState.CANCELLED and existing)
        ):
            if state is OperationState.CANCELLED and existing:
                await connection.execute(
                    "UPDATE harness_operations SET cleanup_required=true "
                    "WHERE operation_id=$1",
                    self.operation_id,
                )
            await self._audit(connection, "settle.cleanup_required", False)
            return False
        tag = await connection.execute(
            "UPDATE harness_operations SET state=$2, detail=$3, "
            "version=version+1, updated_at=now() "
            "WHERE operation_id=$1 AND state <> ALL($4::text[])",
            self.operation_id,
            state.value,
            detail,
            [s.value for s in TERMINAL_STATES],
        )
        written = tag == "UPDATE 1"
        if written and not await close(
            connection,
            operation_id=self.operation_id,
            reason="executor: " + state.value,
            fence_token=self._lease.fence_token,
            holder=self._lease.holder,
        ):
            raise ProviderCallRefused("Lease expired before settlement committed")
        await self._audit(connection, "settle", written, state.value)
        return written

    async def renew(self, *, duration: timedelta | None = None) -> OperationExecutor:
        async with self._connection() as connection:
            try:
                async with connection.transaction():
                    if not await lock_lease(connection, self._lease):
                        raise ProviderCallRefused("Executor lease is no longer live")
                    new_lease = await renew(connection, self._lease, duration=duration)
                    await self._audit(connection, "renew", True)
            except (ProviderCallRefused, LeaseRefused):
                await self._audit(connection, "renew", False)
                raise
        return OperationExecutor(
            new_lease, connect=self._connect, provider_call=self._provider_call
        )

    async def release(self) -> bool:
        async with self._connection() as connection, connection.transaction():
            live = await lock_lease(connection, self._lease)
            released = await release(connection, self._lease) if live else False
            await self._audit(connection, "release", released)
            return released


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _require_lease(lease: object) -> None:
    """Refuse anything that is not a real `ExecutionLease`.

    An isinstance check rather than duck typing, because every safety property in this
    module rests on `holder` and `fence_token` being values the database issued. An
    object that merely has those attributes is a caller-constructed claim, and accepting
    one would make the fence a field the caller fills in.
    """
    if not isinstance(lease, ExecutionLease):
        raise ContractViolation("lease must be an ExecutionLease granted by `acquire`")


def _outcome_detail(outcome: CallOutcome, detail: str | None) -> str:
    """The stored `outcome` string: the enum value, plus the provider's words if given.

    One column for both because the enum is what code branches on and the free text is
    what a human reads, and splitting them would let the two disagree about the same
    call. The enum is written first so a prefix match is enough to branch on it.
    """
    if detail is None or not detail.strip():
        return outcome.value
    return f"{outcome.value}: {detail.strip()}"


def _call_from_row(row: object) -> ProviderCall:
    data = dict(row)  # type: ignore[call-overload]
    stored = data["outcome"]
    return ProviderCall(
        idempotency_key=data["idempotency_key"],
        operation_id=data["operation_id"],
        org_id=data["org_id"],
        workspace_id=data["workspace_id"],
        job_id=data["job_id"],
        attempt_id=data["attempt_id"],
        fence_token=data["fence_token"],
        provider=data["provider"],
        operation_kind=data["operation_kind"],
        target=data["target"],
        stage=CallStage(data["stage"]),
        outcome=None if stored is None else CallOutcome(stored.split(":", 1)[0]),
        provider_ref=data["provider_ref"],
        created_at=data["created_at"],
        updated_at=data["updated_at"],
    )
