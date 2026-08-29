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

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.base import utcnow
from src.shared.schemas.auth import TokenContext

from .adapters.github_comments import GateAnswerStatus, InputPath, apply_gate_answer_for_context
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
}


class GateDecisionRequest(BaseModel):
    """The body of an approve or reject.

    ``extra="forbid"`` is the security control, not tidiness: it is what makes a
    body carrying ``actor_kind`` (or ``actor_id``, or ``actor_role``) a 422
    instead of a field someone later decides to read. Attribution comes from the
    authenticated session only.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


class ResumeRequest(BaseModel):
    """The body of a resume. Same ``extra="forbid"`` reasoning as above.

    AC-9 requires that a request attempting to assert ``actor_kind`` not be
    honoured. Forbidding the field is strictly stronger than ignoring it: an
    ignored field looks accepted to the caller, and a caller who believes they set
    the actor kind has been told something false.
    """

    model_config = ConfigDict(extra="forbid")

    reason: str | None = Field(default=None, max_length=2000)


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
    GateAnswerStatus.ALREADY_ANSWERED: 409,
    GateAnswerStatus.REFUSED_ILLEGAL_TRANSITION: 409,
    GateAnswerStatus.REFUSED_NO_PERMISSION: 403,
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
        status=outcome.status.value,
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
    """
    return await _answer_gate(
        gate_id,
        approve=True,
        reason=body.reason,
        current_user=current_user,
        access=access,
        db=db,
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
    """
    return await _answer_gate(
        gate_id,
        approve=False,
        reason=body.reason,
        current_user=current_user,
        access=access,
        db=db,
    )


@router.post("/nodes/{node_id}/resume", response_model=ResumeResponse)
async def resume_node(
    node_id: Annotated[str, Path(min_length=1, max_length=36)],
    body: ResumeRequest,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    access: Annotated[AccessControl, Depends(get_access_control)],
    db: Annotated[AsyncSession, Depends(get_db)],
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
        rejection_reason = f"node is in '{observed_state}'; only a node in '{NodeState.FAILED.value}' or '{NodeState.HALTED.value}' can be resumed"
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


# --- R-O3f: declared seam, deliberately not implemented ----------------------
#
# Per-run pause / steer / abort is declared here as an interface and NOT
# implemented. The requirement is explicit that shipping a control which *appears*
# to pause a run without doing so is worse than shipping nothing: an operator who
# believes a run is paused stops watching it. So the seam answers 501 with a body
# that says so, and there is no code path here that touches run state.
#
# It is a real route rather than a comment because the contract is what the next
# story implements against, and an undeclared seam gets re-designed from scratch.

_NOT_IMPLEMENTED_DETAIL = "per-run pause/steer/abort is a declared interface only (R-O3f) and is not implemented; no run state was changed"


class RunControlResponse(BaseModel):
    """The declared shape of a per-run control outcome (R-O3f).

    Declared so the next story implements against a contract rather than
    inventing one. Nothing returns this yet — the seam returns 501.
    """

    model_config = ConfigDict(extra="forbid")

    run_id: str
    action: str
    state: str


@router.post("/runs/{run_id}/pause", status_code=501)
async def pause_run(
    run_id: Annotated[str, Path(min_length=1, max_length=64)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Declared seam only (R-O3f). Always 501 — never silently succeeds."""
    raise HTTPException(status_code=501, detail=_NOT_IMPLEMENTED_DETAIL)


@router.post("/runs/{run_id}/steer", status_code=501)
async def steer_run(
    run_id: Annotated[str, Path(min_length=1, max_length=64)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Declared seam only (R-O3f). Always 501 — never silently succeeds."""
    raise HTTPException(status_code=501, detail=_NOT_IMPLEMENTED_DETAIL)


@router.post("/runs/{run_id}/abort", status_code=501)
async def abort_run(
    run_id: Annotated[str, Path(min_length=1, max_length=64)],
    current_user: Annotated[TokenContext, Depends(get_current_user)],
) -> None:
    """Declared seam only (R-O3f). Always 501 — never silently succeeds."""
    raise HTTPException(status_code=501, detail=_NOT_IMPLEMENTED_DETAIL)
