"""Recovery uses completed, scoped diagnosis evidence rather than plan numbers."""

import copy
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

import tests.orchestration.test_operations_recovery as fixtures
from src.orchestration.developer_recovery import recover_developer
from src.orchestration.models import OrchestrationAction
from src.orchestration.operations_retry_diagnosis import retry_diagnosis_blocker
from src.orchestration.results import observe_results

engine = fixtures.engine
session = fixtures.session
session_factory = fixtures.session_factory
reporting = fixtures.reporting
work = fixtures.work


async def settle(session, work, attempt, code, *, repair=False):
    _, execution = await fixtures.exited(session, work, attempt, diagnosis=code, repair=repair)
    assert (await observe_results(session)).advanced == 1
    await session.commit()
    return execution


async def check(session, work):
    return await recover_developer(session, work.node, now=datetime.now(UTC) + timedelta(minutes=31))


@pytest.mark.parametrize(
    "code",
    [
        "missing_protected_input.devops_live_config",
        "missing_protected_input.ADP_L5_DEVOPS_LIVE_CONFIG_JSON",
        "devops_human_evidence_required",
    ],
)
async def test_known_input_diagnosis_does_not_consume_another_attempt(session, work, code):
    execution = await settle(session, work, 1, code)
    diagnosis = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.execution_id == execution.id))
    original = copy.deepcopy(diagnosis.detail)
    assert await check(session, work) == "operations_input_required"
    assert work.node.state == "failed" and work.node.attempts == 1
    assert diagnosis.detail == original


async def test_newer_unfinished_diagnosis_does_not_hide_completed_input_hold(session, work):
    execution = await settle(session, work, 1, "devops_human_evidence_required")
    session.add(
        OrchestrationAction(
            org_id=work.node.org_id,
            execution_id=execution.id,
            kind="operations_task",
            status="succeeded",
            attempt=1,
            operation_key="operations-task:4:diagnose",
            detail={"state": "in_progress", "diagnosis_code": "DA19_RELEASE_QUALIFY_GATE"},
        )
    )
    await session.flush()
    assert await check(session, work) == "operations_input_required"


async def test_new_completed_diagnosis_supersedes_prior_input_hold(session, work):
    await settle(session, work, 1, "missing_protected_input.devops_live_config")
    assert await check(session, work) == "operations_input_required"
    # An explicitly resumed attempt can report a changed failure after input was
    # supplied. An old hold must not block this new completed diagnosis forever.
    await settle(session, work, 2, "runtime_health_check_failed")
    assert await check(session, work) == "scheduled"


async def test_new_completed_diagnosis_in_same_attempt_supersedes_input_hold(session, work):
    execution = await settle(session, work, 1, "missing_protected_input.devops_live_config")
    session.add(
        OrchestrationAction(
            org_id=work.node.org_id,
            execution_id=execution.id,
            kind="operations_task",
            status="succeeded",
            attempt=1,
            operation_key="operations-task:2:diagnose",
            detail={"state": "done", "diagnosis_code": "runtime_health_check_failed"},
        )
    )
    await session.flush()
    assert await check(session, work) == "scheduled"


async def test_open_tasks_do_not_supersede_prior_input_hold(session, work):
    await settle(session, work, 1, "missing_protected_input.devops_live_config")
    await settle(session, work, 2, None)
    assert await check(session, work) == "operations_input_required"


async def test_same_plan_repeated_completed_failure_stops_without_repair(session, work):
    await settle(session, work, 1, "runtime_health_check_failed")
    assert await check(session, work) == "scheduled"
    await settle(session, work, 2, "runtime_health_check_failed")
    assert await check(session, work) == "deterministic_failure_unchanged"


@pytest.mark.parametrize("forged", [False, True])
async def test_only_verified_repair_can_clear_repeated_source_diagnosis(session, work, forged):
    await settle(session, work, 1, "source_defect")
    execution = await settle(session, work, 2, "source_defect", repair=True)
    if forged:
        merge = await session.scalar(
            select(OrchestrationAction).where(
                OrchestrationAction.execution_id == execution.id,
                OrchestrationAction.kind == "operations_repair_merge",
            )
        )
        merge.detail = {**merge.detail, "repository": "unapproved/repository"}
        await session.flush()
    assert await check(session, work) == ("deterministic_failure_unchanged" if forged else "scheduled")


@pytest.mark.parametrize(
    "field,value",
    [
        ("operation_key", "unrelated:diagnose"),
        ("status", "failed"),
        ("detail", {"state": "done", "diagnosis_code": "missing_protected_input.bad value"}),
    ],
)
async def test_invalid_diagnosis_provenance_is_not_an_input_hold(session, work, field, value):
    execution = await settle(session, work, 1, "missing_protected_input.devops_live_config")
    diagnosis = await session.scalar(select(OrchestrationAction).where(OrchestrationAction.execution_id == execution.id))
    setattr(diagnosis, field, value)
    await session.flush()
    assert (
        await retry_diagnosis_blocker(
            session,
            node=work.node,
            contract=work.contract,
            runtime_unverified=True,
            failure_category=None,
        )
        is None
    )


async def test_history_overflow_does_not_silently_discard_a_prior_blocker(session, work, monkeypatch):
    await settle(session, work, 1, "missing_protected_input.devops_live_config")
    await settle(session, work, 2, None)
    monkeypatch.setattr("src.orchestration.operations_retry_diagnosis.MAX_DIAGNOSIS_HISTORY", 0)
    assert await check(session, work) == "recovery_evidence_unavailable"
