"""Explicit code delivery concludes only with the verified engine merge receipt."""

import pytest

from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.merge_controller import PHASES, MergeController, MergeReceipt
from src.orchestration.models import OrchestrationAcceptedPlan, OrchestrationFlow
from tests.orchestration import test_merge_controller as protocol
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


@pytest.mark.parametrize("flow_state", ["pending", "running"])
async def test_explicit_code_only_concludes_after_verified_merge_without_deployment(shared, monkeypatch, flow_state):  # noqa: F811
    async with shared.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, shared.plan.id)
        marker = {**plan.plan_document["execution_continuation"], "delivery_mode": "code_only"}
        plan.plan_document = {**plan.plan_document, "execution_continuation": marker}
        flow = await db.get(OrchestrationFlow, shared.flow.id)
        flow.state = flow_state
        await db.commit()
    async with protocol.prepared_merge(shared, monkeypatch) as ctx:
        ctx.remote["rules"], ctx.remote["protection"], ctx.remote["reviews"] = [], None, []
        ctx.remote["graphql"]["data"]["repository"]["pullRequest"]["reviewDecision"] = None
        ctx.merge_remote()  # The reviewer merges; this controller observes its evidence.
        await protocol.tick(ctx)
        execution, claim, node, _ = await protocol.state(ctx)
        assert node.state == "passed"
        assert execution.phase == execution.status == "concluded"
        assert execution.next_check_at is None
        # A merged story is not permission to release an issue lane behind the
        # existing ownership/reconciliation lifecycle.
        assert claim.state == "held" and claim.generation == 5
        receipt = MergeReceipt.model_validate((await protocol.merge_actions(ctx))[0].detail["merge_receipt"])
        assert receipt.merge_sha == "c" * 40 and receipt.reviewed_head_sha == ctx.head
        result = await run_execution_runner(
            ctx.factory,
            handlers=dict.fromkeys(PHASES, MergeController(ctx.factory, ctx.merge_services)),
            config=RunnerConfig(enabled=True),
        )
        assert result.examined == 0 and ctx.mutations == []
