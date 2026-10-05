"""The CLI gate grant can adopt shared transport without rewriting history."""

import copy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.adapters.github_comments import InputPath, apply_gate_answer_for_context
from src.orchestration.compile import plan_hash
from src.orchestration.continuation import ContinuationRefusedError, ContinuationRequest
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationNode
from src.orchestration.proposal import LoopProposal
from tests.orchestration.test_continuation import accept, preview
from tests.orchestration.test_draft_continuation import draft, legacy, pg_server, pg_url  # noqa: F401


@pytest.fixture
async def gate_accepted(draft):  # noqa: F811
    ctx = draft
    async with ctx.factory() as db:
        plan = await db.scalar(select(OrchestrationAcceptedPlan))
        document = copy.deepcopy(plan.plan_document)
        document["proposed_execution_policy"]["allowed_actions"].remove("evaluate")
        proposal = LoopProposal.model_validate(document)
        plan.plan_document = proposal.model_dump(mode="json")
        plan.plan_hash = plan_hash(proposal)
        ctx.draft_hash = plan.plan_hash
        access = SimpleNamespace(get_user_role=AsyncMock(return_value=(SimpleNamespace(value="owner"), None, None)), check_permission=AsyncMock())
        result = await apply_gate_answer_for_context(
            db,
            context=SimpleNamespace(org_id="org", user_id="owner"),
            node_id=ctx.gate_id,
            approve=True,
            reason="Accept the reviewed draft for implementation",
            access=access,
            input_path=InputPath.DASHBOARD,
            expected_plan_hash=plan.plan_hash,
        )
        assert result.status.value == "applied"
        await db.commit()
        ctx.approval_id = result.decision_id
        ctx.accepted_plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))
        ctx.accepted_document = copy.deepcopy(ctx.accepted_plan.plan_document)
    ctx.now = datetime.now(UTC)
    request = ctx.request.model_dump(mode="json")
    request.update(accept_draft_policy=False, preserve_accepted_policy=True)
    ctx.request = ContinuationRequest.model_validate(request)
    return ctx


async def test_real_gate_grant_with_inert_history_can_continue(gate_accepted):
    ctx = gate_accepted
    async with ctx.factory() as db:
        old = OrchestrationNode(
            org_id="org",
            flow_id=ctx.flow.id,
            epic_ref="E1",
            wave_ref="old",
            node_ref="N0",
            kind="story",
            state="superseded",
            title="Old draft address",
            issue_ref="40",
            attempts=0,
        )
        db.add(old)
        await db.commit()
        result = await preview(ctx, db)
        assert result["ready"], result["blockers"]
        assert result["preserved_authority"]["preserved_acceptance_decision_id"] == ctx.approval_id
        request = ctx.request.model_copy(update={"expected_snapshot": result["snapshot"]})
        await accept(ctx, db, request)
        await db.commit()
        plan = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.superseded_at.is_(None)))
        assert plan.plan_document["execution_policy"]["limits"] == ctx.accepted_document["execution_policy"]["limits"]
        assert plan.plan_document["execution_policy"]["evaluation_acceptance"] == ctx.accepted_document["execution_policy"]["evaluation_acceptance"]
        assert plan.plan_document["execution_continuation"]["preserved_plan_version"] == ctx.accepted_plan.version
        assert (await db.get(OrchestrationNode, old.id)).state == "superseded"
        assert (await db.get(OrchestrationNode, ctx.gate_id)).state == "passed"
        assert all(n.attempts == 0 for n in await db.scalars(select(OrchestrationNode)))


@pytest.mark.parametrize(
    "mutation", ["actor", "service", "unbound", "wrong_hash", "wrong_gate", "from_state", "to_state", "gate_state", "source_policy", "plan_hash"]
)
async def test_gate_grant_requires_complete_bound_acceptance(gate_accepted, mutation):
    ctx = gate_accepted
    async with ctx.factory() as db:
        original = await db.get(OrchestrationDecision, ctx.approval_id)
        decision = OrchestrationDecision(
            **{column.name: getattr(original, column.name) for column in OrchestrationDecision.__table__.columns if column.name != "id"}
        )
        db.add(decision)
        if mutation == "actor":
            decision.actor_id = "another-human"
        elif mutation == "service":
            decision.actor_kind = "service"
        elif mutation == "unbound":
            decision.reason = "No reviewed hash"
        elif mutation == "wrong_hash":
            decision.reason = "[plan-hash=" + "f" * 64 + "]"
        elif mutation == "wrong_gate":
            decision.node_id = ctx.nodes[0].id
        elif mutation == "from_state":
            decision.from_state = "pending"
        elif mutation == "to_state":
            decision.to_state = "ready"
        elif mutation == "gate_state":
            (await db.get(OrchestrationNode, ctx.gate_id)).state = "awaiting_gate"
        elif mutation == "plan_hash":
            (await db.get(OrchestrationAcceptedPlan, ctx.accepted_plan.id)).plan_hash = "e" * 64
        else:
            source = await db.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.version == ctx.accepted_plan.version - 1))
            document = copy.deepcopy(source.plan_document)
            document["proposed_execution_policy"]["limits"]["max_concurrent_actions"] = 9
            source.plan_document = document
        await db.flush()
        (await db.get(OrchestrationAcceptedPlan, ctx.accepted_plan.id)).accepted_by_decision_id = decision.id
        await db.commit()
        result = await preview(ctx, db)
        assert not result["ready"] and result["blockers"][0]["code"] == "accepted_policy_unverifiable"


@pytest.mark.parametrize("mutation", ["historical_attempt", "active_superseded", "historical_failed"])
async def test_superseded_history_cannot_hide_execution(gate_accepted, mutation):
    ctx = gate_accepted
    async with ctx.factory() as db:
        if mutation == "active_superseded":
            (await db.get(OrchestrationNode, ctx.nodes[0].id)).state = "superseded"
        else:
            db.add(
                OrchestrationNode(
                    org_id="org",
                    flow_id=ctx.flow.id,
                    epic_ref="E1",
                    wave_ref="old",
                    node_ref="N0",
                    kind="story",
                    state="failed" if mutation == "historical_failed" else "superseded",
                    title="Old address",
                    attempts=1 if mutation == "historical_attempt" else 0,
                )
            )
        await db.commit()
        result = await preview(ctx, db)
        assert result["blockers"][0]["code"] == "accepted_flow_already_started"


async def test_gate_proof_change_after_preview_refuses_acceptance(gate_accepted):
    ctx = gate_accepted
    async with ctx.factory() as db:
        result = await preview(ctx, db)
        assert result["ready"], result["blockers"]
        async with ctx.factory() as other:
            source = await other.scalar(select(OrchestrationAcceptedPlan).where(OrchestrationAcceptedPlan.version == ctx.accepted_plan.version - 1))
            source.plan_hash = "e" * 64
            await other.commit()
        with pytest.raises(ContinuationRefusedError, match="changed"):
            await accept(ctx, db, ctx.request.model_copy(update={"expected_snapshot": result["snapshot"]}))
