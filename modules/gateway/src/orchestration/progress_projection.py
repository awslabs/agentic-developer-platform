"""One current, tenant-scoped display bucket per node, shared by list and graph.

Historical decisions are audit records, not current stalls. A blocked delivery
can still have node.state=running; only node.state=passed proves completion.
"""

from sqlalchemy import and_, case, func, or_, select

from .display_state import ENGINE_TO_DISPLAY
from .models import OrchestrationAcceptedPlan, OrchestrationExecution, OrchestrationNode, OrchestrationWorkClaim

CAPACITY_WAIT_NOTE = "Waiting for a shared worker slot; no dispatch attempt consumed."


def node_progress_rows(*, org_id: str, flow_ids: list[str] | None = None, preserved_executions=()):
    node, execution, claim = OrchestrationNode, OrchestrationExecution, OrchestrationWorkClaim
    plans = (
        select(OrchestrationAcceptedPlan.flow_id, func.max(OrchestrationAcceptedPlan.version).label("version"))
        .where(OrchestrationAcceptedPlan.org_id == org_id, OrchestrationAcceptedPlan.superseded_at.is_(None))
        .group_by(OrchestrationAcceptedPlan.flow_id)
        .subquery()
    )
    current_plan = execution.accepted_plan_version == func.coalesce(plans.c.version, 0)
    if preserved_executions:
        by_version = {}
        for node_id, version in preserved_executions:
            by_version.setdefault(version, []).append(node_id)
        current_plan = or_(
            current_plan,
            *[and_(execution.accepted_plan_version == version, execution.node_id.in_(node_ids)) for version, node_ids in by_version.items()],
        )
    live_execution = and_(execution.id.is_not(None), current_plan, execution.status.not_in(("concluded", "superseded")))
    # A lapsed held claim is not a free worker slot. Show the ownership hold on
    # ready work too, without releasing it or inferring that its worker exited.
    lapsed_claim = and_(
        node.state == "ready",
        claim.state == "held",
        claim.lease_expires_at <= func.now(),
    )
    base = case({state.value: display.value if display else None for state, display in ENGINE_TO_DISPLAY.items()}, value=node.state)
    display = case(
        (node.state.in_(("passed", "superseded", "failed", "halted", "rejected_at_gate", "awaiting_gate")), base),
        (lapsed_claim, "stalled"),
        (and_(live_execution, execution.status == "blocked"), "stalled"),
        (and_(live_execution, execution.block_code.is_(None), execution.progress_note == CAPACITY_WAIT_NOTE), "queued"),
        else_=base,
    )
    stmt = (
        select(
            node.id.label("node_id"),
            node.flow_id,
            node.epic_ref,
            node.wave_ref,
            node.kind,
            node.issue_ref,
            node.state,
            node.created_at,
            display.label("display_state"),
        )
        .outerjoin(plans, plans.c.flow_id == node.flow_id)
        .outerjoin(
            execution,
            and_(execution.org_id == org_id, execution.flow_id == node.flow_id, execution.node_id == node.id, execution.cycle == node.attempts),
        )
        .outerjoin(
            claim,
            and_(claim.org_id == org_id, claim.id == execution.claim_id, claim.generation == execution.claim_generation),
        )
        .where(node.org_id == org_id)
    )
    if flow_ids is not None:
        stmt = stmt.where(node.flow_id.in_(flow_ids))
    return stmt.subquery()
