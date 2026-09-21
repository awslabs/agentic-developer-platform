"""A reviewed evaluator cannot rewrite running worker authority or reset its meter."""

import asyncio
from copy import deepcopy
from dataclasses import replace

import pytest
from sqlalchemy import select

from src.orchestration.compile import ApprovalContext
from src.orchestration.dispatch_pass import _fetch_ready_nodes
from src.orchestration.evaluation_acceptance import (
    ACCEPTANCE_KIND,
    EvaluationAcceptanceError,
    EvaluationAcceptanceRequest,
    accept_evaluation,
    accepted_contract,
    preview_evaluation,
)
from src.orchestration.evaluation_plan import accepted_evaluation
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from src.orchestration.repository_evaluation import observe_repository_evaluation
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.state import ActorKind
from tests.orchestration.test_repository_evaluation import repository_evaluation  # noqa: F401
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.fixture
async def contract_request(repository_evaluation):  # noqa: F811
    ctx = repository_evaluation
    ctx.actor = ApprovalContext(org_id=ctx.node.org_id, actor_id="human", actor_role="org_admin", reason="Accept exact evidence and existing bounds")
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"][-1]["evaluation"] = None
        document["execution_policy"]["allowed_actions"] = ["develop", "review", "repair", "merge"]
        plan.plan_document = document
        await db.commit()
        ctx.saved_plan = deepcopy(plan.plan_document)
        ctx.request = EvaluationAcceptanceRequest(
            node_id=ctx.eval_id,
            expected_plan_version=plan.version,
            expected_plan_hash=plan.plan_hash,
            specification=ctx.spec,
            authorize_evaluate=True,
            reason="Authorize this repository observation within the existing flow limits",
        )
    return ctx


async def accept(ctx, db, request=None):
    request = request or ctx.request
    preview = await preview_evaluation(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
    request = request.model_copy(update={"expected_snapshot": preview["snapshot"]})
    return await accept_evaluation(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request), request


async def test_acceptance_is_attributed_idempotent_and_keeps_worker_plan_meter_and_runs(contract_request):
    ctx = contract_request
    async with ctx.factory() as db:
        before = [
            (row.id, row.accepted_plan_version, row.phase, row.status, row.revision) for row in await db.scalars(select(OrchestrationExecution))
        ]
        preview = await preview_evaluation(db, flow_id=ctx.flow.id, actor=ctx.actor, request=ctx.request)
        assert preview["wrote_nothing"] and preview["budget_meter_unchanged"]
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == ACCEPTANCE_KIND)) is None
        result, request = await accept(ctx, db)
        await db.commit()
        again = await accept_evaluation(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
        assert result["created"] and not again["created"] and again["decision_id"] == result["decision_id"]
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id, populate_existing=True)
        assert plan.version == 1 and plan.plan_document == ctx.saved_plan
        assert [
            (row.id, row.accepted_plan_version, row.phase, row.status, row.revision) for row in await db.scalars(select(OrchestrationExecution))
        ] == before
        node = await db.get(OrchestrationNode, ctx.eval_id)
        decision, spec, policy = await accepted_contract(db, node=node, plan=plan)
        assert decision.actor_kind == "human" and decision.actor_id == ctx.actor.actor_id
        assert [a.value for a in policy.allowed_actions] == ["evaluate"]
        base = ctx.saved_plan["execution_policy"]
        assert policy.limits.model_dump(mode="json") == base["limits"] and policy.expires_at.isoformat().replace("+00:00", "Z") == base["expires_at"]
        assert (await accepted_evaluation(db, node))[1] == spec
        assert await observe_repository_evaluation(db, node, provider=ctx.provider)
        assert node.state == "passed"
        assert plan.plan_document == ctx.saved_plan


