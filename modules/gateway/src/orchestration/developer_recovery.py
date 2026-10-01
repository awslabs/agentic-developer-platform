"""Retry settled developer failures within the accepted flow's repair allowance.

This pass only makes work eligible. Normal dispatch still resolves identity,
claims, policy/budget, PR truth and model configuration before consuming an
attempt. It never overrides a halt or restores a live continuation.
"""

import json
import logging
import time
from datetime import UTC, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import or_, select

from src.shared.models.base import utcnow

from .developer_personas import DEVELOPER_PERSONAS
from .dispatch_pass import attempt_run_id
from .execution_policy import Action
from .flow_execution import flow_is_paused
from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode, OrchestrationWorkClaim
from .policy_admission import load_in_force_policy
from .results import protected_failure_for_assignment
from .run_reports import OrchestrationRunReport, run_result_for_assignment
from .runtime_policy import flow_started_at
from .state import ActorKind, NodeState, transition

logger = logging.getLogger(__name__)
ACTOR = "system:developer-recovery"
KIND = "developer_retry_scheduled"
CHECKED = "developer_retry_checked"


def recovery_id(node_id, attempt):
    return str(uuid5(NAMESPACE_URL, f"developer-retry:{node_id}:{attempt}"))


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def retry_context(session, node, previous_attempt):
    row = await session.get(OrchestrationDecision, recovery_id(node.id, previous_attempt))
    if row and row.org_id == node.org_id and row.node_id == node.id and row.kind == KIND and row.actor_id == ACTOR and row.actor_kind == "service":
        return json.loads(row.reason)
    return None


async def recover_developer(session, node, *, now):
    """Called under the node lock; all exits leave the old attempt count intact."""
    if node.kind != "story" or node.state != "failed" or node.attempts < 1:
        return "not_a_failed_developer"
    # An operator's later recovery/halt must never be mistaken for worker failure.
    event = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.to_state == "failed",
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    if event is None or event.kind != "result_observed" or event.actor_id != "system:orchestration-results" or event.actor_kind != "service":
        return "failure_requires_operator"
    failure = json.loads(event.reason or "{}")
    run_id = attempt_run_id(node.id, node.attempts)
    if failure.get("attempt") != node.attempts or failure.get("run_id") != run_id or not failure.get("failed_execution_id"):
        return "failure_not_settled"
    # Backoff uses durable failure time, not process memory or another attempt.
    retry_at = aware(event.created_at) + timedelta(seconds=min(60 * 2 ** min(node.attempts - 1, 5), 1800))
    flow = await session.scalar(
        select(OrchestrationFlow)
        .where(
            OrchestrationFlow.id == node.flow_id,
            OrchestrationFlow.org_id == node.org_id,
        )
        .with_for_update()
    )
    if flow is None or flow.state not in {"pending", "running"} or await flow_is_paused(session, org_id=node.org_id, flow_id=node.flow_id, lock=True):
        return "flow_not_active"
    inputs = await load_in_force_policy(session, org_id=node.org_id, flow_id=node.flow_id)
    policy = inputs.policy
    if inputs.refusal or policy is None:
        return "policy_unavailable"
    if policy.expires_at <= now:
        return "policy_expired"
    if Action.REPAIR not in policy.allowed_actions or Action.REPAIR in policy.human_gates:
        return "repair_requires_approval"
    if node.attempts >= policy.limits.max_attempts_per_node:
        return "attempts_exhausted"
    started = await flow_started_at(session, org_id=node.org_id, flow_id=node.flow_id)
    if started is not None and (now - aware(started)).total_seconds() >= policy.limits.max_wall_clock_seconds:
        return "flow_deadline_exhausted"
    execution = await session.get(OrchestrationExecution, failure["failed_execution_id"])
    if (
        execution is None
        or execution.org_id != node.org_id
        or execution.node_id != node.id
        or execution.flow_id != node.flow_id
        or execution.cycle != node.attempts
        or execution.accepted_plan_version != inputs.plan_version
        or execution.status != "concluded"
        or execution.phase != "concluded"
        or execution.pending_action_key
    ):
        return "execution_not_settled"
    claim = await session.scalar(
        select(OrchestrationWorkClaim)
        .where(
            OrchestrationWorkClaim.id == execution.claim_id,
            OrchestrationWorkClaim.org_id == node.org_id,
        )
        .with_for_update()
    )
    if (
        claim is None
        or claim.generation != execution.claim_generation
        or claim.state != "released"
        or claim.release_reason != "failed"
        or claim.active_run_id is not None
        or claim.claim_event_id != run_id
        or claim.owner_kind != "engine_flow"
        or claim.owner_ref != node.flow_id
    ):
        return "work_owner_changed"
    successor = await session.scalar(
        select(OrchestrationAction.id)
        .where(
            OrchestrationAction.org_id == node.org_id,
            OrchestrationAction.execution_id == execution.id,
            or_(OrchestrationAction.kind == "review_cycle_dispatch", OrchestrationAction.status.in_(["prepared", "dispatched", "unknown"])),
        )
        .limit(1)
    )
    if successor:
        return "continuation_requires_reconciliation"
    assignment = await session.get(OrchestrationRunReport, run_id)
    details = {}
    if assignment is not None:
        if assignment.persona not in DEVELOPER_PERSONAS:
            return "not_a_failed_developer"
        details = (assignment.terminal_receipt or {}).get("failure") or {}
        if (
            details.get("category") in {"policy", "provider_refusal", "contract", "cancelled"}
            or details.get("retryable") is False
            or (assignment.block_code and not assignment.retryable)
        ):
            return "failure_requires_operator"
        terminal = await run_result_for_assignment(session, node=node, dispatch={"run_id": run_id, "attempt": node.attempts})
    else:
        terminal = await protected_failure_for_assignment(node=node, dispatch={"run_id": run_id})
    if not terminal or terminal.get("status") != "failed" or terminal.get("terminal_outcome", "failed") != "failed":
        return "worker_exit_unverified"
    if assignment is None and terminal.get("persona") not in DEVELOPER_PERSONAS:
        return "not_a_failed_developer"
    if now < retry_at:
        return "retry_backoff"
    # The result observer's settled execution and released exact-generation claim
    # are durable exit evidence. An advisory activity row can never enter here.
    decision = transition(
        node.state,
        NodeState.READY,
        actor_kind=ActorKind.SERVICE,
        reason="Retry the settled developer failure within the accepted repair allowance.",
        developer_retry_authorized=True,
    )
    if not decision.allowed:
        raise ValueError(decision.rejection_reason)
    data = {
        "previous_run_id": run_id,
        "previous_attempt": node.attempts,
        "failure_decision_id": event.id,
        "execution_id": execution.id,
        "plan_version": inputs.plan_version,
        "retry_at": retry_at.isoformat(),
        "reason": "developer_failed",
        "failure_category": details.get("category", "unknown"),
        "previous_exit_code": details.get("exit_code"),
        "preserve_existing_work": True,
    }
    session.add(
        OrchestrationDecision(
            id=recovery_id(node.id, node.attempts),
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind=KIND,
            actor_id=ACTOR,
            actor_role="engine",
            actor_kind="service",
            from_state="failed",
            to_state="ready",
            reason=json.dumps(data),
        )
    )
    node.state = "ready"
    node.updated_at = now
    await session.flush()
    return "scheduled"


