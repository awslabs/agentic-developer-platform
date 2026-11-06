"""Read the exact current continuation; a flow correlation is not story scope."""

import json
from dataclasses import dataclass

from sqlalchemy import select

from .models import OrchestrationAction, OrchestrationDecision, OrchestrationExecution, OrchestrationNode, OrchestrationWorkClaim
from .review_cycle_dispatch import ACTOR, continuation_run_id, receipt_id


@dataclass(frozen=True)
class ContinuationActivity:
    invocation_id: str
    correlation_id: str
    persona: str
    cycle: int


async def current_continuations(session, *, org_id: str, flow_id: str, node_ids: list[str]) -> dict[str, ContinuationActivity]:
    """Bounded current assignments only, with no authority or ledger changes.

    Recovery preserves the original flow's budget correlation. Reading that
    entire correlation would mix sibling stories into a node's activity. Use
    the exact invocation committed by the dispatcher under the current claim.
    """
    if not node_ids:
        return {}
    action = OrchestrationAction
    execution = OrchestrationExecution
    node = OrchestrationNode
    claim = OrchestrationWorkClaim
    latest = (
        select(action.id)
        .where(action.org_id == org_id, action.execution_id == execution.id, action.kind == "review_cycle_dispatch")
        .order_by(action.created_at.desc(), action.id.desc())
        .limit(1)
        .correlate(execution)
        .scalar_subquery()
    )
    rows = list(
        await session.execute(
            select(node.id, execution, action, claim.active_run_id)
            .join(execution, (execution.node_id == node.id) & (execution.org_id == node.org_id) & (execution.cycle == node.attempts))
            .join(claim, (claim.id == execution.claim_id) & (claim.org_id == execution.org_id))
            .join(action, action.id == latest)
            .where(
                node.org_id == org_id,
                node.flow_id == flow_id,
                node.id.in_(node_ids),
                node.kind == "story",
                node.state.in_(("running", "awaiting_merge")),
                execution.flow_id == flow_id,
                claim.state == "held",
                claim.owner_kind == "engine_flow",
                claim.owner_ref == flow_id,
                claim.generation == execution.claim_generation,
            )
        )
    )
    if not rows:
        return {}
    receipts = {
        r.id: r
        for r in await session.scalars(
            select(OrchestrationDecision).where(
                OrchestrationDecision.id.in_([receipt_id(a.operation_key) for _, _, a, _ in rows]),
                OrchestrationDecision.org_id == org_id,
                OrchestrationDecision.flow_id == flow_id,
                OrchestrationDecision.kind == "agent_dispatched",
                OrchestrationDecision.actor_id == ACTOR,
                OrchestrationDecision.actor_kind == "service",
            )
        )
    }
    result = {}
    for node_id, current, dispatched, active_run_id in rows:
        receipt = receipts.get(receipt_id(dispatched.operation_key))
        if (
            receipt is None
            or receipt.node_id != node_id
            or receipt.org_id != org_id
            or receipt.flow_id != flow_id
            or receipt.actor_id != ACTOR
            or receipt.actor_kind != "service"
            or receipt.kind != "agent_dispatched"
        ):
            continue
        try:
            saved = json.loads(receipt.reason or "{}")
            envelope = saved["envelope"]
            run_id = continuation_run_id(dispatched.operation_key)
            correlation_id = envelope["correlation"]["correlation_id"]
            if (
                saved["execution_id"] != current.id
                or saved["claim_generation"] != current.claim_generation
                or saved["accepted_plan_version"] != current.accepted_plan_version
                or saved["operation_key"] != dispatched.operation_key
                or saved["run_id"] != run_id
                or active_run_id != run_id
                or envelope["message_id"] != run_id
                or envelope["persona"] != "agent-codex-reviewer"
                or not isinstance(correlation_id, str)
                or not correlation_id
            ):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        result[node_id] = ContinuationActivity(run_id, correlation_id, envelope["persona"], current.cycle)
    return result