async def test_lost_acceptance_response_replays_after_evaluation_finishes(contract_request, monkeypatch):
    ctx = contract_request
    async with ctx.factory() as db:
        result, request = await accept(ctx, db)
        await db.commit()
        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert await observe_repository_evaluation(db, node, provider=ctx.provider)
        assert node.state == "passed"
        await db.commit()
    # Acknowledging this historical write must not need a fresh allowance or
    # reopen the completed evaluation when runtime authorization is disabled.
    monkeypatch.setenv("ADP_SHARED_WORKER_CONTINUATION_ENABLED", "false")
    async with ctx.factory() as db:
        before = list(await db.scalars(select(OrchestrationDecision.id)))
        replay = await accept_evaluation(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
        assert replay["accepted"] and not replay["created"] and replay["wrote_nothing"]
        assert replay["decision_id"] == result["decision_id"]
        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert node.state == "passed" and node.attempts == 1
        assert list(await db.scalars(select(OrchestrationDecision.id))) == before


async def test_acceptance_yields_to_production_dispatch_node_then_flow_locks(contract_request):
    ctx = contract_request
    async with ctx.factory() as request_session:
        original, _ = await accept(ctx, request_session)
        await request_session.commit()
        replacement = ctx.request.model_copy(update={"reason": "Replace this exact evidence contract after review"})
        expected = await preview_evaluation(request_session, flow_id=ctx.flow.id, actor=ctx.actor, request=replacement)
        replacement = replacement.model_copy(update={"expected_snapshot": expected["snapshot"]})
        await request_session.commit()
        async with ctx.factory() as dispatcher:
            # The actual scheduler acquires these READY node locks first. Its
            # native evaluator only acquires the flow lock later at settlement.
            candidates = await _fetch_ready_nodes(dispatcher, limit=100)
            node = next(node for node in candidates if node.id == ctx.eval_id)
            with pytest.raises(EvaluationAcceptanceError, match="evaluation_dispatch_in_progress"):
                await asyncio.wait_for(
                    accept_evaluation(request_session, flow_id=ctx.flow.id, actor=ctx.actor, request=replacement), timeout=2
                )
            # No caller rollback: the failed acceptance's savepoint must release
            # its flow/plan locks, allowing the real evaluator to finish unchanged.
            assert await asyncio.wait_for(observe_repository_evaluation(dispatcher, node, provider=ctx.provider), timeout=2)
            assert node.state == "passed" and node.attempts == 1
            await dispatcher.commit()
        decisions = list(await request_session.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == ACCEPTANCE_KIND)))
        assert [row.id for row in decisions] == [original["decision_id"]]
        plan = await request_session.get(OrchestrationAcceptedPlan, ctx.plan.id, populate_existing=True)
        assert plan.plan_document == ctx.saved_plan and plan.version == ctx.request.expected_plan_version


async def test_superseded_acceptance_cannot_replay_as_current(contract_request):
    ctx = contract_request
    async with ctx.factory() as db:
        _, original = await accept(ctx, db)
        replacement = ctx.request.model_copy(update={"reason": "Replace the accepted evidence contract explicitly"})
        await accept(ctx, db, replacement)
        await db.commit()
        with pytest.raises(EvaluationAcceptanceError, match="evaluation_contract_superseded"):
            await accept_evaluation(db, flow_id=ctx.flow.id, actor=ctx.actor, request=original)


async def test_reacceptance_during_observation_prevents_stale_settlement(contract_request):
    ctx = contract_request
    async with ctx.factory() as db:
        await accept(ctx, db)
        await db.commit()
    observed = deepcopy(ctx.provider.observe.return_value)

    async def replace_while_observing(*_args):
        async with ctx.factory() as other:
            replacement = ctx.request.model_copy(update={"reason": "Reaccept the evidence contract during observation"})
            await accept(ctx, other, replacement)
            await other.commit()
        return observed

    ctx.provider.observe.side_effect = replace_while_observing
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        assert await observe_repository_evaluation(db, node, provider=ctx.provider)
        assert node.state == "ready" and node.attempts == 0
        refusal = await db.scalar(
            select(OrchestrationDecision.reason)
            .where(OrchestrationDecision.node_id == node.id, OrchestrationDecision.kind == "transition_rejected")
            .order_by(OrchestrationDecision.created_at.desc())
        )
        assert "evaluation_authority_changed" in refusal


