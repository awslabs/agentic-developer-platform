"""Operator-plane gate approval and loop-resume controls (Issue #4213).

A gate is a place where a human decides whether work proceeds. Before this module
that decision lived in a comment and its effect was whatever the next agent
inferred from reading it. Here it becomes a real control: an authorized person
approves or rejects a gate, or resumes a stalled loop, and the engine records
**who decided what, when, and in which role** — permanently, in an append-only
row. That record is the point. "An agent cannot fake having been approved" is not
checkable unless something says a human approved it.

Endpoints (registered paths; browsers reach them under `/api/...`, which
CloudFront strips before the origin — see the prefix note in `routes.py`):

- ``POST /orchestration/gates/{gate_id}/approve`` — ``awaiting_gate -> passed``
- ``POST /orchestration/gates/{gate_id}/reject``  — ``awaiting_gate -> rejected_at_gate``
- ``POST /orchestration/nodes/{node_id}/resume``  — ``failed -> ready``, or the
  human-only ``halted -> ready``
- ``POST /orchestration/runs/{run_id}/{pause,steer,abort}`` — **declared seam
  only** (R-O3f), returning an explicit 501.

Shape decisions that are load-bearing, not stylistic:

**Approve/reject delegate to the input adapter; they are not a second
implementation.** ``adapters/github_comments.apply_gate_answer`` already performs
the whole authorized gate-answer sequence — permission check, the
"is it actually at a gate" narrowing guard, the state-conditional UPDATE, the
decision append, and the recording of refusals — and it was written to be called
by this route with ``input_path=InputPath.DASHBOARD``. Reimplementing it here
would produce two writers that agree today, which is precisely how the run-status
sets in ``activity/`` drifted three ways. The one thing this module supplies that
a comment cannot is the **already-authenticated Cognito identity**, so the
adapter's identity-resolution step is bypassed via
:func:`apply_gate_answer_for_context` rather than duplicated.

**No new permission.** Gate approval is gated on ``Permission.PLAN_APPROVE``
(`admin/config.py`), which #4200 minted for exactly this authority — writes into
orchestration promotion state — rather than reusing ``ORG_UPDATE``. The adapter
already gates on it and documents the check as "the same permission, at the same
strength, as the dashboard approve path". A second permission guarding the same
room would be a softer door into it, which is the same hole. It is already
registered in ``_ORG_SCOPED_PERMISSIONS``; a permission absent from that set is
evaluated **globally**, which for this one would mean cross-org gate approval.

**`actor_kind` is derived from the session, never from the request.** Every
request model here sets ``extra="forbid"``, so a body carrying ``actor_kind``
is rejected with a 422 rather than being quietly ignored — a service caller must
not be able to claim human actor kind, and the loudest possible failure is the
right one for an attempt to. ``ActorKind.HUMAN`` is passed positionally at each
``transition()`` call site and is never read off the wire.

**Resume is a human act, not a service action.** ``failed -> ready`` and
``halted -> ready`` are ``_HUMAN_ONLY`` edges in ``state.py``. That asymmetry is
what bounds the defect cycle: if the engine could clear its own halt, the cycle
bound it just exhausted would be bypassed and unbounded cycling would return. So
resume goes through ``transition()`` with the human actor kind from the
authenticated session, and the engine has no path to this code.

**Tenant isolation returns 404, never 403.** Gate and node ids resolve with an
``org_id`` filter, and a cross-org id is indistinguishable from a nonexistent
one: a 403 confirms the resource exists somewhere, which lets a caller enumerate
another org's flows by status code.

Requirements: AC-5 (approve + attributed decision row), AC-6
(``rejected_at_gate``, with a reason), AC-9 (resume, human-only halt override),
AC-15 / R-Q9c (human-only gate and recovery edges), AC-17
(``_ORG_SCOPED_PERMISSIONS``), R-N2b (rejections recorded), R-O3f (declared
seam).
"""

from __future__ import annotations

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import JSONResponse
from httpx import HTTPError
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.activity.control_schemas import ControlPingResponse, ControlStateResponse
from src.activity.control_service import ControlError, ControlService, validate_command_body
from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.budget.run_binding import RunBindingResolver
from src.shared.database import get_db
from src.shared.identity import resolve_canonical_user_id
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

from .adapters.github_comments import GateAnswerStatus, InputPath, apply_gate_answer_for_context
from .execution_state import BlockCode, BlockRecord
from .handoff import outstanding_block
from .lifecycle_recovery import RecoveryRefusedError, ResumeContinuationRequest, resume_continuation
from .models import DecisionKind, OrchestrationNode
from .repository import OrchestrationRepository
from .state import ActorKind, NodeState, transition

logger = logging.getLogger("bedrockgateway.orchestration.controls")

# Same prefix convention as `routes.py`: "/orchestration", NOT
# "/api/orchestration". CloudFront's strip-api-prefix viewer function removes the
# leading /api before the origin sees it, so a router mounted at /api/... 404s
# through the dashboard front door while working fine against the internal ALB
# (issue #4330). tests/test_route_prefix_convention.py guards this app-wide.
router = APIRouter(prefix="/orchestration", tags=["orchestration"])


