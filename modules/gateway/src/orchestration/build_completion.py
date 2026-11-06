"""Explicit build completion while a mixed flow retains deployment and acceptance.

Fresh plans declare the boundary on the develop node. A snapshot-bound recovery
can attach it to an already verified merge; it supplies no merge, deployment or
test verdict and never changes the worker plan or budget meter.
"""

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from .dispatch import graph_address
from .execution_policy import Action, ExecutionPolicy
from .execution_state import ExecutionIdentity
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
)
from .proposal import ProposedNode
from .repository_evaluation_contract import canonical
from .review_cycle import CycleBlockedError
from .state import ActorKind

KIND = "build_completion_accepted"
CONTRACT = "verified-build-completion/v1"
DEPLOYMENT_EFFECTS = ("deployment_workflow", "deployment_handoff", "deployment_verification")


class BuildCompletionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=36)
    deployment_node_id: str = Field(min_length=1, max_length=36)
    acceptance_node_id: str = Field(min_length=1, max_length=36)
    expected_plan_version: int = Field(gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    reason: str = Field(min_length=8, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class BuildCompletionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise BuildCompletionError(code)


def digest(value):
    from .continuation import digest as canonical_digest

    return canonical_digest(value)


def definition(plan, flow, node):
    address = graph_address(node, flow_slug=flow.slug)
    matches = [item for item in plan.plan_document.get("nodes", []) if item.get("address") == address]
    require(len(matches) == 1, "build_completion_node_changed")
    raw = matches[0]
    require(all(raw.get(key) == getattr(node, key) for key in ("kind", "title", "issue_ref")), "build_completion_node_changed")
    # Defaulted optional fields are equivalent across schema upgrades/amendments.
    return ProposedNode.model_validate(raw).model_dump(mode="json")


def is_build(raw):
    node = ProposedNode.model_validate(raw)
    return bool(
        node.kind == "story"
        and node.executor is not None
        and node.executor.kind == "agent"
        and node.executor.role == "develop"
        and node.evidence_expectations == ["verified-merge"]
    )


def recoverable_build(raw):
    """Legacy develop nodes may acquire an attributed boundary after verified merge.

    Empty legacy metadata is not an implicit code-only declaration. Only this
    recovery path accepts it; declared evidence or criteria cannot be discarded.
    Fresh code-only plans still require the explicit verified-merge contract.
    """
    node = ProposedNode.model_validate(raw)
    return is_build(raw) or bool(
        node.kind == "story"
        and node.executor is not None
        and node.executor.kind == "agent"
        and node.executor.role == "develop"
        and node.delivery_mode is None
        and not node.evidence_expectations
        and not node.criteria
    )


def require_delivery_acceptance(accept, deploy, policy):
    require(accept["kind"] == "eval", "delivery_acceptance_required")
    mode = policy.evaluation_acceptance.get(accept["address"])
    if mode == "human":
        return
    require(mode == "machine" and Action.EVALUATE in policy.allowed_actions, "delivery_acceptance_required")
    from .repository_evaluation_contract import RepositoryEvaluationSpecification

    try:
        spec = RepositoryEvaluationSpecification.model_validate(accept.get("evaluation"))
    except ValueError:
        raise BuildCompletionError("machine_delivery_acceptance_required") from None
    # Keep an executable acceptance contract tied to the retained operations
    # result, not merely another code check or a caller-authored status value.
    require(
        any(
            workflow.source.predecessor == deploy["address"] and all(artifact.bind_workflow_provenance for artifact in workflow.artifacts)
            for workflow in spec.workflows
        ),
        "machine_delivery_acceptance_required",
    )


def reachable(document, source, target):
    outgoing = {}
    for edge in document.get("edges", []):
        outgoing.setdefault(edge["from_address"], set()).add(edge["to_address"])
    seen, pending = {source}, list(outgoing.get(source, []))
    while pending:
        address = pending.pop()
        if address == target:
            return True
        if address not in seen:
            seen.add(address)
            pending.extend(outgoing.get(address, []))
    return False


async def retained_scope(session, *, plan, flow, node, deployment_id, acceptance_id):
    ids = {node.id, deployment_id, acceptance_id}
    require(len(ids) == 3, "distinct_delivery_nodes_required")
    rows = list(
        await session.scalars(
            select(OrchestrationNode)
            .where(OrchestrationNode.org_id == node.org_id, OrchestrationNode.flow_id == node.flow_id, OrchestrationNode.id.in_(ids))
            .execution_options(populate_existing=True)
        )
    )
    require(len(rows) == 3 and all(row.state != "superseded" for row in rows), "retained_delivery_nodes_missing")
    definitions = {row.id: definition(plan, flow, row) for row in rows}
    build, deploy, accept = (definitions[key] for key in (node.id, deployment_id, acceptance_id))
    require(recoverable_build(build), "verified_merge_build_required")
    executor = deploy.get("executor") or {}
    require(deploy["kind"] == "story" and executor.get("kind") == "agent" and executor.get("role") == "operate", "separate_operations_node_required")
    policy = ExecutionPolicy.model_validate(plan.plan_document["execution_policy"])
    require_delivery_acceptance(accept, deploy, policy)
    require(
        reachable(plan.plan_document, build["address"], deploy["address"]) and reachable(plan.plan_document, deploy["address"], accept["address"]),
        "delivery_dependency_path_required",
    )
    graph_nodes = list(
        await session.scalars(
            select(OrchestrationNode)
            .where(
                OrchestrationNode.org_id == node.org_id,
                OrchestrationNode.flow_id == node.flow_id,
            )
            .limit(10001)
        )
    )
    graph_edges = list(
        await session.scalars(
            select(OrchestrationEdge)
            .where(
                OrchestrationEdge.org_id == node.org_id,
                OrchestrationEdge.flow_id == node.flow_id,
            )
            .limit(10001)
        )
    )
    require(len(graph_nodes) <= 10000 and len(graph_edges) <= 10000, "build_completion_graph_limit")
    addresses = {row.id: graph_address(row, flow_slug=flow.slug) for row in graph_nodes if row.state != "superseded"}
    approved_edges = {(edge["from_address"], edge["to_address"]) for edge in plan.plan_document.get("edges", [])}
    retained = {
        "edges": [
            dict(from_address=addresses[edge.from_node_id], to_address=addresses[edge.to_node_id])
            for edge in graph_edges
            if edge.from_node_id in addresses
            and edge.to_node_id in addresses
            and (addresses[edge.from_node_id], addresses[edge.to_node_id]) in approved_edges
        ]
    }
    require(
        reachable(retained, build["address"], deploy["address"]) and reachable(retained, deploy["address"], accept["address"]),
        "delivery_graph_path_changed",
    )
    return {"build": build, "deployment": deploy, "acceptance": accept}


async def latest(session, node):
    return await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.flow_id == node.flow_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == KIND,
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


async def explicit_code_only(session, *, plan, node):
    """Interpret only an explicit plan field or an attributed, retained receipt."""
    if plan is None:
        return False
    flow = await session.get(OrchestrationFlow, node.flow_id)
    if flow is None:
        return False
    # Legacy plans need not carry executable node metadata.
    matches = [item for item in plan.plan_document.get("nodes", []) if item.get("address") == graph_address(node, flow_slug=flow.slug)]
    if len(matches) == 1 and matches[0].get("delivery_mode") == "code_only":
        try:
            return is_build(definition(plan, flow, node))
        except (ValueError, KeyError, TypeError):
            raise CycleBlockedError("build_completion_contract_changed") from None
    decision = await latest(session, node)
    if decision is None:
        return False
    try:
        from .plan_lineage import receipt_plan

        data = json.loads(decision.reason)
        original = await receipt_plan(session, plan, data, node_id=node.id)
        require(original is not None and data["plan_id"] == original.id, "build_completion_plan_changed")
        require(
            decision.actor_kind == ActorKind.HUMAN.value
            and bool(decision.actor_id)
            and data["contract"] == CONTRACT
            and data["node_id"] == node.id
            and data["node_attempt"] == node.attempts
            and data["delivery_mode"] == "code_only",
            "build_completion_receipt_invalid",
        )
        scope = await retained_scope(
            session, plan=plan, flow=flow, node=node, deployment_id=data["deployment_node_id"], acceptance_id=data["acceptance_node_id"]
        )
        require(scope == data["retained_scope"], "build_completion_scope_changed")
        return True
    except (ValueError, KeyError, TypeError):
        raise CycleBlockedError("build_completion_contract_changed") from None


async def target(session, *, flow_id, actor, request, lock=False):
    require(actor.actor_kind is ActorKind.HUMAN and bool(actor.actor_id), "human_plan_approver_required")
    flow_query = (
        select(OrchestrationFlow)
        .where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == actor.org_id)
        .execution_options(populate_existing=True)
    )
    flow = await session.scalar(flow_query.with_for_update() if lock else flow_query)
    require(flow is not None, "flow_not_found")
    plan_query = (
        select(OrchestrationAcceptedPlan)
        .where(
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.org_id == actor.org_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    plan = await session.scalar(plan_query.with_for_update() if lock else plan_query)
    require(
        plan is not None and (plan.version, plan.plan_hash) == (request.expected_plan_version, request.expected_plan_hash), "accepted_plan_changed"
    )
    query = (
        select(OrchestrationNode)
        .where(
            OrchestrationNode.id == request.node_id,
            OrchestrationNode.org_id == actor.org_id,
            OrchestrationNode.flow_id == flow_id,
        )
        .execution_options(populate_existing=True)
    )
    try:
        node = await session.scalar(query.with_for_update(nowait=True) if lock else query)
    except DBAPIError as error:
        if lock and (getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)) == "55P03":
            raise BuildCompletionError("build_completion_in_progress") from error
        raise
    require(node is not None and node.kind == "story", "build_story_not_found")
    return flow, plan, node


async def preview_build_completion(session, *, flow_id, actor, request, lock=False):
    from src.agentauth.bootstrap import BootstrapRefusedError

    from .deployment_authority import load_delivery_merge
    from .policy_admission import load_in_force_policy

    flow, plan, node = await target(session, flow_id=flow_id, actor=actor, request=request, lock=lock)
    require(flow.state in {"pending", "running"} and flow.execution_paused, "pause_flow_before_build_recovery")
    require(node.state == "passed", "verified_build_not_complete")
    admission = await load_in_force_policy(session, org_id=actor.org_id, flow_id=flow_id)
    require(admission.refusal is None and admission.policy is not None, "build_completion_policy_unavailable")
    require(
        Action.MERGE in admission.policy.allowed_actions and admission.policy.expires_at > datetime.now(UTC), "build_completion_authority_expired"
    )
    scope = await retained_scope(
        session, plan=plan, flow=flow, node=node, deployment_id=request.deployment_node_id, acceptance_id=request.acceptance_node_id
    )
    query = (
        select(OrchestrationExecution)
        .where(
            OrchestrationExecution.org_id == actor.org_id,
            OrchestrationExecution.flow_id == flow_id,
            OrchestrationExecution.node_id == node.id,
            OrchestrationExecution.status.not_in(["concluded", "superseded"]),
        )
        .execution_options(populate_existing=True)
    )
    try:
        executions = list(await session.scalars(query.with_for_update(nowait=True) if lock else query))
    except DBAPIError as error:
        if lock and (getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)) == "55P03":
            raise BuildCompletionError("build_completion_in_progress") from error
        raise
    require(len(executions) == 1, "single_merged_execution_required")
    execution = executions[0]
    require(execution.phase == "deployment_pending" and not execution.pending_action_key, "build_recovery_requires_unstarted_deployment")
    effect = await session.scalar(
        select(OrchestrationAction.id)
        .where(
            OrchestrationAction.org_id == actor.org_id,
            OrchestrationAction.execution_id == execution.id,
            OrchestrationAction.kind.in_(DEPLOYMENT_EFFECTS),
        )
        .limit(1)
    )
    require(effect is None, "deployment_effect_requires_reconciliation")
    identity = ExecutionIdentity(
        **{key: getattr(execution, key) for key in ("org_id", "node_id", "cycle", "accepted_plan_version", "claim_id", "claim_generation")}
    )
    try:
        _, _, merge = await load_delivery_merge(session, identity=identity, node=node)
    except BootstrapRefusedError:
        raise BuildCompletionError("verified_merge_receipt_unavailable") from None
    content = dict(
        contract=CONTRACT,
        plan_id=plan.id,
        plan_version=plan.version,
        plan_hash=plan.plan_hash,
        node_id=node.id,
        node_attempt=node.attempts,
        delivery_mode="code_only",
        deployment_node_id=request.deployment_node_id,
        acceptance_node_id=request.acceptance_node_id,
        retained_scope=scope,
        merge_receipt=merge.model_dump(mode="json"),
        execution_id=execution.id,
        reason=request.reason,
        deployment_verified=False,
        acceptance_granted=False,
    )
    snapshot = digest(
        dict(
            content=content,
            actor_id=actor.actor_id,
            execution_revision=execution.revision,
            execution_status=execution.status,
            node_attempt=node.attempts,
        )
    )
    return dict(
        wrote_nothing=True,
        snapshot=snapshot,
        content=content,
        worker_plan_version_unchanged=plan.version,
        budget_meter_unchanged=True,
        acceptance_preserved=True,
        acceptance_mode=admission.policy.evaluation_acceptance[scope["acceptance"]["address"]].value,
        human_acceptance_preserved=admission.policy.evaluation_acceptance[scope["acceptance"]["address"]] == "human",
    )


async def accept_build_completion(session, *, flow_id, actor, request):
    async with session.begin_nested():
        require(request.expected_snapshot is not None, "preview_snapshot_required")
        _, plan, node = await target(session, flow_id=flow_id, actor=actor, request=request, lock=True)
        identity = str(
            uuid5(
                NAMESPACE_URL,
                canonical(
                    dict(
                        org_id=actor.org_id,
                        flow_id=flow_id,
                        actor_id=actor.actor_id,
                        request=request.model_dump(mode="json", exclude={"expected_snapshot"}),
                    )
                ),
            )
        )
        existing = await session.get(OrchestrationDecision, identity, populate_existing=True)
        if existing is not None:
            require(
                existing.org_id == actor.org_id
                and existing.flow_id == flow_id
                and existing.node_id == node.id
                and existing.kind == KIND
                and existing.actor_kind == "human"
                and existing.actor_id == actor.actor_id,
                "build_completion_identity_conflict",
            )
            current = await latest(session, node)
            require(current is not None and current.id == existing.id, "build_completion_contract_superseded")
            require(await explicit_code_only(session, plan=plan, node=node), "build_completion_contract_changed")
            return dict(accepted=True, created=False, wrote_nothing=True, decision_id=identity)
        result = await preview_build_completion(session, flow_id=flow_id, actor=actor, request=request, lock=True)
        require(result["snapshot"] == request.expected_snapshot, "build_completion_snapshot_changed")
        session.add(
            OrchestrationDecision(
                id=identity,
                org_id=actor.org_id,
                flow_id=flow_id,
                node_id=node.id,
                kind=KIND,
                actor_id=actor.actor_id,
                actor_role=actor.actor_role,
                actor_kind=ActorKind.HUMAN.value,
                reason=canonical(result["content"]),
            )
        )
        # Fence an observation made before this receipt. The flow remains paused;
        # normal reconciliation must independently re-read the exact merge.
        execution = await session.get(OrchestrationExecution, result["content"]["execution_id"])
        execution.revision += 1
        execution.next_check_at = datetime.now(UTC)
        await session.flush()
        return dict(accepted=True, created=True, decision_id=identity, **{**result, "wrote_nothing": False})
