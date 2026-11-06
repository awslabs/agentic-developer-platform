"""Resolve executable node metadata from the immutable accepted plan."""

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .compile import address_of
from .execution_policy import ExecutionPolicy
from .models import NodeState, OrchestrationNode
from .proposal import NamedInputPrerequisite, ProposedNode
from .repository import OrchestrationRepository


@dataclass(frozen=True)
class AcceptedNodeContract:
    plan_version: int
    node: ProposedNode
    environment_connection_ids: frozenset[str]
    repository_ids: frozenset[str]


async def accepted_node_contract(session: AsyncSession, node: OrchestrationNode) -> AcceptedNodeContract | None:
    repo = OrchestrationRepository(session)
    flow = await repo.get_flow(org_id=node.org_id, flow_id=node.flow_id)
    plan = await repo.get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    return contract_from_plan(flow, node, plan)


def contract_from_plan(flow, node, plan) -> AcceptedNodeContract | None:
    """Project a contract from one request's already-resolved current plan."""
    if flow is None or plan is None:
        return None
    if flow.org_id != node.org_id or flow.id != node.flow_id or plan.org_id != node.org_id or plan.flow_id != node.flow_id or plan.superseded_at:
        raise ValueError("Contract requires the current plan from the same flow and tenant")
    document = plan.plan_document or {}
    address = address_of(flow.slug, node)
    matches = [item for item in document.get("nodes", []) if isinstance(item, dict) and item.get("address") == address]
    if len(matches) != 1:
        return None
    try:
        accepted_node = ProposedNode.model_validate(matches[0])
    except ValueError:
        return None
    raw_policy = document.get("execution_policy")
    try:
        policy = ExecutionPolicy.model_validate(raw_policy) if raw_policy is not None else None
    except ValueError:
        policy = None
    return AcceptedNodeContract(
        plan_version=plan.version,
        node=accepted_node,
        environment_connection_ids=frozenset(policy.environment_connection_ids if policy is not None else []),
        repository_ids=frozenset(policy.repository_ids if policy is not None else []),
    )


async def requires_verified_repository_connection(session: AsyncSession, node: OrchestrationNode) -> bool:
    if node.repository_connection is not None:
        return True
    repo = OrchestrationRepository(session)
    flow = await repo.get_flow(org_id=node.org_id, flow_id=node.flow_id)
    plan = await repo.get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    document = plan.plan_document or {} if plan is not None else {}
    if (flow is not None and flow.product_id is not None) or document.get("product_id") is not None:
        return True
    return flow is not None and any(
        isinstance(item, dict) and item.get("address") == address_of(flow.slug, node) and item.get("repository_connection") is not None
        for item in document.get("nodes", [])
    )


def operational_revision_inputs(contract: AcceptedNodeContract) -> dict[str, str] | None:
    sources = {
        "source_revision": ("deployment_source_revision:", r"[0-9a-f]{40}"),
        "definition_revision": ("deployment_definition_revision:", r"[0-9a-f]{40}"),
        "default_branch": ("deployment_branch:", r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}"),
    }
    pins = {}
    for item in contract.node.required_inputs:
        for name, (prefix, pattern) in sources.items():
            if item.source.startswith(prefix):
                value = item.source.removeprefix(prefix)
                if name in pins or re.fullmatch(pattern, value) is None:
                    return None
                pins[name] = value
    return pins if len(pins) == len(sources) else None


async def unresolved_required_inputs(session: AsyncSession, node: OrchestrationNode) -> list[NamedInputPrerequisite]:
    contract = await accepted_node_contract(session, node)
    return await unresolved_contract_inputs(session, node, contract)


async def unresolved_contract_inputs(
    session: AsyncSession, node: OrchestrationNode, contract: AcceptedNodeContract | None
) -> list[NamedInputPrerequisite]:
    if contract is None:
        return []
    unresolved = []
    rows = None
    for item in contract.node.required_inputs:
        source = item.source.strip()
        if source.startswith(("environment:", "environment_connection:")):
            connection_id = source.split(":", 1)[1]
            available = connection_id in contract.environment_connection_ids
        elif source.startswith("repository:"):
            repository = source.split(":", 1)[1]
            available = repository in contract.repository_ids or repository == contract.node.repository_identity
        elif source.startswith(("deployment_source_revision:", "deployment_definition_revision:", "deployment_branch:")):
            available = operational_revision_inputs(contract) is not None
        elif source.startswith("deployment_workflow:"):
            from .developer_personas import operations_executor_enabled

            executor = contract.node.executor
            available = (
                operations_executor_enabled()
                and executor is not None
                and executor.role == "operate"
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.ya?ml", source.split(":", 1)[1]) is not None
            )
        elif source.startswith("node:"):
            address = source.split(":", 1)[1]
            if rows is None:
                repo = OrchestrationRepository(session)
                flow = await repo.get_flow(org_id=node.org_id, flow_id=node.flow_id)
                states = await session.execute(
                    select(OrchestrationNode.epic_ref, OrchestrationNode.wave_ref, OrchestrationNode.node_ref, OrchestrationNode.state).where(
                        OrchestrationNode.org_id == node.org_id, OrchestrationNode.flow_id == node.flow_id
                    )
                )
                rows = {address_of(flow.slug, row): row.state for row in states} if flow else {}
            available = rows.get(address) == NodeState.PASSED.value
            binding = contract.node.operational_release_binding
            if available and binding is not None and any(item.address == address for item in binding.predecessors):
                from .operations_release_predecessor import verified_operations_release_predecessor
                from .operations_release_resolution import verified_release_predecessor
                from .review_cycle import CycleBlockedError

                try:
                    predecessor = next(item for item in binding.predecessors if item.address == address)
                    verifier = (
                        verified_operations_release_predecessor
                        if predecessor.receipt_kind == "verified_operations_release"
                        else verified_release_predecessor
                    )
                    await verifier(session, node, contract, address)
                except CycleBlockedError:
                    available = False
        else:
            available = False
        if not available:
            unresolved.append(item)
    return unresolved


def unresolved_required_inputs_problem(unresolved: list[NamedInputPrerequisite]) -> str | None:
    """Operator-facing statement of why the dispatcher will not start a ready node."""
    if not unresolved:
        return None
    names = ", ".join(f"{item.name} ({item.source})" for item in unresolved)
    return f"Required inputs are not available yet, so the dispatcher will not start this node: {names}."
