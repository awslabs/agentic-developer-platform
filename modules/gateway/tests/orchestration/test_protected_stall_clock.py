"""A protected reviewer gets its own pod clock within the retained story attempt."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationAction, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.stall import StallConfig, detect_stalls
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, store  # noqa: F401


@pytest.mark.parametrize("worker", ["recent", "overdue", "different_claim"])
async def test_protected_reviewer_stall_uses_current_dispatch(cycle, worker):  # noqa: F811
    ctx = cycle
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    now = datetime.now(UTC)
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.node.id)
        node.updated_at = now - timedelta(hours=8)
        dispatch = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "review_cycle_dispatch"))
        dispatch.created_at = now - timedelta(hours=7 if worker == "overdue" else 1)
        if worker == "different_claim":
            claim = await db.get(OrchestrationWorkClaim, ctx.claim.id)
            claim.active_run_id = "another-worker"
        await db.flush()
        result = await detect_stalls(db, now=now, config=StallConfig())
        assert result.errors == 0
        overdue = worker != "recent"
        assert result.stalls_detected == int(overdue)
        await db.refresh(node)
        assert node.state == ("failed" if overdue else "running")


@pytest.mark.parametrize("outcome", ["complete", "failed", "budget_stopped"])
@pytest.mark.parametrize("expired_lease", [False, True])
async def test_terminal_protected_review_reaches_runner_before_legacy_timeout(cycle, monkeypatch, outcome, expired_lease):  # noqa: F811
    """A queued review can be older than its worker; its exit belongs to the runner."""
    from src.orchestration.work_claims import DEFAULT_LEASE_SECONDS

    assert (await protocol.tick(cycle)).effects_succeeded == 1
    run = cycle.calls[-1]["message_id"]
    now = datetime.now(UTC)
    raw = cycle.store._read(f"TENANT#{protocol.ORG}", f"EXEC#{run}")
    raw.update(
        status={"S": "completed"},
        terminal_outcome={"S": outcome},
        terminal_recorded_at={"S": (now - timedelta(minutes=2)).isoformat()},
        workload_binding={"S": "exited-reviewer"},
    )
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: cycle.service.writer)
    async with cycle.factory() as db:
        node = await db.get(OrchestrationNode, cycle.node.id)
        node.updated_at = now - timedelta(hours=14)
        dispatch = await db.scalar(select(OrchestrationAction).where(OrchestrationAction.kind == "review_cycle_dispatch"))
        dispatch.created_at = now - timedelta(hours=13)
        claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
        claim.heartbeat_at = now - timedelta(seconds=DEFAULT_LEASE_SECONDS + 1 if expired_lease else 10)
        claim.lease_expires_at = claim.heartbeat_at + timedelta(seconds=DEFAULT_LEASE_SECONDS)
        await db.flush()
        report = await detect_stalls(db, now=now, config=StallConfig())
        assert report.errors == report.stalls_detected == 0
        await db.refresh(node)
        assert node.state == "running" and node.attempts == cycle.node.attempts
        assert claim.state == "held" and claim.active_run_id == run and claim.generation == cycle.claim.generation
        await db.commit()
    # Only the normal runner may admit another worker, using the existing budget
    # and review-stage limits. Repeated passes cannot duplicate it.
    assert len(cycle.calls) == 1
    admissions = cycle.reserve.await_count
    await protocol.tick(cycle)
    execution, claim, node, actions = await protocol.state(cycle)
    if outcome == "failed":
        assert len(cycle.calls) == 2 and claim.active_run_id != run
        assert actions[-1].detail["review_retry_of"] == run
        assert cycle.reserve.await_count > admissions
    else:
        assert len(cycle.calls) == 1
        if outcome == "budget_stopped":
            assert execution.status == "blocked" and execution.block_code == "budget_exhausted"
    await protocol.tick(cycle)
    assert len(cycle.calls) == (2 if outcome == "failed" else 1)
    assert node.attempts == cycle.node.attempts and claim.generation == cycle.claim.generation


@pytest.mark.parametrize(
    "case",
    ["receipt", "action", "persona", "tenant", "flow", "node", "attempt", "run", "claim", "generation", "unbound", "cancelled", "outcome"],
)
async def test_terminal_handoff_requires_the_exact_current_review(cycle, monkeypatch, case):  # noqa: F811
    from tests.orchestration.test_live_worker_stall import _detect, _worker

    now, run, raw = await _worker(cycle, monkeypatch, kind="reviewer")
    raw.update(status={"S": "completed"}, terminal_outcome={"S": "failed"})
    fields = {
        "receipt": "orchestration_continuation_receipt",
        "action": "orchestration_continuation_action",
        "persona": "persona",
        "tenant": "tenant_id",
        "flow": "flow_id",
        "node": "orchestration_node_id",
        "run": "invocation_id",
        "outcome": "terminal_outcome",
    }
    if case in fields:
        raw[fields[case]] = {"S": "different"}
    elif case == "attempt":
        raw["orchestration_node_attempt"] = {"N": "2"}
    elif case == "unbound":
        raw.pop("workload_binding")
    elif case == "cancelled":
        raw["work_claim_cancellation"] = {"S": "cancelled"}
    else:
        async with cycle.factory() as db:
            claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
            if case == "claim":
                claim.claim_event_id = "different"
            else:
                claim.generation += 1
            await db.commit()
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    await _detect(cycle, now, expected_stalls=1)
    assert len(cycle.calls) == 1


async def test_terminal_exit_between_scan_and_transition_defers_to_runner(cycle, monkeypatch):  # noqa: F811
    from src.orchestration import stall
    from src.orchestration.work_claims import DEFAULT_LEASE_SECONDS
    from tests.orchestration.test_live_worker_stall import _detect, _worker

    now, run, raw = await _worker(cycle, monkeypatch, kind="reviewer")
    async with cycle.factory() as db:
        claim = await db.get(OrchestrationWorkClaim, cycle.claim.id)
        claim.heartbeat_at = now - timedelta(seconds=DEFAULT_LEASE_SECONDS + 1)
        claim.lease_expires_at = now - timedelta(seconds=1)
        await db.commit()
    original = stall._fence_lapsed_lease_assignment

    async def exited_before_transition(db, candidate, **kwargs):
        raw.update(status={"S": "completed"}, terminal_outcome={"S": "failed"})
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
        return await original(db, candidate, **kwargs)

    monkeypatch.setattr(stall, "_fence_lapsed_lease_assignment", exited_before_transition)
    await _detect(cycle, now, expected_stalls=0)
    assert len(cycle.calls) == 1
    await protocol.tick(cycle)
    assert len(cycle.calls) == 2 and cycle.calls[-1]["message_id"] != run


@pytest.mark.parametrize("limit", ["attempts", "spend"])
async def test_terminal_handoff_does_not_bypass_retry_limits(cycle, monkeypatch, limit):  # noqa: F811
    import json
    from decimal import Decimal

    from src.orchestration.models import OrchestrationAcceptedPlan
    from tests.orchestration.test_live_worker_stall import _detect, _worker

    now, run, raw = await _worker(cycle, monkeypatch, kind="reviewer")
    raw.update(status={"S": "completed"}, terminal_outcome={"S": "failed"})
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    if limit == "attempts":
        async with cycle.factory() as db:
            plan = await db.get(OrchestrationAcceptedPlan, cycle.plan.id)
            document = json.loads(json.dumps(plan.plan_document))
            document["execution_policy"]["limits"]["max_attempts_per_node"] = 1
            plan.plan_document = document
            await db.commit()
    else:
        cycle.spend = Decimal(30)
    await _detect(cycle, now, expected_stalls=0)
    await protocol.tick(cycle)
    execution, claim, node, _ = await protocol.state(cycle)
    assert execution.status == "blocked"
    assert execution.block_code == ("attempts_exhausted" if limit == "attempts" else "budget_exhausted")
    assert len(cycle.calls) == 1 and claim.active_run_id == run
    assert node.attempts == cycle.node.attempts and claim.generation == cycle.claim.generation
