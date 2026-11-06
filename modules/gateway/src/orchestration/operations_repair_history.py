"""Historical repair authority across a node-preserving accepted amendment."""

from sqlalchemy import select

from .execution_policy import ExecutionPolicy
from .models import OrchestrationAcceptedPlan
from .plan_lineage import ancestor_plan


def compatible_repair_scope(before, after):
    """Preserve completed effects when the same owner expands repair scope.

    Current admission still enforces the current limits. A historical receipt
    grants neither attempts nor spend and never authorizes a new mutation.
    """
    if before.principal_id != after.principal_id:
        return False
    before_repository = getattr(before, "repair_scope", None) == "repository"
    after_repository = getattr(after, "repair_scope", None) == "repository"
    if before_repository and not after_repository or not after_repository and not set(before.repair_paths) <= set(after.repair_paths):
        return False
    # Downstream evaluation ownership does not retroactively change a merged
    # repair. Current evaluation admission still enforces its accepted policy.
    excluded = {"policy_id", "policy_hash", "limits", "expires_at", "repair_paths", "repair_scope", "evaluation_acceptance"}
    return before.model_dump(mode="json", exclude=excluded) == after.model_dump(mode="json", exclude=excluded)


async def historical_repair_policy(session, *, node, execution, action, contract, current):
    if execution.status != "concluded" or execution.phase != "concluded" or execution.pending_action_key is not None:
        return None
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == node.org_id,
            OrchestrationAcceptedPlan.flow_id == node.flow_id,
            OrchestrationAcceptedPlan.version == contract.plan_version,
        )
    )
    original = await ancestor_plan(session, plan, execution.accepted_plan_version, node_id=node.id)
    if original is None:
        return None
    try:
        policy = ExecutionPolicy.model_validate(original.plan_document["execution_policy"])
    except (KeyError, TypeError, ValueError):
        return None
    if not compatible_repair_scope(policy, current):
        return None
    if (execution.handoff_receipt_ref or "").startswith("operations-agent-return:"):
        from .dispatch_pass import attempt_run_id
        from .operations_agent_recovery import closed_assignment
        from .run_reports import OrchestrationRunReport

        report = await session.get(OrchestrationRunReport, attempt_run_id(node.id, execution.cycle))
        closure = await closed_assignment(session, node=node, execution=execution, report=report) if report is not None else None
        if closure is None:
            return None
        try:
            recorded = ExecutionPolicy.model_validate(closure["policy"])
        except (KeyError, TypeError, ValueError):
            return None
        # The attributed closure carries its independently verified supplements.
        # Its scope must still be exactly that of the historical accepted plan.
        if (
            not compatible_repair_scope(policy, recorded)
            or policy.repair_paths != recorded.repair_paths
            or policy.repair_scope != recorded.repair_scope
        ):
            return None
        policy = recorded
    else:
        from .shared_budget import effective_shared_budget
        from .shared_concurrency import effective_shared_concurrency
        from .shared_retry import effective_shared_retry
        from .shared_window import effective_shared_window

        if action.created_at is None:
            return None
        try:
            for apply in (effective_shared_budget, effective_shared_retry, effective_shared_window, effective_shared_concurrency):
                policy = await apply(session, original, policy, as_of=action.created_at)
        except ValueError:
            return None
    return policy