# The states a resume may recover from, mapped to the decision kind that records
# it. Both edges land on READY and both are `_HUMAN_ONLY` in `state.py`.
#
# The two kinds differ because the two acts are different news to an auditor.
# Clearing a `halted` node overrides the defect-cycle bound that deliberately
# stopped further spend, so it is recorded as HALT_OVERRIDDEN (R-Q9c) and is
# findable as such; resuming a `failed` node is routine recovery from a stall or a
# failure, and reuses NODE_STALLED rather than inventing a kind that would need
# `kind` to grow a member for no new query.
_RESUMABLE_STATES: dict[str, DecisionKind] = {
    NodeState.FAILED.value: DecisionKind.NODE_STALLED,
    NodeState.HALTED.value: DecisionKind.HALT_OVERRIDDEN,
    NodeState.REJECTED_AT_GATE.value: DecisionKind.NODE_RESUMED,
    NodeState.AWAITING_MERGE.value: DecisionKind.NODE_RESUMED,
}


async def get_run_binding_resolver() -> RunBindingResolver:
    """The `webhook-events` row resolver that establishes whether a run has exited.

    A FastAPI dependency rather than a module global for the same reason as in
    `draft_routes.py`: a test injects a stub table instead of patching boto3. It
    matters more here, because this resolver is the *only* positive evidence
    `work_claims.force_handover` will accept that a legacy owner is gone — a
    resolver that could be replaced by a default would be a way to assert an exit
    nobody observed.
    """
    from src.shared.config import get_settings

    settings = get_settings()
    return RunBindingResolver(
        table_name=settings.webhook_events_table,
        aws_region=settings.aws_region,
        redis_url=settings.redis_url,
    )


async def _adopt_lane_for_resume(
    db: AsyncSession,
    *,
    node: OrchestrationNode,
    org_id: str,
    resolver: RunBindingResolver,
    reconciled: bool,
    actor_id: str,
    actor_role: str,
) -> BlockRecord | None:
    """Adopt the legacy lane holding this story, or return the block that refused it.

    This is the production caller of :func:`handoff.adopt_legacy_lane` (#5144). It
    hangs off resume rather than off a tick for a reason the issue states directly:
    adoption "is explicit and requires reconciliation", and the only actor who can
    attest that a prior owner's branches, comments and credentials were reconciled
    is the human already standing in front of this control. A scheduled pass has
    nobody to attest, so an engine-initiated adoption would have to either invent
    the attestation or default it true — and defaulting it true is exactly the
    weakening `force_handover`'s guards exist to prevent.

    Returns:
        `None` when there is nothing to adopt (the ordinary case: no legacy owner,
        or work claims disabled) **or** when the transfer succeeded. A
        `BlockRecord` when a lane was found and the transfer was refused — the
        caller persists it and does not report the story resumed. A refusal is
        never converted into "resumed anyway": that is the shape of the original
        defect, where unfinished work was reported as done.
    """
    from .handoff import AdoptionRefusedError, adopt_legacy_lane, adoption_enabled
    from .models import ClaimState, OrchestrationWorkClaim
    from .policy_admission import load_in_force_policy
    from .work_admission import enabled as work_claims_enabled
    from .work_claims import OwnerKind, WorkClaimError

    if not work_claims_enabled() or not adoption_enabled():
        # Both flags off is the deployed default. Read per call, so neither a test
        # nor a rollback depends on import order.
        return None

    issue = _issue_number(node.issue_ref)
    if issue is None:
        return None

    # The lane is found by the story's own issue and tenant, never by anything the
    # request supplies: a caller must not be able to name which claim gets taken
    # away. `DIRECT_DISPATCH` is the filter that makes this adoption of a *legacy*
    # lane — an `ENGINE_FLOW` claim is already the engine's and needs no transfer,
    # and taking one would let this control steal a live engine lane.
    candidates = (
        select(OrchestrationWorkClaim)
        .where(
            OrchestrationWorkClaim.org_id == org_id,
            OrchestrationWorkClaim.issue_number == issue,
            OrchestrationWorkClaim.owner_kind == OwnerKind.DIRECT_DISPATCH.value,
            OrchestrationWorkClaim.state == ClaimState.HELD.value,
        )
        .execution_options(populate_existing=True)
    )
    if await db.scalar(candidates.with_only_columns(OrchestrationWorkClaim.id).limit(1)) is None:
        return None

    # Claims are unique per repository, not per tenant-wide issue number. Use
    # the same trusted configured repository and tenant installation as dispatch.
    from .dispatch_pass import DispatchPassConfig, resolve_installation_id
    from .work_admission import resolve_repository_id

    try:
        repository = DispatchPassConfig.from_env().repo
        installation = await resolve_installation_id(db, org_id=org_id)
        if not repository or installation is None:
            raise WorkClaimError("repository_unresolved", "No trusted repository/installation binding is available.")
        repository_id = await resolve_repository_id(org_id=org_id, installation_id=installation, repo=repository)
    except (WorkClaimError, ValueError, HTTPError) as exc:
        return outstanding_block(
            BlockCode.AUTHORITY_UNVERIFIABLE,
            owner="platform-operator",
            required_input="resolve this tenant's configured repository identity before adopting its lane",
            detail=f"repository identity unavailable ({exc.code if isinstance(exc, WorkClaimError) else type(exc).__name__})",
        )
    claim = await db.scalar(candidates.where(OrchestrationWorkClaim.provider_repository_id == repository_id).with_for_update())
    if claim is None:
        return None

    # Server-resolved, both of them. `accepted_plan_version` comes from the plan in
    # force for this flow and `decision_id` from the latest approval on it, because
    # adoption is only legitimate under policy a human already accepted. Taking
    # either from the request body would let a caller manufacture the authority
    # that makes the transfer legal.
    inputs = await load_in_force_policy(db, org_id=org_id, flow_id=node.flow_id)
    if inputs.refusal is not None:
        # A policy exists and could not be read. Distinguished from absence for the
        # same reason as in `dispatch_pass`: falling through to a legacy-style
        # transfer on an unreadable policy is how policy-bound work loses its
        # restrictions.
        return outstanding_block(
            BlockCode.AUTHORITY_UNVERIFIABLE,
            owner="platform-operator",
            required_input="resolve the in-force execution policy for this flow before adopting its lane",
            detail=f"in-force policy could not be resolved: {inputs.refusal}",
        )

    if inputs.policy is None or not inputs.policy.policy_id or not inputs.policy.policy_hash:
        return outstanding_block(
            BlockCode.AUTHORITY_UNVERIFIABLE,
            owner="plan-owner",
            required_input="accept an execution policy before adopting this legacy lane",
            detail="an accepted plan alone does not authorize autonomous lane adoption",
        )

    decision_id = await _latest_approval_decision_id(db, org_id=org_id, flow_id=node.flow_id)
    if decision_id is None:
        return outstanding_block(
            BlockCode.AUTHORITY_UNVERIFIABLE,
            owner="platform-operator",
            required_input="accept a plan for this flow before adopting its legacy lane",
            detail="no approval decision authorizes a handover on this flow",
        )

    try:
        receipt = await adopt_legacy_lane(
            db,
            org_id=org_id,
            claim_id=claim.id,
            decision_id=decision_id,
            resolver=resolver,
            # The human's explicit attestation, required by the request model and
            # never defaulted. `force_handover` re-checks both.
            effects_reconciled=reconciled,
            credentials_reconciled=reconciled,
            accepted_plan_version=inputs.plan_version,
        )
    except AdoptionRefusedError as exc:
        # Every refusal arm lands here, including the two the issue names
        # explicitly: a prior owner still `live`, and one whose exit is
        # `unverifiable`. Mapped to a typed block naming a resolvable condition,
        # not prose — and emphatically not to a successful resume.
        logger.warning(
            "orchestration resume: refusing to adopt legacy lane %s for node %s: %s",
            claim.id,
            node.id,
            exc.code,
        )
        return outstanding_block(
            _ADOPTION_BLOCK_CODES.get(exc.code, BlockCode.AUTHORITY_UNVERIFIABLE),
            owner="platform-operator",
            required_input="confirm the prior owner has exited and its effects and credentials are reconciled",
            detail=f"legacy lane adoption refused ({exc.code}): {exc.message}",
        )

    logger.info(
        "orchestration resume: adopted legacy lane %s for node %s at generation %s by %s (%s)",
        claim.id,
        node.id,
        receipt.generation,
        actor_id,
        actor_role,
    )
    return None


