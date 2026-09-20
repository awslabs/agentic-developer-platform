"""The gate in front of dispatch: approved, budget-bound admission.

Issue #5526 (w6-03), EPIC #4910, Wave 6. Implements the ordering #5524 (w6-01) specifies
at `INTEGRATION-CONTRACT.md` §3.1 and the compensation table at §3.5, on top of the
store and outbox #5525 (w6-02) provides.

## What this module adds that the store does not have

`OperationStore.admit()` guarantees an admitted operation is durable, single-effect and
tenant-scoped. It does not ask whether the operation was ever *allowed* -- its own
docstring says so: "No approval check. #5526 (w6-03)." A caller holding a well-formed
`OperationRequest` and a `ResolvedPrincipal` gets admission on the strength of being
well-formed.

`admit_operation()` is the only entry point that answers the second question. It checks
a current approval, reserves and confirms budget against the domain ledger, and only
then calls the store -- so the store's transaction stays the single durability point and
this module adds no second one.

## The sequence, and why nothing here holds the transaction open

    (1) approval     evaluate_approval -- current authority, binding, envelope
         |
    (1b) intent      harness_admission_intent, COMMITTED before any external effect
         |           -> from here, a hold cannot exist that this database cannot name
    (2) reserve      domain ledger hook, idempotent on (job_id, attempt_id)
         |           -> budget held. NOT a spend. Compensatable.
         |           -> intent advances to 'reserved' after the reply
    (3) confirm      domain ledger hook, same key
         |           -> the approved envelope is bound to this attempt
         |           -> intent advances to 'confirmed' after the reply
    +----------- ONE transaction, in the store (#5525) ------------------+
    | (4a) operation record + (4b) outbox row   -- store.admit()         |
    | (4c) consumption row -- the approval is spent, enforced by its PK  |
    +--------------------------------------------------------------------+
         |           commit is the durability point; intent -> 'resolved'.
    (5) deliver      outbox -> executor. At-least-once, duplicate-safe,
                     and claimable ONLY with a spendable consumption row.

Step (1b) is the answer to "what happens if this process dies mid-sequence". Because
(2) and (3) are outside the transaction, a crash between a ledger call landing and its
reply arriving used to leave a hold that no row in this database described -- so there
was no set of obligations to enumerate and recovery was undefined rather than merely
hard. The intent row is committed first and advanced only after each reply, so it can
lag reality but never lead it, and `reconcile_interrupted_admissions` can settle what
it finds. See `schema.py` version 3.

Steps (1) to (3) cross a process boundary and are *deliberately outside* the
transaction. #5524 §3.1 fixes exactly one transaction spanning only the store writes; a
transaction held open across a call to the ledger would make the store's write
throughput a function of the ledger's latency, and a ledger that hung would hold locks
on the admission tables until it timed out.

What makes that safe is not the transaction. It is idempotency plus the fact that a
reservation is **not a spend**: (2) and (3) can each be retried under the same key, and
holding a reservation too long costs headroom while releasing one too early costs
correctness.

## Why (4c) is last, and why the approval's primary key is the lock

The consumption row is written *after* `store.admit()` because it references the
operation by id, and on a retry the operation id that exists is the one the original
admission committed -- not the one this call minted. `store._resolve_conflict` returns
that stored row, so writing the consumption row afterwards points it at a row that is
really there. Writing it first would point a foreign key at an id the idempotency
constraint is about to discard, turning an honest retry into a constraint error.

The consumption primary key enforces single use. A per-approval PostgreSQL session
advisory lock separately serializes admission and recovery across the ledger calls.
A recovering process skips a live writer, then rereads the intent after acquiring
ownership. Connection loss releases the lock and prevents that writer from committing.
The caller must provide an exclusively held connection outside a transaction, so the
intent is committed before any external effect.

## Why a retry cannot buy a second reservation

The job and attempt identity the ledger is keyed on is **derived from the approval id**,
not minted randomly (`derive_operation_identity`). If it were random, every retry would
reserve under a fresh key, the ledger's idempotency would never engage, and an automatic
retry after a dropped response would be a second hold against the same envelope. Since
an approval is single-use it names exactly one operation, one job and one attempt, so
deriving the three from it is both sound and the thing that makes "no budget renewal
through retries" structural rather than checked.

## Why the ledger is an interface and not an implementation

`BudgetLedger` is a Protocol and this package implements none of it. The domain owns the
ledger: `accounting.py:31-34` states "C owns the reservation ledger. A does not write
it, and this module contains no balance, no reservation arithmetic and no spend total,
because a second place computing cost is a second answer that can disagree with the real
one."

So this module holds the *ordering* and the *compensation*, which are shared-harness
concerns, and calls out for every quantity. #5524 §3.6 warns that a Wave 6 implementer
will find `enforce_workspace_creation_quota` and `CostReconciler._suspend_workspace` in
the domain app and must not wire these hooks to them, because that would put admission
authority in the domain app. Nothing here calls them.

`modules/gateway/src/budget/reservations.py:341` does have a real idempotent
`reserve()`. #5524 §3.6 records that no superplane code references it and that "citing
it means proposing a new binding, which is #5526's call to make explicitly". **This
module does not bind to it.** Its key is `request_id` rather than `(job_id,
attempt_id)`, and its ledger is a per-request token budget rather than a workspace
provisioning envelope -- a different quantity in a different store. `BudgetLedger` is
the explicit proposal instead; an adapter over the gateway's reservations would be a
separate, named decision.

## The failures, and the one safe answer to each

Every branch follows #5524 §3.5:

| Failure | Answer |
|---|---|
| (2) reserve refused | Nothing was reserved. Refuse; there is nothing to compensate |
| (2) ok, (3) confirm lost | **Retain.** Retry confirm under the same key |
| (3) ok, (4) commit lost | Establish nothing was dispatched, *then* release |
| Cancellation before dispatch | **Fence first, release second.** Never the reverse |
| Dispatch uncertain | **Retain** until provider reconciliation. Never release |

"Before dispatch" is a claim about the outbox row that has to be *established*, not one
that follows from the row being there: see `DispatchEvidence`. A row no claim has ever
taken is definitely undispatched; any other row may have reached an executor.

The rule underneath all of them: fence before releasing, and establish provider truth
before either. The one that looks wrong and is not: an uncertain dispatch keeps the
reservation. Releasing it would let the same envelope fund a second operation while the
first may be running, and `CostExposure.NONE` is unreachable without
provider-established absence for every expected resource (`accounting.py:199-276`).
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Protocol

from .approval import (
    ApprovalDecision,
    ApprovalRecord,
    ApprovalRefused,
    ApproverStatus,
    SpendEnvelope,
    evaluate_approval,
)
from .identity import (
    ContractViolation,
    OperationBinding,
    OperationRequest,
    ResolvedPrincipal,
)
from .store import (
    _COLUMNS,
    AdmittedOperation,
    Connection,
    OperationStore,
    _is_unique_violation,
    _record,
)

__all__ = [
    "AdmissionIntent",
    "AdmissionOutcome",
    "BudgetDenied",
    "BudgetLedger",
    "BudgetUnavailable",
    "ConsumedApproval",
    "CreationFence",
    "DispatchEvidence",
    "IntentStage",
    "ReconciliationReport",
    "Reservation",
    "ReservationState",
    "admit_operation",
    "cancel_before_dispatch",
    "derive_operation_identity",
    "list_interrupted_admissions",
    "read_consumption",
    "read_consumption_privileged",
    "reconcile_interrupted_admissions",
    "retain_for_uncertain_dispatch",
]

# Namespace for the UUID5 derivation below. A fixed, arbitrary constant: its value
# carries no meaning, but it must never change, because changing it would re-key every
# outstanding approval's reservation and a retry across the change would reserve a
# second time.
_IDENTITY_NAMESPACE = uuid.UUID("6b3f1d6a-9c2e-5f47-8d14-2a7c0e5b9f31")


def derive_operation_identity(approval_id: str) -> tuple[str, str, str]:
    """The (operation_id, job_id, attempt_id) one approval may ever be spent on.

    Deterministic, because the ledger is idempotent on `(job_id, attempt_id)` and that
    idempotency protects nothing unless a retry presents the *same* key. Randomly minted
    ids would make every retry a distinct request to the ledger, so a second hold would
    be granted correctly and the envelope would have been renewed by a retry -- the
    exact failure this story's third design requirement names.

    Three distinct values rather than one reused three times: they are different
    identities with different lifetimes (`identity.py:541-547`), and collapsing them
    would mean a later story that attempts an operation twice either invents a second
    identifier or migrates a table already keyed on this one.
    """
    if not isinstance(approval_id, str) or not approval_id:
        raise ContractViolation("approval_id is required to derive operation identity")
    return (
        str(uuid.uuid5(_IDENTITY_NAMESPACE, f"operation:{approval_id}")),
        str(uuid.uuid5(_IDENTITY_NAMESPACE, f"job:{approval_id}")),
        str(uuid.uuid5(_IDENTITY_NAMESPACE, f"attempt:{approval_id}")),
    )


class ReservationState(str, Enum):
    """How far the reserve/confirm sequence got, durably.

    Persisted so compensation is *decidable* rather than inferred. A recovering process
    that knew only "an approval was consumed" would have to guess whether the ledger
    still holds a reservation, and the two wrong guesses are releasing money for work
    that may be running and holding money for work that never will be.

    `(str, Enum)` to match `OperationState`; the values go in a column with a CHECK
    constraint naming them (`schema.py`, version 2).
    """

    RESERVED = "reserved"
    """Reserve landed, confirm has not. Retry confirm under the same key."""

    CONFIRMED = "confirmed"
    """Confirm landed. The approved envelope is bound to this attempt."""

    RELEASED = "released"
    """The reservation was returned. Only reachable with established absence."""

    RETAINED = "retained"
    """Held pending provider reconciliation. NOT a failure; see the module docstring."""


class DispatchEvidence(str, Enum):
    """What a locked read of the outbox row establishes about delivery (CXR-004).

    Three values rather than the boolean `_outbox_row_exists` used to return, because a
    boolean forced two different facts into one answer and the compensation rules in
    #5524 §3.5 give them opposite answers.

    The reproduction: after an ordinary admission, cancel with an intact outbox row --
    `attempts = 0`, `claimed_until IS NULL`, `delivered_at IS NULL`, `abandoned_at IS
    NULL`. The fence was established successfully, and then the cancellation reported
    `retained` and released nothing. That row is work that provably never left the
    queue: no worker has ever claimed it, so no executor has ever seen it. Retaining its
    budget is a leak justified by the *existence* of a row rather than by any evidence
    about dispatch, and the only way a test could reach the release branch was to delete
    the production row by hand -- which is the tell that the branch was unreachable in
    production.

    Existence is not evidence. `attempts` and `claim_generation` are the evidence, and
    they are already on the row.
    """

    NEVER_QUEUED = "never_queued"
    """No outbox row at all: either it was never written, or it has been consumed.

    Same answer as `DEFINITELY_PENDING` for the release decision, and a separate value
    because the two are different facts for an operator: one is an admission that did
    not commit its outbox row, the other is a queued dispatch that was cancelled in
    time.
    """

    DEFINITELY_PENDING = "definitely_pending"
    """A row no claim has ever taken. `attempts = 0` and no live or expired lease.

    `attempts` is incremented *by the claim statement itself*, in the same UPDATE that
    takes the lease (`outbox.claim`), so `attempts = 0` is not an inference about
    timing: it is the row stating that no worker has ever been handed this envelope.
    Combined with the row lock this function holds, no worker can take it concurrently
    either.
    """

    CLAIMED_OR_DELIVERED = "claimed_or_delivered"
    """A worker has held this row, or delivery completed. Something may have happened.

    `attempts > 0`, or a lease exists, or `delivered_at`/`abandoned_at` is stamped.
    Every one of those means an executor may already have acted, and a fence bounds the
    future without saying anything about the past.
    """


class IntentStage(str, Enum):
    """How far an admission is *known* to have got, written before each external effect.

    Distinct from `ReservationState`, and the distinction is the point.
    `ReservationState` records what this process last told the ledger, and it only
    exists once the consumption row does -- which is after the transaction commits.
    `IntentStage` records what this process is about to attempt, or has attempted
    without hearing back, and it exists before the ledger is contacted at all.

    The column can lag reality but must never run ahead of it: each value is written
    only after the corresponding reply has been received, so "the database says
    reserved" implies a hold exists, while "the database says intended" implies nothing
    either way. That asymmetry is what makes the sweep safe -- it re-asks the ledger
    under the derived key, which is idempotent, so a false "maybe" costs one redundant
    call and a false "no" would cost a leaked hold.

    Values go in a column with a CHECK constraint naming them (`schema.py`, version 3).
    """

    INTENDED = "intended"
    """A reserve is about to be issued, or was issued and its outcome is unknown.

    One state for both, deliberately: after a crash they are indistinguishable from
    inside this process, and a state that claimed to tell them apart would be a guess
    recorded as a fact. The sweep resolves the ambiguity by asking the ledger.
    """

    RESERVED = "reserved"
    """A reserve reply was received, so a hold definitely exists."""

    CONFIRMED = "confirmed"
    """A confirm reply was received: the approved envelope is bound to this attempt."""

    RESOLVED = "resolved"
    """The sequence reached a terminal answer and nothing further is owed."""


# The reservation states under which an admitted operation may still be handed to an
# executor. Exactly one of the four, and each exclusion is a different hazard:
#
# * `reserved` -- the confirm has not landed, so the approved envelope is not yet bound
#   to this attempt. Delivering here would run work whose budget is held but not
#   committed, and a later `BudgetDenied` at confirm would arrive after the provision.
# * `released` -- the reservation was returned. Whatever budget covered this operation
#   is gone, and `cancel_before_dispatch` reaches this state only after fencing
#   creation. Delivering a released row is delivering work nothing pays for, past a
#   fence.
# * `retained` -- either a cancellation that could not be fully established or a
#   dispatch whose outcome is unknown. Both mean "do not start more work on this
#   attempt until a human or a provider reconciliation says so".
#
# Consumed by `outbox.DispatchOutbox.claim`, which is why this lives here rather than
# there: the states are this module's vocabulary, and a second copy in the outbox would
# be a second answer to "is this operation still payable" that could disagree.
DELIVERABLE_RESERVATION_STATES: frozenset[str] = frozenset(
    {ReservationState.CONFIRMED.value}
)


@dataclass(frozen=True)
class Reservation:
    """A ledger's acknowledgement that it is holding budget for one attempt.

    Frozen, and carries no amount. The amount is the ledger's; this is a handle plus the
    key it was made under, which is everything the compensation paths need and nothing
    they do not. A reservation carrying a balance would be a second copy of a number the
    ledger owns (`accounting.py:31-34`).
    """

    reservation_id: str
    job_id: str
    attempt_id: str

    def __post_init__(self) -> None:
        for name in ("reservation_id", "job_id", "attempt_id"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ContractViolation(f"{name} is required on a reservation")


class BudgetUnavailable(RuntimeError):
    """The ledger could not be reached, or gave an answer that establishes nothing.

    A `RuntimeError`, not a `PermissionError`: the caller was not denied, the question
    was not answered. #5524 §2 requires this distinction on every port -- an unanswered
    question is not permission, and it is also not a refusal, because a refusal is
    information and this is the absence of information.

    The consequence is in `_confirm`: an unavailable ledger during confirm **retains**
    the reservation, where a denial releases it.
    """


class BudgetDenied(PermissionError):
    """The ledger refused: the envelope is not available to spend.

    Distinct from `BudgetUnavailable` because a denial is a durable answer and a retry
    will get the same one, whereas an unavailable ledger may answer later.
    """


class BudgetLedger(Protocol):
    """The domain's reservation ledger, as this module needs to use it.

    A `Protocol` rather than a base class so the domain implements it without importing
    this package, and so a test can supply a double without inheriting anything. The
    asymmetry between the four methods is the design:

    * `reserve` and `confirm` are **idempotent on (job_id, attempt_id)**, per #4912 and
      `INTEGRATION-CONTRACT.md:296,368`. A retry returns the same answer and produces no
      second effect. A repeat under the same key with a *changed envelope* must raise
      `BudgetDenied` rather than be honoured -- #5524 §3.2's second property, and
      honouring it is how a retry becomes a budget increase.
    * `release` is the compensation, and this module calls it only after establishing
    that
      nothing was dispatched, or that the provider holds nothing.
    * `retain` records that a reservation is deliberately held pending reconciliation.
    It
      exists so "held because we do not know" is a state the ledger was *told* about,
      rather than one an operator has to infer from a reservation nobody released.
    """

    async def reserve(
        self,
        *,
        job_id: str,
        attempt_id: str,
        org_id: str,
        workspace_id: str,
        envelope: SpendEnvelope,
    ) -> Reservation:
        """Hold budget for one attempt. Idempotent on (job_id, attempt_id)."""
        ...

    async def confirm(
        self, *, reservation: Reservation, envelope: SpendEnvelope
    ) -> None:
        """Bind the approved envelope to this attempt. Idempotent on the same key."""
        ...

    async def release(self, *, reservation: Reservation, reason: str) -> None:
        """Return a reservation as unused. Only with established absence."""
        ...

    async def retain(self, *, reservation: Reservation, reason: str) -> None:
        """Record that the reservation is held pending provider reconciliation."""
        ...


class CreationFence(Protocol):
    """Makes it impossible for a straggling worker to create the resource.

    Separate from the ledger because it is a different concern with a different owner,
    and because the *order* between the two is the safety property: #5524 §3.5 requires
    "fence creation, then release the reservation. Never the reverse: releasing first
    leaves a window where a stale worker can still create the resource the budget no
    longer covers."

    Attempt fencing and leases proper are #5527's (w6-04). This Protocol is the seam
    this story needs in order to express the ordering; it is not an implementation of
    fencing, and `INTEGRATION-CONTRACT.md:174` records that no lease or fence
    implementation exists in ADP today.
    """

    async def fence(self, *, operation_id: str, job_id: str, attempt_id: str) -> bool:
        """Block creation for this attempt. True when the fence is established.

        `False` means it could not be established, which is **not** permission to
        release
        -- see `cancel_before_dispatch`.
        """
        ...


@dataclass(frozen=True)
class ConsumedApproval:
    """The durable record that one approval was spent on one operation.

    Readable via `read_consumption` so a recovering process can find out where the
    sequence got to. Without it, recovery would have to infer the ledger's state from
    the operation's state, and "confirmed but not yet dispatched" and "never reserved"
    look the same from `harness_operations` alone.
    """

    approval_id: str
    operation_id: str
    plan_digest: str
    reservation_id: str | None
    reservation_state: ReservationState


@dataclass(frozen=True)
class AdmissionIntent:
    """An admission this database knows was started, whatever happened next.

    The enumerable unit the reconciliation contract is written in terms of: a hold at
    the ledger can only exist if one of these describes it, so sweeping these rows is a
    complete account of what might be outstanding. `reconcile_interrupted_admissions`
    consumes them.

    Carries the envelope by value, because a recovering process may need to retry a
    confirm for an approval that has since expired or been revoked -- which is exactly
    when the approval store cannot supply it, and exactly when recovery matters.
    """

    approval_id: str
    operation_id: str
    job_id: str
    attempt_id: str
    org_id: str
    workspace_id: str
    envelope: SpendEnvelope
    stage: IntentStage
    reservation_id: str | None
    resolution: str | None

    def reservation(self) -> Reservation | None:
        """The hold this intent names, or None if the ledger never named one.

        A method rather than a field because the reservation is a view over three
        columns that are only jointly meaningful once `reservation_id` is set. Returning
        `None` for an `intended` row is the honest answer: there may be a hold, but
        nothing here can name it, which is why the sweep re-reserves under the derived
        key instead.
        """
        if self.reservation_id is None:
            return None
        return Reservation(
            reservation_id=self.reservation_id,
            job_id=self.job_id,
            attempt_id=self.attempt_id,
        )


@dataclass(frozen=True)
class ReconciliationReport:
    """What one reconciliation sweep settled, and what it deliberately did not.

    Separate counters rather than a single "fixed" total, because the three outcomes
    have different operational meanings: a released hold returned budget, a retained one
    is still holding it on purpose, and an `unresolved` row is still owned by a live
    writer/sweep or could not be settled with the ledger. It remains for another pass.

    `admitted` is absent from this type on purpose. Reconciliation never admits work: it
    settles money for admissions that did not complete. A sweep that could admit would
    be a second admission path, reachable without an approval check, which is the
    CXR-001 bypass arriving through the recovery door.
    """

    scanned: int = 0
    released: int = 0
    retained: int = 0
    unresolved: int = 0


@dataclass(frozen=True)
class AdmissionOutcome:
    """What `admit_operation` achieved.

    `created` is the store's distinction carried through: `False` means an identical
    request had already been admitted under this approval and the retry landed rather
    than writing twice. A caller that could not tell the two apart could not report
    accurately either, and for an operation that provisions cloud capacity the
    difference is whether *this* call committed money.
    """

    operation: AdmittedOperation
    reservation: Reservation
    approval_id: str

    @property
    def created(self) -> bool:
        return self.operation.created


async def admit_operation(
    connection: Connection,
    store: OperationStore,
    ledger: BudgetLedger,
    *,
    principal: ResolvedPrincipal,
    request: OperationRequest,
    approval: ApprovalRecord | None,
    requested_envelope: SpendEnvelope,
    approver_statuses: dict[str, ApproverStatus],
    now: datetime,
) -> AdmissionOutcome:
    """Admit one operation, but only if it is currently approved and within budget.

    **This is the only admission path a request handler should be able to reach.**
    `OperationStore.admit()` is the durable write underneath it and is deliberately not
    a substitute: it answers "is this well-formed, unique and tenant-scoped", never "is
    this allowed". `tests/test_admission_bypass.py` asserts that difference is real
    rather than merely documented.

    There are no `operation_id` / `job_id` / `attempt_id` parameters, unlike
    `OperationStore.admit`. They are derived from the approval (see
    `derive_operation_identity`), because a caller-chosen job id would be a
    caller-chosen ledger idempotency key -- and a caller who can choose the key can
    choose a fresh one and reserve twice against one approval.

    Raises `ApprovalRefused` when no current approval authorizes this exact request;
    `BudgetDenied` when the ledger refuses; `BudgetUnavailable` when it could not
    answer; `OperationRefused` when the store refuses the write. Each leaves the ledger
    in the state the compensation paths chose, never in an unknown one.
    """
    if not isinstance(store, OperationStore):
        raise ContractViolation("admit_operation requires an OperationStore")
    if not isinstance(requested_envelope, SpendEnvelope):
        raise ContractViolation("admit_operation requires a SpendEnvelope")

    # (1) Approval. First, because every later step spends something: reserving budget
    # for a request nobody approved is a call to the ledger that should not have
    # happened, even though a reservation is recoverable.
    decision: ApprovalDecision = evaluate_approval(
        approval,
        principal=principal,
        request=request,
        requested_envelope=requested_envelope,
        approver_statuses=approver_statuses,
        now=now,
    )
    if not decision.permitted:
        raise ApprovalRefused(decision.reason)
    # `evaluate_approval` permits only for a non-None record, so this narrows the type
    # rather than adding a check. Spelled out because the alternative is an optional
    # flowing into the binding below.
    if approval is None:  # pragma: no cover - unreachable via evaluate_approval
        raise ApprovalRefused("no approval record for this request")

    async with _admission_ownership(connection, approval.approval_id) as acquired:
        assert acquired
        return await _admit_approved_operation(
            connection,
            store,
            ledger,
            principal=principal,
            request=request,
            approval=approval,
        )


@asynccontextmanager
async def _admission_ownership(
    connection: Connection, approval_id: str, *, wait: bool = True
):
    """Serialize one admission with recovery without an open SQL transaction.

    The connection must stay exclusively owned by the caller for this context.
    PostgreSQL releases the session lock if the writer dies; that same dead
    connection cannot subsequently commit an admission. Recovery skips live writers.
    """
    if connection.is_in_transaction():
        raise ContractViolation(
            "admission and recovery require an idle database connection"
        )
    lock_name = "harness-admission:" + approval_id
    function = "pg_advisory_lock" if wait else "pg_try_advisory_lock"
    result = await connection.fetchval(
        f"SELECT {function}(hashtextextended($1, 0))", lock_name
    )
    acquired = wait or result is True
    try:
        yield acquired
    finally:
        if acquired:
            # A lost connection already released its locks. Preserve the original
            # admission/ledger error if the connection cannot answer the unlock.
            with suppress(Exception):
                await connection.fetchval(
                    "SELECT pg_advisory_unlock(hashtextextended($1, 0))", lock_name
                )


async def _admit_approved_operation(
    connection: Connection,
    store: OperationStore,
    ledger: BudgetLedger,
    *,
    principal: ResolvedPrincipal,
    request: OperationRequest,
    approval: ApprovalRecord,
) -> AdmissionOutcome:
    operation_id, job_id, attempt_id = derive_operation_identity(approval.approval_id)

    # Minted here rather than left to the store, so the identity the ledger is keyed on
    # is the same identity the store will persist. If the store minted it, the
    # reservation would be keyed on ids nothing durable held.
    binding = OperationBinding.issue(
        principal,
        request,
        operation_id=operation_id,
        attempt_id=attempt_id,
        job_id=job_id,
    )

    consumed = await read_consumption(
        connection, principal, approval_id=approval.approval_id
    )
    if consumed is not None:
        approved_limits = await connection.fetchrow(
            "SELECT max_resource_units, max_runtime_seconds, max_cost_micros "
            "FROM harness_approval_consumption WHERE approval_id = $1 "
            "AND org_id = $2 AND workspace_id = $3",
            approval.approval_id,
            principal.org_id,
            principal.workspace_id,
        )
        if approved_limits is None or tuple(
            dict(approved_limits)[key]
            for key in ("max_resource_units", "max_runtime_seconds", "max_cost_micros")
        ) != (
            approval.envelope.max_resource_units,
            approval.envelope.max_runtime_seconds,
            approval.envelope.max_cost_micros,
        ):
            raise BudgetDenied(
                "a replay cannot change the consumed approval's envelope"
            )
        # Revalidate the stored request through the same transactional store path,
        # but do not reserve again after cancellation or accounting settlement.
        if consumed.reservation_id is None:
            raise ApprovalRefused("the consumed approval has no reservation identity")
        reservation = Reservation(
            reservation_id=consumed.reservation_id, job_id=job_id, attempt_id=attempt_id
        )
        try:
            await _commit_admission(
                connection,
                store,
                binding=binding,
                principal=principal,
                request=request,
                approval=approval,
                reservation=reservation,
            )
        except _ApprovalConflict as conflict:
            return await _resolve_consumed_approval(
                connection,
                ledger,
                approval=approval,
                reservation=reservation,
                on_approval_id=conflict.on_approval_id,
            )
        raise ContractViolation("a consumed approval unexpectedly admitted new work")
    settled = await connection.fetchval(
        "SELECT stage = 'resolved' FROM harness_admission_intent "
        "WHERE approval_id = $1",
        approval.approval_id,
    )
    if settled:
        raise ApprovalRefused(
            "this admission was settled without dispatch; a new approval is required"
        )

    # (1b) Durable intent, committed BEFORE the first external effect (#5526 repair,
    # CXR-003). From here on, a hold at the ledger cannot exist without a row in this
    # database describing it: that is the invariant the reconciliation contract rests
    # on, and before this write there was nothing to enumerate after a crash -- a
    # process killed between `reserve` landing and its reply leaving left a hold that no
    # connection could see and no retry could discover.
    #
    # Its own transaction, and committed, rather than joined to the admission
    # transaction at (4). Joining would defeat the entire purpose: the row has to
    # survive the rollback of the thing it is a record of.
    await _record_intent(
        connection, binding=binding, approval=approval, envelope=approval.envelope
    )

    # (2) Reserve. The envelope is the *approved* one, not the requested one: `covers()`
    # has established the request fits inside it, and reserving the approved ceiling is
    # what makes the hold match what the approver agreed to.
    reservation = await _reserve(ledger, binding=binding, envelope=approval.envelope)
    await _advance_intent(
        connection,
        approval_id=approval.approval_id,
        stage=IntentStage.RESERVED,
        reservation_id=reservation.reservation_id,
    )

    # (3) Confirm. A lost confirm retains the reservation; it neither releases nor
    # re-reserves.
    await _confirm(ledger, reservation=reservation, envelope=approval.envelope)
    await _advance_intent(
        connection,
        approval_id=approval.approval_id,
        stage=IntentStage.CONFIRMED,
        reservation_id=reservation.reservation_id,
    )

    # (4) The single transaction: operation, outbox, consumption.
    try:
        outcome = await _commit_admission(
            connection,
            store,
            binding=binding,
            principal=principal,
            request=request,
            approval=approval,
            reservation=reservation,
        )
    except _ApprovalConflict as conflict:
        # Resolved outside the aborted transaction: the winning row is committed, so
        # this read cannot race the writer that created it.
        #
        # `conflict.on_approval_id` is the classification, carried from the constraint
        # that actually fired rather than reconstructed by a later read (#5526 repair,
        # CXR-002). The two conflicts are different events with different compensations,
        # and guessing between them is what left an orphaned hold.
        return await _resolve_consumed_approval(
            connection,
            ledger,
            approval=approval,
            reservation=reservation,
            on_approval_id=conflict.on_approval_id,
        )
    except Exception as error:
        # (3) ok, (4) lost. #5524 §3.5: the confirm is compensatable *only because
        # nothing was dispatched* -- establish that first, then release.
        compensated = await _release_after_establishing_nothing_dispatched(
            connection,
            ledger,
            operation_id=binding.operation_id,
            reservation=reservation,
            cause=error,
        )
        if compensated:
            await _resolve_intent(
                connection,
                approval_id=approval.approval_id,
                resolution=(
                    f"admission did not commit; compensated ({type(error).__name__})"
                ),
            )
        raise

    # Committed. The intent is settled: the consumption row is now the durable record of
    # this reservation, and `reservation_state` there takes over from `IntentStage`.
    # Left as a resolved row rather than deleted so an operator can still answer "did
    # this approval's hold ever get settled, and how".
    await _resolve_intent(
        connection,
        approval_id=approval.approval_id,
        resolution="admitted; the consumption row now owns this reservation",
    )
    return outcome


async def _reserve(
    ledger: BudgetLedger, *, binding: OperationBinding, envelope: SpendEnvelope
) -> Reservation:
    """Step (2). A failure here has nothing to compensate, so nothing is compensated.

    `BudgetDenied` and `BudgetUnavailable` propagate unchanged: nothing was reserved, so
    there is nothing to release, and wrapping them would lose the distinction between
    "refused" and "unanswered" that a caller needs in order to decide about a retry.
    """
    reservation = await ledger.reserve(
        job_id=binding.job_id,
        attempt_id=binding.attempt_id,
        org_id=binding.org_id,
        workspace_id=binding.workspace_id,
        envelope=envelope,
    )
    if not isinstance(reservation, Reservation):
        # A ledger that returned something else has not told us it is holding budget,
        # and continuing would admit an operation against a reservation that may not
        # exist.
        raise BudgetUnavailable(
            "the ledger did not return a reservation; budget is not established"
        )
    if (
        reservation.job_id != binding.job_id
        or reservation.attempt_id != binding.attempt_id
    ):
        # A reservation keyed on a different attempt is not this attempt's. Every later
        # retry and every compensation path keys on these values, so a mismatch here
        # would mean confirming or releasing somebody else's reservation.
        raise BudgetUnavailable(
            "the ledger returned a reservation for a different job or attempt"
        )
    return reservation


async def _confirm(
    ledger: BudgetLedger, *, reservation: Reservation, envelope: SpendEnvelope
) -> None:
    """Step (3). A lost confirm retains; it never releases and never re-reserves.

    #5524 §3.5: "Reservation stays held. Retry `confirm` under the same key --
    idempotent, so a landed confirm returns its own answer. A reservation is not a
    spend; holding one too long costs headroom, releasing one too early costs
    correctness."
    """
    try:
        await ledger.confirm(reservation=reservation, envelope=envelope)
    except BudgetDenied:
        # A *denial* at confirm is a durable answer: the envelope is not available. The
        # reservation is released, because the ledger has said it will not honour it and
        # nothing was dispatched -- no fence is needed for work that was never queued.
        await _release_quietly(
            ledger,
            reservation=reservation,
            reason="confirm was denied; nothing was admitted or dispatched",
        )
        raise
    except BudgetUnavailable:
        # The load-bearing branch. We do not know whether the confirm landed. Retain,
        # and record why, so a held reservation is a recorded decision rather than a
        # leak an operator finds later.
        await _retain_quietly(
            ledger,
            reservation=reservation,
            reason=(
                "confirm outcome could not be established; the reservation is retained "
                "for retry under the same key rather than released"
            ),
        )
        raise


class _ApprovalConflict(Exception):
    """Internal control flow: this approval or operation is already recorded as paid.

    Private and never raised out of this module. It exists so the transaction can be
    aborted from inside the `async with` block without a sentinel return value a caller
    could mistake for an admission.

    `on_approval_id` distinguishes **which** of the table's two unique constraints
    fired, and it is carried rather than re-derived (#5526 repair, CXR-002):

    * `True` -- the `approval_id` primary key. This approval has already been spent, and
      the caller is a replay. The winning operation exists and can be handed back.
    * `False` -- the `operation_id` unique constraint. A *different* approval already
      paid for this operation. Because `operation_id` is derived from `approval_id`, two
      different approvals cannot normally collide here, so this is either an out-of-band
      write or a derivation collision -- and in neither case may this approval be
      treated as a successful replay.

    The earlier revision used an untargeted `ON CONFLICT DO NOTHING` and then tried to
    tell the two apart by reading the table back. That read cannot distinguish them: a
    missing row under the resolved tenant is produced both by an operation-id conflict
    and by a cross-tenant approval id, so both collapsed into one refusal -- and the
    refusal path compensated nothing, leaving the second reservation confirmed at the
    ledger with no record that it was owed back. The constraint knows the answer; asking
    it later does not.
    """

    def __init__(self, *, on_approval_id: bool) -> None:
        super().__init__(
            "approval_id" if on_approval_id else "operation_id",
        )
        self.on_approval_id = on_approval_id


async def _commit_admission(
    connection: Connection,
    store: OperationStore,
    *,
    binding: OperationBinding,
    principal: ResolvedPrincipal,
    request: OperationRequest,
    approval: ApprovalRecord,
    reservation: Reservation,
) -> AdmissionOutcome:
    """Step (4): operation, outbox and consumption in one transaction.

    `store.admit()` opens its own transaction around the first two writes. This function
    opens the outer one so the consumption row commits with them -- a nested
    `transaction()` is a savepoint on the drivers this package supports, so the outer
    commit remains the single durability point.
    """
    async with connection.transaction():  # type: ignore[attr-defined]
        # (4a) + (4b).
        admitted = await store.admit(
            connection,
            principal,
            request,
            operation_id=binding.operation_id,
            attempt_id=binding.attempt_id,
            job_id=binding.job_id,
        )
        # (4c), after the operation exists, because it references it. On a retry
        # `store.admit` returns the *stored* row, so this is the id that is really
        # there; inserting the consumption row first would point a foreign key at the id
        # this call minted, which the idempotency constraint is about to discard.
        #
        # **Two targeted `ON CONFLICT` clauses, tried in order, rather than one
        # untargeted `DO NOTHING`** (#5526 repair, CXR-002).
        #
        # The untargeted form swallowed both of the table's unique constraints
        # indistinguishably, and the code that had to tell them apart afterwards could
        # not: a read that finds no row under the resolved tenant is produced equally by
        # an operation-id conflict and by an approval id belonging to another tenant. So
        # a second approval for an identical request was refused with no compensation --
        # two reservations confirmed, nothing released, one consumption row.
        #
        # Targeting `(approval_id)` means the statement conflicts *only* on the replay
        # case. Any other unique violation -- the `operation_id` constraint -- now
        # raises a real unique violation, which is caught below and classified as such.
        # The constraint that fired is the answer, and it is available exactly once:
        # here.
        try:
            inserted = await connection.fetchrow(
                """
                INSERT INTO harness_approval_consumption (
                    approval_id, operation_id, org_id, workspace_id, plan_digest,
                    requester, approved_by, max_resource_units, max_runtime_seconds,
                    max_cost_micros, reservation_id, reservation_state
                )
                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12)
                ON CONFLICT (approval_id) DO NOTHING
                RETURNING approval_id
                """,
                approval.approval_id,
                admitted.record.operation_id,
                binding.org_id,
                binding.workspace_id,
                binding.plan_digest,
                approval.binding.requester,
                approval.decided_by,
                approval.envelope.max_resource_units,
                approval.envelope.max_runtime_seconds,
                approval.envelope.max_cost_micros,
                reservation.reservation_id,
                ReservationState.CONFIRMED.value,
            )
        except Exception as error:
            if not _is_unique_violation(error):
                raise
            # The `operation_id` constraint. Classified from the constraint that fired,
            # which is the only moment the answer is available without guessing.
            raise _ApprovalConflict(on_approval_id=False) from error
        if inserted is None:
            # The `approval_id` primary key: a replay. Aborts the transaction, so the
            # operation and outbox rows this call may have written go with it. Without
            # that, the loser of a race would leave an admitted operation no approval
            # paid for.
            raise _ApprovalConflict(on_approval_id=True)
    return AdmissionOutcome(
        operation=admitted,
        reservation=reservation,
        approval_id=approval.approval_id,
    )


async def _resolve_consumed_approval(
    connection: Connection,
    ledger: BudgetLedger,
    *,
    approval: ApprovalRecord,
    reservation: Reservation,
    on_approval_id: bool,
) -> AdmissionOutcome:
    """Hand a replayed approval the operation it was already spent on.

    This is what makes single use a *retry-safe* rule rather than a hostile one. A
    replay gets `created=False` and the original operation, so an honest retry after a
    dropped response is idempotent; a deliberate replay gets no second admission and,
    because the ledger key is derived from the approval, no second reservation either.

    ## The two conflicts are resolved separately (#5526 repair, CXR-002)

    `on_approval_id` is the classification the constraint supplied, and the branches it
    selects are not variations on one answer:

    * **approval-id conflict** -- a replay. Because the ledger key is derived from the
      approval, the reservation this call holds *is* the winner's reservation, granted
      again by an idempotent `reserve`. Nothing is owed back: releasing it would release
      the hold the winning operation is running on. This is the branch that must not
      compensate, and the reason the classification has to be exact.

    * **operation-id conflict** -- a different approval already paid for this operation,
      so this approval admits nothing and its hold is genuinely orphaned. It is released
      *durably*: the intent row records the decision before the ledger call, so a crash
      mid-compensation leaves an enumerable obligation instead of a silent leak.
      Releasing rather than retaining is correct here specifically because the
      transaction rolled back, so this call dispatched nothing -- there is no uncertain
      work for the budget to be covering.

    The reproduced bug was that both fell through one refusal that compensated nothing:
    two reservations confirmed, zero released or retained, one consumption row.
    """
    if not on_approval_id:
        # No `_release_quietly` here. That helper exists so a compensation failure
        # cannot mask the *original* error on a path that is already raising something
        # else -- but this path's whole purpose is the compensation, so swallowing a
        # ledger failure would report a clean refusal while the hold silently survived.
        # Recorded first, released second, so the obligation outlives a crash in
        # between.
        await _compensate_orphaned_hold(
            connection,
            ledger,
            approval=approval,
            reservation=reservation,
            reason=(
                "a different approval already paid for this operation; this approval's "
                "reservation is released because nothing was admitted or dispatched"
            ),
        )
        raise ApprovalRefused(
            "the approval could not be consumed under the resolved tenant"
        )
    row = await connection.fetchrow(
        """
        SELECT operation_id, plan_digest
          FROM harness_approval_consumption
         WHERE approval_id = $1 AND org_id = $2 AND workspace_id = $3
        """,
        approval.approval_id,
        approval.binding.org_id,
        approval.binding.workspace_id,
    )
    if row is None:
        # The approval id is recorded under a *different* tenant. The operation-id
        # conflict no longer arrives here -- it is classified and handled above -- so
        # this branch has exactly one cause, which is what makes compensating it correct
        # rather than a guess.
        #
        # The refusal message is deliberately identical to the operation-id branch's,
        # and deliberately does not confirm that the other row exists: a distinguishable
        # answer is itself the cross-tenant disclosure. Same reasoning as
        # `store._resolve_conflict`. Indistinguishable to the *caller* while being
        # distinct in the *code* is the whole arrangement -- the earlier revision had
        # one branch for both and therefore could not compensate either.
        await _compensate_orphaned_hold(
            connection,
            ledger,
            approval=approval,
            reservation=reservation,
            reason=(
                "the approval is recorded under another tenant; this admission's "
                "reservation is released because nothing was admitted or dispatched"
            ),
        )
        raise ApprovalRefused(
            "the approval could not be consumed under the resolved tenant"
        )
    consumption = dict(row)  # type: ignore[call-overload]
    # No plan-digest comparison here, deliberately. An earlier revision had one, on the
    # reasoning that a replay for a changed plan must be refused rather than answered
    # with the original operation. That is the right rule and it is enforced -- but
    # *before* this function, and the check here could not fire.
    #
    # Because `operation_id` is derived from `approval_id`, a second admission under one
    # approval always presents the same `operation_id` to `harness_operations`, so the
    # store's constraint fires first and `store._resolve_conflict` adjudicates: a
    # changed payload under the same idempotency key raises `OperationRefused` ("a retry
    # may not change the request"), and a changed idempotency key finds no row under the
    # tenant and is refused too. Either way `store.admit()` raises and this function is
    # never reached. `evaluate_approval` also refuses a changed plan earlier still,
    # whenever the approval remains bound to the original one.
    #
    # Removed rather than kept as defence in depth, because dead code that looks
    # load-bearing is worse than no code: a future reader would reasonably conclude this
    # is where changed-plan replays are caught, and could weaken the real check
    # upstream. `tests/test_admission_postgres.py` asserts the refusal by its actual
    # message, so the rule stays covered by the layer that really enforces it.
    #
    # Found by running the suite against a real PostgreSQL: the test for this branch
    # failed with the store's refusal instead, which is how the unreachability
    # surfaced.
    operation_row = await connection.fetchrow(
        f"SELECT {_COLUMNS} FROM harness_operations WHERE operation_id = $1",
        consumption["operation_id"],
    )
    if operation_row is None:
        # A consumption row referencing no operation. `ON DELETE RESTRICT` makes this
        # unreachable through a normal delete, so arriving here means a row was removed
        # out of band. Refused rather than re-admitted: re-admitting would grant a
        # second spend on the strength of a missing row.
        raise ApprovalRefused(
            "this approval is recorded as consumed but its operation is missing; "
            "it cannot be reused"
        )
    return AdmissionOutcome(
        operation=AdmittedOperation(record=_record(operation_row), created=False),
        reservation=reservation,
        approval_id=approval.approval_id,
    )


async def read_consumption(
    connection: Connection, principal: ResolvedPrincipal, *, approval_id: str
) -> ConsumedApproval | None:
    """Where the reserve/confirm/admit sequence got to for one approval, or None.

    The read half of recoverable compensation: a process resuming after a crash asks
    this before deciding anything, because the safe answer differs by state and the only
    other way to obtain it is to ask the ledger about a reservation whose id it does not
    know.

    **Scoped to the principal's tenant** (#5526 repair, CXR-005). An earlier revision
    took only an approval id, and the exported function returned the reservation id,
    operation id and plan digest to anyone who could name any approval -- including
    another organization's. An approval id is not a secret and not a capability: it is a
    correlation value that appears in tickets, logs and audit trails, so treating
    possession of one as authority to read the spend it paid for is a cross-tenant
    disclosure with an authentication story attached.

    The tenant goes in the WHERE clause rather than into a check on the result, so
    "absent" and "another tenant's" produce the identical `None`. A caller that could
    tell them apart could enumerate other tenants' approvals by their answers, which is
    the disclosure again in a quieter form. Same rule as `store.read_for_tenant`.

    For the genuinely global reads that reconciliation needs, see
    `read_consumption_privileged` -- named separately and explicitly, rather than
    reached by omitting an argument.
    """
    if not isinstance(principal, ResolvedPrincipal):
        # A `ContractViolation` rather than a silent empty answer: a caller that passed
        # something else has no tenant, and answering "None" would look like a clean
        # negative rather than a programming error that removed the scoping.
        raise ContractViolation("read_consumption requires a ResolvedPrincipal")
    row = await connection.fetchrow(
        """
        SELECT approval_id, operation_id, plan_digest, reservation_id, reservation_state
          FROM harness_approval_consumption
         WHERE approval_id = $1 AND org_id = $2 AND workspace_id = $3
        """,
        approval_id,
        principal.org_id,
        principal.workspace_id,
    )
    return _consumed(row)


async def read_consumption_privileged(
    connection: Connection, *, approval_id: str
) -> ConsumedApproval | None:
    """An unscoped read of one consumption record, for operator recovery only.

    Separate and explicitly named, because global reads are a real operational need --
    reconciling a hold whose tenant is exactly what is being established -- and the
    wrong way to serve that need is to leave the tenant argument optional on the
    ordinary read. An optional scope is an unscoped default one refactor later, and the
    caller who omits it does not look like they are doing anything unusual.

    **Must not be reachable from a request path.** It is the privileged half of the
    split, in the same position as `DispatchOutbox.list_ineligible`: an operator sweep
    over the
    harness's own records, not an answer to a caller who named an id. Nothing in this
    package calls it from a path a request can reach, and the only in-package caller is
    the reconciliation sweep, which is itself an operator-invoked entry point.

    The name is the control here, which is worth being honest about: a function cannot
    enforce who calls it. What it can do is make the privileged read impossible to
    perform
    *by accident*, so that a review of the composition seam has something to look at.
    """
    row = await connection.fetchrow(
        """
        SELECT approval_id, operation_id, plan_digest, reservation_id, reservation_state
          FROM harness_approval_consumption
         WHERE approval_id = $1
        """,
        approval_id,
    )
    return _consumed(row)


def _consumed(row: object) -> ConsumedApproval | None:
    """Build a `ConsumedApproval` from a row, or `None`.

    Shared by the scoped and privileged reads so the two cannot drift in what they
    return
    -- only in which rows they are allowed to see, which is the entire difference
    between them.
    """
    if row is None:
        return None
    data = dict(row)  # type: ignore[call-overload]
    return ConsumedApproval(
        approval_id=data["approval_id"],
        operation_id=data["operation_id"],
        plan_digest=data["plan_digest"],
        reservation_id=data["reservation_id"],
        reservation_state=ReservationState(data["reservation_state"]),
    )


async def list_interrupted_admissions(
    connection: Connection, *, limit: int = 50
) -> tuple[AdmissionIntent, ...]:
    """Admissions that started and never reached a terminal answer, oldest first.

    **The enumerable half of the reconciliation contract** (#5526 repair, CXR-003).
    Before the intent table there was no answer to "what might the ledger be holding
    that this
    database does not know about", because a process that died between `reserve` landing
    and its reply leaving wrote nothing at all. Recovery was not difficult, it was
    undefined -- there was no set to iterate.

    Oldest first is only scheduling order. Age does not prove a writer has stopped.
    The sweep acquires per-approval ownership and rereads each intent before acting;
    a live writer or another active sweep is skipped and reported as unresolved.

    Unscoped by tenant, like `DispatchOutbox.list_ineligible` and for the same reason:
    an operator reconciling holds is asking about the harness's own obligations, and the
    tenant of a row is part of the answer rather than an input to the question. Not to
    be exposed on a request path.
    """
    bounded = max(1, min(int(limit), 200))
    rows = await connection.fetch(  # type: ignore[attr-defined]
        """
        SELECT approval_id, operation_id, job_id, attempt_id, org_id, workspace_id,
               max_resource_units, max_runtime_seconds, max_cost_micros,
               stage, reservation_id, resolution
          FROM harness_admission_intent
         WHERE stage <> 'resolved'
         ORDER BY created_at
         LIMIT $1
        """,
        bounded,
    )
    return tuple(_intent_from_row(row) for row in rows)


def _intent_from_row(row: object) -> AdmissionIntent:
    data = dict(row)
    return AdmissionIntent(
        approval_id=data["approval_id"],
        operation_id=data["operation_id"],
        job_id=data["job_id"],
        attempt_id=data["attempt_id"],
        org_id=data["org_id"],
        workspace_id=data["workspace_id"],
        envelope=SpendEnvelope(
            max_resource_units=data["max_resource_units"],
            max_runtime_seconds=data["max_runtime_seconds"],
            max_cost_micros=data["max_cost_micros"],
        ),
        stage=IntentStage(data["stage"]),
        reservation_id=data["reservation_id"],
        resolution=data["resolution"],
    )


async def reconcile_interrupted_admissions(
    connection: Connection,
    ledger: BudgetLedger,
    *,
    limit: int = 50,
) -> ReconciliationReport:
    """Settle budget for admissions that were interrupted. Never admits work.

    **The concrete reconciliation contract** the repair owes (#5526, CXR-003). For each
    unresolved intent:

    * **the admission committed after all** -- a consumption row exists for this
      approval. The intent is stale bookkeeping from a reply lost after the commit; mark
      it resolved and touch the ledger not at all. The consumption row's
      `reservation_state` is the authority once it exists, and a sweep that released
      here would release the budget a live operation is running on.

    * **nothing committed** -- no consumption row. The hold, if any, is owed back.

    Within the second case the stage decides *how*, and the `intended` case is the one
    the table exists for: the reply was never received, so this process cannot name a
    reservation. It re-`reserve`s under the derived `(job_id, attempt_id)` key -- which
    is idempotent by the ledger's contract, so it either returns the hold the dead
    attempt created or creates a fresh one that is immediately released. That is why the
    derived key had to be a function of the approval alone: a caller-chosen key would
    leave the dead attempt's hold unnameable forever.

    ## What it deliberately does not do

    * **It does not admit.** No operation row, no outbox row, no consumption row is
      written. A recovery path that could admit would be a second admission path
      reachable without an approval check, which is CXR-001 arriving through the
      recovery door.
    * **It does not widen budget.** The only ledger calls are `release` and `retain`,
      and the envelope it carries is the one copied at intent time. No path here
      reserves *more* than the original approval allowed, and `reserve` is called
      only to obtain a handle on a hold that is about to be released.
    * **It does not consult the approval.** Expiry and revocation are irrelevant to
      returning money: an expired approval's hold is owed back *more* urgently, not
      less. This is exactly why the intent row carries the envelope by value -- the
      approval store may no longer answer, and CXR-003 showed a retry refusing on expiry
      before it ever reached the ledger, leaving the hold forever.

    Uncertain outcomes are counted in `unresolved` rather than forced: a ledger that
    cannot answer is a reason to run the sweep again, and a sweep that guessed would be
    choosing between releasing money for work that may exist and holding money for work
    that never will.
    """
    report = ReconciliationReport()
    for intent in await list_interrupted_admissions(connection, limit=limit):
        report = ReconciliationReport(
            scanned=report.scanned + 1,
            released=report.released,
            retained=report.retained,
            unresolved=report.unresolved,
        )
        async with _admission_ownership(
            connection, intent.approval_id, wait=False
        ) as acquired:
            if not acquired:
                report = ReconciliationReport(
                    scanned=report.scanned,
                    released=report.released,
                    retained=report.retained,
                    unresolved=report.unresolved + 1,
                )
                continue
            # The initial enumeration may be stale: a writer or another sweep can
            # settle this row before we obtain ownership.
            current = await connection.fetchrow(
                "SELECT * FROM harness_admission_intent "
                "WHERE approval_id = $1 AND stage <> 'resolved'",
                intent.approval_id,
            )
            if current is None:
                continue
            intent = _intent_from_row(current)
            # Privileged deliberately: the tenant is on the intent row, and the question
            # is
            # about the harness's own obligation rather than a caller's entitlement to
            # read.
            committed = await read_consumption_privileged(
                connection, approval_id=intent.approval_id
            )
            if committed is not None:
                await _resolve_intent(
                    connection,
                    approval_id=intent.approval_id,
                    resolution=(
                        "admission committed; consumption owns this reservation"
                    ),
                )
                continue
            try:
                settled = await _settle_interrupted_intent(
                    connection, ledger, intent=intent
                )
            except Exception:  # noqa: BLE001 - one bad row must not stop the sweep
                # Left unresolved on purpose, and left for the next sweep. Raising would
                # abandon every remaining row because of one unreachable reservation,
                # and a
                # reconciliation that stops at the first problem is one that never
                # reaches
                # the rows after it.
                report = ReconciliationReport(
                    scanned=report.scanned,
                    released=report.released,
                    retained=report.retained,
                    unresolved=report.unresolved + 1,
                )
                continue
            report = ReconciliationReport(
                scanned=report.scanned,
                released=report.released
                + (1 if settled is ReservationState.RELEASED else 0),
                retained=report.retained
                + (1 if settled is ReservationState.RETAINED else 0),
                unresolved=report.unresolved,
            )
    return report


async def _settle_interrupted_intent(
    connection: Connection,
    ledger: BudgetLedger,
    *,
    intent: AdmissionIntent,
) -> ReservationState:
    """Return or retain the hold one interrupted admission left behind.

    The stage selects the action, and the `intended` branch is the one that recovers a
    hold this process never learned the id of.
    """
    reservation = intent.reservation()
    if reservation is None:
        # Stage `intended`: a reserve may or may not have landed, and no id was
        # recorded. Re-reserve under the derived key to obtain a handle. Idempotent by
        # the ledger's contract, so this returns the dead attempt's hold if there was
        # one -- and if there was not, it creates a hold that the release two lines
        # below immediately returns. Creating one in order to release it is not a
        # widening: the envelope is the approved one, and the net effect is zero.
        reservation = await ledger.reserve(
            job_id=intent.job_id,
            attempt_id=intent.attempt_id,
            org_id=intent.org_id,
            workspace_id=intent.workspace_id,
            envelope=intent.envelope,
        )
        if (
            not isinstance(reservation, Reservation)
            or reservation.job_id != intent.job_id
            or reservation.attempt_id != intent.attempt_id
        ):
            raise BudgetUnavailable(
                "the ledger did not return this intent's reservation; "
                "the hold cannot be settled"
            )
        await _advance_intent(
            connection,
            approval_id=intent.approval_id,
            stage=IntentStage.RESERVED,
            reservation_id=reservation.reservation_id,
        )

    # Nothing was admitted -- there is no consumption row, checked by the caller -- so
    # there is no operation for a worker to have acted on and no fence is owed. But a
    # dispatch row could exist if the admission transaction committed the operation and
    # outbox rows and then lost its consumption row to a crash. That is the uncertain
    # case, and #5524 §3.5 is explicit: retain, never release.
    if await _outbox_row_exists(connection, operation_id=intent.operation_id):
        await ledger.retain(
            reservation=reservation,
            reason=(
                f"interrupted admission for operation {intent.operation_id} has a "
                "dispatch row; the reservation is retained until provider "
                "reconciliation"
            ),
        )
        await _resolve_intent(
            connection,
            approval_id=intent.approval_id,
            resolution="retained: a dispatch row exists, so the outcome is uncertain",
        )
        return ReservationState.RETAINED

    # Resolve only after the idempotent release succeeds. Failure or process
    # loss on either side leaves an enumerable obligation for the next sweep.
    await ledger.release(
        reservation=reservation,
        reason=(
            f"admission for operation {intent.operation_id} was interrupted before it "
            "committed and nothing was dispatched; the reservation is released"
        ),
    )
    await _resolve_intent(
        connection,
        approval_id=intent.approval_id,
        resolution="released: interrupted before admission, nothing was dispatched",
    )
    return ReservationState.RELEASED


async def _release_after_establishing_nothing_dispatched(
    connection: Connection,
    ledger: BudgetLedger,
    *,
    operation_id: str,
    reservation: Reservation,
    cause: BaseException,
) -> bool:
    """Compensate a failed (4), in the order #5524 §3.5 requires.

    "(2) confirm succeeds, (3) commit lost -> No admission, no outbox row, therefore no
    dispatch. The confirm is compensatable **only because nothing was dispatched** --
    establish that first, then release."

    So this *establishes* it with a query rather than inferring it from the exception. A
    rolled-back transaction is strong evidence that no outbox row exists, and "strong
    evidence" is what the uncertain-dispatch rule exists to refuse: if a row is there,
    the reservation is retained, because a dispatch that may have happened is not a
    dispatch that did not.
    """
    if await _outbox_row_exists(connection, operation_id=operation_id):
        return await _retain_quietly(
            ledger,
            reservation=reservation,
            reason=(
                "admission failed but a dispatch row exists; the reservation is "
                "retained until provider reconciliation rather than released "
                f"({type(cause).__name__})"
            ),
        )
    return await _release_quietly(
        ledger,
        reservation=reservation,
        reason=(
            "admission did not commit and no dispatch row exists, so nothing was "
            f"dispatched ({type(cause).__name__})"
        ),
    )


async def _outbox_row_exists(connection: Connection, *, operation_id: str) -> bool:
    """Whether anything could have been dispatched for this operation.

    Its own function because "was anything dispatched" is the premise of two different
    compensation branches, and a premise established two ways is a premise that can
    disagree with itself.

    Deliberately *unlocked* and deliberately conservative: both callers reach it from a
    path where the admission transaction has already rolled back or never committed, so
    they are asking "could an outbox row have survived" rather than "may this row still
    be dispatched". `cancel_before_dispatch` asks the second question and uses
    `_classify_dispatch` instead -- see `DispatchEvidence` for why existence is not an
    answer to it.
    """
    found = await connection.fetchval(
        "SELECT EXISTS (SELECT 1 FROM harness_dispatch_outbox WHERE operation_id = $1)",
        operation_id,
    )
    return bool(found)


async def _classify_dispatch(
    connection: Connection, *, operation_id: str
) -> DispatchEvidence:
    """Lock this operation's outbox row and say what it proves about delivery (CXR-004).

    `FOR UPDATE` on the outbox row is what makes the answer usable rather than merely
    accurate-when-read. `outbox.claim` selects its candidates `FOR UPDATE OF o SKIP
    LOCKED`, so while this row lock is held a concurrent claim *skips* this row instead
    of blocking on it: the classification cannot be invalidated between being computed
    and being acted on. Without the lock, "no claim has ever taken this row" would be a
    statement about the past with no bearing on the microsecond after it was made, and
    the fence would be racing the very worker it exists to shut out.

    The caller must therefore hold a transaction across this call and the fence. It
    does; see `cancel_before_dispatch`.

    `attempts = 0 AND claimed_until IS NULL` is the pending predicate rather than
    `claimed_until IS NULL` alone. A claim increments `attempts` and takes a lease in
    one statement, and a failure report clears the lease while leaving `attempts`
    standing (`outbox._record_failure`) -- so a row that was claimed, attempted and
    failed has a null lease and is emphatically not undispatched. Reading only the lease
    would classify it as never-queued and release budget for work an executor may have
    received.

    `delivered_at` and `abandoned_at` are checked too, though either one implies
    `attempts > 0` on any row this code produced. Checked anyway because they are the
    direct evidence and `attempts` is the indirect kind: if a future path ever stamps
    one without the other, the safe answer should not depend on which column a reader
    happened to trust.
    """
    row = await connection.fetchrow(
        """
        SELECT attempts, claimed_until, delivered_at, abandoned_at
          FROM harness_dispatch_outbox
         WHERE operation_id = $1
           FOR UPDATE
        """,
        operation_id,
    )
    if row is None:
        return DispatchEvidence.NEVER_QUEUED
    data = dict(row)  # type: ignore[call-overload]
    if (
        int(data["attempts"]) == 0
        and data["claimed_until"] is None
        and data["delivered_at"] is None
        and data["abandoned_at"] is None
    ):
        return DispatchEvidence.DEFINITELY_PENDING
    return DispatchEvidence.CLAIMED_OR_DELIVERED


async def _withdraw_pending_dispatch(
    connection: Connection, *, operation_id: str
) -> bool:
    """Remove a never-claimed outbox row so the cancellation cannot be undone.

    Returns whether a row was withdrawn.

    Deleting rather than flagging, and this is the one place in this package that
    removes
    an outbox row, so the reasons are worth stating:

    * The row is the *only* thing that makes a dispatch possible, and the fence has
      already been established when this runs. Leaving it queued after releasing its
      budget would mean a claimable dispatch whose money has been returned -- the
      release-before-fence window #5524 §3.5 forbids, arriving one step later.
    * Nothing is lost that an operator needs. `harness_operations` keeps the operation,
      and `harness_approval_consumption` keeps the approval, the envelope and the now-
      `released` reservation state. What disappears is a queue entry for work that
      provably never ran; the record that it was accepted and cancelled survives in the
      two tables that are meant to hold it.
    * The alternative -- a `withdrawn_at` column excluded from `claim` -- is a fourth
      lifecycle column and a fourth predicate on the hot claim query, to express
      something the row's absence already expresses unambiguously.

    The `attempts = 0 AND claimed_until IS NULL` guard is repeated here even though
    `_classify_dispatch` just established it under a row lock. That is not redundancy
    for its own sake: it makes this statement correct on its own terms, so a future
    caller that reaches it without the lock cannot delete a claimed row, and the return
    value tells the caller which happened rather than assuming.
    """
    deleted = await connection.fetchval(
        """
        DELETE FROM harness_dispatch_outbox
         WHERE operation_id = $1
           AND attempts = 0
           AND claimed_until IS NULL
           AND delivered_at IS NULL
           AND abandoned_at IS NULL
        RETURNING id
        """,
        operation_id,
    )
    return deleted is not None


async def cancel_before_dispatch(
    connection: Connection,
    ledger: BudgetLedger,
    fence: CreationFence,
    *,
    operation_id: str,
    job_id: str,
    attempt_id: str,
    reservation: Reservation,
    reason: str,
) -> ReservationState:
    """Cancel or expire an operation before dispatch: fence creation, then release.

    #5524 §3.5: "**Fence creation, then release the reservation.** Never the reverse:
    releasing first leaves a window where a stale worker can still create the resource
    the budget no longer covers."

    The ordering is the whole function, and the `False` branch is why it returns a state
    rather than `None`: a fence that could not be established means a straggling worker
    may still create the resource, so the reservation is **retained**. Releasing anyway
    would reopen exactly the window the rule forbids, reached by treating a failed fence
    as a successful one.

    ## The classification, and why it is not "does a row exist" (CXR-004)

    An earlier revision asked `_outbox_row_exists` here, and the result was that an
    ordinary cancellation *never released anything*. Admit, then cancel: the fence is
    established, the outbox row is sitting there untouched at `attempts = 0`, and the
    existence check reports "dispatched". Every cancellation of every queued operation
    retained its budget, and the only way to reach the release branch was for a test to
    delete the production row by hand -- which is the diagnosis, not a workaround: a
    branch reachable only by a manual DELETE is a branch production cannot reach.

    So the row is *classified* rather than counted, and both halves of the coordination
    happen in one transaction:

    * **`DEFINITELY_PENDING`** -- no claim has ever taken this row. Fence, withdraw the
      row so the cancellation cannot be undone by a later claim, then release. Safe
      because `attempts` is incremented by the claim statement itself, so zero is the
      row's own statement that no executor has ever been handed it.
    * **`NEVER_QUEUED`** -- no row at all. Same answer: nothing to withdraw, and nothing
      could have been delivered.
    * **`CLAIMED_OR_DELIVERED`** -- a worker has held it. **Retain.** A fence bounds the
      future; it says nothing about the past, and an executor that already received the
      envelope may have created the resource.

    ## Why one transaction, and why the fence is inside it

    "Atomically coordinate the outbox and the creation fence" is the requirement, and
    the two are coordinated by the row lock `_classify_dispatch` takes plus the
    transaction that holds it across the fence call. `outbox.claim` selects `FOR UPDATE
    ... SKIP LOCKED`, so a concurrent claim skips a row locked here rather than waiting
    for it: between "no claim has ever taken this row" and the fence being established,
    no claim can. Without the transaction the classification would be a fact about a
    moment that had already passed.

    The fence is an external call inside a transaction, which this module otherwise
    avoids -- the sequence in the module docstring keeps the ledger strictly outside the
    store's transaction for exactly that reason. It is correct here and not there
    because the quantities differ: the admission path holds a transaction open across a
    ledger call on *every* admission, making steady-state throughput a function of the
    ledger's latency, while cancellation is an exceptional path whose whole purpose is
    to make a decision that a concurrent claim must not invalidate. Paying a held row
    lock for correctness on the rare path is a different trade from paying it on the
    common one.

    The ledger call is deliberately *outside* the transaction, after it commits. A
    release is not undone by a rollback -- the money is already back -- so a release
    inside a transaction that then failed to commit would leave the withdrawal reverted
    and the budget returned, which is the one combination that admits unfunded work.
    Committing the withdrawal first means the worst case is a released-in-the-ledger
    hold whose row reads `confirmed`, which `_record_reservation_state` already
    documents as the harmless direction and which an operator resolves by asking the
    ledger.
    """
    async with connection.transaction():  # type: ignore[attr-defined]
        evidence = await _classify_dispatch(connection, operation_id=operation_id)
        fenced = await fence.fence(
            operation_id=operation_id, job_id=job_id, attempt_id=attempt_id
        )
        if fenced and evidence is DispatchEvidence.DEFINITELY_PENDING:
            # Fenced first, then the queue entry withdrawn, then (after the commit) the
            # release. A row left claimable after its budget was returned would be
            # unfunded work with a valid-looking dispatch envelope.
            await _withdraw_pending_dispatch(connection, operation_id=operation_id)

    if not fenced:
        await ledger.retain(
            reservation=reservation,
            reason=(
                f"creation could not be fenced for operation {operation_id}; the "
                "reservation is retained because a stale worker may still create the "
                f"resource ({reason})"
            ),
        )
        return await _record_reservation_state(
            connection, operation_id=operation_id, state=ReservationState.RETAINED
        )

    if evidence is DispatchEvidence.CLAIMED_OR_DELIVERED:
        # Fenced, but a worker has held this row, so an executor may already have acted.
        # A fence stops future creation; it says nothing about what already happened.
        # Retained until provider reconciliation.
        await ledger.retain(
            reservation=reservation,
            reason=(
                f"operation {operation_id} was fenced but its dispatch had already "
                "been claimed; the reservation is retained until provider "
                f"reconciliation ({reason})"
            ),
        )
        return await _record_reservation_state(
            connection, operation_id=operation_id, state=ReservationState.RETAINED
        )

    await ledger.release(
        reservation=reservation,
        reason=(
            "cancelled before dispatch, after fencing creation and withdrawing an "
            f"unclaimed dispatch ({reason})"
        ),
    )
    return await _record_reservation_state(
        connection, operation_id=operation_id, state=ReservationState.RELEASED
    )


async def retain_for_uncertain_dispatch(
    connection: Connection,
    ledger: BudgetLedger,
    *,
    operation_id: str,
    reservation: Reservation,
    reason: str,
) -> ReservationState:
    """Hold a reservation whose dispatch outcome cannot be established.

    #5524 §3.5: "Uncertain dispatch, budget still reserved -> **Reservation is retained
    until provider reconciliation.**" There is deliberately no parameter that would let
    a caller turn this into a release: the only thing that may release after an
    uncertain dispatch is provider-established absence, which is `accounting`'s answer
    to give and not this module's.

    Exposed as its own function so the uncertain path has a name a caller can find. A
    caller that had to express "retain" by *not* calling anything would express it by
    forgetting, and a forgotten reservation is indistinguishable from a leak.
    """
    await ledger.retain(reservation=reservation, reason=reason)
    return await _record_reservation_state(
        connection, operation_id=operation_id, state=ReservationState.RETAINED
    )


async def _record_intent(
    connection: Connection,
    *,
    binding: OperationBinding,
    approval: ApprovalRecord,
    envelope: SpendEnvelope,
) -> None:
    """Write the durable admission intent, before any external effect (CXR-003).

    `ON CONFLICT (approval_id)` resets the row to `intended` and clears the reservation
    id, rather than doing nothing. That is deliberate: a retry of an admission whose
    previous attempt died mid-sequence is re-entering the sequence from the top, and a
    stale `reserved` stage left over from the dead attempt would tell the sweep a hold
    definitely exists when the current attempt has not yet asked for one. `intended` is
    the state that claims least, and claiming least is what keeps the column from
    running ahead of reality.

    A resolved row is *not* reset -- `WHERE ... stage <> 'resolved'` -- because a
    settled admission is history, and a replay must not reopen an obligation that was
    already discharged. The replay is adjudicated by the consumption row's primary key
    instead, which is the control that has always owned single use.
    """
    await connection.execute(  # type: ignore[attr-defined]
        """
        INSERT INTO harness_admission_intent (
            approval_id, operation_id, job_id, attempt_id, org_id, workspace_id,
            max_resource_units, max_runtime_seconds, max_cost_micros, stage
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
        ON CONFLICT (approval_id) DO UPDATE
           SET stage = $10,
               reservation_id = NULL,
               resolution = NULL,
               updated_at = now()
         WHERE harness_admission_intent.stage <> 'resolved'
        """,
        approval.approval_id,
        binding.operation_id,
        binding.job_id,
        binding.attempt_id,
        binding.org_id,
        binding.workspace_id,
        envelope.max_resource_units,
        envelope.max_runtime_seconds,
        envelope.max_cost_micros,
        IntentStage.INTENDED.value,
    )


async def _advance_intent(
    connection: Connection,
    *,
    approval_id: str,
    stage: IntentStage,
    reservation_id: str,
) -> None:
    """Record that an external effect is known to have happened.

    Called *after* each ledger reply, never before, so the column lags reality rather
    than leading it. The direction matters: a row reading `intended` while a hold exists
    costs one redundant idempotent call during reconciliation, whereas a row reading
    `reserved` for a hold that was never granted would have the sweep release a
    reservation the ledger does not have -- and a ledger asked to release an unknown
    reservation is entitled to treat that as an error, which would stall the sweep on a
    row that can never be settled.

    Guarded on `stage <> 'resolved'` so a late reply cannot reopen a settled row.
    """
    await connection.execute(  # type: ignore[attr-defined]
        """
        UPDATE harness_admission_intent
           SET stage = $2, reservation_id = $3, updated_at = now()
         WHERE approval_id = $1 AND stage <> 'resolved'
        """,
        approval_id,
        stage.value,
        reservation_id,
    )


async def _resolve_intent(
    connection: Connection, *, approval_id: str, resolution: str
) -> None:
    """Mark an intent settled: nothing further is owed for this approval.

    Marked rather than deleted, so "was this approval's hold ever settled, and how"
    stays answerable after the incident that prompted the question. The partial index
    keeps the sweep cheap regardless of how many settled rows accumulate.
    """
    await connection.execute(  # type: ignore[attr-defined]
        """
        UPDATE harness_admission_intent
           SET stage = $2, resolution = $3, updated_at = now()
         WHERE approval_id = $1
        """,
        approval_id,
        IntentStage.RESOLVED.value,
        resolution,
    )


async def _compensate_orphaned_hold(
    connection: Connection,
    ledger: BudgetLedger,
    *,
    approval: ApprovalRecord,
    reservation: Reservation,
    reason: str,
) -> None:
    """Keep failed or interrupted release enumerable until the ledger confirms it."""
    await ledger.release(reservation=reservation, reason=reason)
    await _resolve_intent(
        connection,
        approval_id=approval.approval_id,
        resolution=f"released orphaned reservation: {reason}",
    )


async def _record_reservation_state(
    connection: Connection, *, operation_id: str, state: ReservationState
) -> ReservationState:
    """Persist the reservation's state beside the consumption record, and return it.

    The ledger is the authority on its own reservations; this column is the harness's
    record of what it last told the ledger. Updated *after* the ledger call so the
    column can never claim a release the ledger did not make -- the harmless direction
    of a crash in between is a row reading `confirmed` for a released reservation, which
    an operator resolves by asking the ledger. The other order would have the row assert
    a release that never happened.
    """
    await connection.execute(
        """
        UPDATE harness_approval_consumption
           SET reservation_state = $1
         WHERE operation_id = $2
        """,
        state.value,
        operation_id,
    )
    return state


async def _release_quietly(
    ledger: BudgetLedger, *, reservation: Reservation, reason: str
) -> bool:
    """Release, without letting a ledger failure mask the error being compensated.

    A compensation path is reached because something already went wrong. If the
    compensation itself raised, that exception would replace the original and the caller
    would be told the ledger was unreachable when the actual event was a refused
    admission. The reservation stays held in that case, which is the safe direction: a
    held reservation costs headroom, a swallowed original error costs the diagnosis.
    """
    try:
        await ledger.release(reservation=reservation, reason=reason)
    except Exception:  # noqa: BLE001 - see the docstring
        return False
    return True


async def _retain_quietly(
    ledger: BudgetLedger, *, reservation: Reservation, reason: str
) -> bool:
    """Retain, for the same reason as `_release_quietly`.

    Failing to record a retention is additionally the benign direction: the reservation
    is held either way, since holding is what happens when nobody releases. The `retain`
    call makes it an explicit decision rather than an apparent leak.
    """
    try:
        await ledger.retain(reservation=reservation, reason=reason)
    except Exception:  # noqa: BLE001 - see the docstring
        return False
    return True
