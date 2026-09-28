"""A shared draft start is one attributed gate, policy and meter transaction."""

import copy
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, select

from src.orchestration.continuation import ContinuationRefusedError, ContinuationRequest
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationExecution,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from src.orchestration.proposal import LoopProposal
from src.orchestration.run_reports import OrchestrationRunReport
from src.shared.models.base import Base
from tests.orchestration.test_continuation import accept, legacy, pg_server, pg_url, preview  # noqa: F401


@pytest.fixture
async def draft(legacy):  # noqa: F811
    ctx = legacy
    engine = ctx.factory.kw["bind"]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=[OrchestrationRunReport.__table__]))
    raw = ctx.request.execution_policy.model_dump(mode="json")
    raw.pop("user_credentials")
    raw["schema_version"] = 1
    raw["allowed_actions"].append("evaluate")
    raw["evaluation_acceptance"] = {"existing/E1/W1/N4": "machine"}
    async with ctx.factory() as db:
        await db.execute(delete(OrchestrationPullRequestBinding))
        await db.execute(delete(OrchestrationEdge))
        nodes = list(await db.scalars(select(OrchestrationNode).order_by(OrchestrationNode.node_ref)))
        for node in nodes:
            node.attempts = 0
            node.state = "awaiting_gate" if node.kind == "gate" else "pending"
        gate = nodes[2]
        gate.node_ref = "accept"
        nodes[4].kind = "eval"
        pairs = [(gate, nodes[i]) for i in [0, 1, 3]] + [(nodes[i], nodes[4]) for i in [0, 1, 3]]

        def address(node):
            return f"existing/{node.epic_ref}/{node.wave_ref}/{node.node_ref}"

        proposal = LoopProposal.model_validate(
            {
                "org_id": "org",
                "flow_slug": "existing",
                "title": "Draft",
                "spec_revision": "a" * 40,
                "nodes": [{"address": address(n), "kind": n.kind, "title": n.title, "issue_ref": n.issue_ref} for n in nodes],
                "edges": [{"from_address": address(a), "to_address": address(b)} for a, b in pairs],
                "proposed_execution_policy": raw,
            }
        )
        decision = OrchestrationDecision(
            org_id="org", flow_id=ctx.flow.id, kind="plan_drafted", actor_id="author", actor_kind="service", actor_role="owner"
        )
        db.add(decision)
        for a, b in pairs:
            db.add(OrchestrationEdge(org_id="org", flow_id=ctx.flow.id, from_node_id=a.id, to_node_id=b.id))
        await db.flush()
        plan = await db.scalar(select(OrchestrationAcceptedPlan))
        plan.plan_document = proposal.model_dump(mode="json")
        plan.accepted_by_decision_id = decision.id
        await db.commit()
        ctx.draft_document = copy.deepcopy(plan.plan_document)
        ctx.gate_id = gate.id
        ctx.graph = [(n.id, n.kind, n.state, n.attempts) for n in nodes]
        ctx.edges = {(a.id, b.id) for a, b in pairs}
    request = ctx.request.model_dump(mode="json")
    request.update(accept_draft_policy=True, delivery_mode="code_only")
    request["execution_policy"]["evaluation_acceptance"] = raw["evaluation_acceptance"]
    ctx.request = ContinuationRequest.model_validate(request)
    return ctx


