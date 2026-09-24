"""Version an unapproved draft without creating execution authority (#5331).

The flow lock serializes this operation with acceptance and amendments. Existing
approval decisions, execution records or started nodes make a flow ineligible.
Only PLAN_DRAFTED is appended; compile's non-approval supersede guard is unchanged.
"""

import hashlib
import json
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .amend import AmendmentContext, FlowNotFoundError, _reconcile_definitions, _supersede_absent_nodes
from .compile import ApprovalContext, ProposalRejectedError, TenantMismatchError, address_of, plan_hash, upsert_edges, upsert_nodes
from .genesis import APPROVAL_DECISION_KINDS
from .models import DecisionKind, OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from .proposal import LoopProposal, validate_proposal
from .registration import acceptance_gate_address, transform_for_registration
from .repository import OrchestrationRepository
from .state import ActorKind, NodeState


class DraftRevisionConflictError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


class DraftRevisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    proposal: LoopProposal
    expected_plan_version: int = Field(ge=1)
    expected_plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    reason: str | None = Field(default=None, max_length=2000)


class SaveDraftRevisionRequest(DraftRevisionRequest):
    expected_proposal_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


@dataclass
class DraftContext:
    flow: OrchestrationFlow
    plan: OrchestrationAcceptedPlan
    nodes: list[OrchestrationNode]
    decision: OrchestrationDecision
    proposal: LoopProposal
    gate_id: str
    gate_address: str


def _conflict(code: str, message: str):
    raise DraftRevisionConflictError(code, message)


async def _context(session: AsyncSession, flow_id: str, actor: ApprovalContext, *, lock: bool) -> DraftContext:
    if ActorKind(actor.actor_kind) is not ActorKind.HUMAN:
        _conflict("draft_revision_operator_required", "An authorized human operator must request this draft revision.")
    query = select(OrchestrationFlow).where(OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == actor.org_id)
    if lock:
        query = query.with_for_update()
    flow = await session.scalar(query.execution_options(populate_existing=True))
    if flow is None:
        raise FlowNotFoundError("No orchestration flow in this tenant.")
    repo = OrchestrationRepository(session)
    plan = await repo.get_accepted_plan(org_id=actor.org_id, flow_id=flow_id)
    if plan is None:
        _conflict("draft_revision_unavailable", "This flow has no registered draft to revise.")
    await session.refresh(plan)
    decisions = await repo.list_decisions(org_id=actor.org_id, flow_id=flow_id)
    decision = next((d for d in decisions if d.id == plan.accepted_by_decision_id), None)
    if decision is None or decision.kind != DecisionKind.PLAN_DRAFTED.value or any(d.kind in APPROVAL_DECISION_KINDS for d in decisions):
        _conflict("draft_already_approved", "This flow has been approved; use the accepted-plan amendment path.")
    if plan.plan_document.get("execution_policy") is not None or plan.plan_document.get("execution_continuation"):
        _conflict("draft_already_approved", "Active execution authority cannot be edited as an inert draft.")
    proposal = LoopProposal.model_validate(plan.plan_document)
    if flow.state != NodeState.PENDING.value:
        _conflict("draft_execution_started", "The flow has left its initial pending state.")
    node_query = select(OrchestrationNode).where(OrchestrationNode.org_id == actor.org_id, OrchestrationNode.flow_id == flow_id)
    node_query = node_query.order_by(OrchestrationNode.id)
    if lock:
        node_query = node_query.with_for_update()
    nodes = list((await session.scalars(node_query.execution_options(populate_existing=True))).all())
    gate_address = acceptance_gate_address(proposal)
    gate = next((n for n in nodes if address_of(flow.slug, n) == gate_address), None)
    if gate is None or gate.state != NodeState.AWAITING_GATE.value:
        _conflict("draft_gate_changed", "The original acceptance gate must remain unanswered.")
    if any(d.node_id == gate.id and d.kind in {DecisionKind.GATE_APPROVED.value, DecisionKind.GATE_REJECTED.value} for d in decisions):
        _conflict("draft_gate_changed", "The acceptance gate already has an answer in its history.")
    if any(n.attempts or (n.id != gate.id and n.state not in {NodeState.PENDING.value, NodeState.SUPERSEDED.value}) for n in nodes):
        _conflict("draft_execution_started", "Only unstarted draft nodes can be revised.")
    execution = await session.scalar(
        select(OrchestrationExecution.id).where(OrchestrationExecution.org_id == actor.org_id, OrchestrationExecution.flow_id == flow_id).limit(1)
    )
    if execution is not None:
        _conflict("draft_execution_started", "This flow already has execution history.")
    return DraftContext(flow, plan, nodes, decision, proposal, gate.id, gate_address)