async def recover_failed_developers(session):
    """Bounded, fair sweep, including failures already settled before deployment."""
    from .execution_runner import RunnerConfig

    if not RunnerConfig.from_env().enabled:
        return 0
    deadline = time.monotonic() + 15
    # A per-node cursor keeps blocked old failures from starving eligible work.
    from sqlalchemy import func

    cursor = (
        select(OrchestrationDecision.node_id, func.max(OrchestrationDecision.created_at).label("checked_at"))
        .where(OrchestrationDecision.kind == CHECKED)
        .group_by(OrchestrationDecision.node_id)
        .subquery()
    )
    candidates = (
        await session.scalars(
            select(OrchestrationNode.id)
            .outerjoin(cursor, cursor.c.node_id == OrchestrationNode.id)
            .where(
                OrchestrationNode.kind == "story",
                OrchestrationNode.state == "failed",
            )
            .order_by(cursor.c.checked_at.asc().nullsfirst(), OrchestrationNode.id)
            .limit(20)
        )
    ).all()
    recovered = 0
    for node_id in candidates:
        if time.monotonic() >= deadline:
            break
        scope = None
        try:
            async with session.begin_nested():
                node = await session.scalar(
                    select(OrchestrationNode)
                    .where(OrchestrationNode.id == node_id)
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if node is None or node.state != "failed":
                    continue
                scope = {"org_id": node.org_id, "flow_id": node.flow_id, "node_id": node.id}
                attempt = node.attempts
                result = await recover_developer(session, node, now=utcnow())
                recovered += int(result == "scheduled")
                session.add(
                    OrchestrationDecision(
                        org_id=node.org_id,
                        flow_id=node.flow_id,
                        node_id=node.id,
                        kind=CHECKED,
                        actor_id=ACTOR,
                        actor_role="engine",
                        actor_kind="service",
                        reason=json.dumps({"attempt": node.attempts, "result": result}),
                    )
                )
        except Exception:
            logger.exception("Developer recovery unavailable node=%s", node_id)
            if scope is not None:
                session.add(
                    OrchestrationDecision(
                        **scope,
                        kind=CHECKED,
                        actor_id=ACTOR,
                        actor_role="engine",
                        actor_kind="service",
                        reason=json.dumps({"attempt": attempt, "result": "recovery_evidence_unavailable"}),
                    )
                )
    return recovered