async def test_draft_acceptance_arms_only_initial_gate_and_keeps_graph_and_evaluation(draft):
    ctx = draft
    async with ctx.factory() as db:
        result = await preview(ctx, db)
        assert result["ready"], result["blockers"]
        assert result["draft_acceptance"]["deferred_actions"] == ["evaluate"]
        assert next(s for s in result["stages"] if s["node_id"] == ctx.gate_id)["action"] == "approve_initial_acceptance_gate"
        request = ctx.request.model_copy(update={"expected_snapshot": result["snapshot"]})
        receipt = await accept(ctx, db, request)
        await db.commit()
    async with ctx.factory() as db:
        repeated = await accept(ctx, db, request)
        assert repeated["already_accepted"] and repeated["decision_id"] == receipt["decision_id"]
        plans = list(await db.scalars(select(OrchestrationAcceptedPlan).order_by(OrchestrationAcceptedPlan.version)))
        assert len(plans) == 2 and plans[0].plan_document == ctx.draft_document
        document = plans[1].plan_document
        assert "proposed_execution_policy" not in document
        assert document["execution_policy"]["limits"] == ctx.draft_document["proposed_execution_policy"]["limits"]
        assert document["execution_policy"]["expires_at"] == ctx.draft_document["proposed_execution_policy"]["expires_at"]
        assert "evaluate" not in document["execution_policy"]["allowed_actions"]
        assert document["execution_policy"]["evaluation_acceptance"] == {"existing/E1/W1/N4": "machine"}
        marker = document["execution_continuation"]
        assert marker["delivery_mode"] == "code_only" and marker["draft_acceptance"]["gate_node_id"] == ctx.gate_id
        assert marker["accepted_at"] == ctx.now.isoformat()
        for node_id, kind, state, attempts in ctx.graph:
            node = await db.get(OrchestrationNode, node_id)
            assert (node.kind, node.state, node.attempts) == (kind, "passed" if node_id == ctx.gate_id else state, attempts)
        assert {(e.from_node_id, e.to_node_id) for e in await db.scalars(select(OrchestrationEdge))} == ctx.edges
        approvals = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "gate_approved")))
        assert len(approvals) == 1 and approvals[0].actor_id == "owner" and approvals[0].actor_kind == "human"
        assert approvals[0].node_id == ctx.gate_id and approvals[0].to_state == "passed"
        assert await db.scalar(select(OrchestrationExecution.id)) is None
        from src.orchestration.adapters.github_comments import _proposed_policy_grant_target

        assert await _proposed_policy_grant_target(db, org_id="org", flow_id=ctx.flow.id, node_id=ctx.gate_id) is None


async def test_draft_start_preserves_authored_gate(draft):
    ctx = draft
    async with ctx.factory() as db:
        other = await db.get(OrchestrationNode, ctx.nodes[1].id)
        other.kind = "gate"
        plan = await db.scalar(select(OrchestrationAcceptedPlan))
        document = copy.deepcopy(plan.plan_document)
        next(n for n in document["nodes"] if n["address"].endswith("/N1"))["kind"] = "gate"
        plan.plan_document = document
        await db.commit()
        result = await preview(ctx, db)
        assert result["ready"], result["blockers"]
        await accept(ctx, db, ctx.request.model_copy(update={"expected_snapshot": result["snapshot"]}))
        await db.refresh(other)
        assert other.state == "pending"
        assert (await db.get(OrchestrationNode, ctx.gate_id)).state == "passed"


@pytest.mark.parametrize("mutation", ["spend", "expiry", "evaluation_map", "gate_state", "graph", "prior_attempt"])
async def test_draft_cannot_change_bounds_or_approve_a_different_graph(draft, mutation):
    ctx = draft
    request = ctx.request.model_dump(mode="json")
    async with ctx.factory() as db:
        if mutation == "spend":
            request["execution_policy"]["limits"]["max_spend_usd"] = "200"
        elif mutation == "expiry":
            request["execution_policy"]["expires_at"] = "2099-01-01T00:00:00Z"
        elif mutation == "evaluation_map":
            request["execution_policy"]["evaluation_acceptance"] = {}
        elif mutation == "gate_state":
            (await db.get(OrchestrationNode, ctx.gate_id)).state = "passed"
        elif mutation == "graph":
            edge = await db.scalar(select(OrchestrationEdge))
            await db.delete(edge)
        else:
            (await db.get(OrchestrationNode, ctx.nodes[0].id)).attempts = 1
        await db.commit()
        request = ContinuationRequest.model_validate(request)
        result = await preview(ctx, db, request)
        assert not result["ready"]
        with pytest.raises(ContinuationRefusedError):
            await accept(ctx, db, request.model_copy(update={"expected_snapshot": result["snapshot"]}))
        assert len(list(await db.scalars(select(OrchestrationAcceptedPlan)))) == 1


async def test_meter_failure_does_not_release_initial_gate(draft, monkeypatch):
    ctx = draft
    monkeypatch.setattr("src.orchestration.continuation.initialize_meter", AsyncMock(return_value=False))
    async with ctx.factory() as db:
        result = await preview(ctx, db)
        with pytest.raises(ContinuationRefusedError, match="budget"):
            await accept(ctx, db, ctx.request.model_copy(update={"expected_snapshot": result["snapshot"]}))
        assert (await db.get(OrchestrationNode, ctx.gate_id)).state == "awaiting_gate"
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == "gate_approved")) is None


async def test_legacy_continuation_cannot_leave_draft_bounds_inert(draft):
    ctx = draft
    request = ctx.request.model_dump(mode="json")
    request["accept_draft_policy"] = False
    request["execution_policy"]["evaluation_acceptance"] = {}
    async with ctx.factory() as db:
        result = await preview(ctx, db, ContinuationRequest.model_validate(request))
        assert result["blockers"][0]["code"] == "draft_acceptance_required"
