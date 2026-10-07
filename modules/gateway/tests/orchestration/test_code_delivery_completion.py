"""Code policy settlement preserves merge evidence and separate evaluation gates."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from src.orchestration.deployment_workflows import DeploymentWorkflows, WorkflowServices
from src.orchestration.execution_runner import RunnerConfig, run_execution_runner
from src.orchestration.execution_state import ExecutionPhase
from src.orchestration.models import OrchestrationAction, OrchestrationExecution, OrchestrationFlow, OrchestrationNode
from tests.orchestration.test_merge_controller import merge, tick  # noqa: F401
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, state, store  # noqa: F401


@pytest.mark.parametrize("cycle", [{"delivery": False}, {"delivery": True}], indirect=True)
async def test_policy_controls_post_merge_path_and_preserves_evaluation(merge):  # noqa: F811
    async with merge.factory() as db:
        evaluation = OrchestrationNode(
            org_id=merge.node.org_id,
            flow_id=merge.node.flow_id,
            epic_ref="E1",
            wave_ref="W1",
            node_ref="human-eval",
            kind="eval",
            state="awaiting_gate",
            title="Human inspection",
        )
        db.add(evaluation)
        await db.commit()
        eval_id = evaluation.id
    await tick(merge)
    await tick(merge)
    execution, _, node, _ = await state(merge)
    async with merge.factory() as db:
        from src.orchestration.policy_admission import load_in_force_policy

        policy = (await load_in_force_policy(db, org_id=node.org_id, flow_id=node.flow_id)).policy
        assert execution.phase == ("deployment_pending" if policy.environment_connection_ids else "concluded")
        assert node.state == "passed"
        assert (await db.get(OrchestrationNode, eval_id)).state == "awaiting_gate"


@pytest.mark.parametrize("obstruction", [None, "deployment_effect", "missing_merge_receipt"])
async def test_recover_old_post_merge_execution_without_deploying(merge, monkeypatch, obstruction):  # noqa: F811
    # Reproduce a pre-fix successful code merge that entered deployment_pending.
    with monkeypatch.context() as patch:
        patch.setattr("src.orchestration.merge_controller.code_only_delivery", AsyncMock(return_value=False))
        await tick(merge)
        await tick(merge)
    execution, _, node, _ = await state(merge)
    async with merge.factory() as db:
        flow = await db.get(OrchestrationFlow, node.flow_id)
        flow.state = "pending"  # Default container state in protected flows.
        current = await db.get(OrchestrationExecution, execution.id)
        current.next_check_at = datetime.now(UTC)
        if obstruction == "deployment_effect":
            db.add(
                OrchestrationAction(
                    org_id=node.org_id, execution_id=execution.id, operation_key="previous-deploy", kind="deployment_workflow", status="prepared"
                )
            )
        elif obstruction == "missing_merge_receipt":
            action = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "merge_pull_request"))
            detail = dict(action.detail)
            detail.pop("merge_receipt")
            action.detail = detail
        await db.commit()
    services = WorkflowServices(merge.factory)
    services.snapshot = AsyncMock(side_effect=AssertionError("code-only completion must not enter deployment"))
    result = await run_execution_runner(
        merge.factory,
        handlers={ExecutionPhase.DEPLOYMENT_PENDING: DeploymentWorkflows(merge.factory, services)},
        config=RunnerConfig(enabled=True, max_attempts=8, io_timeout_seconds=10),
        notifier=AsyncMock(return_value="test-notice"),
    )
    execution, _, node, _ = await state(merge)
    assert result.errors == 0
    assert node.state == "passed"
    assert execution.status == ("blocked" if obstruction else "concluded")
    assert result.effects_attempted == 0
    services.snapshot.assert_not_called()