def _replacement(context: DraftContext, request: DraftRevisionRequest, actor: ApprovalContext) -> LoopProposal:
    proposal = request.proposal
    if proposal.org_id != actor.org_id:
        raise TenantMismatchError("The proposal must belong to the authenticated tenant.")
    if proposal.flow_slug != context.flow.slug or proposal.intent_ref != context.proposal.intent_ref:
        raise ProposalRejectedError("A draft revision must retain the flow slug and originating intent.")
    # Accept authored documents only. The same registration transform supplies the
    # gate and demotes submitted bounds, so no caller can bypass the root gate.
    effective, gate_address = transform_for_registration(proposal)
    violations = validate_proposal(effective)
    if violations:
        raise ProposalRejectedError("Invalid draft revision.", violations=violations)
    if gate_address != context.gate_address or acceptance_gate_address(effective) != context.gate_address:
        _conflict("draft_gate_changed", "Keep the first wave and the existing acceptance gate address.")
    old_gate = next(n for n in context.proposal.nodes if n.address == context.gate_address)
    new_gate = next(n for n in effective.nodes if n.address == context.gate_address)
    if old_gate != new_gate:
        _conflict("draft_gate_changed", "The existing acceptance gate definition must be preserved.")
    # Mirroring registration, live evaluation specifications cannot be authored
    # through an inert write. Bind implemented runners through human acceptance.
    if any(n.evaluation is not None for n in effective.nodes):
        raise ProposalRejectedError("Runtime evaluation specifications require the evaluation acceptance path.")
    # Do not silently discard existing proposed bounds during a graph edit.
    if context.proposal.proposed_execution_policy is not None and effective.proposed_execution_policy is None:
        _conflict("draft_policy_removed", "Retain proposed policy bounds; revising a draft cannot remove its execution limits.")
    by_address = {address_of(context.flow.slug, n): n for n in context.nodes}
    for node in effective.nodes:
        existing = by_address.get(node.address)
        if existing and (existing.state == NodeState.SUPERSEDED.value or existing.kind != node.kind):
            _conflict("draft_node_identity_conflict", f"Use a new address instead of reusing {node.address}.")
    return effective


def _base_matches(context: DraftContext, request: DraftRevisionRequest) -> bool:
    return context.plan.version == request.expected_plan_version and context.plan.plan_hash == request.expected_plan_hash


