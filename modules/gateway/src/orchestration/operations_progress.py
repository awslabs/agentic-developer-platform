"""Read-only flow-card descriptions of the current operations task board."""

from collections import defaultdict

from sqlalchemy import select

from .execution_state import TERMINAL_EXECUTION_STATUSES
from .models import OrchestrationAction
from .operations_tasks import STAGES

_DETAILS = {
    "diagnose": ("diagnosis", "The operations agent is investigating the workflow failure."),
    "repair": ("repair", "The operations agent is preparing a repair."),
    "test": ("checks", "The operations agent is running or waiting for the required checks."),
    "publish": ("merge", "The operations agent is publishing and merging the checked repair."),
    "deploy": ("deployment", "The operations agent is observing the deployment workflow."),
    "verify": ("verification", "The operations agent is checking the deployed result and its evidence."),
}


async def current_operations_progress(session, *, org_id, executions):
    ids = [row.id for row in executions.values() if row.org_id == org_id and row.status not in TERMINAL_EXECUTION_STATUSES]
    if not ids:
        return {}
    rows = await session.scalars(
        select(OrchestrationAction).where(
            OrchestrationAction.org_id == org_id,
            OrchestrationAction.execution_id.in_(ids),
            OrchestrationAction.kind == "operations_task",
            OrchestrationAction.status == "succeeded",
        )
    )
    cycles = defaultdict(lambda: defaultdict(dict))
    for row in rows:
        value = row.detail
        parts = row.operation_key.split(":")
        if not isinstance(value, dict) or len(parts) != 3 or parts[0] != "operations-task":
            continue
        cycle, stage = value.get("repair_cycle"), parts[2]
        if type(cycle) is not int or not 1 <= cycle <= 1000 or parts[1] != str(cycle) or stage not in STAGES:
            continue
        state = value.get("state")
        if isinstance(state, str) and state in {"open", "in_progress", "blocked", "done"}:
            cycles[row.execution_id][cycle][stage] = state
    result = {}
    for execution_id, attempts in cycles.items():
        tasks = attempts[max(attempts)]
        # Only the latest repair cycle describes current work. Stored titles and
        # free-form diagnosis text never become public flow-card content.
        active = next((stage for state in ("blocked", "in_progress") for stage in STAGES if tasks.get(stage) == state), None)
        if active:
            stage, detail = _DETAILS[active]
            if tasks[active] == "blocked":
                detail = "The operations agent reported a blocker in this task: " + STAGES[active].lower() + ". Check its recorded evidence."
        elif all(tasks.get(stage) == "done" for stage in STAGES):
            stage, detail = "verification", "Operations tasks are reported complete; delivery evidence still needs engine acceptance."
        else:
            stage, detail = "operations", "The operations agent is working on the configured workflow and checks."
        result[execution_id] = {"stage": stage, "detail": detail}
    return result


def with_operations_progress(progress, *, execution, summaries):
    """Display worker activity without changing holds, scheduling or authority."""
    if (
        progress is None
        or execution is None
        or execution.status in TERMINAL_EXECUTION_STATUSES
        or progress.automation != "engine"
        or progress.blocker
        or progress.stage not in {execution.phase, "operations_continuation"}
        or execution.id not in summaries
    ):
        return progress
    return progress.model_copy(update={**summaries[execution.id], "actor": "operations"})
