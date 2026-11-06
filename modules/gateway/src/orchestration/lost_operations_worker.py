"""Recognize the same unreported worker exit already settled by the results pass.

This is retry evidence, never a reconstructed worker result or runtime success.
Callers must first verify the engine's failed decision and retain normal policy,
assignment, pending-effect and release checks.
"""

from types import SimpleNamespace

from .developer_personas import OPERATIONS_PERSONAS
from .models import OrchestrationWorkClaim
from .run_reports import run_result_for_assignment


async def settled_lost_worker(session, *, node, execution, report, attempt, check_released_claim=True):
    from .dispatch_pass import attempt_run_id

    if execution is None or report is None:
        return False
    run_id = attempt_run_id(node.id, execution.cycle)
    if (
        (execution.org_id, execution.flow_id, execution.node_id, execution.cycle) != (node.org_id, node.flow_id, node.id, attempt)
        or (report.org_id, report.flow_id, report.node_id, report.attempt, report.run_id) != (node.org_id, node.flow_id, node.id, attempt, run_id)
        or report.persona not in OPERATIONS_PERSONAS
        or report.terminal_receipt is not None
        or execution.status != "concluded"
        or execution.phase != "concluded"
        or execution.pending_action_key is not None
    ):
        return False
    observed = await run_result_for_assignment(session, node=node, dispatch={"run_id": run_id, "attempt": attempt})
    if not observed or observed.get("status") != "in_progress":
        return False
    release_reason = "abandoned"
    if observed.get("worker_started") is False and observed.get("block_code") is None and observed.get("retryable") is True:
        from .results import protected_failure_for_assignment

        # Bootstrap can exit before registering a run report. Reuse the exact
        # protected failure that results accepted; never invent a worker receipt.
        # Readers may already be validating the replacement attempt's assignment.
        previous = SimpleNamespace(id=node.id, org_id=node.org_id, flow_id=node.flow_id, attempts=attempt)
        try:
            failure = await protected_failure_for_assignment(node=previous, dispatch={"run_id": run_id})
        except ValueError:
            return False
        if not failure or failure.get("terminal_outcome") != "failed" or failure.get("persona") != report.persona:
            return False
        release_reason = "failed"
    elif not (
        observed.get("worker_started") is True and observed.get("block_code") == "delivery_recovery_required" and observed.get("retryable") is False
    ):
        return False
    fences = report.dispatch_metadata.get("handoff_expect") or {}
    if (
        fences.get("execution_id") != execution.id
        or fences.get("accepted_plan_version") != execution.accepted_plan_version
        or fences.get("claim_id") != execution.claim_id
        or fences.get("claim_generation") != execution.claim_generation
    ):
        return False
    if not check_released_claim:
        # The resume reader verifies the results pass's failed decision and the
        # explicit human resume. That settlement already fenced the old owner.
        # Dispatch may now hold the claim for the replacement generation; reading
        # it as the old worker's release would reject the handoff we just admitted.
        return True
    claim = await session.get(OrchestrationWorkClaim, execution.claim_id)
    return bool(
        claim is not None
        and claim.org_id == node.org_id
        and claim.state == "released"
        and claim.release_reason == release_reason
        and claim.active_run_id is None
        and claim.claim_event_id == run_id
        and claim.generation == execution.claim_generation
        and claim.owner_kind == "engine_flow"
        and claim.owner_ref == node.flow_id
        and claim.provider_repository_id == report.provider_repository_id
        and str(claim.issue_number) == str(node.issue_ref).lstrip("#")
    )
