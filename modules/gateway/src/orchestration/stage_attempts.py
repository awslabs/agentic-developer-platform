"""Stage allowances derived from durable reservations, never reset on recovery.

The legacy policy field max_attempts_per_node applies independently to each
stage of a node. Node.attempts retains development admission history; execution
attempts remains a total for audit compatibility, not an authorization counter.
"""

from sqlalchemy import and_, case, func, not_, select

from .execution_policy import Action
from .execution_state import UNRESOLVED_ACTION_STATUSES
from .models import OrchestrationAction, OrchestrationExecution

LEGACY_STAGES = {
    "merge_pull_request": Action.MERGE.value,
    "deployment_workflow": Action.DEPLOY.value,
    "repository_scan_dispatch": Action.EVALUATE.value,
    "cli_qualification_dispatch": Action.EVALUATE.value,
    "evaluation_correction_issue": Action.REPAIR.value,
}


def stage_expression():
    # Existing deployed effects predate attempt_stage. Evidence/notification rows
    # deliberately have no stage: recording an observation is not an attempt.
    return case(
        (OrchestrationAction.kind == "review_cycle_dispatch", func.coalesce(OrchestrationAction.detail["action"].as_string(), Action.REVIEW.value)),
        else_=func.coalesce(OrchestrationAction.detail["attempt_stage"].as_string(), case(LEGACY_STAGES, value=OrchestrationAction.kind)),
    )


def _actions(org_id, node_ids):
    return (
        select(OrchestrationExecution.node_id, stage_expression().label("stage"), func.count())
        .join(OrchestrationAction, OrchestrationExecution.id == OrchestrationAction.execution_id)
        .where(OrchestrationAction.org_id == org_id, OrchestrationExecution.org_id == org_id, OrchestrationExecution.node_id.in_(node_ids))
    )


async def stage_counts(session, *, org_id, node_ids):
    """Grouped history across cycles, independent of display pagination."""
    rows = (await session.execute(_actions(org_id, node_ids).group_by(OrchestrationExecution.node_id, "stage"))).all()
    result = {node_id: {} for node_id in node_ids}
    for node_id, stage, count in rows:
        if stage is not None:
            result[node_id][stage] = count
    return result


async def stage_attempts(session, *, org_id, node_id, action, exclude_operation_key=None):
    """Count reserved effects, including failures and uncertain outcomes.

    Reauthorization may exclude the exact unresolved reservation being executed.
    A settled failure still counts, even when a caller reuses its operation key.
    """
    query = _actions(org_id, [node_id]).where(stage_expression() == action.value)
    if exclude_operation_key is not None:
        query = query.where(
            not_(
                and_(
                    OrchestrationAction.operation_key == exclude_operation_key,
                    OrchestrationAction.status.in_([status.value for status in UNRESOLVED_ACTION_STATUSES]),
                )
            )
        )
    rows = (await session.execute(query.group_by(OrchestrationExecution.node_id, "stage"))).all()
    return rows[0][2] if rows else 0
