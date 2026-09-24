"""Accept one bounded evidence contract without changing active worker assignments."""

from __future__ import annotations

import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from .compile import ApprovalContext
from .dispatch import graph_address
from .execution_policy import _STAMPED_FIELDS, AcceptanceMode, Action, ExecutionPolicy, stamp_policy
from .models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationEdge, OrchestrationFlow, OrchestrationNode
from .repository_evaluation_contract import canonical, harness_digest, native_specification
from .review_cycle import CycleBlockedError
from .shared_policy import shared_inputs
from .state import ActorKind

ACCEPTANCE_KIND = "evaluation_contract_accepted"
CONTRACT = "repository-evaluation-acceptance/v1"


class EvaluationAcceptanceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str = Field(min_length=1, max_length=36)
    expected_plan_version: int = Field(gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    specification: dict
    authorize_evaluate: StrictBool = False
    authorize_workflow_dispatch: StrictBool = False
    authorize_cli_qualification_dispatch: StrictBool = False
    reason: str = Field(min_length=8, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class EvaluationAcceptanceError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, reason):
    if not condition:
        raise EvaluationAcceptanceError(reason)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def node_scope(node):
    return {key: getattr(node, key) for key in ("kind", "title", "issue_ref")}


def evaluation_policy(base, *, actor_id, address):
    """A strict subset except the single explicitly accepted evaluate action.

    This policy never replaces the worker policy or keys a new budget meter.
    Its receipt explicitly retains the original policy/meter reference.
    """
    raw = base.model_dump(mode="json", exclude=_STAMPED_FIELDS)
    raw.update(allowed_actions=["evaluate"], human_gates=[], evaluation_acceptance={address: "machine"}, user_credentials=None, coordination=None)
    return stamp_policy(ExecutionPolicy.model_validate(raw), principal_id=actor_id, org_id=base.org_id)


async def latest_acceptance(session, *, node, plan):
    return await session.scalar(
        select(OrchestrationDecision)
        .where(
            OrchestrationDecision.org_id == node.org_id,
            OrchestrationDecision.flow_id == node.flow_id,
            OrchestrationDecision.node_id == node.id,
            OrchestrationDecision.kind == ACCEPTANCE_KIND,
            OrchestrationDecision.reason.is_not(None),
        )
        .order_by(OrchestrationDecision.created_at.desc(), OrchestrationDecision.id.desc())
        .limit(1)
        .execution_options(populate_existing=True)
    )


async def accepted_contract(session, *, node, plan):
    """Only a human-attributed, unchanged current-plan contract can supply evidence."""
    decision = await latest_acceptance(session, node=node, plan=plan)
    if decision is None:
        return None
    try:
        data = json.loads(decision.reason)
        from .plan_lineage import receipt_plan

        plan = await receipt_plan(session, plan, data, node_id=node.id)
        if plan is None or data.get("plan_id") != plan.id:
            return None
        base = ExecutionPolicy.model_validate((plan.plan_document or {}).get("execution_policy"))
        address = data["address"]
        flow = await session.get(OrchestrationFlow, node.flow_id)
        require(flow is not None and address == graph_address(node, flow_slug=flow.slug), "evaluation_address_changed")
        spec = native_specification(data["specification"])
        policy = evaluation_policy(base, actor_id=decision.actor_id, address=address)
        require(
            decision.actor_kind == ActorKind.HUMAN.value
            and data["contract"] == CONTRACT
            and data["node_id"] == node.id
            and data["node_scope"] == node_scope(node)
            and data["base_policy_id"] == base.policy_id
            and data["base_policy_hash"] == base.policy_hash
            and data["specification_hash"] == digest(spec.model_dump(mode="json"))
            and data["evaluation_policy"] == policy.model_dump(mode="json")
            and base.evaluation_acceptance.get(address) is AcceptanceMode.MACHINE
            and Action.EVALUATE not in base.human_gates
            and (Action.EVALUATE in base.allowed_actions or data["authorize_evaluate"] is True),
            "evaluation_acceptance_unverifiable",
        )
        is_cli = spec.evidence_schema == "cli-live-evaluation/v1"
        grant = "authorize_cli_qualification_dispatch" if is_cli else "authorize_workflow_dispatch"
        require(spec.producer is None or data.get(grant) is True, "producer_authorization_required")
        return decision, spec, policy
    except (ValueError, KeyError, TypeError):
        raise CycleBlockedError("evaluation_acceptance_unverifiable") from None


async def current_target(session, *, flow_id, actor, request, lock=False):
    """Refresh the current scope under the same lock order as settlement."""
    require(actor.actor_kind is ActorKind.HUMAN and bool(actor.actor_id), "human_plan_approver_required")
    flow_query = (
        select(OrchestrationFlow)
        .where(OrchestrationFlow.org_id == actor.org_id, OrchestrationFlow.id == flow_id)
        .execution_options(populate_existing=True)
    )
    flow = await session.scalar(flow_query.with_for_update() if lock else flow_query)
    require(flow is not None, "flow_not_found")
    plan_query = (
        select(OrchestrationAcceptedPlan)
        .where(
            OrchestrationAcceptedPlan.org_id == actor.org_id,
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    plan = await session.scalar(plan_query.with_for_update() if lock else plan_query)
    require(
        plan is not None and (plan.version, plan.plan_hash) == (request.expected_plan_version, request.expected_plan_hash),
        "accepted_plan_changed",
    )
    query = (
        select(OrchestrationNode)
        .where(
            OrchestrationNode.org_id == actor.org_id,
            OrchestrationNode.flow_id == flow_id,
            OrchestrationNode.id == request.node_id,
        )
        .execution_options(populate_existing=True)
    )
    try:
        # Production dispatch already holds READY-node locks before it takes the
        # flow lock. Never wait for its node while holding the flow/plan locks;
        # acceptance's savepoint releases them before returning a retryable conflict.
        node = await session.scalar(query.with_for_update(nowait=True) if lock else query)
    except DBAPIError as error:
        if lock and (getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)) == "55P03":
            raise EvaluationAcceptanceError("evaluation_dispatch_in_progress") from error
        raise
    require(node is not None and node.kind == "eval", "evaluation_not_found")
    return flow, plan, node


async def preview_evaluation(session, *, flow_id, actor: ApprovalContext, request: EvaluationAcceptanceRequest, lock=False):
    flow, plan, node = await current_target(session, flow_id=flow_id, actor=actor, request=request, lock=lock)
    require(flow.state in {"pending", "running"}, "flow_not_active")
    require(node.state in {"pending", "ready"}, "evaluation_not_pending")
    inputs, _ = await shared_inputs(session, org_id=actor.org_id, flow_id=flow_id)
    address = graph_address(node, flow_slug=flow.slug)
    nodes = [row for row in (plan.plan_document or {}).get("nodes", []) if row.get("address") == address]
    require(len(nodes) == 1 and {key: nodes[0].get(key) for key in node_scope(node)} == node_scope(node), "accepted_node_changed")
    original = nodes[0].get("evaluation")
    require(
        original is None or original.get("evidence_schema") in {"repository-evaluation/v1", "cli-live-evaluation/v1"},
        "live_contract_cannot_be_replaced",
    )
    base = inputs.policy
    require(base.evaluation_acceptance.get(address) is AcceptanceMode.MACHINE, "machine_acceptance_not_in_plan")
    require(Action.EVALUATE not in base.human_gates, "explicit_human_gate_preserved")
    require(Action.EVALUATE in base.allowed_actions or request.authorize_evaluate, "explicit_evaluate_authorization_required")
    spec = native_specification(request.specification)
    attached = await accepted_contract(session, node=node, plan=plan)
    require(
        not (
            (original or {}).get("evidence_schema") == "cli-live-evaluation/v1"
            or (attached and attached[1].evidence_schema == "cli-live-evaluation/v1")
        )
        or spec.evidence_schema == "cli-live-evaluation/v1",
        "live_contract_cannot_be_replaced",
    )
    is_cli = spec.evidence_schema == "cli-live-evaluation/v1"
    require(not is_cli or node.issue_ref == str(spec.qualification.owner_issue), "cli_qualification_owner_changed")
    grant = request.authorize_cli_qualification_dispatch if is_cli else request.authorize_workflow_dispatch
    require(not (request.authorize_workflow_dispatch if is_cli else request.authorize_cli_qualification_dispatch), "wrong_producer_dispatch_grant")
    require(spec.producer is None or grant, "explicit_producer_authorization_required")
    require(spec.producer is not None or not grant, "producer_specification_required")
    require(spec.runner.repository in base.repository_ids, "repository_not_permitted")
    require(spec.runner.harness_sha256 == harness_digest(), "evaluation_harness_mismatch")
    require(len(canonical(spec.model_dump(mode="json"))) <= 256 * 1024, "evaluation_specification_too_large")
    parents = list(
        await session.scalars(
            select(OrchestrationNode)
            .join(OrchestrationEdge, OrchestrationEdge.from_node_id == OrchestrationNode.id)
            .where(
                OrchestrationNode.org_id == actor.org_id,
                OrchestrationNode.flow_id == flow_id,
                OrchestrationNode.kind == "story",
                OrchestrationEdge.org_id == actor.org_id,
                OrchestrationEdge.to_node_id == node.id,
            )
            .limit(129)
        )
    )
    require(
        {graph_address(parent, flow_slug=flow.slug) for parent in parents} == {item.address for item in spec.predecessors}, "predecessor_set_changed"
    )
    policy = evaluation_policy(base, actor_id=actor.actor_id, address=address)
    previous = await latest_acceptance(session, node=node, plan=plan)
    content = dict(
        contract=CONTRACT,
        plan_id=plan.id,
        plan_version=plan.version,
        plan_hash=plan.plan_hash,
        node_id=node.id,
        node_scope=node_scope(node),
        address=address,
        specification=spec.model_dump(mode="json"),
        specification_hash=digest(spec.model_dump(mode="json")),
        base_policy_id=base.policy_id,
        base_policy_hash=base.policy_hash,
        evaluation_policy=policy.model_dump(mode="json"),
        authorize_evaluate=request.authorize_evaluate,
        authorize_workflow_dispatch=request.authorize_workflow_dispatch,
        authorize_cli_qualification_dispatch=request.authorize_cli_qualification_dispatch,
        reason=request.reason,
    )
    snapshot = digest(
        dict(
            content=content, actor_id=actor.actor_id, node_state=node.state, node_attempt=node.attempts, previous_id=previous.id if previous else None
        )
    )
    return dict(wrote_nothing=True, snapshot=snapshot, content=content, worker_plan_version_unchanged=plan.version, budget_meter_unchanged=True)


async def accept_evaluation(session, *, flow_id, actor, request):
    # A contended node must release the preceding flow/plan locks even when a
    # caller catches the conflict and continues its surrounding transaction.
    async with session.begin_nested():
        return await _accept_evaluation_locked(session, flow_id=flow_id, actor=actor, request=request)


async def _accept_evaluation_locked(session, *, flow_id, actor, request):
    require(request.expected_snapshot is not None, "preview_snapshot_required")
    _, plan, node = await current_target(session, flow_id=flow_id, actor=actor, request=request, lock=True)
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
            and existing.node_id == request.node_id
            and existing.kind == ACCEPTANCE_KIND
            and existing.actor_kind == ActorKind.HUMAN.value
            and existing.actor_id == actor.actor_id,
            "acceptance_identity_conflict",
        )
        latest = await accepted_contract(session, node=node, plan=plan)
        require(latest is not None and latest[0].id == existing.id, "evaluation_contract_superseded")
        content = json.loads(existing.reason)
        require(
            content["specification"] == native_specification(request.specification).model_dump(mode="json")
            and content["authorize_evaluate"] == request.authorize_evaluate
            and content.get("authorize_workflow_dispatch", False) == request.authorize_workflow_dispatch
            and content.get("authorize_cli_qualification_dispatch", False) == request.authorize_cli_qualification_dispatch
            and content["reason"] == request.reason,
            "acceptance_identity_conflict",
        )
        # A lost acceptance response remains acknowledgeable after the evaluator
        # finishes or the original allowance expires. This creates no authority.
        return dict(
            accepted=True,
            created=False,
            decision_id=existing.id,
            wrote_nothing=True,
            content=content,
            worker_plan_version_unchanged=plan.version,
            budget_meter_unchanged=True,
        )
    result = await preview_evaluation(session, flow_id=flow_id, actor=actor, request=request, lock=True)
    require(result["snapshot"] == request.expected_snapshot, "evaluation_snapshot_changed")
    session.add(
        OrchestrationDecision(
            id=identity,
            org_id=actor.org_id,
            flow_id=flow_id,
            node_id=request.node_id,
            kind=ACCEPTANCE_KIND,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            actor_kind=ActorKind.HUMAN.value,
            reason=canonical(result["content"]),
        )
    )
    await session.flush()
    return dict(accepted=True, created=True, decision_id=identity, **{**result, "wrote_nothing": False})
