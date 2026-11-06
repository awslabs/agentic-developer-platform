"""Read the existing Resume decision for a settled operations failure.

Resume records a human failed->ready edge, not an automatic retry receipt. Both
must reach the same release/assignment and normal admission checks. This reader
does not change state, counters, limits, evidence or the original decision log.
"""

import json

from sqlalchemy import select

from .developer_personas import OPERATIONS_PERSONAS
from .models import OrchestrationDecision, OrchestrationExecution
from .run_reports import OrchestrationRunReport


async def retry_context(session, node, previous_attempt):
    from .dispatch_pass import attempt_run_id

    recent = (
        await session.scalars(
            select(OrchestrationDecision)
            .where(
                OrchestrationDecision.org_id == node.org_id,
                OrchestrationDecision.flow_id == node.flow_id,
                OrchestrationDecision.node_id == node.id,
                OrchestrationDecision.to_state.is_not(None),
                OrchestrationDecision.rejection_reason.is_(None),
            )
            .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
            .limit(2)
        )
    ).all()
    if len(recent) != 2:
        return None
    resumed, failed = recent
    # The public resume route historically calls this edge node_stalled.
    if (
        resumed.kind != "node_stalled"
        or (resumed.from_state, resumed.to_state, resumed.actor_kind) != ("failed", "ready", "human")
        or not resumed.actor_id
        or failed.kind != "result_observed"
        or failed.actor_id != "system:orchestration-results"
        or failed.from_state not in {"running", "awaiting_merge"}
        or (failed.to_state, failed.actor_kind) != ("failed", "service")
    ):
        return None
    try:
        failure = json.loads(failed.reason)
    except (TypeError, ValueError):
        return None
    run_id = attempt_run_id(node.id, previous_attempt)
    if (
        not isinstance(failure, dict)
        or failure.get("attempt") != previous_attempt
        or failure.get("run_id") != run_id
        or not isinstance(failure.get("failed_execution_id"), str)
    ):
        return None
    runtime_unverified = failure.get("operations_retry") is True and failure.get("hold_reason") == "evidence_missing"
    if not runtime_unverified and ("operations_retry" in failure or "hold_reason" in failure):
        return None
    expected_outcome = "complete" if runtime_unverified else "failed"
    execution = await session.get(OrchestrationExecution, failure["failed_execution_id"])
    report = await session.get(OrchestrationRunReport, run_id)
    from .lost_operations_worker import settled_lost_worker

    lost = not runtime_unverified and await settled_lost_worker(
        session, node=node, execution=execution, report=report, attempt=previous_attempt, check_released_claim=False
    )
    if (
        execution is None
        or (execution.org_id, execution.flow_id, execution.node_id, execution.cycle) != (node.org_id, node.flow_id, node.id, previous_attempt)
        or execution.status != "concluded"
        or execution.phase != "concluded"
        or execution.pending_action_key is not None
        or report is None
        or (report.org_id, report.flow_id, report.node_id, report.attempt) != (node.org_id, node.flow_id, node.id, previous_attempt)
        or report.persona not in OPERATIONS_PERSONAS
        or (
            not lost
            and (
                (report.terminal_receipt or {}).get("outcome") != expected_outcome
                or (report.terminal_receipt or {}).get("run_id") != run_id
                or (report.terminal_receipt or {}).get("attempt") != previous_attempt
            )
        )
    ):
        return None
    details = (report.terminal_receipt or {}).get("failure") or {}
    if (
        not runtime_unverified
        and not lost
        and (
            not isinstance(details, dict)
            or details.get("category") in {"policy", "provider_refusal", "contract", "cancelled"}
            or details.get("retryable") is False
            or (report.block_code and not report.retryable)
        )
    ):
        return None
    return {
        "previous_run_id": run_id,
        "previous_attempt": previous_attempt,
        "failure_decision_id": failed.id,
        "resume_decision_id": resumed.id,
        "execution_id": execution.id,
        "plan_version": execution.accepted_plan_version,
        "reason": "operations_runtime_unverified" if runtime_unverified else "operations_failed",
        "preserve_existing_work": True,
    }
