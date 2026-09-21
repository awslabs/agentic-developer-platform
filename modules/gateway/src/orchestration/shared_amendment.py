"""Append prerequisites at a settled shared-flow boundary without renewing authority."""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError

from .compile import address_of, upsert_edges, upsert_nodes
from .continuation import digest
from .models import (
    OrchestrationAcceptedPlan,
    OrchestrationAction,
    OrchestrationAmendmentRequest,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
    OrchestrationWorkClaim,
)
from .pr_bindings import binding_snapshot
from .proposal import LoopProposal, ProposedEdge, ProposedNode, split_address, validate_proposal
from .repository import OrchestrationRepository
from .run_reports import OrchestrationRunReport
from .shared_policy import read_flow_meter, shared_inputs
from .state import ActorKind

CONTRACT = "shared-flow-append/v1"
HISTORY_LIMIT = 10000


class SharedAppendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_plan_version: int = Field(gt=0)
    expected_plan_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    added_nodes: list[ProposedNode] = Field(min_length=1, max_length=50)
    added_edges: list[ProposedEdge] = Field(min_length=1, max_length=200)
    reason: str = Field(min_length=8, max_length=4000)
    expected_snapshot: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class SharedAppendError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise SharedAppendError(code)


def request_hash(actor, request):
    return digest(dict(org_id=actor.org_id, actor_id=actor.actor_id, request=request.model_dump(mode="json", exclude={"expected_snapshot"})))


async def rows(session, model, filters, *, lock=False, nowait=False, key="id"):
    query = select(model).where(*filters).order_by(getattr(model, key)).limit(HISTORY_LIMIT + 1).execution_options(populate_existing=True)
    try:
        found = list(await session.scalars(query.with_for_update(nowait=nowait) if lock else query))
    except DBAPIError as error:
        if nowait and (getattr(error.orig, "sqlstate", None) or getattr(error.orig, "pgcode", None)) == "55P03":
            # Dispatch locks READY nodes before taking the flow lock. Never wait
            # for its nodes while holding that flow lock: unwind the enclosing
            # savepoint, release our locks, and let dispatch finish before retry.
            raise SharedAppendError("amendment_dispatch_in_progress") from error
        raise
    require(len(found) <= HISTORY_LIMIT, "amendment_history_limit")
    return found


async def current_plan(session, *, flow_id, actor, lock=False):
    require(actor.actor_kind is ActorKind.HUMAN and actor.actor_id, "human_plan_approver_required")
    flows = await rows(session, OrchestrationFlow, [OrchestrationFlow.id == flow_id, OrchestrationFlow.org_id == actor.org_id], lock=lock)
    require(len(flows) == 1, "flow_not_found")
    plans = await rows(
        session,
        OrchestrationAcceptedPlan,
        [
            OrchestrationAcceptedPlan.flow_id == flow_id,
            OrchestrationAcceptedPlan.org_id == actor.org_id,
            OrchestrationAcceptedPlan.superseded_at.is_(None),
        ],
        lock=lock,
    )
    require(len(plans) == 1, "accepted_plan_missing_or_ambiguous")
    return flows[0], plans[0]


async def history(session, *, flow, lock=False):
    found = {}
    for name, model in (
        ("nodes", OrchestrationNode),
        ("edges", OrchestrationEdge),
        ("bindings", OrchestrationPullRequestBinding),
        ("executions", OrchestrationExecution),
        ("reports", OrchestrationRunReport),
        ("authoring", OrchestrationAmendmentRequest),
    ):
        found[name] = await rows(
            session,
            model,
            [model.org_id == flow.org_id, model.flow_id == flow.id],
            lock=lock and name == "nodes",
            nowait=lock and name == "nodes",
            key="run_id" if name == "reports" else "id",
        )
    found["actions"] = await rows(
        session,
        OrchestrationAction,
        [OrchestrationAction.org_id == flow.org_id, OrchestrationAction.execution_id.in_([e.id for e in found["executions"]])],
    )
    found["claims"] = await rows(
        session,
        OrchestrationWorkClaim,
        [
            OrchestrationWorkClaim.org_id == flow.org_id,
            OrchestrationWorkClaim.owner_kind == "engine_flow",
            OrchestrationWorkClaim.owner_ref == flow.id,
        ],
    )
    found["evaluations"] = await rows(
        session,
        OrchestrationDecision,
        [
            OrchestrationDecision.org_id == flow.org_id,
            OrchestrationDecision.flow_id == flow.id,
            OrchestrationDecision.kind == "evaluation_contract_accepted",
        ],
    )
    return found


