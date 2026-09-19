"""Provider-handle recording and reconciliation endpoints — issue #5054 (U11c).

Routes:
    POST /internal/provider-operations                       — record a handle before the call
    POST /internal/provider-operations/{key}/conclude        — report the outcome (idempotent)
    GET  /internal/provider-operations                       — scoped recovery read
    POST /internal/provider-operations/allocations/{id}/release-assessment
                                                             — report release/exposure

## Why these are INTERNAL rather than user-facing

The caller is an adapter or B's recovery driver, never a browser. They authenticate
with the same workspace-scoped submitter credential the observation receiver uses
(`app/services/observations.py`'s configured submitter list) rather than a Cognito
token, because the authority needed is "this machine identity may speak for this
workspace" — which is what that credential expresses and what a user token does
not. Writes additionally require active B authority for the exact operation and
authenticated executor; the workspace credential alone is insufficient. Reusing it avoids a second machine-authentication path, which would be a
second place for a scoping bug to live.

Each route is registered in `app/endpoint_inventory.py` as INTERNAL. That is not
bookkeeping: the app-wide domain guard refuses any route with no recorded
authorization decision, so an unregistered route here fails closed rather than
shipping reachable.

## Why recording returns `confirmed_at`

The response carries the instant persistence committed, and the caller feeds it
into the contract's `HandleRecord` to obtain permission for the provider call. A
caller that cannot reach this endpoint gets no confirmation instant and therefore
no authorization — the failure mode is a refused call, not an unrecorded one.
"""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession
from superplane_contracts import (
    CallOutcome,
    ContractViolation,
    OperationKind,
    ProviderHandle,
    ProviderObservation,
    ProviderPresence,
    ReconcileResult,
    Submitter,
)

from app.database import get_session
from app.services import observations as observation_service
from app.services import provider_handles as handle_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"])


async def _authenticated_submitter(request: Request) -> Submitter:
    """Resolve the caller's credential to an authenticated submitter.

    Shares the observation receiver's configured submitter list, so a credential
    is granted the same workspaces for both surfaces and there is one place a
    grant is defined. Unlike the observation submit route, no HMAC body signature
    is required here: these routes take parsed JSON and their bodies are not
    re-serialized for signature verification.

    That difference is deliberate rather than an omission, and worth stating
    because `endpoint_inventory.py` records the observation routes as using
    "credential and HMAC signature" — a reader comparing the two surfaces should
    not have to guess. The signature there authenticates the *raw transmitted
    bytes* of a signed envelope and binds its caller-supplied `reported_at`
    freshness field, which is why that handler reads `await request.body()`. Here
    there is no caller-supplied instant to protect: the trust-bearing value
    (`confirmed_at`) is generated server-side from the committed write. Replay is
    inert by construction — replaying a record attempt conflicts, replaying a
    conclusion returns `applied=false` without writing, and the release assessment
    writes nothing at all. The other pre-existing internal *writes* (the
    observation event and lease routes) already authenticate by credential alone.
    """
    credential = (request.headers.get("authorization") or "").strip()
    resolver = observation_service.load_submitters()
    submitter = resolver.resolve(credential) if credential else None
    if submitter is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="unauthenticated: credential not accepted",
        )
    return submitter


class RecordHandleRequest(BaseModel):
    """A provider operation's identity, as chosen before the call.

    `workspace` is present because the caller must say which workspace the
    operation belongs to — but it is checked against the credential's grant, not
    trusted. Naming a workspace the credential does not cover is refused.

    There is no `provider_reference` field: it does not exist yet. A field that
    could only be filled in after the provider answers would put the whole record
    after the call, which is the ordering bug this story closes.
    """

    operation_authority: str = Field(min_length=1, max_length=4096)
    operation: OperationKind
    provider: str = Field(min_length=1, max_length=64)
    resource_name: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(min_length=1, max_length=255)
    allocation_id: str = Field(min_length=1, max_length=255)
    workspace: str = Field(min_length=1, max_length=255)