def draft_hash(proposal: LoopProposal) -> str:
    """Bind the complete reviewed document, including descriptive draft edits."""
    document = json.dumps(proposal.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(document.encode()).hexdigest()


def _is_replay(context: DraftContext, request: SaveDraftRevisionRequest, actor: ApprovalContext, digest: str) -> bool:
    if (
        draft_hash(context.proposal) != digest
        or context.plan.version != request.expected_plan_version + 1
        or context.decision.actor_id != actor.actor_id
    ):
        return False
    try:
        receipt = json.loads(context.decision.reason or "")
    except ValueError:
        return False
    return (
        isinstance(receipt, dict)
        and receipt.get("operation") == "draft_revision"
        and receipt.get("base_plan_version") == request.expected_plan_version
        and receipt.get("base_plan_hash") == request.expected_plan_hash
        and receipt.get("proposal_hash") == digest
    )


def _summary(context: DraftContext, effective: LoopProposal) -> dict:
    before = {n.address for n in context.proposal.nodes}
    after = {n.address for n in effective.nodes}
    old_edges = {(e.from_address, e.to_address) for e in context.proposal.edges}
    new_edges = {(e.from_address, e.to_address) for e in effective.edges}
    return {
        "flow_id": context.flow.id,
        "base_plan_version": context.plan.version,
        "base_plan_hash": context.plan.plan_hash,
        "proposal_hash": draft_hash(effective),
        "acceptance_gate_id": context.gate_id,
        "acceptance_gate_address": context.gate_address,
        "execution_paused": context.flow.execution_paused,
        "execution_authorized": False,
        "added_nodes": sorted(after - before),
        "removed_nodes": sorted(before - after),
        "retained_nodes": sorted(before & after),
        "added_edges": sorted(new_edges - old_edges),
        "removed_edges": sorted(old_edges - new_edges),
        "proposal": effective.model_dump(mode="json"),
    }


async def preview_draft_revision(session: AsyncSession, flow_id: str, request: DraftRevisionRequest, actor: ApprovalContext) -> dict:
    context = await _context(session, flow_id, actor, lock=False)
    if not _base_matches(context, request):
        _conflict("stale_draft_revision", "The current draft changed; read its plan version and hash and preview again.")
    effective = _replacement(context, request, actor)
    return {**_summary(context, effective), "wrote_nothing": True}


async def save_draft_revision(session: AsyncSession, flow_id: str, request: SaveDraftRevisionRequest, actor: ApprovalContext) -> dict:
    async with session.begin_nested():
        context = await _context(session, flow_id, actor, lock=True)
        effective = _replacement(context, request, actor)
        digest = draft_hash(effective)
        if digest != request.expected_proposal_hash:
            _conflict("stale_draft_preview", "The effective proposal differs from the reviewed preview; preview again.")
        replay = _is_replay(context, request, actor, digest)
        if not _base_matches(context, request) and not replay:
            _conflict("stale_draft_revision", "The current draft changed; read its plan version and hash and preview again.")
        summary = _summary(context, effective)
        if replay or draft_hash(context.proposal) == digest:
            return {**summary, "plan_version": context.plan.version, "plan_hash": context.plan.plan_hash, "already_revised": True}
        repo = OrchestrationRepository(session)
        _reconcile_definitions(context.nodes, effective)
        _supersede_absent_nodes(
            context.nodes,
            proposed_addresses={n.address for n in effective.nodes},
            flow_slug=context.flow.slug,
            actor=AmendmentContext(org_id=actor.org_id, actor_id=actor.actor_id, actor_role=actor.actor_role, reason=request.reason),
        )
        node_ids, _ = await upsert_nodes(repo, proposal=effective, org_id=actor.org_id, flow_id=flow_id)
        wanted = {(node_ids[e.from_address], node_ids[e.to_address]) for e in effective.edges}
        for edge in await repo.list_edges(org_id=actor.org_id, flow_id=flow_id):
            if (edge.from_node_id, edge.to_node_id) not in wanted:
                await session.delete(edge)
        await session.flush()
        await upsert_edges(repo, proposal=effective, org_id=actor.org_id, flow_id=flow_id, node_ids=node_ids)
        context.flow.title = effective.title
        context.flow.description = effective.description
        context.flow.design_history = effective.design_history.model_dump(mode="json") if effective.design_history else None
        decision = await repo.append_decision(
            org_id=actor.org_id,
            flow_id=flow_id,
            kind=DecisionKind.PLAN_DRAFTED.value,
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            actor_kind=ActorKind.HUMAN.value,
            reason=json.dumps(
                {
                    "operation": "draft_revision",
                    "base_plan_version": context.plan.version,
                    "base_plan_hash": context.plan.plan_hash,
                    "proposal_hash": digest,
                    "reason": request.reason,
                }
            ),
        )
        plan = await repo.record_accepted_plan(
            org_id=actor.org_id,
            flow_id=flow_id,
            plan_document=effective.model_dump(mode="json"),
            plan_hash=plan_hash(effective),
            accepted_by_decision_id=decision.id,
        )
        return {**summary, "plan_version": plan.version, "plan_hash": plan.plan_hash, "already_revised": False}
