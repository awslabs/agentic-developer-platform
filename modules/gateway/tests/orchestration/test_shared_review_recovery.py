"""Recovery of positively exited reviews keeps claims, attempts and old evidence."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.orchestration.models import OrchestrationDecision, OrchestrationNode, OrchestrationWorkClaim
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_recovery import ReviewRecoveryRequest, request_review_recovery
from src.orchestration.run_reports import OrchestrationRunReport, RunReportError
from src.orchestration.shared_cycle import validate_current_report_assignment
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_shared_cycle import cycle, pg_server, pg_url, shared, store  # noqa: F401
from tests.orchestration.test_shared_review import document_for


@pytest.fixture
async def recovery(shared, monkeypatch):  # noqa: F811
    assert (await protocol.tick(shared)).effects_succeeded == 1
    envelope = shared.calls[-1]
    run = envelope["message_id"]
    resolver = SimpleNamespace(
        read_current=AsyncMock(return_value={"tenant_id": protocol.ORG, "status": "failed", "arrived_at": "2026-01-01T00:00:00Z"})
    )
    monkeypatch.setattr("src.orchestration.controls.get_run_binding_resolver", AsyncMock(return_value=resolver))
    monkeypatch.setattr(
        "src.orchestration.pr_identity.resolve_pr_identity",
        AsyncMock(
            side_effect=lambda **kw: SimpleNamespace(
                head_sha=shared.head,
                provider_repository_id=shared.binding.provider_repository_id,
                provider_pr_node_id=shared.binding.provider_pr_node_id,
            )
        ),
    )
    async with shared.factory() as db:
        row = await db.get(OrchestrationRunReport, run)
        row.worker_receipt = {"recorded_at": datetime.now(UTC).isoformat()}
        row.block_code = "stale_head"
        row.review_receipt = None
        await db.commit()
    body = ReviewRecoveryRequest(
        expected_attempt=1,
        expected_plan_version=1,
        expected_run_id=run,
        expected_head_sha=shared.head,
        reason="Continue the retained story with a fresh Codex review after verified worker exit.",
    )
    return SimpleNamespace(ctx=shared, body=body, resolver=resolver, run=run)


async def request(ctx, *, accept=False, role="owner"):
    async with ctx.ctx.factory() as db:
        result = await request_review_recovery(
            db, org_id=protocol.ORG, node_id=ctx.ctx.node.id, actor_id="human", actor_role=role, request=ctx.body, accept=accept
        )
        if accept:
            await db.commit()
        return result


@pytest.mark.parametrize("failed_node", [False, True])
@pytest.mark.parametrize("failed_terminal", [False, True])
async def test_fresh_review_recovers_without_fabricating_old_receipts(recovery, monkeypatch, failed_node, failed_terminal):
    ctx = recovery.ctx
    prior_terminal = {"outcome": "failed"} if failed_terminal else None
    if failed_terminal:
        async with ctx.factory() as db:
            (await db.get(OrchestrationRunReport, recovery.run)).terminal_receipt = prior_terminal
            await db.commit()
        assert (await protocol.tick(ctx)).blocked == 1
    if failed_node:
        async with ctx.factory() as db:
            (await db.get(OrchestrationNode, ctx.node.id)).state = "failed"
            await db.commit()
    before = await protocol.state(ctx)
    preview = await request(recovery)
    assert (await protocol.state(ctx))[2].state == before[2].state
    recovery.body.expected_snapshot = preview["snapshot"]
    accepted = await request(recovery, accept=True)
    assert accepted["created"]
    again = await request(recovery, accept=True)
    assert not again["created"] and again["decision_id"] == accepted["decision_id"]
    assert (await protocol.state(ctx))[0].attempts == before[0].attempts
    assert (await protocol.tick(ctx)).effects_succeeded == 1
    envelope = ctx.calls[-1]
    assert envelope["message_id"] != recovery.run and envelope["persona"] == "agent-codex-reviewer"
    after = await protocol.state(ctx)
    assert after[1].generation == before[1].generation and after[2].attempts == before[2].attempts
    async with ctx.factory() as db:
        old = await db.get(OrchestrationRunReport, recovery.run)
        assert old.terminal_receipt == prior_terminal and old.review_receipt is None and old.block_code == "stale_head"
        with pytest.raises(RunReportError, match="superseded"):
            await validate_current_report_assignment(db, old)
    # Fresh authenticated R1, not the recovery decision, makes the PR merge-ready.
    from src.orchestration.shared_review import record_shared_review

    monkeypatch.setenv("AGENT_RUN_LOGS_BUCKET", "review-test")
    monkeypatch.setattr("src.orchestration.pr_identity.resolve_head_check_runs", AsyncMock(return_value=frozenset({"check-run:105036077448"})))
    async with ctx.factory() as db:
        receipt = await record_shared_review(
            db,
            credential=envelope["run_report"]["credential"],
            content=json.dumps(document_for(ctx, envelope)),
            storage=SimpleNamespace(put_object=lambda **kw: None),
        )
        assert receipt["recorded"], receipt
        await db.commit()
    await ctx.finish(envelope["message_id"])
    await protocol.tick(ctx)
    assert (await protocol.state(ctx))[0].phase == "merge_ready", vars((await protocol.state(ctx))[0])
    assert len(ctx.calls) == 2
    from tests.orchestration.test_shared_initial_dispatch import ACTUAL_ACTIVE_COUNT

    async with ctx.factory() as db:
        assert await ACTUAL_ACTIVE_COUNT(db, org_id=ctx.node.org_id, flow_id=ctx.flow.id) == 0


@pytest.mark.parametrize("status", [None, "in_progress", "pending", "unknown"])
async def test_missing_or_live_worker_cannot_be_displaced(recovery, status):
    recovery.resolver.read_current.return_value["status"] = status
    with pytest.raises(CycleBlockedError, match="prior_worker_active_or_unverified"):
        await request(recovery)
    assert len(recovery.ctx.calls) == 1


@pytest.mark.parametrize("change", ["head", "attempt", "plan", "claim", "receipt", "role"])
async def test_changed_recovery_preview_is_refused(recovery, change):
    recovery.body.expected_snapshot = (await request(recovery))["snapshot"]
    if change == "head":
        recovery.ctx.head = "b" * 40
    elif change == "attempt":
        recovery.body.expected_attempt = 2
    elif change == "plan":
        recovery.body.expected_plan_version = 2
    elif change in {"claim", "receipt"}:
        async with recovery.ctx.factory() as db:
            if change == "claim":
                (await db.get(OrchestrationWorkClaim, recovery.ctx.claim.id)).active_run_id = "other"
            else:
                (await db.get(OrchestrationRunReport, recovery.run)).terminal_receipt = {"outcome": "failed"}
            await db.commit()
    with pytest.raises((CycleBlockedError, RunReportError)):
        await request(recovery, accept=True, role="developer" if change == "role" else "owner")


@pytest.mark.parametrize("change", ["actor", "head", "receipt", "missing_exit", "live_exit"])
async def test_dispatch_revalidates_owner_decision_and_current_evidence(recovery, change):
    recovery.body.expected_snapshot = (await request(recovery))["snapshot"]
    accepted = await request(recovery, accept=True)
    if change == "head":
        recovery.ctx.head = "b" * 40
    else:
        async with recovery.ctx.factory() as db:
            if change == "actor":
                original = await db.get(OrchestrationDecision, accepted["decision_id"])
                db.add(
                    OrchestrationDecision(
                        org_id=original.org_id,
                        flow_id=original.flow_id,
                        node_id=original.node_id,
                        kind=original.kind,
                        actor_id="engine",
                        actor_kind="service",
                        actor_role="service",
                        reason=original.reason,
                    )
                )
            elif change == "receipt":
                (await db.get(OrchestrationRunReport, recovery.run)).terminal_receipt = {"outcome": "complete"}
            else:
                decision = await db.get(OrchestrationDecision, accepted["decision_id"])
                data = json.loads(decision.reason)
                data["worker_exit"] = None if change == "missing_exit" else {"status": "in_progress"}
                db.add(
                    OrchestrationDecision(
                        org_id=decision.org_id,
                        flow_id=decision.flow_id,
                        node_id=decision.node_id,
                        kind=decision.kind,
                        actor_id=decision.actor_id,
                        actor_kind=decision.actor_kind,
                        actor_role=decision.actor_role,
                        reason=json.dumps(data),
                    )
                )
            await db.commit()
    assert (await protocol.tick(recovery.ctx)).blocked == 1
    assert len(recovery.ctx.calls) == 1


async def test_accepted_exit_does_not_require_registry_access_by_scheduler(recovery):
    recovery.body.expected_snapshot = (await request(recovery))["snapshot"]
    await request(recovery, accept=True)
    recovery.resolver.read_current.reset_mock()
    recovery.resolver.read_current.side_effect = PermissionError("scheduler has no registry query grant")
    assert (await protocol.tick(recovery.ctx)).effects_succeeded == 1
    recovery.resolver.read_current.assert_not_called()
    assert recovery.ctx.calls[-1]["persona"] == "agent-codex-reviewer"
    assert recovery.ctx.calls[-1]["message_id"] != recovery.run


async def test_stale_cached_exit_cannot_authorize_a_live_worker(recovery):
    recovery.resolver.resolve = AsyncMock(return_value={"tenant_id": protocol.ORG, "status": "complete"})
    recovery.resolver.read_current.return_value["status"] = "in_progress"
    with pytest.raises(CycleBlockedError, match="prior_worker_active_or_unverified"):
        await request(recovery)
    recovery.resolver.resolve.assert_not_called()