class HandleRecorded(BaseModel):
    """Proof the handle reached storage, and when.

    `confirmed_at` is what the contract's `HandleRecord` requires alongside
    `durable=True`. It comes from the committed write, so it cannot be produced by
    a caller asserting a boolean.
    """

    durable: bool
    confirmed_at: datetime
    idempotency_key: str
    state: str


class ObservationPayload(BaseModel):
    """What a query to the provider returned.

    `queried_by` records which identifier the query used, because a query by the
    wrong identity can be answered confidently and still be about the wrong
    resource. The contract refuses a conclusion whose observation does not identify
    the recorded operation.
    """

    presence: ProviderPresence
    queried_by: str = Field(min_length=1, max_length=255)
    provider_state: str | None = None
    detail: str = ""


class ConcludeRequest(BaseModel):
    """The outcome of a provider call, plus B's authority to act on it.

    `observation` is optional because an ambiguous outcome with no provider
    observation is a legitimate and important case: the contract answers it with
    `UNRESOLVED`, which authorizes nothing. Requiring one here would push callers
    toward inventing an observation to satisfy the schema.

    `workspace` completes the operation's stored identity alongside the key in the
    path, because an idempotency key is unique only within a workspace. It is
    checked against the credential's grant, so naming another tenant's workspace
    reaches nothing — the answer is the same 404 as for a key that does not exist.

    `provider_reference` must carry at least one non-whitespace character. A blank
    or whitespace-only value is a malformed request, not a conflicting reference:
    on this route 409 means "a *different* reference is already recorded", which a
    client must not retry past, so sending one for bad input would stop a driver
    from correcting and re-reporting. Rejected here as invalid input instead.
    """

    outcome: CallOutcome
    operation_authority: str = Field(min_length=1, max_length=4096)
    workspace: str = Field(min_length=1, max_length=255)
    observation: ObservationPayload | None = None
    provider_reference: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("provider_reference")
    @classmethod
    def _reference_is_not_blank(cls, value: str | None) -> str | None:
        """Reject a whitespace-only reference, matching the contract's own rule.

        `min_length` alone admits `"   "`, which the contract then refuses as a
        `ContractViolation` — and every violation from that call maps to 409, the
        one status that tells a caller its request can never succeed. Applying the
        same `strip()` test the contract applies keeps malformed input a 422.
        """
        if value is not None and not value.strip():
            raise ValueError(
                "provider_reference must contain at least one non-whitespace "
                "character when present"
            )
        return value


class ConcludeResponse(BaseModel):
    """The established conclusion and what it permits.

    `applied` is false for an idempotent retry — the conclusion was already
    recorded. `may_repeat_operation` is the only field a caller should consult
    before repeating an operation; it is true solely when the provider established
    absence, so an unresolved outcome cannot be read as permission to launch a
    replacement.
    """

    applied: bool
    result: str
    state: str
    may_repeat_operation: bool
    resources_unresolved: bool
    reason: str


class OperationSummary(BaseModel):
    """One unconcluded operation, as a recovery caller needs to see it."""

    idempotency_key: str
    operation: str
    provider: str
    resource_name: str
    allocation_id: str
    workspace: str
    provider_reference: str | None
    conflicting_provider_references: list[str]
    state: str
    reconcile_result: str | None
    recorded_at: datetime
    detail: str | None


class ReleaseAssessmentResponse(BaseModel):
    """What provider evidence establishes about releasing an allocation.

    `may_mark_released` and `may_return_reservation_unused` are separate because
    they are separate claims: a resource deleted but still inside a committed-spend
    window would permit the first and deny the second. A reports this; C owns the
    ledger and decides what to do with it.
    """

    state: str
    exposure: str
    allocation_id: str
    unresolved_resources: list[str]
    may_mark_released: bool
    may_return_reservation_unused: bool
    reason: str


