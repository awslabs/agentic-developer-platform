"""Retry settled developer failures and unverified operations runs within the accepted allowance.

This pass only makes work eligible. Normal dispatch still resolves identity,
claims, policy/budget, PR truth and model configuration before consuming an
attempt. It never overrides a halt or restores a live continuation.
"""

import asyncio
import json
import logging
import time
from datetime import UTC, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import or_, select

from src.shared.models.base import utcnow

from .developer_personas import DEVELOPER_PERSONAS, OPERATIONS_PERSONAS
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


def recovery_id(node_id, attempt, *, operations=False):
    return str(uuid5(NAMESPACE_URL, f"{'operations' if operations else 'developer'}-retry:{node_id}:{attempt}"))


def aware(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


async def retry_context(session, node, previous_attempt):
    for operations in (True, False):
        row = await session.get(OrchestrationDecision, recovery_id(node.id, previous_attempt, operations=operations))
        if (
            row
            and row.org_id == node.org_id
            and row.node_id == node.id
            and row.kind == ("operations_retry_scheduled" if operations else KIND)
            and row.actor_id == ("system:operations-recovery" if operations else ACTOR)
            and row.actor_kind == "service"
        ):
            return json.loads(row.reason)
    from .operations_agent_recovery import retry_context as closed_controller_context
    from .operations_resume_retry import retry_context as resumed_operations_context

    return await closed_controller_context(session, node, previous_attempt) or await resumed_operations_context(session, node, previous_attempt)


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
    if (
        event is None
        or event.actor_kind != "service"
        or not (
            (event.kind == "result_observed" and event.actor_id == "system:orchestration-results")
            or (event.kind == "node_stalled" and event.actor_id == "system:orchestration-stall-detector")
        )
    ):
        return "failure_requires_operator"
    try:
        failure = json.loads(event.reason or "{}")
    except (TypeError, ValueError):
        return "failure_requires_operator"
    stall = event.kind == "node_stalled"
    # The results pass settled the attempt on its released work claim (#7727): the
    # worker released it as failed through its own claim credentials, or the engine
    # released it as abandoned once the pod was proven gone. No terminal receipt
    # exists or can arrive; the claim row re-verified below is the exit evidence.
    released = event.kind == "result_observed" and failure.get("status_source") == "claim_released"
    if stall and failure.get("fence") == "lease_lapsed_stall":
        # The stall detector fenced an acknowledged worker whose lease lapsed a full
        # period ago and released its claim as abandoned: the same exit evidence as a
        # claim-released settlement, re-verified on the claim row below.
        stall, released = False, True
    if stall and failure.get("fence") != "unacknowledged_stall":
        return "failure_requires_operator"
    runtime_unverified = failure.get("operations_retry") is True and failure.get("hold_reason") == "evidence_missing"
    run_id = attempt_run_id(node.id, node.attempts)
    if failure.get("attempt") != node.attempts or failure.get("run_id") != run_id or not failure.get("failed_execution_id"):
        return "failure_not_settled"
    if stall and (failure.get("claim_id") is None or type(failure.get("claim_generation")) is not int):
        return "failure_requires_operator"
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
    execution = await session.get(OrchestrationExecution, failure["failed_execution_id"])
    if (
        execution is None
        or execution.org_id != node.org_id
        or execution.node_id != node.id
        or execution.flow_id != node.flow_id
        or execution.cycle != node.attempts
        or execution.status != "concluded"
        or execution.phase != "concluded"
        or execution.pending_action_key
        or (
            stall
            and (
                execution.claim_id != failure["claim_id"]
                or execution.claim_generation != failure["claim_generation"]
                or execution.handoff_receipt_ref is not None
            )
        )
    ):
        return "execution_not_settled"
    from .plan_lineage import execution_plan_matches

    # The failed attempt was admitted under an earlier version; an amendment that
    # preserved this node does not make its settlement stale (#7535).
    if not await execution_plan_matches(
        session,
        org_id=node.org_id,
        flow_id=node.flow_id,
        node_id=node.id,
        version=execution.accepted_plan_version,
        current_version=inputs.plan_version,
    ):
        return "execution_not_settled"
    assignment = await session.get(OrchestrationRunReport, run_id)
    from .lost_operations_worker import settled_lost_worker

    lost = (
        not stall
        and not runtime_unverified
        and await settled_lost_worker(session, node=node, execution=execution, report=assignment, attempt=node.attempts)
    )
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
        # The operations retry settles a run that finished cleanly but left no
        # verifiable runtime evidence; its worker released the claim as completed,
        # and that release still names the run as the last owner (#7826).
        or claim.release_reason
        not in (({"failed", "abandoned"} if released or lost else {"failed"}) | ({"completed"} if runtime_unverified else set()))
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
    # A terminal native workflow can outlive its worker in the action ledger.
    # Defer its reconciliation until all ordinary retry/assignment checks pass.
    native_cancellation = False
    if successor and runtime_unverified:
        unresolved = await session.get(OrchestrationAction, successor)
        native_cancellation = unresolved is not None and unresolved.kind == "operations_native_release_dispatch"
    if successor and not native_cancellation:
        return "continuation_requires_reconciliation"
    if stall:
        prior = await session.scalars(
            select(OrchestrationDecision).where(
                OrchestrationDecision.org_id == node.org_id,
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.kind == KIND,
                OrchestrationDecision.actor_id == ACTOR,
                OrchestrationDecision.actor_kind == "service",
            )
        )
        if any(json.loads(row.reason or "{}").get("reason") == "stall_retry" for row in prior):
            block_id = str(uuid5(NAMESPACE_URL, f"stall-block:{node.id}:{node.attempts}"))
            if await session.get(OrchestrationDecision, block_id) is None:
                session.add(
                    OrchestrationDecision(
                        id=block_id,
                        org_id=node.org_id,
                        flow_id=node.flow_id,
                        node_id=node.id,
                        kind="developer_retry_blocked",
                        actor_id=ACTOR,
                        actor_role="engine",
                        actor_kind="service",
                        from_state="failed",
                        to_state="failed",
                        reason="worker vanished twice",
                    )
                )
            return "failure_requires_operator"
    if node.attempts >= policy.limits.max_attempts_per_node:
        return "attempts_exhausted"
    started = await flow_started_at(session, org_id=node.org_id, flow_id=node.flow_id)
    if started is not None and (now - aware(started)).total_seconds() >= policy.limits.max_wall_clock_seconds:
        return "flow_deadline_exhausted"
    operations = assignment is not None and assignment.persona in OPERATIONS_PERSONAS
    if runtime_unverified and not operations:
        return "not_a_failed_developer"
    if stall and (operations or (assignment is not None and (assignment.worker_receipt or assignment.terminal_receipt))):
        return "worker_exit_unverified"
    details = {}
    if lost:
        terminal = {"status": "failed", "terminal_outcome": "failed", "persona": assignment.persona}
    elif released:
        if operations or (assignment is not None and assignment.persona not in DEVELOPER_PERSONAS):
            return "not_a_failed_developer"
        persona = assignment.persona if assignment is not None else await dispatched_persona(session, node, run_id)
        if persona not in DEVELOPER_PERSONAS:
            return "not_a_failed_developer"
        terminal = {"status": "failed", "terminal_outcome": "failed", "persona": persona}
    elif assignment is not None:
        if assignment.persona not in (OPERATIONS_PERSONAS if operations else DEVELOPER_PERSONAS):
            return "not_a_failed_developer"
        details = (assignment.terminal_receipt or {}).get("failure") or {}
        if (
            details.get("category") in {"policy", "provider_refusal", "contract", "cancelled"}
            or details.get("retryable") is False
            or (assignment.block_code and not assignment.retryable)
        ):
            return "failure_requires_operator"
        terminal = await run_result_for_assignment(session, node=node, dispatch={"run_id": run_id, "attempt": node.attempts})
    if released or lost:
        pass
    elif stall:
        from src.agentauth.engine import get_engine_authority_writer

        try:
            raw = await asyncio.to_thread(get_engine_authority_writer().store._read, f"TENANT#{node.org_id}", f"EXEC#{run_id}")
        except Exception:
            return "worker_exit_unverified"
        expected = {
            "tenant_id": {"S": node.org_id},
            "invocation_id": {"S": run_id},
            "flow_id": {"S": node.flow_id},
            "orchestration_node_id": {"S": node.id},
            "orchestration_node_attempt": {"N": str(node.attempts)},
        }
        if (
            not raw
            or any(raw.get(key) != value for key, value in expected.items())
            or raw.get("status") != {"S": "cancelled"}
            or raw.get("work_claim_cancellation") != {"S": "unacknowledged_stall"}
            or any(key in raw for key in ("workload_binding", "bootstrap_authority_issued_at", "terminal_outcome"))
        ):
            return "worker_exit_unverified"
        terminal = {"status": "failed", "terminal_outcome": "failed", "persona": raw.get("persona", {}).get("S", "developer")}
    elif assignment is None:
        terminal = await protected_failure_for_assignment(node=node, dispatch={"run_id": run_id})
    expected_outcome = "complete" if runtime_unverified else "failed"
    if not terminal or terminal.get("status") != expected_outcome or terminal.get("terminal_outcome", expected_outcome) != expected_outcome:
        return "worker_exit_unverified"
    if assignment is None and (operations or terminal.get("persona") not in DEVELOPER_PERSONAS):
        return "not_a_failed_developer"
    if stall:
        from .flow_meter import reconcile_flow_meter

        meter = await reconcile_flow_meter(session, org_id=node.org_id, flow_id=node.flow_id, policy=policy)
        if meter is None:
            return "budget_unavailable"
        if meter.total_usd >= policy.limits.max_spend_usd:
            return "budget_exhausted"
    if now < retry_at:
        return "retry_backoff"
    continuation = {}
    if operations:
        from .accepted_node import accepted_node_contract
        from .operations_evidence import assignment_version_preserved, operational_expectations

        contract = await accepted_node_contract(session, node)
        fences = assignment.dispatch_metadata.get("handoff_expect") or {}
        if (
            contract is None
            or contract.plan_version != inputs.plan_version
            or contract.node.executor is None
            or contract.node.executor.role != "operate"
            or contract.node.executor.persona != assignment.persona
            or contract.node.repository_identity != assignment.repo
            or (assignment.dispatch_metadata.get("orchestration") or {}).get("executor_role") != "operate"
            # The run was fenced to the plan version it was dispatched under; an
            # amendment that preserved this node does not change its assignment
            # (#7821, same rule as the evidence check and execution_plan_matches).
            or not await assignment_version_preserved(session, node, fenced=fences.get("accepted_plan_version"), in_force=inputs.plan_version)
            or fences.get("execution_id") != execution.id
            or fences.get("claim_id") != execution.claim_id
            or fences.get("claim_generation") != execution.claim_generation
            or await operational_expectations(session, node, {"conclusion_authority": "evidence"}) is None
        ):
            return "operations_assignment_changed"
        if Action.OPERATE not in policy.allowed_actions or Action.OPERATE in policy.human_gates:
            return "operations_requires_approval"
        from .operations_retry_diagnosis import retry_diagnosis_blocker

        blocker = await retry_diagnosis_blocker(
            session, node=node, contract=contract, runtime_unverified=runtime_unverified, failure_category=details.get("category")
        )
        if blocker is not None:
            return blocker
        from .story_checkpoints import CheckpointError, latest_checkpoint, report_scope
        from .story_run_insights import failure_diagnosis

        diagnostic = failure_diagnosis(assignment.terminal_receipt or {}, assignment.operations_report_receipt).code
        if diagnostic in {"turn_limit", "context_limit"}:
            continuation["runtime_diagnostic"] = diagnostic
            # A checkpoint supplies context, never more authority. The ordinary
            # settled-worker, policy, attempts, deadline and dispatch budget
            # checks above/below remain mandatory for this continuation.
            try:
                saved = await latest_checkpoint(session, await report_scope(session, assignment))
            except CheckpointError:
                return "checkpoint_requires_reconciliation"
            if saved is not None:
                continuation["checkpoint_revision"] = saved["revision"]
    if native_cancellation:
        from .operations_cancelled_release import reconcile_cancelled_release

        result = await reconcile_cancelled_release(
            session, node=node, flow=flow, execution=execution, report=assignment, contract=contract, policy=policy
        )
        if result != "reconciled":
            return result
    # Recording a verified terminal effect does not authorize another attempt.
    # Keep the repair gate on retry admission, after settlement, so a gated
    # failure cannot prevent the owner from amending its execution policy.
    if Action.REPAIR not in policy.allowed_actions or Action.REPAIR in policy.human_gates:
        return "repair_requires_approval"
    from .recovery_control import automatic_recovery_enabled

    if not await automatic_recovery_enabled(session, org_id=node.org_id, flow_id=node.flow_id):
        return "automatic_recovery_disabled"
    # The result observer's settled execution and released exact-generation claim
    # are durable exit evidence. An advisory activity row can never enter here.
    decision = transition(
        node.state,
        NodeState.READY,
        actor_kind=ActorKind.SERVICE,
        reason="Retry the settled operations report within the accepted repair allowance."
        if operations
        else "Retry the settled developer failure within the accepted repair allowance.",
        developer_retry_authorized=not operations,
        operations_retry_authorized=operations,
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
        "reason": (
            "operations_checkpoint_continuation"
            if continuation.get("checkpoint_revision")
            else "claim_released_retry"
            if released
            else "stall_retry"
            if stall
            else "operations_runtime_unverified"
            if runtime_unverified
            else "operations_failed"
            if operations
            else "developer_failed"
        ),
        "failure_category": details.get("category", "unknown"),
        **({"fence_receipt": {"claim_id": claim.id, "claim_generation": claim.generation, "run_id": run_id}} if stall else {}),
        "previous_exit_code": details.get("exit_code"),
        "preserve_existing_work": True,
        "automatic": True,
        **continuation,
    }
    session.add(
        OrchestrationDecision(
            id=recovery_id(node.id, node.attempts, operations=operations),
            org_id=node.org_id,
            flow_id=node.flow_id,
            node_id=node.id,
            kind="operations_retry_scheduled" if operations else KIND,
            actor_id="system:operations-recovery" if operations else ACTOR,
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


async def dispatched_persona(session, node, run_id):
    """The persona the engine dispatched for this exact run, from its durable dispatch decision."""
    row = await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == "node_dispatched",
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    try:
        dispatch = json.loads(row.reason or "{}") if row is not None else {}
    except (TypeError, ValueError):
        return None
    if dispatch.get("run_id") != run_id or dispatch.get("attempt") != node.attempts:
        return None
    persona = (dispatch.get("dispatch_envelope") or {}).get("persona") or dispatch.get("persona")
    return persona if isinstance(persona, str) else None


async def recover_failed_developers(session):
    """Bounded, fair sweep of settled developer and operations work."""
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
                context = await retry_context(session, node, attempt) if result == "scheduled" else None
                block = (
                    await session.get(OrchestrationDecision, str(uuid5(NAMESPACE_URL, f"stall-block:{node.id}:{attempt}")))
                    if result == "failure_requires_operator"
                    else None
                )
                session.add(
                    OrchestrationDecision(
                        org_id=node.org_id,
                        flow_id=node.flow_id,
                        node_id=node.id,
                        kind=CHECKED,
                        actor_id=ACTOR,
                        actor_role="engine",
                        actor_kind="service",
                        reason=json.dumps(
                            {
                                "attempt": node.attempts,
                                "result": result,
                                "reason": context.get("reason") if context else None,
                                "detail": block.reason if block else None,
                            }
                        ),
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
