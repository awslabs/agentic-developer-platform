"""Human recovery of an outer timeout without repeating delivered development."""

import json

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from src.shared.models.base import utcnow

from .models import DecisionKind, OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from .pr_bindings import active_binding_for_node, binding_scope_matches
from .run_reports import OrchestrationRunReport, RunReportError
from .shared_cycle import validate_current_report_assignment
from .shared_policy import shared_inputs
from .state import ActorKind, NodeState, transition


class ResumeContinuationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_attempt: int = Field(ge=1)
    expected_plan_version: int = Field(ge=1)
    expected_run_id: str = Field(min_length=1, max_length=255)
    reason: str = Field(min_length=10, max_length=2000)


class RecoveryRefusedError(ValueError):
    pass


async def resume_continuation(session, *, org_id, node_id, actor_id, actor_role, request):
    """Restore only the current assignment after an engine-recorded timeout.

    This does not take over a claim, mark a worker finished or create a dispatch.
    The same run retains its start-once capability; normal outbox/runner checks
    still govern any subsequent work. Real worker failures require normal retry.
    """
    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id))
    if node is None:
        raise RecoveryRefusedError("node_not_found")
    inputs, _ = await shared_inputs(session, org_id=org_id, flow_id=node.flow_id, lock=True)
    if inputs.plan_version != request.expected_plan_version or inputs.policy.expires_at <= utcnow():
        raise RecoveryRefusedError("accepted_policy_changed_or_expired")
    row = await session.get(OrchestrationRunReport, request.expected_run_id)
    if row is None or row.org_id != org_id or row.node_id != node_id or row.attempt != request.expected_attempt:
        raise RecoveryRefusedError("report_assignment_changed")
    if row.flow_id != node.flow_id or (row.terminal_receipt or {}).get("outcome") == "failed":
        raise RecoveryRefusedError("worker_failed")
    if row.expires_at.replace(tzinfo=utcnow().tzinfo) <= utcnow():
        raise RecoveryRefusedError("report_assignment_expired")
    try:
        execution, identity = await validate_current_report_assignment(session, row)
    except RunReportError as error:
        raise RecoveryRefusedError(error.code) from None
    claim = await session.scalar(select(OrchestrationWorkClaim).where(OrchestrationWorkClaim.id == identity.claim_id).with_for_update())
    node = await session.scalar(
        select(OrchestrationNode)
        .where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id)
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if (
        claim is None
        or claim.org_id != org_id
        or claim.state != "held"
        or claim.owner_kind != "engine_flow"
        or claim.owner_ref != node.flow_id
        or claim.generation != identity.claim_generation
        or claim.active_run_id != row.run_id
        or node.kind != "story"
        or node.attempts != request.expected_attempt
        or execution.status in {"concluded", "superseded"}
    ):
        raise RecoveryRefusedError("current_execution_changed")
    binding = await active_binding_for_node(session, org_id=org_id, node_id=node_id, attempt=node.attempts)
    if binding is None or not binding_scope_matches(binding, node):
        raise RecoveryRefusedError("bound_delivery_missing")
    last = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == org_id,
            OrchestrationDecision.node_id == node_id,
            OrchestrationDecision.to_state.is_not(None),
            OrchestrationDecision.rejection_reason.is_(None),
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    if node.state == "running" and last and last.kind == DecisionKind.NODE_RESUMED.value:
        return {"node_id": node_id, "run_id": row.run_id, "attempt": node.attempts, "resumed": False, "decision_id": last.id}
    if node.state != "failed" or last is None or last.kind != DecisionKind.NODE_STALLED.value or last.to_state != "failed":
        raise RecoveryRefusedError("not_an_outer_timeout")
    result = transition(node.state, NodeState.RUNNING, actor_kind=ActorKind.HUMAN, reason=request.reason)
    if not result.allowed:
        raise RecoveryRefusedError(result.rejection_reason)
    await session.execute(
        update(OrchestrationNode)
        .where(
            OrchestrationNode.org_id == org_id,
            OrchestrationNode.id == node_id,
            OrchestrationNode.state == "failed",
            OrchestrationNode.attempts == request.expected_attempt,
        )
        .values(state=result.new_state.value, updated_at=utcnow())
    )
    decision = OrchestrationDecision(
        org_id=org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        kind=DecisionKind.NODE_RESUMED.value,
        actor_id=actor_id,
        actor_role=actor_role,
        actor_kind=ActorKind.HUMAN.value,
        from_state="failed",
        to_state="running",
        reason=json.dumps(
            {
                "mode": "same_assignment_continuation",
                "reason": request.reason,
                "run_id": row.run_id,
                "attempt": node.attempts,
                "execution_id": execution.id,
                "plan_version": identity.accepted_plan_version,
                "claim_id": identity.claim_id,
                "claim_generation": identity.claim_generation,
                "binding_id": binding.id,
            }
        ),
    )
    session.add(decision)
    await session.flush()
    return {"node_id": node_id, "run_id": row.run_id, "attempt": node.attempts, "resumed": True, "decision_id": decision.id}
