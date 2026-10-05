"""Human recovery of an outer timeout without repeating delivered development."""

import asyncio
import json
import os
from types import SimpleNamespace

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update

from src.shared.models.base import utcnow

from .models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
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


async def protected_assignment(session, *, node, request):
    """Recover a finished protected reviewer without re-running development."""
    from src.agentauth.bootstrap import BootstrapRefusedError
    from src.agentauth.engine import get_engine_authority_writer

    from .execution_store import load_execution
    from .handoff import identity_for_attempt
    from .policy_admission import load_in_force_policy
    from .review_cycle_dispatch import validate_continuation_assignment

    if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
        raise RecoveryRefusedError("protected_authority_required")
    flow = await session.scalar(
        select(OrchestrationFlow).where(OrchestrationFlow.org_id == node.org_id, OrchestrationFlow.id == node.flow_id).with_for_update()
    )
    if flow is None or flow.state not in {"pending", "running"}:
        raise RecoveryRefusedError("continued_flow_not_active")
    inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    if inputs.refusal or inputs.policy is None or inputs.plan_version != request.expected_plan_version or inputs.policy.expires_at <= utcnow():
        raise RecoveryRefusedError("accepted_policy_changed_or_expired")
    identity = await identity_for_attempt(session, org_id=node.org_id, node_id=node.id, attempt=request.expected_attempt)
    if identity is None or node.attempts != request.expected_attempt or identity.accepted_plan_version != inputs.plan_version:
        raise RecoveryRefusedError("report_assignment_changed")
    loaded = await load_execution(session, identity=identity)
    if loaded is None or loaded.record is None:
        raise RecoveryRefusedError("current_execution_changed")
    store = get_engine_authority_writer().store
    raw = await asyncio.to_thread(store._read, f"TENANT#{node.org_id}", f"EXEC#{request.expected_run_id}")
    if (
        not raw
        or raw.get("persona") != {"S": "agent-codex-reviewer"}
        or raw.get("orchestration_continuation_action") != {"S": "review"}
        or raw.get("status") != {"S": "completed"}
        or raw.get("terminal_outcome", {}).get("S") not in {"complete", "failed"}
    ):
        raise RecoveryRefusedError("reviewer_exit_unverified")
    try:
        grant = await asyncio.to_thread(
            store.live_grant, invocation_id=request.expected_run_id, tenant_id=node.org_id, attempt=int(raw["current_attempt"]["N"]), now=utcnow()
        )
        await validate_continuation_assignment(session, execution=raw, grant=grant, node=node)
    except (BootstrapRefusedError, KeyError, ValueError):
        raise RecoveryRefusedError("protected_assignment_changed") from None
    return SimpleNamespace(run_id=request.expected_run_id), loaded.record, identity


async def resume_continuation(session, *, org_id, node_id, actor_id, actor_role, request):
    """Restore only the current assignment after an engine-recorded timeout.

    This does not take over a claim, mark a worker finished or create a dispatch.
    The same run retains its start-once capability; normal outbox/runner checks
    still govern any subsequent work. A terminal failed protected reviewer is
    eligible for the existing bounded review retry; this operation neither
    supplies success nor increases its allowance.
    """
    node = await session.scalar(select(OrchestrationNode).where(OrchestrationNode.org_id == org_id, OrchestrationNode.id == node_id))
    if node is None:
        raise RecoveryRefusedError("node_not_found")
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    if plan is not None and (plan.plan_document or {}).get("execution_continuation") is None:
        row, execution, identity = await protected_assignment(session, node=node, request=request)
    else:
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
