"""Human preview/accept for dependency changes in never-started waves.

This bounded operation preserves node definitions and all shared contracts. It
does not perform a general plan replacement or restart any existing worker.
"""

import copy
import json
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field

from .compile import address_of
from .continuation import digest
from .models import OrchestrationDecision, OrchestrationEdge
from .plan_lineage import CONTRACT, MAX_HOPS, edge_set, wave
from .proposal import LoopProposal, ProposedEdge, validate_proposal
from .repository import OrchestrationRepository
from .shared_amendment import current_plan, history, request_hash, require, rows
from .shared_policy import shared_inputs


class WaveDependencyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_plan_version: int = Field(strict=True, gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    added_edges: list[ProposedEdge] = Field(default_factory=list, max_length=200)
    removed_edges: list[ProposedEdge] = Field(default_factory=list, max_length=200)
    reason: str = Field(min_length=8, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def frozen_waves(found, decisions):
    """History is sticky: resetting a state/attempt never reopens its wave."""
    started = {n.id for n in found["nodes"] if n.attempts or n.state not in {"pending", "ready"}}
    for key in ("executions", "reports", "bindings"):
        started.update(row.node_id for row in found[key])
    for decision in decisions:
        if (
            decision.kind in {"node_dispatched", "agent_dispatched", "evaluation_contract_accepted", "evaluation_waived"}
            or decision.from_state not in {None, "pending", "ready"}
            or decision.to_state not in {None, "pending", "ready"}
        ):
            started.add(decision.node_id)
    # Claims survive failed dispatches and can outlive reset counters.
    claimed_issues = {str(c.issue_number) for c in found["claims"]}
    return {(n.epic_ref, n.wave_ref) for n in found["nodes"] if n.id in started or str(n.issue_ref or "").lstrip("#") in claimed_issues}


async def prepare_dependencies(session, *, flow, plan, actor, request, lock=False):
    require(flow.execution_paused, "flow_must_be_paused")
    require(flow.state in {"pending", "running"}, "flow_not_active")
    require((plan.version, plan.plan_hash) == (request.expected_plan_version, request.expected_plan_hash), "accepted_plan_changed")
    require(plan.plan_hash == digest(plan.plan_document), "accepted_document_hash_changed")
    previous = await session.get(OrchestrationDecision, plan.accepted_by_decision_id)
    try:
        prior_data = json.loads(previous.reason or "{}") if previous else {}
    except ValueError:
        prior_data = {}  # Original plan approvals can carry a plain-text reason.
    depth = prior_data.get("amendment_depth", 0) if isinstance(prior_data, dict) and prior_data.get("contract") == CONTRACT else 0
    require(type(depth) is int and 0 <= depth < MAX_HOPS, "amendment_lineage_limit")
    inputs, marker = await shared_inputs(session, org_id=actor.org_id, flow_id=flow.id)
    require(inputs.policy.principal_id == actor.actor_id, "existing_policy_owner_required")
    require(marker.get("delivery_mode") == "code_only", "code_only_continuation_required")
    require(datetime.now(UTC) < inputs.policy.expires_at, "policy_expired")
    found = await history(session, flow=flow, lock=lock)
    require(all(r.state not in {"queued", "dispatched"} for r in found["authoring"]), "unfinished_amendment_authoring")
    decisions = await rows(
        session,
        OrchestrationDecision,
        [OrchestrationDecision.org_id == flow.org_id, OrchestrationDecision.flow_id == flow.id, OrchestrationDecision.node_id.is_not(None)],
    )
    document = copy.deepcopy(plan.plan_document)
    require(set(document) <= set(LoopProposal.model_fields) | {"execution_continuation"}, "unsupported_plan_fields")
    require(not document.get("proposed_execution_policy"), "unaccepted_policy_present")
    accepted = LoopProposal.model_validate({k: v for k, v in document.items() if k != "execution_continuation"})
    require(accepted.org_id == flow.org_id and accepted.flow_slug == flow.slug, "accepted_graph_scope_changed")
    actual = {address_of(flow.slug, n): n for n in found["nodes"] if n.state != "superseded"}
    require(set(actual) == {n.address for n in accepted.nodes}, "accepted_node_set_changed")
    for n in accepted.nodes:
        require(all(getattr(actual[n.address], k) == getattr(n, k) for k in ("kind", "title", "issue_ref")), "accepted_node_changed")
    addresses = {n.id: address_of(flow.slug, n) for n in found["nodes"]}
    existing = edge_set(document)
    require(existing == {(addresses[e.from_node_id], addresses[e.to_node_id]) for e in found["edges"]}, "accepted_edges_changed")
    added = {(e.from_address, e.to_address) for e in request.added_edges}
    removed = {(e.from_address, e.to_address) for e in request.removed_edges}
    require(len(added) == len(request.added_edges) and len(removed) == len(request.removed_edges), "duplicate_edge_change")
    require(bool(added or removed) and not added & removed, "empty_or_overlapping_changes")
    require(not added & existing and removed <= existing, "edge_change_conflict")
    require(all(a in actual and b in actual for a, b in added | removed), "unknown_node_address")
    frozen = frozen_waves(found, decisions)
    # Legacy continuation receipts also remember historical attempts.
    frozen.update(wave(addresses[node_id]) for node_id, attempts in marker.get("prior_attempts", {}).items() if attempts and node_id in addresses)
    frozen.update(wave(addresses[node_id]) for node_id in marker.get("initial_runs", {}) if node_id in addresses)
    changed = {wave(target) for _, target in added | removed}
    require(not changed & frozen, "started_wave_is_immutable")
    # This feature is intentionally code-delivery only. A running evaluator or
    # repository producer has a separately bound contract and cannot be carried.
    by_id = {n.id: n for n in found["nodes"]}
    require(
        all(e.node_id in by_id and (by_id[e.node_id].kind == "story" or e.status in {"concluded", "superseded"}) for e in found["executions"]),
        "active_non_story_execution",
    )
    document["edges"] = [dict(from_address=a, to_address=b) for a, b in sorted((existing - removed) | added)]
    violations = validate_proposal(LoopProposal.model_validate({k: v for k, v in document.items() if k != "execution_continuation"}))
    require(not violations, "invalid_dependency_graph:" + ",".join(sorted({v.rule for v in violations})))
    # Mutable progress in frozen waves is deliberately absent from this token:
    # they may finish while paused. Eligibility and effective approvals are
    # rechecked under flow + NOWAIT node locks at acceptance.
    result = dict(
        contract=CONTRACT,
        amendment_depth=depth + 1,
        flow_id=flow.id,
        base_plan_id=plan.id,
        base_plan_version=plan.version,
        base_plan_hash=plan.plan_hash,
        plan_version=plan.version + 1,
        plan_hash=digest(document),
        changed_waves=sorted(changed),
        frozen_waves=sorted(frozen),
        added_edges=[e.model_dump(mode="json") for e in request.added_edges],
        removed_edges=[e.model_dump(mode="json") for e in request.removed_edges],
        preserved_policy_hash=inputs.policy.policy_hash,
        preserved_evaluation_decision_ids=[d.id for d in decisions if d.kind in {"evaluation_contract_accepted", "evaluation_waived"}],
        original_accepted_at=marker["accepted_at"],
        expires_at=inputs.policy.expires_at.isoformat(),
    )
    snapshot = digest(dict(request_hash=request_hash(actor, request), **result))
    return dict(wrote_nothing=True, snapshot=snapshot, **result), document, found


async def preview_wave_dependencies(session, *, flow_id, actor, request):
    flow, plan = await current_plan(session, flow_id=flow_id, actor=actor)
    result, _, _ = await prepare_dependencies(session, flow=flow, plan=plan, actor=actor, request=request)
    return result


async def accept_wave_dependencies(session, *, flow_id, actor, request):
    require(request.expected_snapshot is not None, "preview_snapshot_required")
    async with session.begin_nested():
        flow, plan = await current_plan(session, flow_id=flow_id, actor=actor, lock=True)
        identity = str(uuid5(NAMESPACE_URL, CONTRACT + ":" + flow_id + ":" + request_hash(actor, request)))
        receipt = await session.get(OrchestrationDecision, identity, populate_existing=True)
        if receipt is not None:
            data = json.loads(receipt.reason or "{}")
            require(
                receipt.org_id == actor.org_id
                and receipt.flow_id == flow_id
                and receipt.actor_id == actor.actor_id
                and receipt.actor_kind == "human"
                and receipt.kind == "plan_amended"
                and data.get("contract") == CONTRACT
                and data.get("snapshot") == request.expected_snapshot,
                "amendment_receipt_conflict",
            )
            require(plan.accepted_by_decision_id == identity and plan.plan_hash == data.get("plan_hash"), "amendment_superseded")
            return dict(accepted=True, created=False, decision_id=identity, flow_id=flow_id, plan_version=plan.version, plan_hash=plan.plan_hash)
        result, document, found = await prepare_dependencies(session, flow=flow, plan=plan, actor=actor, request=request, lock=True)
        require(result["snapshot"] == request.expected_snapshot, "amendment_snapshot_changed")
        by_address = {address_of(flow.slug, n): n for n in found["nodes"]}
        removed = {(by_address[e.from_address].id, by_address[e.to_address].id) for e in request.removed_edges}
        for edge in found["edges"]:
            if (edge.from_node_id, edge.to_node_id) in removed:
                await session.delete(edge)
        for edge in request.added_edges:
            session.add(
                OrchestrationEdge(
                    org_id=actor.org_id, flow_id=flow_id, from_node_id=by_address[edge.from_address].id, to_node_id=by_address[edge.to_address].id
                )
            )
        # READY was computed against the old prerequisites. Recompute normally
        # on the next tick; never dispatch a stale ready node after resume.
        affected = {e.to_address for e in request.added_edges + request.removed_edges}
        for address in affected:
            if by_address[address].state == "ready":
                by_address[address].state = "pending"
        repo = OrchestrationRepository(session)
        decision = await repo.append_decision(
            org_id=actor.org_id,
            flow_id=flow_id,
            kind="plan_amended",
            actor_id=actor.actor_id,
            actor_kind="human",
            actor_role=actor.actor_role,
            decision_id=identity,
            reason=json.dumps(dict(**result, request_hash=request_hash(actor, request), reason=request.reason)),
        )
        current = await repo.record_accepted_plan(
            org_id=actor.org_id, flow_id=flow_id, plan_document=document, plan_hash=result["plan_hash"], accepted_by_decision_id=decision.id
        )
        require(current.version == result["plan_version"], "plan_version_allocation_changed")
        return dict(accepted=True, created=True, decision_id=identity, **{**result, "wrote_nothing": False})
