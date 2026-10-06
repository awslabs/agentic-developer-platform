"""Bounded failure evidence from the existing deployment action ledger."""

from datetime import UTC

from sqlalchemy import and_, select

from src.orchestration.execution_state import BlockCode
from src.orchestration.models import OrchestrationAction, OrchestrationExecution, OrchestrationPullRequestBinding

STAGES = {
    "deployment_workflow": "workflow_dispatch",
    "deployment_handoff": "workflow_handoff",
    "deployment_verification": "runtime_verification",
}


def _timestamp(value):
    if value is None:
        return None
    return value.replace(tzinfo=UTC).isoformat() if value.tzinfo is None else value.isoformat()


async def read_installation_failure(db, scope) -> dict:
    bindings = OrchestrationPullRequestBinding
    executions = OrchestrationExecution
    actions = OrchestrationAction
    rows = (
        await db.execute(
            select(actions, executions, bindings)
            .join(executions, and_(actions.org_id == executions.org_id, actions.execution_id == executions.id))
            .join(
                bindings,
                and_(
                    bindings.org_id == executions.org_id,
                    bindings.flow_id == executions.flow_id,
                    bindings.node_id == executions.node_id,
                    bindings.attempt == executions.cycle,
                ),
            )
            .where(
                bindings.org_id == scope.tenant_id,
                bindings.installation_id == scope.installation_id,
                actions.kind.in_(STAGES),
                actions.status.in_(("failed", "unknown")),
            )
            .order_by(actions.created_at.desc(), actions.id.desc())
            .limit(50)
        )
    ).all()
    for action, execution, _binding in rows:
        if _binding.revision != 1:
            continue
        attributed = (
            await db.scalars(
                select(bindings.installation_id)
                .where(
                    bindings.org_id == scope.tenant_id,
                    bindings.flow_id == execution.flow_id,
                    bindings.node_id == execution.node_id,
                    bindings.attempt == execution.cycle,
                )
                .limit(2)
            )
        ).all()
        if len(attributed) != 1 or attributed[0] != scope.installation_id:
            continue
        try:
            reason = BlockCode(execution.block_code).value if execution.block_code else None
        except ValueError:
            reason = None
        return {
            "status": "partial",
            "installation_id": scope.installation_id,
            "failure": {
                "stage": STAGES[action.kind],
                "outcome": action.status,
                "started_at": _timestamp(action.created_at),
                "observed_at": _timestamp(action.observed_at),
                "reason": reason,
            },
            "coverage": "orchestration_action_only",
        }
    return {"status": "unavailable", "installation_id": scope.installation_id, "reason": "diagnostic_record_unavailable"}