# Which typed block a refusal reason routes to. `OWNERSHIP_LOST` for the liveness
# arms specifically: "the prior owner is still there" is an ownership fact an
# operator resolves differently from an unreadable policy, and collapsing the two
# into `AUTHORITY_UNVERIFIABLE` would send both to the same wrong runbook.
_ADOPTION_BLOCK_CODES: dict[str, BlockCode] = {
    "run_live": BlockCode.OWNERSHIP_LOST,
    "run_unverifiable": BlockCode.OWNERSHIP_LOST,
    "liveness_unavailable": BlockCode.PROVIDER_UNAVAILABLE,
    "liveness_unknown": BlockCode.OWNERSHIP_LOST,
    "claim_not_held": BlockCode.OWNERSHIP_LOST,
    "credentials_not_reconciled": BlockCode.CREDENTIAL_UNAVAILABLE,
}


def _issue_number(issue_ref: str | None) -> int | None:
    """The positive issue number `issue_ref` denotes, or None.

    Reuses `dispatch_pass`'s parser rather than re-deriving it, so the lane this
    control looks up is keyed exactly the way the producer keyed it when it claimed
    the issue. A second parser that disagreed on, say, a `#`-prefixed value would
    silently look up a different lane — or none.
    """
    from .dispatch_pass import issue_number_for_dispatch

    return issue_number_for_dispatch(issue_ref)


async def _latest_approval_decision_id(db: AsyncSession, *, org_id: str, flow_id: str) -> str | None:
    """The most recent approval decision on this flow, via `dispatch_pass`'s reader."""
    from .dispatch_pass import _latest_approval_decision_id as reader

    return await reader(db, org_id=org_id, flow_id=flow_id)


