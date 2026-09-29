"""Execution deadline visibility, including ready nodes with no execution row."""

import math
from datetime import UTC, datetime, timedelta

from .repository import OrchestrationRepository
from .runtime_policy import flow_started_at


async def execution_window_view(session, *, flow, nodes, inputs):
    if inputs.refusal is not None:
        return {"status": "unavailable", "reason": "Execution policy could not be verified."}
    policy = inputs.policy
    if policy is None:
        return None
    if not any(node.state not in {"passed", "superseded"} for node in nodes):
        return {"status": "complete"}
    now = datetime.now(UTC)
    started = await flow_started_at(session, org_id=flow.org_id, flow_id=flow.id)
    deadline = min(policy.expires_at, started + timedelta(seconds=policy.limits.max_wall_clock_seconds)) if started else policy.expires_at
    result = {"status": "expired" if deadline <= now else "active", "deadline_at": deadline.isoformat(), "observed_at": now.isoformat()}
    if deadline > now:
        return result
    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=flow.org_id, flow_id=flow.id)
    if plan is None or started is None:
        return {**result, "renewal_unavailable": "The accepted plan or execution start could not be verified."}
    seconds = max(policy.limits.max_wall_clock_seconds, math.ceil((now + timedelta(hours=4) - started).total_seconds()))
    if seconds > 604800:
        return {**result, "renewal_unavailable": "This flow has reached the seven-day execution limit. A new accepted plan is required."}
    result["renewal_request"] = {
        "expected_plan_version": plan.version,
        "expected_plan_hash": plan.plan_hash,
        "expires_at": max(policy.expires_at, now + timedelta(hours=4)).isoformat(),
        "max_wall_clock_seconds": seconds if seconds > policy.limits.max_wall_clock_seconds else None,
        "resume_expired": True,
        "reason": "Owner renewed the expired execution window from the flow dashboard.",
    }
    return result
