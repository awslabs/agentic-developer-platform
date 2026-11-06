"""Per-flow veto for automatic retries; never expands accepted authority.

The default preserves existing policy-authorized retries. Disabling adds a hold,
without rewriting accepted plan hashes, stopping observation, or clearing caps.
"""

from sqlalchemy import select

from .models import OrchestrationDecision

ENABLED = "flow_recovery_enabled"
DISABLED = "flow_recovery_disabled"


async def automatic_recovery_enabled(session, *, org_id, flow_id):
    decision = await session.scalar(
        select(OrchestrationDecision.kind)
        .where(
            OrchestrationDecision.org_id == org_id,
            OrchestrationDecision.flow_id == flow_id,
            OrchestrationDecision.node_id.is_(None),
            OrchestrationDecision.kind.in_([ENABLED, DISABLED]),
            OrchestrationDecision.actor_kind == "human",
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
    )
    return decision != DISABLED