async def _record_adoption_block(
    repo: OrchestrationRepository,
    *,
    org_id: str,
    node: OrchestrationNode,
    block: BlockRecord,
    observed_state: str,
    actor_id: str,
    actor_role: str,
    reason: str | None,
) -> None:
    """Persist a refused adoption as attributed, queryable evidence (#5144).

    ``TRANSITION_REJECTED`` and a structured ``rejection_reason``, matching
    ``dispatch_pass._record_admission_refusal`` field for field — an operator
    filtering for #5144 blocks must find the dispatch-side and the resume-side
    refusals with one query, and two different shapes would mean whichever one the
    reader did not know about stays invisible.

    ``to_state`` is NULL: the node went nowhere. Recording ``ready`` here would read
    as a resume that happened and was then undone.
    """
    await repo.append_decision(
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        kind=DecisionKind.TRANSITION_REJECTED.value,
        actor_id=actor_id,
        actor_role=actor_role,
        # The human really did request this; the refusal is the engine's, but the
        # act being recorded is theirs, and `SERVICE` here would lose who asked.
        actor_kind=ActorKind.HUMAN.value,
        reason=reason,
        rejection_reason=json.dumps(
            {
                "issue": "5144",
                "block_code": block.code.value,
                "owner": block.owner,
                "required_input": block.required_input,
                "detail": block.detail,
            }
        ),
        from_state=observed_state,
        to_state=None,
    )


class GateDecisionRequest(BaseModel):
    """The body of an approve or reject.

    ``extra="forbid"`` is the security control, not tidiness: it is what makes a
    body carrying ``actor_kind`` (or ``actor_id``, or ``actor_role``) a 422
    instead of a field someone later decides to read. Attribution comes from the
    authenticated session only.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)
    # Optional revision binding (#5331). When present, the answer applies only if
    # this is still the plan in force for the gate's flow, compared inside the same
    # transaction that moves the node — so a plan amended between a client's read
    # and its approval cannot be the plan the approval landed against. Absent means
    # "no precondition", which is what every existing caller sends and what keeps
    # their behaviour identical.
    #
    # `min_length=1` so an explicitly empty string is a 422 at the edge rather than
    # a value the adapter has to interpret. The adapter ALSO fails closed on an
    # empty value, because it has a second caller (the GitHub comment path) that
    # does not go through this model, and a precondition that is only enforced by
    # whichever door you came in is not a precondition.
    expected_plan_hash: str | None = Field(default=None, min_length=1, max_length=64)


class ResumeRequest(BaseModel):
    """The body of a resume. Same ``extra="forbid"`` reasoning as above.

    AC-9 requires that a request attempting to assert ``actor_kind`` not be
    honoured. Forbidding the field is strictly stronger than ignoring it: an
    ignored field looks accepted to the caller, and a caller who believes they set
    the actor kind has been told something false.

    ``reconciled`` is the human's explicit attestation that a prior owner's
    outstanding effects **and** credentials have been accounted for (#5144). It
    defaults to ``False`` and is the one thing on this body that is read, because
    nothing on the server can observe it: a database fence cannot revoke a GitHub
    installation token that has already been issued, so the only honest source is
    the operator who checked. A default of ``True`` would hand every resume the
    attestation that makes a lane transfer legal, which is precisely the guard
    ``work_claims.force_handover`` exists to hold.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)
    reconciled: bool = Field(
        default=False,
        description="Attest that a prior owner's outstanding effects and credentials are reconciled. Required to adopt a legacy lane.",
    )


class GateDecisionResponse(BaseModel):
    """The outcome of a gate answer.

    ``decision_id`` is the append-only row this produced — the evidence the story
    exists to create. It is null only for ``already_answered``, where someone
    else's decision stands and writing a second row would make the trail claim
    the gate was answered twice.
    """

    model_config = ConfigDict(extra="forbid")

    node_id: str
    status: str
    state: str | None
    decision_id: str | None
    actor_kind: str | None
    message: str


class ResumeResponse(BaseModel):
    """The outcome of a resume."""

    model_config = ConfigDict(extra="forbid")

    node_id: str
    from_state: str
    state: str
    decision_id: str
    actor_kind: str


class DecisionResponse(BaseModel):
    """One append-only decision row, as an auditor reads it.

    The three attribution fields are separate on the wire for the same reason they
    are separate columns: ``tenant_access_requests.decided_by`` mixes real Cognito
    subs with synthetic values like ``system:org-member-match`` in one string, so
    "was this approved by a human?" is unanswerable from it. ``actor_kind`` is the
    discriminator that makes it answerable here.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    node_id: str | None
    kind: str
    actor_id: str
    actor_role: str
    actor_kind: str
    reason: str | None
    rejection_reason: str | None
    from_state: str | None
    to_state: str | None
    created_at: str


async def get_access_control(db: Annotated[AsyncSession, Depends(get_db)]) -> AccessControl:
    """Access control bound to the request's session."""
    return AccessControl(db)