def require_quiescence(found):
    require(not any(n.state in {"running", "awaiting_merge"} for n in found["nodes"]), "active_nodes_cross_plan_boundary")
    require(all(e.status in {"concluded", "superseded"} and not e.pending_action_key for e in found["executions"]), "unfinished_execution")
    require(all(a.status in {"succeeded", "failed"} for a in found["actions"]), "unsettled_action")
    reports = {r.run_id: r for r in found["reports"]}
    for row in reports.values():
        terminal = row.terminal_receipt or {}
        require(
            terminal.get("contract_version") == 1
            and terminal.get("run_id") == row.run_id
            and terminal.get("attempt") == row.attempt
            and terminal.get("outcome") in {"complete", "failed"}
            and terminal.get("recorded_at"),
            "unfinished_or_unverified_worker_report",
        )
    for claim in found["claims"]:
        if claim.state == "released":
            continue
        # A concluded cycle can retain its lane until ordinary reconciliation.
        # Preserve it, but only when its exact worker and generation are settled.
        report = reports.get(claim.active_run_id)
        require(
            claim.state == "held"
            and report is not None
            and any(
                e.status == "concluded" and e.claim_id == claim.id and e.claim_generation == claim.generation and e.node_id == report.node_id
                for e in found["executions"]
            ),
            "unsettled_work_claim",
        )
    require(all(r.state not in {"queued", "dispatched"} for r in found["authoring"]), "unfinished_amendment_authoring")


def build_append(flow, plan, found, request):
    document = copy.deepcopy(plan.plan_document)
    require(set(document) <= set(LoopProposal.model_fields) | {"execution_continuation"}, "unsupported_plan_fields")
    require(plan.plan_hash == digest(document), "accepted_document_hash_changed")
    require(not document.get("proposed_execution_policy"), "unaccepted_policy_present")
    proposal = LoopProposal.model_validate({k: v for k, v in document.items() if k != "execution_continuation"})
    require(proposal.org_id == flow.org_id and proposal.flow_slug == flow.slug, "accepted_graph_scope_changed")
    actual = {address_of(flow.slug, n): n for n in found["nodes"] if n.state != "superseded"}
    accepted = {n.address: n for n in proposal.nodes}
    require(set(actual) == set(accepted), "accepted_node_set_changed")
    for address, node in actual.items():
        require(all(getattr(node, key) == getattr(accepted[address], key) for key in ("kind", "title", "issue_ref")), "accepted_node_changed")
    node_addresses = {n.id: address_of(flow.slug, n) for n in found["nodes"]}
    edges = {(node_addresses[e.from_node_id], node_addresses[e.to_node_id]) for e in found["edges"]}
    require(edges == {(e.from_address, e.to_address) for e in proposal.edges}, "accepted_edges_changed")
    existing = set(node_addresses.values())
    waves = {split_address(address)[:3] for address in accepted}
    additions = {n.address for n in request.added_nodes}
    require(len(additions) == len(request.added_nodes) and not additions & existing, "node_address_already_used")
    for node in request.added_nodes:
        require(node.kind == "story" and node.evaluation is None, "only_story_producers_may_be_added")
        require(split_address(node.address)[:3] in waves, "new_wave_not_permitted")
        require(str(node.issue_ref or "").lstrip("#").isdigit() and int(str(node.issue_ref).lstrip("#")) > 0, "story_issue_required")
    added_edges = {(e.from_address, e.to_address) for e in request.added_edges}
    require(len(added_edges) == len(request.added_edges) and not added_edges & edges, "edge_already_present")
    for source, target in added_edges:
        require(source in additions or target in additions, "existing_dependencies_cannot_be_rewritten")
        if target in actual:
            require(actual[target].attempts == 0 and actual[target].state in {"pending", "ready"}, "started_node_prerequisites_changed")
    document["nodes"].extend(n.model_dump(mode="json") for n in request.added_nodes)
    document["edges"].extend(e.model_dump(mode="json") for e in request.added_edges)
    candidate = LoopProposal.model_validate({k: v for k, v in document.items() if k != "execution_continuation"})
    violations = validate_proposal(candidate)
    require(not violations, "invalid_append_graph:" + ",".join(sorted({v.rule for v in violations})))
    return document, candidate