def _observation_from(payload: ObservationPayload | None) -> ProviderObservation | None:
    """Build the contract's observation, surfacing its validation as a 400.

    The contract refuses a PRESENT observation with no provider state and an
    UNKNOWN one carrying a state or no reason. Those are client errors here rather
    than server faults, so they are translated instead of escaping as a 500.
    """
    if payload is None:
        return None
    try:
        return ProviderObservation(
            presence=payload.presence,
            queried_by=payload.queried_by,
            provider_state=payload.provider_state,
            detail=payload.detail,
        )
    except ContractViolation as violation:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(violation)
        ) from None


@router.post(
    "/provider-operations",
    response_model=HandleRecorded,
    status_code=status.HTTP_201_CREATED,
)
async def record_provider_handle(
    body: RecordHandleRequest,
    submitter: Submitter = Depends(_authenticated_submitter),
    db: AsyncSession = Depends(get_session),
) -> HandleRecorded:
    """Record a provider handle durably, before the call it identifies.

    Returns 201 with the confirmation instant, or 409 if this idempotency key is
    already recorded. A duplicate is refused rather than overwritten: overwriting
    would erase the pre-call form a post-crash reconciliation needs to find.
    """
    try:
        handle = ProviderHandle(
            operation=body.operation,
            provider=body.provider,
            resource_name=body.resource_name,
            idempotency_key=body.idempotency_key,
            allocation_id=body.allocation_id,
            workspace=body.workspace,
        )
    except ContractViolation as violation:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(violation)
        ) from None

    try:
        record = await handle_service.record_handle(
            db,
            submitter=submitter,
            handle=handle,
            operation_authority=body.operation_authority,
        )
    except handle_service.HandleRefused as refusal:
        raise HTTPException(
            status_code=refusal.status_code, detail=refusal.reason
        ) from None

    # `confirmed_at` is non-None for a durable record — `HandleRecord` refuses
    # `durable=True` without it.
    assert record.confirmed_at is not None
    return HandleRecorded(
        durable=record.durable,
        confirmed_at=record.confirmed_at,
        idempotency_key=handle.idempotency_key,
        state=handle_service.OperationState.RECORDED.value,
    )


@router.post(
    "/provider-operations/{idempotency_key}/conclude",
    response_model=ConcludeResponse,
)
async def conclude_provider_operation(
    idempotency_key: str,
    body: ConcludeRequest,
    submitter: Submitter = Depends(_authenticated_submitter),
    db: AsyncSession = Depends(get_session),
) -> ConcludeResponse:
    """Establish and record what a provider call's outcome actually was.

    Idempotent: re-reporting a conclusion returns it with `applied=false` and
    writes nothing. The exception is a report carrying provider-confirmed
    *existence* for an operation concluded as absent — that supersedes, because
    otherwise a resource the provider is holding stays invisible while the API
    keeps authorizing a replacement launch (see the service's
    `_supersedes_conclusion`).

    An ambiguous outcome with no provider observation resolves to `unresolved`,
    which permits no repeat and leaves the operation on the books.
    """
    observation = _observation_from(body.observation)
    try:
        row, applied, result = await handle_service.conclude_operation(
            db,
            submitter=submitter,
            workspace=body.workspace,
            idempotency_key=idempotency_key,
            outcome=body.outcome,
            operation_authority=body.operation_authority,
            observation=observation,
            provider_reference=body.provider_reference,
        )
    except handle_service.HandleRefused as refusal:
        raise HTTPException(
            status_code=refusal.status_code, detail=refusal.reason
        ) from None

    return ConcludeResponse(
        applied=applied,
        result=result.value,
        state=row.state,
        # Read from the contract's own predicate rather than recomputed here, so
        # "may I launch again?" has one definition.
        may_repeat_operation=result is ReconcileResult.RETRY_PERMITTED,
        resources_unresolved=result
        in (ReconcileResult.UNRESOLVED, ReconcileResult.RECONCILED_EXISTS),
        reason=row.detail or "",
    )