# HTTP status per adapter outcome. A refusal that reveals nothing is a 404 and a
# refusal that reveals only the caller's own lack of authority is a 403; the
# mapping lives here rather than at each call site so the two cannot drift apart.
_GATE_STATUS_CODES: dict[GateAnswerStatus, int] = {
    GateAnswerStatus.APPLIED: 200,
    GateAnswerStatus.IDEMPOTENT_REPLAY: 200,
    GateAnswerStatus.ALREADY_ANSWERED: 409,
    GateAnswerStatus.REFUSED_ILLEGAL_TRANSITION: 409,
    GateAnswerStatus.REFUSED_NO_PERMISSION: 403,
    # 409, like the other two "the state is not what you thought" refusals: the
    # request was well-formed and authorized, and the plan of record moved. A 412
    # would also be defensible, but these three are the same class of answer to a
    # caller — re-read, then decide again — and giving one of them its own code
    # would split that handling for no gain.
    GateAnswerStatus.REFUSED_STALE_PLAN: 409,
    # 409 as well, and for the same reason: the request was well-formed and
    # authorized, and the state of the plan is not what the caller assumed — it
    # proposes authority that only a bound approval can grant. The caller's remedy is
    # the same shape too (re-read the plan, answer with its revision), so giving this
    # its own code would split handling a client should not split. The message names
    # the policy, which is what actually tells an operator what to do differently.
    GateAnswerStatus.REFUSED_UNBOUND_POLICY_GRANT: 409,
    # Both of these mean "no such gate for you", and they must stay
    # indistinguishable from each other and from a cross-org id.
    GateAnswerStatus.REFUSED_NOT_FOUND: 404,
    GateAnswerStatus.REFUSED_UNKNOWN_IDENTITY: 404,
}


async def _answer_gate(
    gate_id: str,
    *,
    approve: bool,
    reason: str | None,
    current_user: TokenContext,
    access: AccessControl,
    db: AsyncSession,
    expected_plan_hash: str | None = None,
) -> GateDecisionResponse:
    """Approve or reject one gate through the shared adapter.

    The permission check, the at-a-gate narrowing guard, the state-conditional
    UPDATE and the decision append all live in the adapter. This function's whole
    job is to hand it the authenticated context and translate the outcome into
    HTTP — which is what keeps the dashboard row and the GitHub-comment row the
    same shape produced by the same code.
    """
    outcome = await apply_gate_answer_for_context(
        db,
        context=current_user,
        node_id=gate_id,
        approve=approve,
        reason=reason,
        access=access,
        input_path=InputPath.DASHBOARD,
        expected_plan_hash=expected_plan_hash,
    )

    status_code = _GATE_STATUS_CODES[outcome.status]
    if status_code != 200:
        # Nothing is committed on a refusal path except the decision rows the
        # adapter deliberately appended (R-N2b), which the caller's transaction
        # still owns. Raising here rolls back nothing the adapter wanted kept
        # because the session is committed by the success path only — see below.
        if outcome.decision is not None:
            # An in-org but unauthorized (or illegal-transition) attempt is
            # evidence, and evidence that rolls back is not evidence. Commit it.
            await db.commit()
        raise HTTPException(status_code=status_code, detail=outcome.message)

    await db.commit()

    decision = outcome.decision
    return GateDecisionResponse(
        node_id=outcome.node_id,
        # A response-lost retry returns the original wire result. Internally the
        # adapter distinguishes the replay so callers cannot mistake it for a
        # second state change, but the client receives the same successful
        # envelope and decision id it would have received the first time.
        status=(GateAnswerStatus.APPLIED.value if outcome.status is GateAnswerStatus.IDEMPOTENT_REPLAY else outcome.status.value),
        state=decision.to_state if decision else None,
        decision_id=outcome.decision_id,
        actor_kind=decision.actor_kind.value if decision else None,
        message=outcome.message,
    )


