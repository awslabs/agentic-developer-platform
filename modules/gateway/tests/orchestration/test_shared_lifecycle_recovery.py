"""Recover actual SQL reviewer assignments without another development attempt."""

from datetime import UTC, datetime, timedelta

import pytest

from src.orchestration.dispatch_pass import DispatchPassReport
from src.orchestration.lifecycle_recovery import RecoveryRefusedError, ResumeContinuationRequest, resume_continuation
from src.orchestration.models import DecisionKind, OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.report_dispatch import recover_pending_reports
from src.orchestration.run_reports import OrchestrationRunReport
from src.orchestration.stall import StallConfig, detect_stalls
from src.orchestration.state import ActorKind, NodeState, transition
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401


async def timeout(ctx):
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    run = ctx.calls[-1]["message_id"]
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.node.id)
        node.state = "failed"
        db.add(
            OrchestrationDecision(
                org_id=node.org_id,
                flow_id=node.flow_id,
                node_id=node.id,
                kind=DecisionKind.NODE_STALLED.value,
                actor_kind="service",
                actor_id="system:orchestration-stall",
                actor_role="service",
                from_state="running",
                to_state="failed",
                reason="outer timeout",
            )
        )
        await db.commit()
    return run


def request(run):
    return ResumeContinuationRequest(
        expected_run_id=run,
        expected_attempt=1,
        expected_plan_version=1,
        reason="Resume the existing pending review after repairing its checkout and outbox.",
    )


async def recover(db, ctx, body, org=None):
    return await resume_continuation(db, org_id=org or protocol.ORG, node_id=ctx.node.id, actor_id="human", actor_role="owner", request=body)


async def test_timeout_recovery_replays_same_assignment_and_preserves_evidence(shared, monkeypatch):  # noqa: F811
    ctx = shared
    monkeypatch.setenv("ADP_SHARED_RUN_REPORTING_ENABLED", "true")
    run = await timeout(ctx)
    prior = await protocol.state(ctx)
    async with ctx.factory() as db:
        original = await db.get(OrchestrationRunReport, run)
        metadata, credential_hash = original.dispatch_metadata, original.credential_hash
        result = await recover(db, ctx, request(run))
        assert result["resumed"] and result["attempt"] == 1
        await db.commit()
    async with ctx.factory() as db:
        again = await recover(db, ctx, request(run))
        assert not again["resumed"] and again["decision_id"] == result["decision_id"]
        pending = DispatchPassReport(enabled=True)
        await recover_pending_reports(db, config=ctx.service.config, report=pending)
        assert len(pending.pending) == 1
        assert pending.pending[0].envelope == ctx.calls[-1]
        assert pending.pending[0].deduplication_id == run
        assert pending.pending[0].group_id == f"review-cycle-{ctx.claim.id}"
        row = await db.get(OrchestrationRunReport, run)
        assert row.dispatch_metadata == metadata and row.credential_hash == credential_hash
        assert row.worker_receipt is None and row.terminal_receipt is None
        await db.commit()
    after = await protocol.state(ctx)
    assert after[0].attempts == prior[0].attempts
    assert after[1].generation == prior[1].generation and after[1].active_run_id == run
    assert after[2].attempts == 1 and after[2].state == "running"
    assert len(after[3]) == len(prior[3])


@pytest.mark.parametrize(
    "change,code",
    [
        ("tenant", "node_not_found"),
        ("attempt", "report_assignment_changed"),
        ("policy", "accepted_policy_changed_or_expired"),
        ("failed", "worker_failed"),
        ("claim", "execution_assignment_superseded"),
        ("halt", "not_an_outer_timeout"),
        ("actual_failure", "not_an_outer_timeout"),
    ],
)
async def test_recovery_refuses_changed_authority_or_real_failure(shared, change, code):  # noqa: F811
    ctx = shared
    run = await timeout(ctx)
    body = request(run)
    async with ctx.factory() as db:
        row = await db.get(OrchestrationRunReport, run)
        node = await db.get(OrchestrationNode, ctx.node.id)
        if change == "attempt":
            body.expected_attempt = 2
        elif change == "policy":
            body.expected_plan_version = 2
        elif change == "failed":
            row.terminal_receipt = {"outcome": "failed"}
        elif change == "claim":
            claim = await db.get(OrchestrationWorkClaim, ctx.claim.id)
            claim.active_run_id = "new-run"
        elif change == "halt":
            node.state = "halted"
        elif change == "actual_failure":
            db.add(
                OrchestrationDecision(
                    org_id=node.org_id,
                    flow_id=node.flow_id,
                    node_id=node.id,
                    kind="node_failed",
                    actor_kind="service",
                    actor_id="worker",
                    actor_role="service",
                    from_state="running",
                    to_state="failed",
                )
            )
        await db.flush()
        with pytest.raises(RecoveryRefusedError, match=code):
            await recover(db, ctx, body, org="another-tenant" if change == "tenant" else None)


@pytest.mark.parametrize("changed", ["started", "terminal", "head", "claim"])
async def test_outbox_does_not_restart_started_or_changed_review(shared, monkeypatch, changed):  # noqa: F811
    ctx = shared
    monkeypatch.setenv("ADP_SHARED_RUN_REPORTING_ENABLED", "true")
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    run = ctx.calls[-1]["message_id"]
    async with ctx.factory() as db:
        row = await db.get(OrchestrationRunReport, run)
        if changed == "started":
            row.worker_receipt = {"recorded_at": datetime.now(UTC).isoformat()}
        elif changed == "terminal":
            row.terminal_receipt = {"outcome": "complete"}
        elif changed == "head":
            ctx.head = "c" * 40
        else:
            claim = await db.get(OrchestrationWorkClaim, ctx.claim.id)
            claim.active_run_id = "superseding-run"
        await db.flush()
        pending = DispatchPassReport(enabled=True)
        await recover_pending_reports(db, config=ctx.service.config, report=pending)
        assert pending.pending == []


@pytest.mark.parametrize("worker", ["started", "complete", "overdue"])
async def test_stall_uses_current_worker_clock(shared, worker):  # noqa: F811
    ctx = shared
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    now = datetime.now(UTC)
    run = ctx.calls[-1]["message_id"]
    async with ctx.factory() as db:
        node = await db.get(OrchestrationNode, ctx.node.id)
        row = await db.get(OrchestrationRunReport, run)
        node.updated_at = now - timedelta(hours=8)
        if worker == "complete":
            row.terminal_receipt = {"outcome": "complete"}
        else:
            row.worker_receipt = {"recorded_at": (now - timedelta(hours=7 if worker == "overdue" else 1)).isoformat()}
        await db.flush()
        result = await detect_stalls(db, now=now, config=StallConfig())
        assert result.errors == 0
        assert result.stalls_detected == (1 if worker == "overdue" else 0)
        await db.refresh(node)
        assert node.state == ("failed" if worker == "overdue" else "running")


def test_engine_cannot_reopen_an_outer_timeout():
    assert not transition(NodeState.FAILED, NodeState.RUNNING, actor_kind=ActorKind.SERVICE, reason="retry").allowed