async def prepare_append(session, *, flow, plan, actor, request, lock=False):
    require(flow.state in {"pending", "running"}, "flow_not_active")
    require((plan.version, plan.plan_hash) == (request.expected_plan_version, request.expected_plan_hash), "accepted_plan_changed")
    policy, marker = await shared_inputs(session, org_id=actor.org_id, flow_id=flow.id)
    require(policy.policy.principal_id == actor.actor_id, "existing_policy_owner_required")
    require(marker.get("delivery_mode") == "code_only", "code_only_continuation_required")
    require(datetime.now(UTC) < policy.policy.expires_at, "policy_expired")
    meter = await read_flow_meter(org_id=actor.org_id, flow_id=flow.id, policy=policy.policy)
    require(
        meter is not None and Decimal(marker["prior_spend_usd"]) <= meter.total_usd < policy.policy.limits.max_spend_usd,
        "budget_unavailable_or_exhausted",
    )
    found = await history(session, flow=flow, lock=lock)
    require_quiescence(found)
    document, candidate = build_append(flow, plan, found, request)
    snapshot = digest(
        dict(
            request=request_hash(actor, request),
            plan_id=plan.id,
            document=plan.plan_document,
            flow_state=flow.state,
            nodes=[
                dict(id=n.id, address=address_of(flow.slug, n), state=n.state, attempts=n.attempts, title=n.title, kind=n.kind, issue_ref=n.issue_ref)
                for n in found["nodes"]
            ],
            bindings=[binding_snapshot(b) for b in found["bindings"]],
            executions=[dict(id=e.id, revision=e.revision, status=e.status) for e in found["executions"]],
            reports=[dict(run_id=r.run_id, terminal=r.terminal_receipt) for r in found["reports"]],
            actions=[dict(id=a.id, status=a.status, updated_at=a.updated_at) for a in found["actions"]],
            claims=[dict(id=c.id, generation=c.generation, state=c.state, active_run_id=c.active_run_id) for c in found["claims"]],
            evaluations=[dict(id=d.id, reason=d.reason) for d in found["evaluations"]],
        )
    )
    result = dict(
        wrote_nothing=True,
        snapshot=snapshot,
        flow_id=flow.id,
        base_plan_version=plan.version,
        plan_version=plan.version + 1,
        plan_hash=digest(document),
        added_nodes=[n.model_dump(mode="json") for n in request.added_nodes],
        added_edges=[e.model_dump(mode="json") for e in request.added_edges],
        preserved_policy_hash=policy.policy.policy_hash,
        original_accepted_at=marker["accepted_at"],
        remaining_spend_usd=str(policy.policy.limits.max_spend_usd - meter.total_usd),
        expires_at=policy.policy.expires_at.isoformat(),
        budget_meter_unchanged=True,
        existing_node_ids_unchanged=True,
        evaluation_contracts_requiring_reacceptance=[d.id for d in found["evaluations"] if json.loads(d.reason or "{}").get("plan_id") == plan.id],
    )
    return result, document, candidate


async def preview_shared_append(session, *, flow_id, actor, request):
    flow, plan = await current_plan(session, flow_id=flow_id, actor=actor)
    result, _, _ = await prepare_append(session, flow=flow, plan=plan, actor=actor, request=request)
    return result


async def accept_shared_append(session, *, flow_id, actor, request):
    require(request.expected_snapshot is not None, "preview_snapshot_required")
    async with session.begin_nested():
        flow, plan = await current_plan(session, flow_id=flow_id, actor=actor, lock=True)
        identity = str(uuid5(NAMESPACE_URL, CONTRACT + ":" + flow_id + ":" + request_hash(actor, request)))
        existing = await session.get(OrchestrationDecision, identity, populate_existing=True)
        if existing is not None:
            data = json.loads(existing.reason or "{}")
            require(
                existing.actor_kind == "human"
                and existing.actor_id == actor.actor_id
                and existing.org_id == actor.org_id
                and existing.flow_id == flow_id
                and existing.kind == "plan_amended"
                and data.get("contract") == CONTRACT
                and data.get("snapshot") == request.expected_snapshot
                and data.get("request_hash") == request_hash(actor, request),
                "amendment_receipt_conflict",
            )
            require(plan.accepted_by_decision_id == existing.id and plan.plan_hash == data.get("plan_hash"), "amendment_superseded")
            return dict(
                accepted=True,
                created=False,
                decision_id=existing.id,
                flow_id=flow_id,
                plan_version=plan.version,
                plan_hash=plan.plan_hash,
                wrote_nothing=True,
            )
        result, document, candidate = await prepare_append(session, flow=flow, plan=plan, actor=actor, request=request, lock=True)
        require(result["snapshot"] == request.expected_snapshot, "amendment_snapshot_changed")
        repo = OrchestrationRepository(session)
        node_ids, created = await upsert_nodes(repo, proposal=candidate, org_id=actor.org_id, flow_id=flow_id)
        edges_created = await upsert_edges(repo, proposal=candidate, org_id=actor.org_id, flow_id=flow_id, node_ids=node_ids)
        decision = await repo.append_decision(
            org_id=actor.org_id,
            flow_id=flow_id,
            kind="plan_amended",
            actor_id=actor.actor_id,
            actor_role=actor.actor_role,
            actor_kind="human",
            decision_id=identity,
            reason=json.dumps(dict(contract=CONTRACT, request_hash=request_hash(actor, request), reason=request.reason, **result)),
        )
        current = await repo.record_accepted_plan(
            org_id=actor.org_id,
            flow_id=flow_id,
            plan_document=document,
            plan_hash=result["plan_hash"],
            accepted_by_decision_id=decision.id,
        )
        require(current.version == result["plan_version"], "plan_version_allocation_changed")
        return dict(
            accepted=True,
            created=True,
            decision_id=decision.id,
            nodes_created=created,
            edges_created=edges_created,
            **{**result, "wrote_nothing": False},
        )
