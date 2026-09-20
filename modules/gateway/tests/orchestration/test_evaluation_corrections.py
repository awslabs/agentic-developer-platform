"""E3 uses real PostgreSQL, E2 failed evidence and the protected source grant."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import select

from src.orchestration.evaluation_correction_state import CORRECTION_KIND, child_id, correction_link
from src.orchestration.evaluation_corrections import EvaluationCorrections
from src.orchestration.evaluation_issue_provider import CorrectionIssue
from src.orchestration.execution_policy import Action
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationAction, OrchestrationNode
from tests.orchestration.test_evaluation_controller import cycle, deployment, evaluation, merge, pg_server, pg_url, runtime, store, tick  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"delivery": True}], indirect=True)


@pytest.fixture
async def correction(evaluation):  # noqa: F811
    ctx = evaluation
    ctx.failed_criterion = True
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["allowed_actions"].append(Action.REPAIR.value)
        plan.plan_document = document
        await db.commit()
    await tick(ctx)
    await tick(ctx)
    ctx.issue = None
    ctx.lost_issue_response = False

    async def find(*args, **kwargs):
        return ctx.issue

    async def create(binding, content, *, reauthorize):
        await reauthorize()
        ctx.issue = CorrectionIssue(
            71, "I_correction", binding.provider_repository_id, "open", f"https://github.com/{binding.repo}/issues/71", content["correlation"]
        )
        if ctx.lost_issue_response:
            raise httpx.ReadTimeout("lost after creation")
        return ctx.issue

    ctx.issue_provider = SimpleNamespace(find=AsyncMock(side_effect=find), create=AsyncMock(side_effect=create))
    ctx.corrections = EvaluationCorrections(ctx.evaluation_services, provider=ctx.issue_provider)
    ctx.evaluation_services.corrections = ctx.corrections
    return ctx


@pytest.mark.parametrize("lose_response", [False, True])
async def test_one_issue_and_child_survive_lost_creation_response(correction, lose_response):
    ctx = correction
    ctx.lost_issue_response = lose_response
    first = await tick(ctx)
    assert first.errors == 0
    ctx.issue_provider.create.assert_awaited_once()
    second = await tick(ctx)
    assert second.errors == 0
    async with ctx.factory() as db:
        actions = list((await db.scalars(select(OrchestrationAction).where(OrchestrationAction.kind == CORRECTION_KIND))).all())
        assert len(actions) == 1
        action = actions[0]
        assert action.detail["creation_started"] and action.status == "succeeded"
        node = await db.get(OrchestrationNode, child_id(action.operation_key))
        assert node is not None and node.kind == "story" and node.state == "ready" and node.issue_ref == "71"
        assert (await correction_link(db, node)).id == action.id
        assert (await db.get(OrchestrationNode, ctx.node.id)).state == "passed"
        assert (await db.get(OrchestrationNode, ctx.eval_id)).state == "running"
    await tick(ctx)
    ctx.issue_provider.create.assert_awaited_once()
