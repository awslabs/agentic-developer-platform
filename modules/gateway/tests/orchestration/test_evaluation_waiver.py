"""An owner exception releases dependencies without inventing evaluation evidence."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.orchestration.compile import ApprovalContext
from src.orchestration.evaluation_waiver import KIND, WaiverError, WaiverRequest, accept_waiver, preview_waiver, valid_waiver
from src.orchestration.models import (
    OrchestrationAcceptedPlan,
    OrchestrationDecision,
    OrchestrationEdge,
    OrchestrationFlow,
    OrchestrationNode,
    OrchestrationPullRequestBinding,
)
from src.orchestration.state import ActorKind, NodeState, transition
from src.orchestration.tick import _predecessor_states, _unsatisfied, run_tick
from tests.orchestration.test_repository_evaluation import repository_evaluation  # noqa: F401
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.fixture
async def waiver(repository_evaluation):  # noqa: F811
    ctx = repository_evaluation
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"][-1]["evaluation"] = None
        document["execution_policy"]["allowed_actions"] = ["develop", "review", "repair", "merge"]
        plan.plan_document = document
        node = await db.get(OrchestrationNode, ctx.eval_id)
        node.state = "pending"
        child = OrchestrationNode(
            org_id=node.org_id,
            flow_id=node.flow_id,
            epic_ref="E1",
            wave_ref="W2",
            node_ref="NEXT",
            kind="story",
            title="Next implementation",
            issue_ref="1000",
            state="pending",
            attempts=0,
        )
        db.add(child)
        await db.flush()
        db.add(OrchestrationEdge(org_id=node.org_id, flow_id=node.flow_id, from_node_id=node.id, to_node_id=child.id))
        await db.commit()
        ctx.child_id = child.id
        ctx.original_plan = deepcopy(plan.plan_document)
        ctx.actor = ApprovalContext(org_id=node.org_id, actor_id="human", actor_role="org_admin", reason="Owner accepts evaluation gap")
        ctx.request = WaiverRequest(
            node_id=node.id,
            expected_plan_version=plan.version,
            expected_plan_hash=plan.plan_hash,
            criterion_ids=["V0-01", "V0-02"],
            reason="Owner accepts the missing independent evaluation",
        )
    return ctx


async def accept(ctx, db):
    preview = await preview_waiver(db, flow_id=ctx.flow.id, actor=ctx.actor, request=ctx.request)
    request = ctx.request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    return await accept_waiver(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request), request


async def test_owner_waiver_is_not_a_pass_and_only_normal_tick_releases_successor(waiver):
    ctx = waiver
    async with ctx.factory() as db:
        result, request = await accept(ctx, db)
        await db.commit()
        node = await db.get(OrchestrationNode, ctx.eval_id)
        child = await db.get(OrchestrationNode, ctx.child_id)
        assert node.state == "waived" and node.attempts == 0 and child.state == "pending"
        decision = await valid_waiver(db, node)
        assert decision.actor_kind == "human" and decision.actor_id == "human"
        assert decision.kind == KIND and decision.from_state == "pending" and decision.to_state == "waived"
        assert not result["content"]["evaluation_executed"] and not result["content"]["machine_pass_claimed"]
        assert result["content"]["predecessors"][0]["binding"]["head_sha"] == ctx.head
        replay = await accept_waiver(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
        assert replay["wrote_nothing"] and not replay["created"] and replay["decision_id"] == result["decision_id"]
        report = await run_tick(db)
        await db.refresh(child)
        assert report.success and child.state == "ready" and child.attempts == 0
        assert (await db.get(OrchestrationAcceptedPlan, ctx.plan.id)).plan_document == ctx.original_plan
        records = list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.node_id == ctx.eval_id)))
        assert len(records) == 1 and records[0].kind == KIND
    ctx.provider.token.assert_not_awaited()
    ctx.provider.observe.assert_not_awaited()
    ctx.claim_spy.assert_not_awaited()


@pytest.mark.parametrize(
    "change", ["service", "tenant", "story", "attempt", "running", "configured", "human_gate", "parent", "binding", "expired", "plan"]
)
async def test_waiver_refusals_write_nothing(waiver, change):
    ctx = waiver
    async with ctx.factory() as db:
        actor, request = ctx.actor, ctx.request
        node = await db.get(OrchestrationNode, ctx.eval_id)
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        if change == "service":
            actor = replace(actor, actor_kind=ActorKind.SERVICE)
        elif change == "tenant":
            actor = replace(actor, org_id="another-tenant")
        elif change == "story":
            request = request.model_copy(update={"node_id": ctx.node.id})
        elif change == "attempt":
            node.attempts = 1
        elif change == "running":
            node.state = "running"
        elif change == "configured":
            document["nodes"][-1]["evaluation"] = ctx.spec
        elif change == "human_gate":
            document["execution_policy"]["allowed_actions"].append("evaluate")
            document["execution_policy"]["human_gates"] = ["evaluate"]
        elif change == "parent":
            (await db.get(OrchestrationNode, ctx.node.id)).state = "failed"
        elif change == "binding":
            (await db.get(OrchestrationPullRequestBinding, ctx.binding.id)).state = "superseded"
        elif change == "expired":
            document["execution_policy"]["expires_at"] = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        elif change == "plan":
            request = request.model_copy(update={"expected_plan_version": 999})
        plan.plan_document = document
        await db.commit()
        with pytest.raises(WaiverError):
            await preview_waiver(db, flow_id=ctx.flow.id, actor=actor, request=request)
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == KIND)) is None


async def test_changed_predecessor_invalidates_preview_and_accepted_waiver(waiver):
    ctx = waiver
    async with ctx.factory() as db:
        preview = await preview_waiver(db, flow_id=ctx.flow.id, actor=ctx.actor, request=ctx.request)
        request = ctx.request.model_copy(update={"expected_snapshot": preview["snapshot"]})
        binding = await db.get(OrchestrationPullRequestBinding, ctx.binding.id)
        binding.revision += 1
        await db.commit()
        with pytest.raises(WaiverError, match="waiver_snapshot_changed"):
            await accept_waiver(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
        await accept(ctx, db)
        await db.commit()
        binding.revision += 1
        await db.commit()
        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert await valid_waiver(db, node) is None
        assert _unsatisfied(await _predecessor_states(db, org_id=node.org_id, node_id=ctx.child_id)) == [node.id]


async def test_changed_plan_invalidates_waiver_and_no_fake_waived_state_satisfies_a_dependency(waiver):
    ctx = waiver
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        node.state = "waived"
        await db.commit()
        assert await valid_waiver(db, node) is None
        assert _unsatisfied(await _predecessor_states(db, org_id=node.org_id, node_id=ctx.child_id)) == [node.id]
        node.state = "pending"
        await db.commit()
        await accept(ctx, db)
        await db.commit()
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        plan.superseded_at = datetime.now(UTC)
        db.add(
            OrchestrationAcceptedPlan(
                org_id=node.org_id,
                flow_id=node.flow_id,
                version=2,
                plan_hash="d" * 64,
                plan_document=deepcopy(plan.plan_document),
                accepted_by_decision_id=plan.accepted_by_decision_id,
            )
        )
        await db.commit()
        assert await valid_waiver(db, node) is None
        report = await run_tick(db)
        assert report.blocked[ctx.child_id] == [node.id]


@pytest.mark.parametrize("state", [NodeState.PENDING, NodeState.READY])
def test_engine_cannot_waive(state):
    assert not transition(state, NodeState.WAIVED, actor_kind=ActorKind.SERVICE, reason="engine cannot approve").allowed
    assert transition(state, NodeState.WAIVED, actor_kind=ActorKind.HUMAN, reason="owner approved exception").allowed


async def test_dispatch_lock_conflict_rolls_back_and_releases_flow_lock(waiver):
    ctx = waiver
    async with ctx.factory() as caller:
        preview = await preview_waiver(caller, flow_id=ctx.flow.id, actor=ctx.actor, request=ctx.request)
        request = ctx.request.model_copy(update={"expected_snapshot": preview["snapshot"]})
        await caller.commit()
        async with ctx.factory() as dispatcher:
            await dispatcher.scalar(select(OrchestrationNode).where(OrchestrationNode.id == ctx.eval_id).with_for_update())
            with pytest.raises(WaiverError, match="evaluation_dispatch_in_progress"):
                await asyncio.wait_for(accept_waiver(caller, flow_id=ctx.flow.id, actor=ctx.actor, request=request), timeout=3)
            # No explicit caller rollback: the savepoint must release its flow lock.
            await dispatcher.scalar(select(OrchestrationFlow).where(OrchestrationFlow.id == ctx.flow.id).with_for_update(nowait=True))
        assert await caller.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == KIND)) is None
