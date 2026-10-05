"""A reviewed gate grants exactly one runnable evaluation, in its transaction."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.adapters.github_comments import InputPath, apply_gate_answer_for_context
from src.orchestration.compile import PolicyNotAcceptableError
from src.orchestration.dispatch import graph_address
from src.orchestration.evaluation_acceptance import ACCEPTANCE_KIND, accepted_contract
from src.orchestration.gate_execution import prepare_gate_execution
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationDecision, OrchestrationEdge, OrchestrationNode
from tests.orchestration.test_evaluation_acceptance import contract_request  # noqa: F401
from tests.orchestration.test_repository_evaluation import repository_evaluation  # noqa: F401
from tests.orchestration.test_repository_producer import scan  # noqa: F401
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.fixture
async def executable_gate(scan):  # noqa: F811
    ctx = scan
    spec = deepcopy(ctx.request.specification)
    spec.update(evidence_schema="workflow-evaluation/v1", acceptance_mode="human")
    workflow = spec["workflows"][0]
    workflow.update(
        path=".github/workflows/eval-cli-uplift.yml",
        source={"revision": "f" * 40},
        definition={"revision": "f" * 40},
        required_jobs=["Live evaluation (dev)", "Recovery sweep (this run, plus anything expired)"],
    )
    producer = spec["producer"]
    producer.pop("images")
    producer["target"].update(resource_kind="cli-evaluation", resource_id="dev")
    producer["inputs"] = dict(
        environment="dev", expected_revision="f" * 40, mode="start", fixtures_json="{}", suites="knowledge", evaluation_id="", inject_fault="none"
    )
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.eval_id)
        node.state = "pending"
        gate = OrchestrationNode(
            org_id=node.org_id,
            flow_id=node.flow_id,
            epic_ref=node.epic_ref,
            wave_ref=node.wave_ref,
            node_ref="release",
            kind="gate",
            title="Start qualification",
            state="awaiting_gate",
            attempts=0,
        )
        db.add(gate)
        await db.flush()
        db.add(OrchestrationEdge(org_id=node.org_id, flow_id=node.flow_id, from_node_id=gate.id, to_node_id=node.id))
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"][-1]["evaluation"] = spec
        document["execution_policy"]["evaluation_acceptance"][graph_address(node, flow_slug="cycle")] = "human"
        document["nodes"].append(dict(address=graph_address(gate, flow_slug="cycle"), kind="gate", title=gate.title, issue_ref=None))
        plan.plan_document = document
        ctx.gate_id = gate.id
        await db.commit()
    ctx.access = SimpleNamespace(get_user_role=AsyncMock(return_value=(SimpleNamespace(value="owner"), None, None)), check_permission=AsyncMock())
    return ctx


async def answer(ctx, db, reviewed):
    return await apply_gate_answer_for_context(
        db,
        context=SimpleNamespace(org_id=ctx.actor.org_id, user_id=ctx.actor.actor_id),
        node_id=ctx.gate_id,
        approve=True,
        reason="Run reviewed qualification",
        access=ctx.access,
        input_path=InputPath.DASHBOARD,
        expected_plan_hash=ctx.plan.plan_hash,
        execution_preview=reviewed,
    )


async def test_one_approval_grants_execution_and_replay_writes_nothing(executable_gate):
    ctx = executable_gate
    async with ctx.factory() as db:
        gate = await db.get(OrchestrationNode, ctx.gate_id)
        preview = await prepare_gate_execution(db, gate=gate, actor=ctx.actor)
        assert preview["ready"] and len(preview["runs"]) == 1
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == ACCEPTANCE_KIND)) is None
        result = await answer(ctx, db, preview)
        assert result.status.value == "applied"
        await db.commit()
        assert (await db.get(OrchestrationNode, ctx.gate_id)).state == "passed"
        node = await db.get(OrchestrationNode, ctx.eval_id)
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        accepted = await accepted_contract(db, node=node, plan=plan)
        assert accepted[1].acceptance_mode == "human"
        assert [a.value for a in accepted[2].allowed_actions] == ["evaluate"]
        before = list(await db.scalars(select(OrchestrationDecision.id)))
        await answer(ctx, db, preview)
        await db.commit()
        assert list(await db.scalars(select(OrchestrationDecision.id))) == before


async def test_changed_preview_rolls_back_gate_and_grant(executable_gate):
    ctx = executable_gate
    async with ctx.factory() as db:
        with pytest.raises(PolicyNotAcceptableError, match="Review the next-step"):
            await answer(ctx, db, {"snapshot": "0" * 64})
        await db.rollback()
        assert (await db.get(OrchestrationNode, ctx.gate_id)).state == "awaiting_gate"
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == ACCEPTANCE_KIND)) is None


async def test_lost_transition_does_not_grant(executable_gate, monkeypatch):
    ctx = executable_gate
    monkeypatch.setattr("src.orchestration.adapters.github_comments._gate_transition", AsyncMock(return_value=(0, True, None)))
    async with ctx.factory() as db:
        result = await answer(ctx, db, None)
        assert result.status.value == "already_answered"
        await db.commit()
        assert await db.scalar(select(OrchestrationDecision.id).where(OrchestrationDecision.kind == ACCEPTANCE_KIND)) is None


async def test_missing_contract_is_visible_before_approval(executable_gate):
    ctx = executable_gate
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = deepcopy(plan.plan_document)
        document["nodes"][-2]["evaluation"] = None
        plan.plan_document = document
        await db.flush()
        gate = await db.get(OrchestrationNode, ctx.gate_id)
        preview = await prepare_gate_execution(db, gate=gate, actor=ctx.actor)
        assert not preview["ready"] and "executable evaluation specification" in preview["problems"][0]
        assert gate.state == "awaiting_gate"