@pytest.mark.parametrize("changed", ["node", "plan", "flow"])
async def test_acceptance_refreshes_previously_loaded_scope_after_lock(contract_request, changed):
    ctx = contract_request
    async with ctx.factory() as db:
        preview = await preview_evaluation(db, flow_id=ctx.flow.id, actor=ctx.actor, request=ctx.request)
        request = ctx.request.model_copy(update={"expected_snapshot": preview["snapshot"]})
        # Keep strong references so this session really retains stale identities.
        node = await db.get(OrchestrationNode, ctx.eval_id)
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        flow = await db.get(OrchestrationFlow, ctx.flow.id)
        await db.commit()
        async with ctx.factory() as other:
            if changed == "node":
                (await other.get(OrchestrationNode, node.id)).state = "running"
            elif changed == "plan":
                (await other.get(OrchestrationAcceptedPlan, plan.id)).plan_hash = "f" * 64
            else:
                (await other.get(OrchestrationFlow, flow.id)).state = "halted"
            await other.commit()
        assert node.state == "ready" and plan.plan_hash == request.expected_plan_hash and flow.state == "running"
        with pytest.raises(EvaluationAcceptanceError):
            await accept_evaluation(db, flow_id=ctx.flow.id, actor=ctx.actor, request=request)
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == ACCEPTANCE_KIND)) is None


@pytest.mark.parametrize("case", ["implicit_authority", "service", "stale_plan", "changed_snapshot", "scope", "running"])
async def test_refused_contract_does_not_write_or_change_active_plan(contract_request, case):
    ctx = contract_request
    async with ctx.factory() as db:
        request, actor = ctx.request, ctx.actor
        if case == "implicit_authority":
            request = request.model_copy(update={"authorize_evaluate": False})
        elif case == "service":
            actor = replace(actor, actor_kind=ActorKind.SERVICE)
        elif case == "stale_plan":
            request = request.model_copy(update={"expected_plan_version": 99})
        elif case == "scope":
            spec = deepcopy(request.specification)
            spec["runner"]["repository"] = "different/repository"
            request = request.model_copy(update={"specification": spec})
        elif case == "running":
            node = await db.get(OrchestrationNode, ctx.eval_id)
            node.state = "running"
            await db.flush()
        if case == "changed_snapshot":
            preview = await preview_evaluation(db, flow_id=ctx.flow.id, actor=actor, request=request)
            request = request.model_copy(update={"expected_snapshot": preview["snapshot"], "reason": "A different acceptance than the preview"})
            operation = accept_evaluation
        else:
            operation = preview_evaluation
        with pytest.raises(EvaluationAcceptanceError):
            await operation(db, flow_id=ctx.flow.id, actor=actor, request=request)
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == ACCEPTANCE_KIND)) is None
        assert (await db.get(OrchestrationAcceptedPlan, ctx.plan.id)).plan_document == ctx.saved_plan


@pytest.mark.parametrize("tamper", ["actor", "limits", "plan"])
async def test_evaluator_rechecks_acceptance_actor_bounds_and_current_plan(contract_request, tamper):
    import json

    ctx = contract_request
    async with ctx.factory() as db:
        result, _ = await accept(ctx, db)
        node = await db.get(OrchestrationNode, ctx.eval_id)
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        row = await db.get(OrchestrationDecision, result["decision_id"])
        if tamper in {"actor", "limits"}:
            data = json.loads(row.reason)
            if tamper == "limits":
                data["evaluation_policy"]["limits"]["max_spend_usd"] = "99999"
            db.add(
                OrchestrationDecision(
                    org_id=row.org_id,
                    flow_id=row.flow_id,
                    node_id=row.node_id,
                    kind=row.kind,
                    actor_id=row.actor_id,
                    actor_role=row.actor_role,
                    actor_kind="service" if tamper == "actor" else "human",
                    reason=json.dumps(data),
                )
            )
        else:
            plan.plan_hash = "f" * 64
        await db.flush()
        if tamper == "plan":
            assert await accepted_contract(db, node=node, plan=plan) is None
        else:
            with pytest.raises(CycleBlockedError, match="evaluation_acceptance_unverifiable"):
                await accepted_contract(db, node=node, plan=plan)
        assert node.state == "ready" and node.attempts == 0