@router.get(
    "/provider-operations",
    response_model=list[OperationSummary],
)
async def list_provider_operations(
    workspace: str = Query(min_length=1, max_length=255),
    # `min_length=1` because an empty value would otherwise be accepted and then
    # skipped by the falsy check below, so a request asking to narrow to one
    # allocation would silently return every operation in the workspace — the
    # opposite of what it asked. Omit the parameter to mean "no filter".
    allocation_id: str | None = Query(default=None, min_length=1, max_length=255),
    submitter: Submitter = Depends(_authenticated_submitter),
    db: AsyncSession = Depends(get_session),
) -> list[OperationSummary]:
    """Recover unknown outcomes and concluded calls with outstanding resources.

    `workspace` is a required query parameter, not an optional filter. An absent
    selector would have to mean either "refuse" or "every workspace", and the
    second is a cross-tenant read — so the parameter is mandatory and its value is
    checked against the caller's grant.
    """
    try:
        rows = await handle_service.list_unconcluded(
            db, submitter=submitter, workspace=workspace, allocation_id=allocation_id
        )
    except handle_service.HandleRefused as refusal:
        raise HTTPException(
            status_code=refusal.status_code, detail=refusal.reason
        ) from None

    return [
        OperationSummary(
            idempotency_key=row.idempotency_key,
            operation=row.operation,
            provider=row.provider,
            resource_name=row.resource_name,
            allocation_id=row.allocation_id,
            workspace=row.workspace,
            provider_reference=row.provider_reference,
            conflicting_provider_references=[
                item.provider_reference for item in row.conflicts
            ],
            state=row.state,
            reconcile_result=row.reconcile_result,
            recorded_at=row.recorded_at,
            detail=row.detail,
        )
        for row in rows
    ]


class ReleaseAssessmentRequest(BaseModel):
    """Provider observations for an allocation's resources, keyed by resource id.

    The expected inventory is NOT taken from these keys — it comes from the stored
    independently persisted complete allocation membership. Operation names alone
    cannot clear compute, storage or network exposure. Missing inventory or an
    omitted resource remains unresolved.
    """

    operation_authority: str = Field(min_length=1, max_length=4096)
    workspace: str = Field(min_length=1, max_length=255)
    observations: dict[str, ObservationPayload]


@router.post(
    "/provider-operations/allocations/{allocation_id}/release-assessment",
    response_model=ReleaseAssessmentResponse,
)
async def assess_allocation_release(
    allocation_id: str,
    body: ReleaseAssessmentRequest,
    submitter: Submitter = Depends(_authenticated_submitter),
    db: AsyncSession = Depends(get_session),
) -> ReleaseAssessmentResponse:
    """Report what provider evidence establishes about releasing an allocation.

    Reports only — nothing here marks an allocation released or returns a
    reservation to a budget. Zero cost exposure is reachable only when a provider
    re-check established absence for every resource in the stored inventory.
    """
    observations = {
        name: observation
        for name, payload in body.observations.items()
        if (observation := _observation_from(payload)) is not None
    }
    try:
        assessment = await handle_service.assess_allocation_release(
            db,
            submitter=submitter,
            workspace=body.workspace,
            allocation_id=allocation_id,
            observations=observations,
            operation_authority=body.operation_authority,
        )
    except handle_service.HandleRefused as refusal:
        raise HTTPException(
            status_code=refusal.status_code, detail=refusal.reason
        ) from None

    return ReleaseAssessmentResponse(
        state=assessment.state.value,
        exposure=assessment.exposure.value,
        allocation_id=assessment.allocation_id,
        unresolved_resources=list(assessment.unresolved_resources),
        may_mark_released=assessment.may_mark_released,
        may_return_reservation_unused=assessment.may_return_reservation_unused,
        reason=assessment.reason,
    )
