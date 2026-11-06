"""Operations activity is visible without becoming completion authority."""

from types import SimpleNamespace

import pytest

from src.orchestration.delivery_progress import DeliveryProgress
from src.orchestration.models import OrchestrationAction
from src.orchestration.operations_progress import current_operations_progress, with_operations_progress
from src.orchestration.operations_tasks import STAGES, record_operations_task
from tests.orchestration.test_operations_tasks import assignment, cycle
from tests.orchestration.test_report_dispatch import engine, prepare, session, session_factory  # noqa: F401


async def test_current_task_progress_uses_latest_repair_cycle_and_scoped_execution(session):  # noqa: F811
    node = await prepare(session)
    old = await cycle(session, node, 1)
    await record_operations_task(session, assignment(node), stage="verify", state="in_progress")
    current = await cycle(session, node, 2)
    who = assignment(node, attempt=2)
    await record_operations_task(session, who, stage="deploy", state="in_progress", repair_cycle=1)
    await record_operations_task(session, who, stage="test", state="in_progress", repair_cycle=2)
    summaries = await current_operations_progress(session, org_id=node.org_id, executions={node.id: current})
    assert set(summaries) == {current.id} and old.id not in summaries
    assert summaries[current.id]["stage"] == "checks"
    assert "required checks" in summaries[current.id]["detail"]
    assert await current_operations_progress(session, org_id="another-tenant", executions={node.id: current}) == {}


async def test_completed_tasks_do_not_claim_delivery_acceptance(session):  # noqa: F811
    node = await prepare(session)
    current = await cycle(session, node, 1)
    for stage in STAGES:
        await record_operations_task(session, assignment(node), stage=stage, state="done")
    summaries = await current_operations_progress(session, org_id=node.org_id, executions={node.id: current})
    assert summaries[current.id]["stage"] == "verification"
    assert "still needs engine acceptance" in summaries[current.id]["detail"]
    current.status = "concluded"
    assert await current_operations_progress(session, org_id=node.org_id, executions={node.id: current}) == {}


async def test_stored_free_text_and_malformed_task_do_not_leak_into_card(session):  # noqa: F811
    node = await prepare(session)
    current = await cycle(session, node, 1)
    session.add_all(
        [
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=current.id,
                operation_key="operations-task:1:test",
                kind="operations_task",
                status="succeeded",
                attempt=1,
                detail={"repair_cycle": 1, "state": "blocked", "title": "secret text", "diagnosis_code": "secret text"},
            ),
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=current.id,
                operation_key="operations-task:2:verify",
                kind="operations_task",
                status="succeeded",
                attempt=1,
                detail={"repair_cycle": "2", "state": "in_progress", "title": "secret text"},
            ),
            OrchestrationAction(
                org_id=node.org_id,
                execution_id=current.id,
                operation_key="operations-task:3:verify",
                kind="operations_task",
                status="succeeded",
                attempt=1,
                detail={"repair_cycle": 3, "state": ["secret text"]},
            ),
        ]
    )
    await session.flush()
    summaries = await current_operations_progress(session, org_id=node.org_id, executions={node.id: current})
    assert summaries[current.id]["stage"] == "checks"
    assert "blocker" in summaries[current.id]["detail"]
    assert "secret" not in str(summaries)


def test_operations_caption_retains_engine_schedule_and_does_not_invent_a_hold():
    execution = SimpleNamespace(id="exec", phase="admitted", status="awaiting_external")
    progress = DeliveryProgress(
        stage="admitted",
        actor="engine",
        automation="engine",
        detail="Development is still producing its bound PR.",
        next_check_at="2026-10-25T10:30:00Z",
        scheduled_action="Reconcile this execution",
    )
    updated = with_operations_progress(progress, execution=execution, summaries={"exec": {"stage": "checks", "detail": "Checking the repair."}})
    assert updated.stage == "checks" and updated.actor == "operations"
    assert updated.next_check_at == progress.next_check_at and updated.scheduled_action == progress.scheduled_action
    assert updated.automation == "engine" and updated.blocker is None
    assert progress.stage == "admitted"


@pytest.mark.parametrize(
    "updates",
    [
        {"stage": "continuation", "blocker": "execution_stale", "automation": "paused"},
        {"stage": "failed", "automation": "paused"},
        {"blocker": "delivery_target_busy", "automation": "blocked"},
        {"stage": "complete", "automation": "not_applicable"},
    ],
)
def test_operations_caption_never_masks_stale_execution_engine_block_or_human_hold(updates):
    execution = SimpleNamespace(id="exec", phase="admitted", status="awaiting_external")
    progress = DeliveryProgress(**{"stage": "admitted", "actor": "engine", "automation": "engine", "detail": "Existing diagnosis", **updates})
    assert with_operations_progress(progress, execution=execution, summaries={"exec": {"stage": "checks", "detail": "Checking"}}) is progress


def test_other_execution_task_board_cannot_override_this_caption():
    execution = SimpleNamespace(id="exec", phase="admitted", status="awaiting_external")
    progress = DeliveryProgress(stage="admitted", actor="engine", automation="engine", detail="Existing diagnosis")
    assert with_operations_progress(progress, execution=execution, summaries={"other": {"stage": "checks", "detail": "Checking"}}) is progress
