"""Durable provider-handle persistence and reconciliation — issue #5054 (U11c).

The storage half of the contract U11 (#5049) authored. That contract decides what
an ambiguous provider outcome *means*; this module is what makes those decisions
survive the process that made them dying mid-call.

## The three things this module owns, and why each needs storage

1. **Recording a handle before the call.** `authorize_provider_call` refuses a
   call whose `HandleRecord` is not durable, and `HandleRecord` refuses
   `durable=True` without a `confirmed_at` instant. `record_handle` returns that
   instant *from the committed write*, so a caller cannot manufacture one. This is
   the only place "recorded before the call counts as made" becomes enforceable.

2. **Concluding an operation idempotently.** A recovery caller retries by nature.
   `conclude_operation` applies the contract's `reconcile` once and stores the
   result; a second report of the same conclusion returns the stored one and says
   nothing was newly applied. Re-deciding on every report would let a later,
   worse observation overwrite an established conclusion.

3. **Retaining what could not be established.** An operation whose provider could
   not be consulted is neither in flight nor closed. It stays in `UNRESOLVED` and
   stays visible to the recovery read, so a failed check cannot quietly remove it
   from the books.

   `assess_allocation_release` answers a *different* question, from current
   provider evidence rather than from this stored state, and the distinction is
   deliberate. Its inventory of what must be accounted for comes from the stored
   allocation rows — so a resource the provider does not mention stays unresolved
   instead of vanishing — but whether each one is still there is decided by the
   observations supplied to it. A stored `UNRESOLVED` records that an *earlier*
   query failed; a fresh `ABSENT` is the provider itself answering now. Letting
   the stale local record veto the current provider answer would invert this
   module's own principle that provider truth outranks internal state.

## Authorization: the credential decides, the body never does

Every function here takes an authenticated `Submitter` and consults its grant
through the contract's `authorize_read`/`_covers` logic. A workspace named in a
request is *validated against that grant* before anything is written or read, so
it conveys no authority of its own — which is the point, because the natural
implementation (compare the body's workspace against the body's allocation)
compares a claim with itself.

The refusal reason is identical whether a workspace does not exist or exists and
is not the caller's. Distinguishing them would let a caller enumerate other
tenants' workspaces by reading error messages.

Writes also require live authority from a trusted B validator. Its operation, run
and attempt binding is persisted at creation and checked before every conclusion,
including terminal repeats. No validator is installed by default; these writes
return 503 until B supplies the integration. A does not mint substitute authority.

Operations are addressed by workspace *and* idempotency key, because the key
alone is not unique to a tenant: the adapters derive keys like `sp-aws-a100-1`
from cloud, GPU type and count, none of which names a workspace. Scoping the
identity keeps duplicate detection working inside a workspace while making one
tenant's key invisible to another — see `app/models/provider_handle.py`.

## What this module deliberately does not do

**No scheduler, timer, queue or retry worker.** B owns the operation lifecycle,
cancellation ordering, leases/fencing and the recovery driver; none of that exists
in ADP today. A recovery caller *asks* this module what was in flight
(`list_unconcluded`) — this module never goes looking on its own. Adding a timer
here would make the adapter a second lifecycle owner, which the boundary forbids.

**No budget ledger writes.** `assess_allocation_release` reports exposure; C owns
the ledger and applies it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, replace
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from superplane_contracts import (
    AllocationResources,
    CallOutcome,
    ContractViolation,
    CostExposure,
    HandleRecord,
    OperationKind,
    ProviderHandle,
    ProviderObservation,
    ProviderPresence,
    ReconcileRequest,
    ReconcileResult,
    ReleaseAssessment,
    ReleaseState,
    Submitter,
    assess_release,
    authorize_read,
    reconcile,
)

from app.models.provider_handle import (
    ProviderAllocation,
    ProviderAllocationResource,
    ProviderOperation,
    ProviderReferenceConflict,
)
from app.models.workspace import Workspace
from app.services.provider_authority import (
    VerifiedProviderAuthority,
    get_provider_authority_validator,
)
from app.services.provider_inventory import (
    AllocationResourceIdentity,
    VerifiedAllocationInventory,
    get_allocation_inventory_reader,
)


class OperationState(StrEnum):
    """How far an operation has got, as opposed to how far it was driven.

    ``UNRESOLVED`` is the value that makes this an enum rather than a boolean. An
    operation whose provider could not be consulted is not in flight (nothing is
    waiting on it) and not concluded (nothing is known about it). A two-value
    split has nowhere to put it, and the place it would get put is "closed" —
    which is how "we could not check" becomes "there is nothing there".
    """

    RECORDED = "recorded"
    """Written before the provider call. The call may or may not have been made."""

    CONCLUDED = "concluded"
    """The provider answered and the answer was established."""

    UNRESOLVED = "unresolved"
    """The provider could not be consulted. Retained and reported, never erased."""


#: States a recovery caller needs to see: an operation nothing has concluded.
#: ``UNRESOLVED`` is included because an unresolved operation still needs an
#: operator or B's driver to come back to it — omitting it would make "not
#: concluded" invisible the moment a single re-check failed.
OPEN_STATES: frozenset[str] = frozenset(
    {OperationState.RECORDED.value, OperationState.UNRESOLVED.value}
)

# Identical for "no such workspace" and "not your workspace", so a caller cannot
# enumerate other tenants by reading refusals. Mirrors the observation receiver's
# `_UNKNOWN_SUBJECT` for the same reason.
_NOT_AUTHORIZED = "workspace not found or not in caller scope"
_UNKNOWN_OPERATION = "operation not found or not in caller scope"
# Reachable only for a key already recorded in a workspace the caller is
# authorized for, since the stored identity is (workspace, idempotency_key). It
# therefore reports the caller's own prior write and never another tenant's.
_ALREADY_RECORDED = "an operation with this idempotency key is already recorded"


class HandleRefused(Exception):
    """A request the receiver will not accept.

    Carries a caller-safe reason and the HTTP status the route should return.
    Raised rather than returned so no caller can proceed past a refusal by
    ignoring a result object.
    """

    def __init__(self, reason: str, status_code: int = 403) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


async def _authorize_workspace(
    db: AsyncSession,
    submitter: Submitter,
    workspace: str,
    *,
    status_code: int = 403,
) -> str:
    """Resolve immutable workspace/tenant identity before operation storage access."""
    try:
        identity = uuid.UUID(workspace)
    except (ValueError, TypeError, AttributeError):
        raise HandleRefused(_NOT_AUTHORIZED, status_code=status_code) from None
    if str(identity) != workspace or not authorize_read(submitter, workspace).allowed:
        raise HandleRefused(_NOT_AUTHORIZED, status_code=status_code)
    result = await db.execute(select(Workspace.org_id).where(Workspace.id == identity))
    org_id = result.scalar_one_or_none()
    if org_id is None:
        raise HandleRefused(_NOT_AUTHORIZED, status_code=status_code)
    return str(org_id)


async def _lock_allocation(
    db: AsyncSession,
    workspace: str,
    allocation_id: str,
    org_id: str,
    *,
    create: bool = False,
) -> ProviderAllocation:
    """All evidence writes lock this stable row before consulting live authority.

    Operations may be added during recovery, so locking the existing operation
    rows alone is insufficient. Atomic insert handles simultaneous first records
    without misreporting an allocation-key race as a duplicate operation.
    """
    if create:
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy.dialects.postgresql import insert
        elif db.get_bind().dialect.name == "sqlite":
            from sqlalchemy.dialects.sqlite import insert
        else:
            raise HandleRefused(
                "unsupported provider-evidence database", status_code=503
            )
        await db.execute(
            insert(ProviderAllocation)
            .values(
                workspace=workspace,
                allocation_id=allocation_id,
                org_id=org_id,
            )
            .on_conflict_do_nothing(index_elements=["workspace", "allocation_id"])
        )
    allocation = await db.get(
        ProviderAllocation,
        {"workspace": workspace, "allocation_id": allocation_id},
        with_for_update=True,
        populate_existing=True,
    )
    if allocation is None or allocation.org_id != org_id:
        raise HandleRefused(_UNKNOWN_OPERATION, status_code=404)
    return allocation


def _require_current_authority(expires_at: datetime) -> None:
    if expires_at <= datetime.now(UTC):
        raise HandleRefused("operation authority expired before the decision completed")


async def _verify_authority(
    authority: str,
    submitter: Submitter,
    handle: ProviderHandle,
    now: datetime,
) -> VerifiedProviderAuthority:
    if not authority or not authority.strip():
        raise HandleRefused("operation_authority must be nonblank", status_code=400)
    validator = get_provider_authority_validator()
    if validator is None:
        raise HandleRefused("B operation authority is unavailable", status_code=503)
    identity = replace(handle, provider_reference=None)
    try:
        binding = await validator.resolve(
            authority, submitter=submitter, handle=identity
        )
    except Exception:
        # Never expose the opaque authority or an upstream exception containing it.
        raise HandleRefused(
            "B operation authority is unavailable", status_code=503
        ) from None
    if (
        not isinstance(binding, VerifiedProviderAuthority)
        or binding.active is not True
        or binding.submitter_id != submitter.submitter_id
        or binding.handle != identity
        or not all(
            isinstance(value, str) and value.strip() and len(value) <= 255
            for value in (binding.operation_id, binding.run_id, binding.attempt_id)
        )
        or not isinstance(binding.expires_at, datetime)
        or binding.expires_at.tzinfo is None
        or binding.expires_at.utcoffset() is None
        or binding.expires_at <= max(now, datetime.now(UTC))
    ):
        raise HandleRefused("operation authority not active or not in caller scope")
    return binding


async def record_handle(
    db: AsyncSession,
    *,
    submitter: Submitter,
    handle: ProviderHandle,
    operation_authority: str,
    now: datetime | None = None,
) -> HandleRecord:
    """Persist a handle before its provider call, and return the durable record.

    The returned `HandleRecord` carries `durable=True` and the `confirmed_at`
    instant of the committed write. That is the whole contract of this function:
    the caller cannot obtain a record that `authorize_provider_call` will accept
    without a row having actually reached storage.

    Refuses a duplicate idempotency key with 409 rather than overwriting. An
    overwrite would erase the pre-call form a post-crash reconciliation needs, and
    silently accepting would hand out a second authorization for one operation.
    """
    org_id = await _authorize_workspace(db, submitter, handle.workspace)

    await _lock_allocation(
        db, handle.workspace, handle.allocation_id, org_id, create=True
    )
    recorded_at = now or datetime.now(UTC)
    binding = await _verify_authority(
        operation_authority, submitter, handle, recorded_at
    )
    row = ProviderOperation(
        org_id=org_id,
        authority_operation_id=binding.operation_id,
        authority_run_id=binding.run_id,
        authority_attempt_id=binding.attempt_id,
        idempotency_key=handle.idempotency_key,
        operation=handle.operation.value,
        provider=handle.provider,
        resource_name=handle.resource_name,
        allocation_id=handle.allocation_id,
        workspace=handle.workspace,
        # Absent by design: the provider has not answered yet. A column filled in
        # only after the call would put the whole record after the call.
        provider_reference=handle.provider_reference,
        state=OperationState.RECORDED.value,
        recorded_at=recorded_at,
    )
    db.add(row)
    try:
        await db.commit()
    except IntegrityError:
        # The primary-key conflict IS the duplicate detection — see the model's
        # docstring on why the idempotency key is the key. Rolled back so the
        # session stays usable for the caller's error path.
        await db.rollback()
        raise HandleRefused(_ALREADY_RECORDED, status_code=409) from None

    # `confirmed_at` is the committed instant, which is what the contract requires
    # and what the provider-truth report cites as evidence for the durability
    # claim. Timezone-aware: `HandleRecord` refuses a naive datetime.
    _require_current_authority(binding.expires_at)
    return HandleRecord(handle=handle, durable=True, confirmed_at=recorded_at)


def _handle_from_row(row: ProviderOperation) -> ProviderHandle:
    """Rebuild the contract handle from a stored row.

    Reconstructed rather than cached so reconciliation operates on exactly what
    was persisted — the form a crash left behind — instead of on whatever the
    current caller happens to have passed in.
    """
    return ProviderHandle(
        operation=OperationKind(row.operation),
        provider=row.provider,
        resource_name=row.resource_name,
        idempotency_key=row.idempotency_key,
        allocation_id=row.allocation_id,
        workspace=row.workspace,
        provider_reference=row.provider_reference,
    )


async def load_operation(
    db: AsyncSession,
    *,
    submitter: Submitter,
    workspace: str,
    idempotency_key: str,
    lock_for_update: bool = False,
) -> ProviderOperation:
    """Fetch one operation, identified by workspace and key, that the caller may see.

    The workspace is authorized *before* the lookup and then used as half of the
    stored identity, so a row outside the caller's grant is never loaded rather
    than loaded and then rejected. A caller who knows another tenant's idempotency
    key therefore gets the same answer as for a key that does not exist.

    Both "no such row" and "not in caller scope" raise the same 404 reason: a 403
    distinguishing them would confirm the operation exists, which is itself
    cross-tenant information.
    """
    org_id = await _authorize_workspace(db, submitter, workspace, status_code=404)

    # Keyed by name rather than as a tuple: a positional composite key silently
    # depends on the mapper's column order, so reordering the model would swap the
    # two values and look up a workspace as a key.
    row = await db.get(
        ProviderOperation,
        {"idempotency_key": idempotency_key, "workspace": workspace},
        populate_existing=True,
    )
    if row is None or row.org_id != org_id:
        raise HandleRefused(_UNKNOWN_OPERATION, status_code=404)
    if lock_for_update:
        await _lock_allocation(db, workspace, row.allocation_id, org_id)
        row = await db.get(
            ProviderOperation,
            {"workspace": workspace, "idempotency_key": idempotency_key},
            with_for_update=True,
            populate_existing=True,
        )
        if row is None or row.org_id != org_id:
            raise HandleRefused(_UNKNOWN_OPERATION, status_code=404)
    return row


#: Conclusions that assert nothing of the operation's resource is there. Each is
#: reached from provider-established absence — or, for `RETRY_PERMITTED` via a
#: `FAILED` outcome, from the provider's own refusal — and each authorizes either a
#: repeat or a release. A later observation that the resource *does* exist
#: contradicts them, and in the direction that costs money.
_ABSENCE_CONCLUSIONS: frozenset[str] = frozenset(
    {
        ReconcileResult.RETRY_PERMITTED.value,
        ReconcileResult.RECONCILED_ABSENT.value,
    }
)


def _identifies_operation(
    handle: ProviderHandle, observation: ProviderObservation
) -> bool:
    """True when the observation was queried by an identifier of this operation.

    The same membership test `reconcile` applies before it will draw any conclusion
    from an observation: a provider answer is about *this* operation only if the
    query used one of the operation's own identifiers. A confident answer obtained
    by querying something else is about a different resource, however certain it
    sounds.

    Duplicated here rather than imported because the contract does not export it as
    a predicate. It is applied to the same `handle` that is passed to `reconcile`,
    including any `provider_reference` this report supplied, so the two tests cannot
    disagree about which identifiers count — a report may legitimately query by the
    reference it is in the act of reporting.
    """
    return observation.queried_by in {
        handle.resource_name,
        handle.idempotency_key,
        handle.provider_reference,
    }


def _supersedes_conclusion(
    row: ProviderOperation,
    handle: ProviderHandle,
    observation: ProviderObservation | None,
    outcome: CallOutcome,
) -> bool:
    """Success or a provider reference invalidates a prior blind-retry decision.

    A reference alone cannot prove presence, but it disproves the assumption that
    nothing ran. Require an identifying absence check before permitting a repeat.
    Weaker reports cannot undo established existence.
    """
    if (row.reconcile_result or "") not in _ABSENCE_CONCLUSIONS:
        return False
    return (
        outcome is CallOutcome.SUCCEEDED
        or (
            handle.provider_reference is not None
            and handle.provider_reference != row.provider_reference
        )
        or (
            observation is not None
            and observation.presence is ProviderPresence.PRESENT
            and _identifies_operation(handle, observation)
        )
    )


async def _withdraw_absence_conclusion(
    db: AsyncSession,
    row: ProviderOperation,
    reason: str,
) -> None:
    """Rejecting a contradictory report must not leave blind retry enabled."""
    if row.reconcile_result in _ABSENCE_CONCLUSIONS:
        row.state = OperationState.UNRESOLVED.value
        row.reconcile_result = ReconcileResult.UNRESOLVED.value
        row.concluded_at = None
        row.detail = reason
        await db.commit()


async def conclude_operation(
    db: AsyncSession,
    *,
    submitter: Submitter,
    workspace: str,
    idempotency_key: str,
    outcome: CallOutcome,
    operation_authority: str,
    observation: ProviderObservation | None,
    provider_reference: str | None = None,
    now: datetime | None = None,
) -> tuple[ProviderOperation, bool, ReconcileResult]:
    """Establish what an operation's outcome was, and record it once.

    Returns the row, whether this call newly applied the conclusion, and the
    reconciliation result. `applied=False` means an identical conclusion was
    already recorded — the retry landed, it did not write twice.

    Idempotency is a *stored-state* check rather than a request-deduplication
    trick: a terminal operation short-circuits to its recorded conclusion instead
    of re-running `reconcile`. Re-deciding on every report would let a later,
    weaker observation (say, a provider that has since become unreachable)
    overwrite a conclusion already established from a good one.

    That short-circuit is **evidence-aware rather than state-only**, because the
    state-only form discarded the one report that costs money — see
    `_supersedes_conclusion`.

    It is also reached only *after* the supplied `provider_reference` is checked
    against the stored one, so a conflicting reference is refused rather than
    answered "already handled", and a reference this operation did not yet have is
    recorded rather than dropped. `applied=False` therefore means no *conclusion*
    was newly applied; it does not promise that no column changed.
    """
    row = await load_operation(
        db,
        submitter=submitter,
        workspace=workspace,
        idempotency_key=idempotency_key,
        # The terminal check and the conclusion write are one state transition.
        # Without a row lock, two recovery reports can both read RECORDED and
        # commit conflicting conclusions; whichever commits last wins even when
        # the other report established provider presence. Serializing on the
        # operation row makes the evidence-aware short-circuit effective under
        # concurrent delivery as well as sequential retries.
        lock_for_update=True,
    )

    # The reference is examined BEFORE the idempotency short-circuit, because it is
    # the one field of a repeat report that can carry information the stored row
    # does not already have. Deciding a report is a no-op before reading it made
    # two things go wrong at once, both losing track of a real provider resource:
    #
    #   * a *conflicting* reference was answered "already handled" and dropped, so
    #     an operation that had demonstrably run twice — the only thing two
    #     references can mean — reported the retry as harmless while the second
    #     resource kept running with nothing in our records naming it;
    #   * a *first* reference learned later was discarded, leaving the worst pair
    #     available: provider evidence the resource exists, and no identifier to
    #     release it by. Establishing presence without a reference is normal (a
    #     check can confirm a resource is there without returning its id), so the
    #     later, richer query is exactly how the identifier legitimately arrives.
    #
    # `with_provider_reference` is the contract's own comparison rather than an
    # equality test written here, so "two references for one operation is a
    # conflict" keeps one definition. A conflict never overwrites the reference.
    handle = _handle_from_row(row)
    binding = await _verify_authority(
        operation_authority, submitter, handle, now or datetime.now(UTC)
    )
    if (binding.operation_id, binding.run_id, binding.attempt_id) != (
        row.authority_operation_id,
        row.authority_run_id,
        row.authority_attempt_id,
    ):
        raise HandleRefused("operation authority does not match the recorded binding")
    if provider_reference is not None:
        try:
            handle = handle.with_provider_reference(provider_reference)
        except ContractViolation as violation:
            if not any(
                item.provider_reference == provider_reference for item in row.conflicts
            ):
                row.conflicts.append(
                    ProviderReferenceConflict(
                        workspace=row.workspace,
                        idempotency_key=row.idempotency_key,
                        provider_reference=provider_reference,
                        outcome=outcome.value,
                        provider_presence=observation.presence.value
                        if observation
                        else None,
                        provider_state=observation.provider_state
                        if observation
                        else None,
                        observation_queried_by=observation.queried_by
                        if observation
                        else None,
                    )
                )
            row.state = OperationState.UNRESOLVED.value
            row.reconcile_result = ReconcileResult.UNRESOLVED.value
            row.concluded_at = None
            row.detail = (
                "conflicting provider references require authoritative recovery"
            )
            await db.commit()
            raise HandleRefused(str(violation), status_code=409) from None

    if row.conflicts:
        # An ordinary outcome report cannot resolve extra resources. B must supply
        # an explicit multi-resource recovery contract before these can be cleared.
        return row, False, ReconcileResult.UNRESOLVED

    if (
        row.state == OperationState.CONCLUDED.value
        and row.reconcile_result
        and not _supersedes_conclusion(row, handle, observation, outcome)
    ):
        # Already established, and this report carries no stronger *evidence*. The
        # conclusion is not re-decided — that protection is the whole point of the
        # short-circuit, and a later weaker observation must not overwrite an
        # answer drawn from a good one.
        #
        # A reference this row did not have is still recorded, because it changes
        # no conclusion: the outcome stays exactly as established and only the
        # identifier the resource must be released by is filled in. A conflicting
        # reference never reaches here — it was refused above.
        if handle.provider_reference is not None and row.provider_reference is None:
            row.provider_reference = handle.provider_reference
            await db.commit()
        # `applied` stays False: no conclusion was newly applied. It reports on the
        # conclusion, not on whether any column changed.
        _require_current_authority(binding.expires_at)
        return row, False, ReconcileResult(row.reconcile_result)

    # A returned resource ID makes a claimed refusal ambiguous. The provider
    # must establish absence; a failure label alone no longer authorizes retry.
    effective_outcome = outcome
    if outcome is CallOutcome.FAILED and handle.provider_reference is not None:
        effective_outcome = CallOutcome.AMBIGUOUS
    try:
        request = ReconcileRequest(
            handle=handle,
            outcome=effective_outcome,
            operation_authority=operation_authority,
        )
    except ContractViolation as violation:
        # Contract shape validation is additional to live verification above.
        raise HandleRefused(str(violation), status_code=400) from None

    decision = reconcile(request, observation)

    if (
        observation is not None
        and observation.presence is ProviderPresence.PRESENT
        and decision.result.value in _ABSENCE_CONCLUSIONS
    ):
        # Keep rejecting a contradictory outcome, but withdraw any prior retry
        # permission. A refusal response alone cannot repair unsafe stored state.
        await _withdraw_absence_conclusion(
            db,
            row,
            "provider presence contradicts the reported failure; recovery required",
        )
        raise HandleRefused(
            "the reported outcome says the operation did not run, but the provider "
            "observation says the resource is present; report an ambiguous outcome "
            "with this observation instead of a failure",
            status_code=409,
        )

    # UNRESOLVED is retained, not closed: the provider could not be consulted (or
    # answered about the wrong resource), so nothing is established and the
    # operation stays findable. This is the branch that must never read as "done".
    if decision.result is ReconcileResult.UNRESOLVED:
        row.state = OperationState.UNRESOLVED.value
        row.concluded_at = None
    else:
        row.state = OperationState.CONCLUDED.value
        row.concluded_at = now or datetime.now(UTC)

    row.reconcile_result = decision.result.value
    row.detail = decision.reason
    if handle.provider_reference is not None:
        row.provider_reference = handle.provider_reference
    if decision.observation is not None:
        row.provider_presence = decision.observation.presence.value
        row.provider_state = decision.observation.provider_state
        row.observation_queried_by = decision.observation.queried_by

    await db.commit()
    _require_current_authority(binding.expires_at)
    return row, True, decision.result


async def list_unconcluded(
    db: AsyncSession,
    *,
    submitter: Submitter,
    workspace: str,
    allocation_id: str | None = None,
) -> list[ProviderOperation]:
    """Recover operations with unknown outcomes or outstanding provider resources.

    A concluded provider call can still own a live billable resource. Preserve its
    durable identity in recovery after a lost conclusion response, including a
    successful call that did not return a provider reference. Only conclusions
    establishing absence/refusal stop appearing. This query reports evidence;
    it does not authorize retry or close B/C lifecycle and accounting state.

    The query filters by the authorized workspace rather than narrowing results
    after fetching, so a row outside the caller's grant is never loaded.
    """
    org_id = await _authorize_workspace(db, submitter, workspace)

    query = select(ProviderOperation).where(
        ProviderOperation.workspace == workspace,
        ProviderOperation.org_id == org_id,
        or_(
            ProviderOperation.state.in_(sorted(OPEN_STATES)),
            ProviderOperation.reconcile_result
            == ReconcileResult.RECONCILED_EXISTS.value,
        ),
    )
    if allocation_id:
        query = query.where(ProviderOperation.allocation_id == allocation_id)
    result = await db.execute(query.order_by(ProviderOperation.recorded_at))
    return list(result.scalars().all())


async def assess_allocation_release(
    db: AsyncSession,
    *,
    submitter: Submitter,
    workspace: str,
    allocation_id: str,
    observations: dict[str, ProviderObservation],
    operation_authority: str,
) -> ReleaseAssessment:
    """Report what provider evidence establishes about releasing an allocation.

    A trusted B reader supplies complete fenced allocation membership, including
    independently billed compute/storage/network resources. Membership is persisted
    independently of operation rows and only grows. Missing/expired/incomplete
    inventory retains exposure; observations never define the expected set.

    Reports only. C owns the budget ledger; nothing here marks an allocation
    released or returns a reservation.
    """
    org_id = await _authorize_workspace(db, submitter, workspace)
    await _lock_allocation(db, workspace, allocation_id, org_id)

    result = await db.execute(
        select(ProviderOperation)
        .execution_options(populate_existing=True)
        .where(
            ProviderOperation.workspace == workspace,
            ProviderOperation.org_id == org_id,
            ProviderOperation.allocation_id == allocation_id,
        )
    )
    rows = list(result.scalars().all())
    if not rows:
        raise HandleRefused(_UNKNOWN_OPERATION, status_code=404)

    def unresolved(reason: str, resources: tuple[str, ...] = ()) -> ReleaseAssessment:
        return ReleaseAssessment(
            allocation_id=allocation_id,
            state=ReleaseState.UNRESOLVED,
            exposure=CostExposure.UNRESOLVED,
            unresolved_resources=resources
            or tuple(
                sorted(
                    {row.idempotency_key for row in rows}
                    | {
                        conflict.provider_reference
                        for row in rows
                        for conflict in row.conflicts
                    }
                )
            ),
            reason=reason,
        )

    if not operation_authority or not operation_authority.strip():
        raise HandleRefused("active allocation authority is required")
    report_digest = hashlib.sha256(
        json.dumps(
            {key: asdict(value) for key, value in observations.items()},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    reader = get_allocation_inventory_reader()
    if reader is None:
        return unresolved("authoritative complete allocation inventory is unavailable")
    try:
        inventory = await reader.read(
            submitter=submitter,
            workspace=workspace,
            allocation_id=allocation_id,
            operation_authority=operation_authority,
            report_digest=report_digest,
        )
    except Exception:
        return unresolved("authoritative complete allocation inventory is unavailable")
    if (
        not isinstance(inventory, VerifiedAllocationInventory)
        or inventory.workspace != workspace
        or inventory.org_id != org_id
        or inventory.allocation_id != allocation_id
        or inventory.executor_id != submitter.submitter_id
        or inventory.active is not True
        or inventory.attested_report_digest != report_digest
        or not isinstance(inventory.expires_at, datetime)
        or inventory.expires_at.tzinfo is None
        or inventory.expires_at.utcoffset() is None
        or inventory.expires_at <= datetime.now(UTC)
    ):
        raise HandleRefused(
            "allocation authority or provider-report attestation is not valid"
        )
    if (
        inventory.complete is not True
        or not isinstance(inventory.expires_at, datetime)
        or inventory.expires_at.tzinfo is None
        or inventory.expires_at.utcoffset() is None
        or inventory.expires_at <= datetime.now(UTC)
        or not isinstance(inventory.revision, str)
        or not inventory.revision.strip()
        or len(inventory.revision) > 255
        or not isinstance(inventory.resources, tuple)
        or any(
            not isinstance(item, AllocationResourceIdentity)
            or not isinstance(item.operation_keys, frozenset)
            or any(
                not isinstance(key, str) or not key.strip() or len(key) > 255
                for key in item.operation_keys
            )
            or any(
                not isinstance(value, str) or not value.strip() or len(value) > limit
                for value, limit in (
                    (item.resource_id, 255),
                    (item.provider, 64),
                    (item.provider_reference, 255),
                    (item.kind, 64),
                )
            )
            for item in inventory.resources
        )
    ):
        return unresolved(
            "allocation inventory is incomplete, expired or incorrectly bound"
        )
    if len({item.resource_id for item in inventory.resources}) != len(
        inventory.resources
    ):
        return unresolved("allocation inventory contains ambiguous resource identities")

    saved = list(
        (
            await db.execute(
                select(ProviderAllocationResource)
                .execution_options(populate_existing=True)
                .where(
                    ProviderAllocationResource.workspace == workspace,
                    ProviderAllocationResource.org_id == org_id,
                    ProviderAllocationResource.allocation_id == allocation_id,
                )
            )
        )
        .scalars()
        .all()
    )
    by_id = {item.resource_id: item for item in saved}
    for item in inventory.resources:
        prior = by_id.get(item.resource_id)
        if prior and (
            prior.provider,
            prior.provider_reference,
            prior.resource_kind,
        ) != (item.provider, item.provider_reference, item.kind):
            return unresolved(
                "allocation inventory changes a persisted resource identity"
            )
    for item in inventory.resources:
        if item.resource_id not in by_id:
            row = ProviderAllocationResource(
                workspace=workspace,
                org_id=org_id,
                allocation_id=allocation_id,
                resource_id=item.resource_id,
                provider=item.provider,
                provider_reference=item.provider_reference,
                resource_kind=item.kind,
                inventory_revision=inventory.revision,
                operation_keys=sorted(item.operation_keys),
            )
            db.add(row)
            by_id[item.resource_id] = row
        else:
            member = by_id[item.resource_id]
            member.operation_keys = sorted(
                set(member.operation_keys) | item.operation_keys
            )
    try:
        # Keep the allocation anchor locked until the permission-bearing decision
        # is derived. A commit here lets another writer overtake this snapshot.
        await db.flush()
    except IntegrityError:
        await db.rollback()
        return unresolved(
            "allocation inventory changed concurrently; repeat the assessment"
        )

    async def finish(assessment: ReleaseAssessment) -> ReleaseAssessment:
        _require_current_authority(inventory.expires_at)
        await db.commit()
        # A delayed commit must not return permission from expired authority.
        _require_current_authority(inventory.expires_at)
        return assessment

    known_references = {
        (member.provider, member.provider_reference) for member in by_id.values()
    }
    expected_references = {
        (row.provider, row.provider_reference) for row in rows if row.provider_reference
    } | {
        (row.provider, conflict.provider_reference)
        for row in rows
        for conflict in row.conflicts
    }
    missing_references = tuple(
        sorted(
            {
                reference
                for provider, reference in expected_references - known_references
            }
        )
    )
    if missing_references:
        return await finish(
            unresolved(
                "inventory omits a provider reference already recorded for the allocation",
                missing_references,
            )
        )

    unmapped_operations = tuple(
        sorted(
            row.idempotency_key
            for row in rows
            if (
                row.provider_reference is None
                and not (
                    row.state == OperationState.CONCLUDED.value
                    and row.reconcile_result in _ABSENCE_CONCLUSIONS
                )
                and not any(
                    row.idempotency_key in member.operation_keys
                    and member.provider == row.provider
                    and member.provider_reference
                    in {row.resource_name, row.idempotency_key}
                    for member in by_id.values()
                )
            )
        )
    )
    if unmapped_operations:
        return await finish(
            unresolved(
                "operations without provider references lack identifying inventory evidence",
                unmapped_operations,
            )
        )

    if not by_id:
        # An authoritative empty inventory is meaningful only alongside durable
        # proof that every attempted operation created no resource. Do not invent
        # a resource to satisfy the non-empty accounting membership contract.
        if observations or any(
            row.state != OperationState.CONCLUDED.value
            or row.reconcile_result not in _ABSENCE_CONCLUSIONS
            or row.provider_reference is not None
            or row.conflicts
            for row in rows
        ):
            return await finish(
                unresolved("empty inventory does not establish operation absence")
            )
        _require_current_authority(inventory.expires_at)
        return await finish(
            ReleaseAssessment(
                allocation_id=allocation_id,
                state=ReleaseState.RELEASED,
                exposure=CostExposure.NONE,
                reason="complete authoritative inventory and concluded operations establish no resources",
            )
        )

    # Membership only grows. A later snapshot omitting an already known disk or
    # network resource cannot erase its exposure. Translate only after verifying
    # the query used the persisted provider identity for that specific resource.
    evidence = {}
    for resource_id, observed in observations.items():
        member = by_id.get(resource_id)
        if member is not None and observed.queried_by == member.provider_reference:
            evidence[resource_id] = replace(observed, queried_by=resource_id)
        else:
            evidence[resource_id] = ProviderObservation(
                presence=ProviderPresence.UNKNOWN,
                queried_by=resource_id,
                detail="provider evidence does not match authoritative membership",
            )
    _require_current_authority(inventory.expires_at)
    try:
        assessment = assess_release(
            evidence,
            allocation=AllocationResources(
                allocation_id=allocation_id,
                resource_ids=frozenset(by_id),
            ),
        )
    except ContractViolation as violation:
        raise HandleRefused(str(violation), status_code=400) from None
    return await finish(assessment)


__all__ = [
    "OPEN_STATES",
    "HandleRefused",
    "OperationState",
    "ProviderPresence",
    "assess_allocation_release",
    "conclude_operation",
    "list_unconcluded",
    "load_operation",
    "record_handle",
]
