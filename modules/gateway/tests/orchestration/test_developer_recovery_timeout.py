"""A reserved first review survives an outer timeout without new development."""

# ruff: noqa: F811

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from src.orchestration.lifecycle_recovery import RecoveryRefusedError, ResumeContinuationRequest, resume_continuation
from src.orchestration.models import DecisionKind, OrchestrationDecision, OrchestrationExecution, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.protected_developer_recovery import reservation_id
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.stall import StallConfig, StallReport, _fetch_candidate_page, _propose, detect_stalls
from src.orchestration.state import NodeState
from tests.orchestration.test_protected_developer_recovery import call, stranded
from tests.orchestration.test_review_cycle import ORG, cycle, pg_server, pg_url, state, store, tick  # noqa: F401

pytestmark = pytest.mark.parametrize("cycle", [{"missing_handoff": True, "plan_version": 2}], indirect=True)


async def reserve(ctx):
    req, _ = await stranded(ctx, held=True)
    req.expected_snapshot = (await call(ctx, req))["snapshot"]
    result = await call(ctx, req, accept=True)
    ctx.identity = replace(ctx.identity, claim_generation=6)
    return reservation_id(result["decision_id"])


async def test_acceptance_starts_a_new_bounded_outer_timeout(cycle):
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.updated_at = datetime.now(UTC) - timedelta(hours=8)
        await db.commit()
    await reserve(cycle)
    async with cycle.factory() as db:
        result = await detect_stalls(db, now=datetime.now(UTC), config=StallConfig())
        assert result.errors == result.stalls_detected == 0
    assert (await tick(cycle)).effects_succeeded == 1
    assert (await state(cycle))[2].attempts == 1


async def test_stale_timeout_observation_cannot_overwrite_accepted_recovery(cycle):
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.updated_at = datetime.now(UTC) - timedelta(hours=8)
        await db.commit()
        candidate = (await _fetch_candidate_page(db, after_id=None, limit=10))[0]
    await reserve(cycle)
    async with cycle.factory() as db:
        report = StallReport()
        await _propose(
            db,
            candidate,
            report,
            to_state=NodeState.FAILED,
            kind=DecisionKind.NODE_STALLED,
            reason="Timeout observed before recovery acceptance",
            detail={},
            detected_counter="stalls_detected",
            notify_config=None,
        )
        assert report.stalls_detected == 0 and report.lost_races == 1
        await db.commit()
    assert (await state(cycle))[2].state == "running"


async def timed_out(ctx):
    run = await reserve(ctx)
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.node.id)
        node.updated_at = datetime.now(UTC) - timedelta(hours=8)
        await db.flush()
        result = await detect_stalls(db, now=datetime.now(UTC), config=StallConfig())
        assert result.stalls_detected == 1
        await db.commit()
    await tick(ctx)
    assert (await state(ctx))[0].block_detail == "outer_gate_not_running"
    return ResumeContinuationRequest(
        expected_run_id=run,
        expected_attempt=1,
        expected_plan_version=2,
        reason="Resume the accepted first-review reservation after the outer timeout.",
    )


async def resume(ctx, body):
    async with ctx.factory() as db:
        result = await resume_continuation(db, org_id=ORG, node_id=ctx.node.id, actor_id="human", actor_role="owner", request=body)
        await db.commit()
        return result


async def test_reserved_developer_review_resumes_once_after_timeout(cycle, monkeypatch):
    body = await timed_out(cycle)
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.ReviewCycleServices", lambda _: cycle.service)
    before = await state(cycle)
    result = await resume(cycle, body)
    assert result["resumed"]
    duplicate = await resume(cycle, body)
    assert not duplicate["resumed"] and duplicate["decision_id"] == result["decision_id"]
    after = await state(cycle)
    assert after[0].id == before[0].id and after[0].status == "runnable"
    assert after[0].handoff_receipt_ref is None and after[0].attempts == before[0].attempts
    assert after[1].active_run_id == body.expected_run_id and after[1].generation == before[1].generation
    assert after[2].state == "running" and after[2].attempts == 1 and not cycle.calls
    assert (await tick(cycle)).effects_succeeded == 1
    assert len(cycle.calls) == 1 and cycle.calls[0]["review_expect"]["author_run_id"] == cycle.root


@pytest.mark.parametrize("change", ["head", "claim", "receipt", "attempt", "plan", "worker", "failure", "pending_effect"])
async def test_reserved_review_recovery_refuses_changed_assignment(cycle, monkeypatch, change):
    body = await timed_out(cycle)
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.ReviewCycleServices", lambda _: cycle.service)
    if change == "head":
        cycle.head = "f" * 40
    elif change == "attempt":
        body.expected_attempt = 2
    elif change == "plan":
        body.expected_plan_version = 3
    elif change == "receipt":
        body.expected_run_id = "recovery:00000000-0000-0000-0000-000000000000"
    elif change == "worker":
        raw = cycle.store._read(f"TENANT#{ORG}", f"EXEC#{cycle.root}")
        raw["status"] = {"S": "active"}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    else:
        async with cycle.factory() as db:
            if change == "claim":
                claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
                claim.active_run_id = "another-worker"
            elif change == "pending_effect":
                execution = await db.get(OrchestrationExecution, cycle.execution.id)
                execution.pending_action_key = "unresolved-provider-effect"
            else:
                db.add(
                    OrchestrationDecision(
                        org_id=ORG,
                        flow_id=cycle.node.flow_id,
                        node_id=cycle.node.id,
                        kind="node_failed",
                        actor_kind="service",
                        actor_id="worker",
                        actor_role="service",
                        from_state="running",
                        to_state="failed",
                    )
                )
            await db.commit()
    with pytest.raises((RecoveryRefusedError, CycleBlockedError)):
        await resume(cycle, body)
    assert (await state(cycle))[2].state == "failed" and not cycle.calls
