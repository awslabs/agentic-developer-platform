"""Verify narrowly scoped amendments without rewriting worker assignment identities.

Only human-accepted dependency amendments can bridge versions. Each hop must
preserve every non-edge field and every prerequisite in the protected wave.
Ordinary replacement/append plans still invalidate old execution authority.
"""

import json

from sqlalchemy import select

from .continuation import digest
from .models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationExecution, OrchestrationNode
from .proposal import split_address

CONTRACT = "paused-wave-dependencies/v1"
MAX_HOPS = 100


def edge_set(document):
    return {(e["from_address"], e["to_address"]) for e in document["edges"]}


def wave(address):
    return tuple(split_address(address)[1:3])


async def ancestor_plan(session, plan, version, *, node_id=None):
    """Return the exact earlier plan only through verified preservation receipts."""
    if plan is None or type(version) is not int or version < 1 or version > plan.version:
        return None
    if plan.version == version:
        return plan  # Existing callers already enforce their node/tenant binding.
    protected = None
    if node_id is not None:
        node = await session.get(OrchestrationNode, node_id)
        if node is None or node.org_id != plan.org_id or node.flow_id != plan.flow_id:
            return None
        protected = (node.epic_ref, node.wave_ref)
    for _ in range(MAX_HOPS + 1):
        if plan.version == version:
            return plan
        marker = plan.plan_document.get("execution_continuation") if isinstance(plan.plan_document, dict) else None
        if (
            not plan.accepted_by_decision_id
            or not isinstance(marker, dict)
            or marker.get("mode") != "shared_worker_role"
            or marker.get("delivery_mode") != "code_only"
        ):
            return None
        receipt = await session.get(OrchestrationDecision, plan.accepted_by_decision_id)
        try:
            data = json.loads(receipt.reason) if receipt else {}
            if (
                receipt is None
                or receipt.org_id != plan.org_id
                or receipt.flow_id != plan.flow_id
                or receipt.kind != "plan_amended"
                or receipt.actor_kind != "human"
                or not receipt.actor_id
                or data.get("contract") != CONTRACT
                or data.get("plan_version") != plan.version
                or data.get("plan_hash") != plan.plan_hash
                or plan.plan_hash != digest(plan.plan_document)
                or receipt.actor_id != plan.plan_document["execution_policy"]["principal_id"]
            ):
                return None
            base = await session.get(OrchestrationAcceptedPlan, data["base_plan_id"])
            if (
                base is None
                or base.org_id != plan.org_id
                or base.flow_id != plan.flow_id
                or base.version != plan.version - 1
                or base.version != data["base_plan_version"]
                or base.plan_hash != data["base_plan_hash"]
                or base.plan_hash != digest(base.plan_document)
                or base.superseded_at is None
            ):
                return None
            before, after = base.plan_document, plan.plan_document
            if {k: v for k, v in before.items() if k != "edges"} != {k: v for k, v in after.items() if k != "edges"}:
                return None
            changes = edge_set(before) ^ edge_set(after)
            changed_waves = {wave(target) for _, target in changes}
            if not changes or changed_waves != {tuple(w) for w in data["changed_waves"]}:
                return None
            frozen = {tuple(w) for w in data["frozen_waves"]}
            if changed_waves & frozen or (protected is not None and protected not in frozen):
                return None
            plan = base
        except (ValueError, KeyError, TypeError, AttributeError):
            return None
    return None


async def execution_plan_matches(session, *, org_id, flow_id, node_id, version, current_version=None):
    # Common path stays cheap. The caller obtained current_version from the
    # current accepted plan, never from worker-supplied metadata.
    if current_version is not None and current_version == version:
        return True
    plan = await session.scalar(
        select(OrchestrationAcceptedPlan).where(
            OrchestrationAcceptedPlan.org_id == org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
    )
    return await ancestor_plan(session, plan, version, node_id=node_id) is not None


async def receipt_plan(session, plan, data, *, node_id=None):
    """Keep the receipt's original hash/version checks, after proving lineage."""
    original = await ancestor_plan(session, plan, data.get("plan_version"), node_id=node_id)
    if original is None or original.plan_hash != data.get("plan_hash"):
        return None
    if "plan_id" in data and original.id != data["plan_id"]:
        return None
    return original


async def preserved_execution_pairs(session, *, org_id, flow_ids=None):
    """Bounded, verified facts for both SQL rollups and individual stage cards."""
    execution, plan = OrchestrationExecution, OrchestrationAcceptedPlan
    query = (
        select(execution, plan, OrchestrationNode)
        .join(
            plan,
            (plan.org_id == execution.org_id) & (plan.flow_id == execution.flow_id),
        )
        .join(OrchestrationNode, (OrchestrationNode.id == execution.node_id) & (OrchestrationNode.attempts == execution.cycle))
        .where(plan.org_id == org_id, plan.superseded_at.is_(None), execution.accepted_plan_version != plan.version)
    )
    if flow_ids is not None:
        query = query.where(plan.flow_id.in_(flow_ids))
    records = list(await session.execute(query.limit(1001)))
    if len(records) > 1000:
        return []  # Display never supplies authority; fail closed on excess history.
    if not records:
        return []
    # Keep strong references to the chain in the identity map so verification
    # below does not issue a query per node/hop on a page of amended flows.
    lineage = list(
        await session.execute(
            select(plan, OrchestrationDecision)
            .outerjoin(OrchestrationDecision, OrchestrationDecision.id == plan.accepted_by_decision_id)
            .where(plan.org_id == org_id, plan.flow_id.in_({p.flow_id for _, p, _ in records}))
            .limit(10001)
        )
    )
    if len(lineage) > 10000:
        return []
    return [
        (e.node_id, e.accepted_plan_version)
        for e, p, _ in records
        if await ancestor_plan(session, p, e.accepted_plan_version, node_id=e.node_id) is not None
    ]