@router.post("/gates/{gate_id}/approve", response_model=GateDecisionResponse)
async def approve_gate(
    gate_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: GateDecisionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GateDecisionResponse:
    """Approve a gate: ``awaiting_gate -> passed`` (AC-5).

    Requires ``PLAN_APPROVE`` scoped to the caller's own org. A gate id in another
    tenant returns **404**, identical to a nonexistent one.

    The decision row carries the acting SSO identity, the role held **at decision
    time**, and ``actor_kind="human"`` derived from the authenticated session — the
    attribution that makes the approval evidence rather than a convention.

    Pass ``expected_plan_hash`` to bind the approval to the revision you reviewed
    (#5331). It is compared against the plan in force inside the same transaction
    that moves the node, so an amendment landing between your read and this call
    refuses with **409** and approves nothing — the window a client-side re-read
    cannot close. Omit it and nothing about this route's behaviour changes.
    """
    return await _answer_gate(
        gate_id,
        approve=True,
        reason=body.reason,
        current_user=current_user,
        access=access,
        db=db,
        expected_plan_hash=body.expected_plan_hash,
    )


@router.post("/gates/{gate_id}/reject", response_model=GateDecisionResponse)
async def reject_gate(
    gate_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: GateDecisionRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> GateDecisionResponse:
    """Reject a gate: ``awaiting_gate -> rejected_at_gate`` (AC-6).

    The target state is ``rejected_at_gate``, **not** ``rejected`` — the latter is
    a frontend-only phantom with no backend writer that ``NodeState`` raises
    ``ValueError`` on. The distinction matters to an operator: a rejected gate
    leaves the node live and re-openable, and successors stay pending.

    The reason is recorded on the decision row.

    ``expected_plan_hash`` is honoured here too, and deliberately so: rejecting a
    revision you did not read is the same misattribution as approving one. A
    reviewer who rejects "the plan I was shown" should not have that recorded
    against a plan that has since been amended.
    """
    return await _answer_gate(
        gate_id,
        approve=False,
        reason=body.reason,
        current_user=current_user,
        access=access,
        db=db,
        expected_plan_hash=body.expected_plan_hash,
    )


@router.post("/nodes/{node_id}/resume-continuation")
async def resume_current_continuation(
    node_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: ResumeContinuationRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
):
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session

    from .review_cycle import CycleBlockedError

    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)
    try:
        human = await authorize_human_session(current_user, db)
    except BootstrapRefusedError:
        raise HTTPException(403, "An authenticated human plan approver is required.") from None
    try:
        result = await resume_continuation(
            db,
            org_id=human.tenant_id,
            node_id=node_id,
            actor_id=human.user_id,
            actor_role=(await access.get_user_role(current_user))[0].value,
            request=body,
        )
        await db.commit()
        return result
    except (RecoveryRefusedError, CycleBlockedError) as error:
        await db.rollback()
        raise HTTPException(404 if str(error) == "node_not_found" else 409, str(error)) from None


@router.post("/nodes/{node_id}/resume", response_model=ResumeResponse)
async def resume_node(
    node_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: ResumeRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
    run_bindings: Annotated[RunBindingResolver, Depends(get_run_binding_resolver)],
) -> ResumeResponse:
    """Resume a stalled or halted node: ``failed -> ready`` / ``halted -> ready`` (AC-9).

    Both edges are ``_HUMAN_ONLY`` in ``state.py``, and the ``actor_kind`` passed
    to ``transition()`` is hardcoded ``HUMAN`` **from the authenticated session** —
    never read from the body, which forbids the field outright. That is what keeps
    the defect-cycle bound intact: were a service caller able to claim human actor
    kind, clearing a halt would become a service action and unbounded cycling
    would return.

    Clearing a ``halted`` node records ``HALT_OVERRIDDEN`` specifically, so an
    auditor can find bound overrides without inferring them from ``from_state``.

    A node id in another tenant returns **404**.

    **Adopting a legacy lane (#5144).** A story held in ``awaiting_merge`` because
    its worker exited without committing a durable continuation receipt may still
    be owned by a pre-engine, directly-dispatched lane. Resuming such a story is
    the one moment adoption is both necessary and attributable, so this route is
    where :func:`handoff.adopt_legacy_lane` is actually called from. If the
    transfer is refused — the prior owner is still ``live``, its exit is
    ``unverifiable``, or the operator has not attested reconciliation — a typed
    block is persisted and the resume answers **409**. It does not resume the node
    anyway: reporting work resumed while its owner may still be running is the
    same class of lie as a worker exiting 0 with review outstanding.
    """
    await access.check_permission(current_user, Permission.PLAN_APPROVE, target_org_id=current_user.org_id)

    org_id = current_user.org_id
    repo = OrchestrationRepository(db)

    # Org-filtered resolution before anything else, so a cross-tenant node id can
    # never reach the transition. 404 rather than 403: a 403 confirms the id
    # exists somewhere.
    node = await repo.get_node(org_id=org_id, node_id=node_id)
    if node is None:
        raise HTTPException(status_code=404, detail=f"no orchestration node {node_id!r} in this tenant")

    observed_state = node.state
    actor_role = (await access.get_user_role(current_user))[0].value

    # Narrow *which* edge this control may request, before consulting the table.
    # `transition()` owns legality; this states that resume is only ever a
    # recovery from failed/halted. Without it, a legal-but-different edge (say
    # `pending -> ready`, which the engine takes when predecessors are satisfied)
    # would be reachable through a button labelled "resume".
    if observed_state not in _RESUMABLE_STATES:
        rejection_reason = f"node is in '{observed_state}'; only a failed, halted, rejected, or awaiting-merge node can be resumed"
        await repo.append_decision(
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=DecisionKind.TRANSITION_REJECTED.value,
            actor_id=current_user.user_id,
            actor_role=actor_role,
            actor_kind=ActorKind.HUMAN.value,
            reason=body.reason,
            rejection_reason=rejection_reason,
            from_state=observed_state,
            to_state=NodeState.READY.value,
        )
        await db.commit()
        raise HTTPException(status_code=409, detail=rejection_reason)

    # #5144: before promoting the node, make sure this engine actually owns the
    # lane. Placed *before* `transition()` for the same reason policy admission sits
    # before `dispatch_node`: a refusal must leave the node exactly where it was,
    # with nothing promoted. Running it after the UPDATE would mean a story reported
    # ready while a legacy owner might still be working it.
    block = await _adopt_lane_for_resume(
        db,
        node=node,
        org_id=org_id,
        resolver=run_bindings,
        reconciled=body.reconciled,
        actor_id=current_user.user_id,
        actor_role=actor_role,
    )
    if block is not None:
        await _record_adoption_block(
            repo,
            org_id=org_id,
            node=node,
            block=block,
            observed_state=observed_state,
            actor_id=current_user.user_id,
            actor_role=actor_role,
            reason=body.reason,
        )
        await db.commit()
        raise HTTPException(status_code=409, detail=block.required_input)

    result = transition(
        observed_state,
        NodeState.READY,
        # Derived from the authenticated session. Never from `body`.
        actor_kind=ActorKind.HUMAN,
        reason=body.reason or "",
    )

    if not result.allowed:  # pragma: no cover - unreachable while the guard above stands
        # Both resumable states have a human-legal edge to READY, so this cannot
        # fire today. Kept rather than asserted because `transition()` owns the
        # table: if a future edit narrows those edges, this must refuse and record
        # rather than proceed to the UPDATE on a refused transition.
        await repo.append_decision(
            org_id=org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=DecisionKind.TRANSITION_REJECTED.value,
            actor_id=current_user.user_id,
            actor_role=actor_role,
            actor_kind=ActorKind.HUMAN.value,
            reason=body.reason,
            rejection_reason=result.rejection_reason,
            from_state=observed_state,
            to_state=NodeState.READY.value,
        )
        await db.commit()
        raise HTTPException(status_code=409, detail=result.rejection_reason)

    # Conditional on the observed state, mirroring `_gate_transition` and
    # `dispatch.py::_dispatch_transition`: two operators resuming the same node at
    # once produce one transition, not two. `attempts` is deliberately NOT
    # incremented here — dispatch increments it when the node is actually
    # dispatched, and bumping it on resume would consume the defect-cycle bound
    # for work that has not run yet.
    stmt = (
        update(OrchestrationNode)
        .where(
            OrchestrationNode.id == node.id,
            OrchestrationNode.org_id == org_id,
            OrchestrationNode.state == observed_state,
        )
        .values(state=result.new_state.value, updated_at=utcnow())
    )
    rows = (await db.execute(stmt)).rowcount or 0
    if rows == 0:
        # Lost race: someone else resumed it between our read and our write. Their
        # decision row stands.
        await db.rollback()
        raise HTTPException(status_code=409, detail="this node was already resumed by a concurrent decision")

    decision = await repo.append_decision(
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        kind=_RESUMABLE_STATES[observed_state].value,
        actor_id=current_user.user_id,
        actor_role=actor_role,
        actor_kind=ActorKind.HUMAN.value,
        reason=body.reason,
        from_state=observed_state,
        to_state=result.new_state.value,
    )
    await db.commit()

    logger.info(
        "orchestration resume: node %s %s -> %s by %s",
        node.id,
        observed_state,
        result.new_state.value,
        current_user.user_id,
    )

    return ResumeResponse(
        node_id=node.id,
        from_state=observed_state,
        state=result.new_state.value,
        decision_id=decision.id,
        actor_kind=ActorKind.HUMAN.value,
    )


@router.get("/flows/{flow_id}/decisions", response_model=list[DecisionResponse])
async def list_flow_decisions(
    flow_id: Annotated[str, Path(min_length=1, max_length=36)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> list[DecisionResponse]:
    """The append-only decision log for one flow, oldest first.

    This is the read path that makes attribution *checkable* rather than merely
    stored — "who approved this gate" has no answer without it, and the story's
    own smoke test queries it.

    Gated on ``USAGE_READ``, not ``PLAN_APPROVE``: reading who approved something
    is a read, and requiring approval authority to audit approvals would mean only
    people who can approve can review. Both are in ``_ORG_SCOPED_PERMISSIONS``.
    A ``flow_id`` from another tenant returns **404**.
    """
    await access.check_permission(current_user, Permission.USAGE_READ, target_org_id=current_user.org_id)

    repo = OrchestrationRepository(db)

    flow = await repo.get_flow(org_id=current_user.org_id, flow_id=flow_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"no orchestration flow {flow_id!r} in this tenant")

    decisions = await repo.list_decisions(org_id=current_user.org_id, flow_id=flow.id)
    return [
        DecisionResponse(
            id=decision.id,
            node_id=decision.node_id,
            kind=decision.kind,
            actor_id=decision.actor_id,
            actor_role=decision.actor_role,
            actor_kind=decision.actor_kind,
            reason=decision.reason,
            rejection_reason=decision.rejection_reason,
            from_state=decision.from_state,
            to_state=decision.to_state,
            created_at=decision.created_at.isoformat(),
        )
        for decision in decisions
    ]


# --- Per-run live controls: adapters over the shared control service ---------
#
# Issue #3960 replaces the R-O3f hardcoded-501 seam with adapters over
# `activity/control_service.py`. The seam's requirement is *preserved*, not
# discarded: a control that appears to pause a run without doing so is worse than
# shipping nothing, so these still answer 501 for every verb — but now because
# the shared service reports the verb unsupported, after it has authenticated the
# caller, authorized tenant and owner, and checked the flag and the run's
# lifecycle. The difference matters on the day a verb is implemented: the
# authorization path these routes use is the one already under test, so enabling
# a verb is a change in one place rather than a new gate written here.
#
# `run_id` here is the invocation/event identifier Agent Activity uses — NOT an
# orchestration node id and never a pod name. An orchestration caller holding a
# node id must resolve its currently-bound invocation through the repository
# first; there is no path from an id of any other kind to a pod address
# (revival-design §2).
#
# Gate approval and loop-resume above keep their own semantics and permissions.
# They act on promotion state through the engine's state machine; these act on a
# live pod through a tenant-and-owner check. Sharing a permission between them
# would put a softer door into whichever room needs the stronger one.

_CONTROL_ACTIONS = ("pause", "resume", "steer", "abort")


class RunControlResponse(BaseModel):
    """The declared shape of a per-run control outcome (R-O3f).

    Retained field-for-field. Issue #3960 extends this contract additively
    through `activity/control_schemas.py::ControlCommandResponse`, which adds
    `command_id` and `command_status` while keeping `run_id`, `action` and
    `state` — so the shape declared here stays honoured rather than replaced.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    action: str
    state: str


def get_run_control_service() -> ControlService:
    """Provide the control service; overridden via dependency_overrides in tests.

    Deliberately the same class the activity routes resolve, so a test that
    proves the authorization contract on one adapter is proving it for the other.
    """
    return ControlService()


async def _run_control_identity(current_user: TokenContext, db: AsyncSession) -> tuple[str, str]:
    """Resolve the (canonical user id, tenant id) this adapter authorizes on.

    Identical to the activity adapter's resolution, and identical for the same
    reason: rows are keyed by the canonical `users.id` rather than the Cognito
    sub, and `org_id` is the authenticated-only tenant field. Reading
    `attributed_org_id` instead would let a caller nominate the tenant whose runs
    they may control.
    """
    canonical_user_id = await resolve_canonical_user_id(db, current_user.user_id, org_id=current_user.org_id)
    return canonical_user_id, current_user.org_id


async def _run_control(
    run_id: str,
    action: str,
    control: ControlService,
    current_user: TokenContext,
    db: AsyncSession,
    request: Request,
) -> JSONResponse:
    """Validate the body, apply the shared authorization gate, report the status.

    One helper for all four verbs so no verb can accidentally acquire a weaker
    check than its siblings — abort in particular, which is the most damaging
    one to get wrong on another tenant's run.

    The body validation is #3960 review finding F1. These routes previously
    declared no body parameter at all, so the 413 cap, the `extra="forbid"`
    rejection of `actor`/`target`/`token` and the UUID check ran only on the
    activity adapter. Harmless while no verb accepts a payload; a real hole the
    moment one does, on the adapter that had no body tests. Both adapters now
    call `control_service.validate_command_body`, which is also why it moved out
    of `activity/routes.py` — a shared authorization gate with a per-edge
    validation policy is still two policies.

    Validation precedes authorization here, matching the activity adapter, so
    the ordering W1-05 pins (400 outranks 501) holds identically on both.
    """
    try:
        validate_command_body(action, await request.body())
    except ControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.human_control import authorize_human_session

    user_id, tenant_id = await _run_control_identity(current_user, db)
    try:
        control.authorize_command(run_id, action, user_id=user_id, tenant_id=tenant_id)
        session = await authorize_human_session(current_user, db)
        result, status = await control.command(run_id, action, request_body=await request.body(), session=session)
        return JSONResponse(result.model_dump(), status_code=status, headers={"Cache-Control": "no-store"})
    except BootstrapRefusedError:
        raise HTTPException(status_code=404, detail="run not found") from None
    except ControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.post("/runs/{run_id}/pause")
async def pause_run(
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_run_control_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Pause a live run through the shared signed human control path."""
    return await _run_control(run_id, "pause", control, current_user, db, request)


@router.post("/runs/{run_id}/resume")
async def resume_run(
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_run_control_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Resume a paused run through the shared signed human control path.

    New in #3960: the original seam declared pause/steer/abort but not resume,
    which would have left the two adapters offering different verb sets. Note
    this is distinct from `POST /orchestration/nodes/{node_id}/resume` above —
    that clears a halted or failed *node* in the engine's state machine, and is
    human-only for reasons documented there. This one releases a live pod's pause
    barrier.
    """
    return await _run_control(run_id, "resume", control, current_user, db, request)


@router.post("/runs/{run_id}/steer")
async def steer_run(
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_run_control_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Steer a live run — authorized here, not yet implemented (501)."""
    return await _run_control(run_id, "steer", control, current_user, db, request)


@router.post("/runs/{run_id}/abort")
async def abort_run(
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
    request: Request,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_run_control_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """Abort a live run — authorized here, not yet implemented (501)."""
    return await _run_control(run_id, "abort", control, current_user, db, request)


@router.get("/runs/{run_id}/ping", response_model=ControlPingResponse)
async def ping_run(
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_run_control_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ControlPingResponse:
    """Check control reachability for a run — the same slice the activity route serves."""
    user_id, tenant_id = await _run_control_identity(current_user, db)
    try:
        return await control.ping(run_id, user_id=user_id, tenant_id=tenant_id)
    except ControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc


@router.get("/runs/{run_id}/state", response_model=ControlStateResponse)
async def get_run_control_state(
    run_id: Annotated[str, Path(min_length=1, max_length=128)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    control: Annotated[ControlService, Depends(get_run_control_service)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ControlStateResponse:
    """Read control state for a run — the same contract the activity route serves."""
    user_id, tenant_id = await _run_control_identity(current_user, db)
    try:
        return await control.get_state(run_id, user_id=user_id, tenant_id=tenant_id)
    except ControlError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
