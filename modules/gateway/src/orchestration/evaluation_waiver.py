"""An attributed exception for one unexecuted evaluation, never a test verdict."""

import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from .compile import ApprovalContext
from .dispatch import graph_address
from .evaluation_acceptance import EvaluationAcceptanceError as WaiverError
from .evaluation_acceptance import current_target, digest, node_scope
from .execution_policy import AcceptanceMode, Action
from .models import OrchestrationDecision, OrchestrationEdge, OrchestrationExecution, OrchestrationNode, OrchestrationPullRequestBinding
from .pr_bindings import BindingError, active_binding_for_node, binding_scope_matches, binding_snapshot
from .repository import OrchestrationRepository
from .shared_policy import shared_inputs
from .state import ActorKind, NodeState, transition

KIND = "evaluation_waived"
CONTRACT = "evaluation-waiver/v1"


class WaiverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=36)
    expected_plan_version: int = Field(gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    # This is a whole-node exception; these identifiers document the owner's scope.
    criterion_ids: list[str] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=8, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def require(condition, code):
    if not condition:
        raise WaiverError(code)


async def predecessors(session, node, flow, *, lock=False):
    query = (
        select(OrchestrationNode)
        .join(OrchestrationEdge, OrchestrationEdge.from_node_id == OrchestrationNode.id)
        .where(
            OrchestrationEdge.org_id == node.org_id,
            OrchestrationEdge.flow_id == node.flow_id,
            OrchestrationEdge.to_node_id == node.id,
            OrchestrationNode.org_id == node.org_id,
            OrchestrationNode.flow_id == node.flow_id,
        )
        .order_by(OrchestrationNode.id)
        .limit(129)
        .execution_options(populate_existing=True)
    )
    try:
        parents = list(await session.scalars(query.with_for_update(of=OrchestrationNode, nowait=True) if lock else query))
    except DBAPIError as error:
        if lock and (getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)) == "55P03":
            raise WaiverError("evaluation_dispatch_in_progress") from error
        raise
    require(0 < len(parents) <= 128, "waiver_predecessors_missing_or_excessive")
    require(all(parent.state == "passed" for parent in parents), "waiver_predecessors_not_complete")
    result = []
    for parent in parents:
        try:
            binding = await active_binding_for_node(session, org_id=node.org_id, node_id=parent.id, attempt=parent.attempts)
        except BindingError as error:
            raise WaiverError("waiver_predecessor_binding_ambiguous") from error
        if binding is not None and lock:
            # Parent locks serialize normal binding changes. Also retain the
            # exact binding row through the acceptance commit.
            binding = await session.scalar(
                select(OrchestrationPullRequestBinding)
                .where(OrchestrationPullRequestBinding.id == binding.id, OrchestrationPullRequestBinding.org_id == node.org_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        require(
            parent.kind != "story" or (binding is not None and binding.state == "active" and binding_scope_matches(binding, parent)),
            "waiver_predecessor_binding_missing",
        )
        result.append(
            dict(
                node_id=parent.id,
                address=graph_address(parent, flow_slug=flow.slug),
                scope=node_scope(parent),
                state=parent.state,
                attempt=parent.attempts,
                binding=binding_snapshot(binding) if binding is not None else None,
            )
        )
    return result


async def latest_waiver(session, node):
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


async def valid_waiver(session, node):
    """Recheck the immutable exception before it can satisfy any dependency."""
    if node.kind != "eval" or node.state != NodeState.WAIVED or node.attempts != 0:
        return None
    record = await latest_waiver(session, node)
    if record is None or record.actor_kind != "human" or not record.actor_id or record.to_state != "waived":
        return None
    repo = OrchestrationRepository(session)
    plan = await repo.get_accepted_plan(org_id=node.org_id, flow_id=node.flow_id)
    flow = await repo.get_flow(org_id=node.org_id, flow_id=node.flow_id)
    if plan is None or flow is None:
        return None
    try:
        content = json.loads(record.reason)
        expected = dict(
            contract=CONTRACT,
            plan_id=plan.id,
            plan_version=plan.version,
            plan_hash=plan.plan_hash,
            node_id=node.id,
            node_scope=node_scope(node),
            address=graph_address(node, flow_slug=flow.slug),
            evaluation_executed=False,
            machine_pass_claimed=False,
        )
        if any(content.get(key) != value for key, value in expected.items()):
            return None
        if content["predecessors"] != await predecessors(session, node, flow):
            return None
        return record
    except (ValueError, KeyError, TypeError):
        return None


async def preview_waiver(session, *, flow_id, actor: ApprovalContext, request: WaiverRequest, lock=False):
    flow, plan, node = await current_target(session, flow_id=flow_id, actor=actor, request=request, lock=lock)
    require(flow.state in {"pending", "running"}, "flow_not_active")
    require(node.state in {"pending", "ready", "waived"} and node.attempts == 0, "waiver_requires_unexecuted_evaluation")
    require(
        not await session.scalar(
            select(OrchestrationExecution.id).where(OrchestrationExecution.org_id == node.org_id, OrchestrationExecution.node_id == node.id).limit(1)
        ),
        "waiver_evaluation_execution_exists",
    )
    require(
        len(set(request.criterion_ids)) == len(request.criterion_ids) and all(0 < len(item.strip()) <= 128 for item in request.criterion_ids),
        "waiver_criterion_ids_invalid",
    )
    inputs, _ = await shared_inputs(session, org_id=actor.org_id, flow_id=flow_id)
    require(inputs.policy.principal_id == actor.actor_id, "existing_policy_owner_required")
    require(inputs.policy.expires_at > datetime.now(UTC), "execution_policy_expired")
    address = graph_address(node, flow_slug=flow.slug)
    rows = [row for row in plan.plan_document.get("nodes", []) if row.get("address") == address]
    require(len(rows) == 1 and all(rows[0].get(key) == value for key, value in node_scope(node).items()), "accepted_node_changed")
    require(inputs.policy.evaluation_acceptance.get(address) is AcceptanceMode.MACHINE, "waiver_requires_machine_evaluation")
    require(Action.EVALUATE not in inputs.policy.human_gates, "explicit_human_gate_preserved")
    # A configured/live evaluator needs a separate explicit change, not this
    # exception for a checkpoint that has never acquired a runnable contract.
    require(rows[0].get("evaluation") is None, "waiver_requires_unconfigured_evaluation")
    from .evaluation_acceptance import accepted_contract

    require(await accepted_contract(session, node=node, plan=plan) is None, "waiver_evaluation_contract_exists")
    previous = await latest_waiver(session, node)
    content = dict(
        contract=CONTRACT,
        plan_id=plan.id,
        plan_version=plan.version,
        plan_hash=plan.plan_hash,
        node_id=node.id,
        node_scope=node_scope(node),
        address=address,
        criterion_ids=request.criterion_ids,
        reason=request.reason,
        predecessors=await predecessors(session, node, flow, lock=lock),
        evaluation_executed=False,
        machine_pass_claimed=False,
    )
    snapshot = digest(dict(content=content, actor_id=actor.actor_id, node_state=node.state, previous_id=previous.id if previous else None))
    return dict(
        wrote_nothing=True,
        snapshot=snapshot,
        content=content,
        worker_plan_version_unchanged=plan.version,
        budget_meter_unchanged=True,
        resulting_state="waived",
    )


async def accept_waiver(session, *, flow_id, actor, request):
    require(request.expected_snapshot is not None, "preview_snapshot_required")
    async with session.begin_nested():
        _, plan, node = await current_target(session, flow_id=flow_id, actor=actor, request=request, lock=True)
        identity = str(
            uuid5(
                NAMESPACE_URL,
                digest(
                    dict(
                        kind=KIND,
                        org_id=actor.org_id,
                        flow_id=flow_id,
                        actor_id=actor.actor_id,
                        request=request.model_dump(mode="json", exclude={"expected_snapshot"}),
                    )
                ),
            )
        )
        existing = await session.get(OrchestrationDecision, identity)
        if existing is not None:
            current = await valid_waiver(session, node)
            require(current is not None and current.id == existing.id, "waiver_superseded_or_invalid")
            return dict(
                accepted=True,
                created=False,
                wrote_nothing=True,
                decision_id=identity,
                state="waived",
                content=json.loads(existing.reason),
                worker_plan_version_unchanged=plan.version,
                budget_meter_unchanged=True,
            )
        preview = await preview_waiver(session, flow_id=flow_id, actor=actor, request=request, lock=True)
        require(preview["snapshot"] == request.expected_snapshot, "waiver_snapshot_changed")
        before = node.state
        if before != "waived":
            require(transition(before, NodeState.WAIVED, actor_kind=actor.actor_kind, reason=request.reason).allowed, "waiver_transition_refused")
            node.state = NodeState.WAIVED.value
        node.updated_at = datetime.now(UTC)
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
                from_state=before,
                to_state=NodeState.WAIVED.value,
                reason=json.dumps(preview["content"], sort_keys=True),
            )
        )
        await session.flush()
        # The normal tick performs successor readiness and policy admission. No
        # run, attempt, claim, budget reservation or evaluation evidence is made.
        return dict(accepted=True, created=True, decision_id=identity, state="waived", **{**preview, "wrote_nothing": False})
