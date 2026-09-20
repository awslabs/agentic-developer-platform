"""Resolve accepted evaluation nodes and their current D3 predecessors."""

from dataclasses import asdict

from sqlalchemy import select

from .deployment_controller import DEPLOYMENT_KIND
from .deployment_runtime_contract import DeploymentReceipt
from .dispatch import graph_address
from .evaluation_contract import specification
from .execution_runner import RunnerContext
from .execution_state import ExecutionIdentity, OutcomeKind
from .execution_store import load_execution
from .models import OrchestrationAction, OrchestrationEdge, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from .repository import OrchestrationRepository
from .review_cycle import CycleBlockedError


def require(condition, reason):
    if not condition:
        raise CycleBlockedError(reason)


async def accepted_evaluation(session, node):
    if node.kind != "eval":
        return None
    plan = await OrchestrationRepository(session).get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    if plan is None:
        return None
    flow = await session.get(OrchestrationFlow, node.flow_id, populate_existing=True)
    require(flow is not None and flow.org_id == node.org_id, "evaluation_flow_missing")
    address = graph_address(node, flow_slug=flow.slug)
    rows = [item for item in (plan.plan_document or {}).get("nodes", []) if item.get("address") == address]
    require(len(rows) <= 1, "evaluation_accepted_node_ambiguous")
    if not rows or rows[0].get("evaluation") is None:
        return None
    return plan, specification(rows[0]["evaluation"]), address


async def managed_evaluation(session, node):
    accepted = await accepted_evaluation(session, node)
    # Unbounded legacy/human flows keep their original worker/control path.
    return bool(accepted and (accepted[0].plan_document or {}).get("execution_policy"))


def identity_for(record):
    return ExecutionIdentity(record.org_id, record.node_id, record.cycle, record.accepted_plan_version, record.claim_id, record.claim_generation)


async def predecessor_deployments(session, node, plan_version, *, now):
    parents = list(
        (
            await session.scalars(
                select(OrchestrationNode)
                .join(OrchestrationEdge, OrchestrationEdge.from_node_id == OrchestrationNode.id)
                .where(
                    OrchestrationEdge.org_id == node.org_id,
                    OrchestrationEdge.to_node_id == node.id,
                    OrchestrationNode.org_id == node.org_id,
                    OrchestrationNode.flow_id == node.flow_id,
                )
                .order_by(OrchestrationNode.id)
                .limit(129)
            )
        ).all()
    )
    require(len(parents) <= 128, "evaluation_predecessor_limit")
    if not parents or any(parent.state != "passed" for parent in parents):
        return None
    deployments = []
    for parent in parents:
        if parent.kind != "story":
            continue
        current = await current_deployment(session, parent, plan_version, now=now)
        if current is None:
            return None
        deployments.append(current)
    return deployments or None


async def current_deployment(session, parent, plan_version, *, now):
    records = list(
        (
            await session.scalars(
                select(OrchestrationExecution)
                .where(
                    OrchestrationExecution.org_id == parent.org_id,
                    OrchestrationExecution.node_id == parent.id,
                    OrchestrationExecution.cycle == parent.attempts,
                    OrchestrationExecution.accepted_plan_version == plan_version,
                )
                .limit(2)
            )
        ).all()
    )
    if not records:
        return None
    require(len(records) == 1, "evaluation_predecessor_execution_ambiguous")
    identity = identity_for(records[0])
    loaded = await load_execution(session, identity=identity)
    require(loaded is not None and loaded.kind is OutcomeKind.APPLIED, "evaluation_predecessor_authority_changed")
    actions = list(
        (
            await session.scalars(
                select(OrchestrationAction)
                .where(
                    OrchestrationAction.org_id == parent.org_id,
                    OrchestrationAction.execution_id == records[0].id,
                    OrchestrationAction.kind == DEPLOYMENT_KIND,
                    OrchestrationAction.status == "succeeded",
                )
                .order_by(OrchestrationAction.created_at.desc())
                .limit(101)
            )
        ).all()
    )
    require(len(actions) <= 100, "evaluation_deployment_history_limit")
    final = []
    for action in actions:
        raw = (action.detail or {}).get("deployment_receipt")
        if not raw or raw.get("delivery_complete") is not True:
            continue
        receipt = DeploymentReceipt.model_validate(raw)
        require(all(getattr(receipt, key) == value for key, value in asdict(identity).items()), "evaluation_deployment_scope_changed")
        require(
            receipt.execution_id == records[0].id and receipt.flow_id == parent.flow_id and receipt.operation_key == action.operation_key,
            "evaluation_deployment_reference_changed",
        )
        final.append(receipt)
    if not final:
        return None
    require(len(final) == 1, "evaluation_final_deployment_ambiguous")
    return RunnerContext(identity, loaded.record, now), parent, final[0]
