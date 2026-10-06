"""Real claim/grant recovery preserves failures and normal retry allowance."""

# ruff: noqa: F811

import pytest
from sqlalchemy import select

from src.orchestration.models import OrchestrationDecision
from src.orchestration.protected_review_recovery import KIND, request_recovery
from src.orchestration.review_cycle import CycleBlockedError
from src.orchestration.review_recovery import ReviewRecoveryRequest
from tests.orchestration.test_review_cycle import ORG, cycle, pg_server, pg_url, review, state, store, tick  # noqa: F401


async def failed(ctx):
    await tick(ctx)
    run = ctx.calls[-1]["message_id"]
    await review(ctx, findings=[{"finding_id": "ci", "summary": "Final-head CI pending"}])
    raw = ctx.store._read(f"TENANT#{ORG}", f"EXEC#{run}")
    raw["terminal_outcome"] = {"S": "failed"}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    await tick(ctx)
    assert (await state(ctx))[0].block_detail == "reviewer_delivery_blocked"
    req = ReviewRecoveryRequest(
        expected_attempt=1,
        expected_plan_version=1,
        expected_run_id=run,
        expected_head_sha=ctx.head,
        reason="Owner authorizes fresh review of retained work after CI completes.",
    )
    return req, raw


async def call(ctx, req, *, accept=False, actor_id="human"):
    async with ctx.factory() as db:
        value = await request_recovery(
            db, org_id=ORG, node_id=ctx.node.id, actor_id=actor_id, actor_role="owner", request=req, accept=accept, services=ctx.service
        )
        if accept:
            await db.commit()
        return value


async def test_owner_recovery_restarts_review_without_accepting_code_or_resetting_attempts(cycle):
    req, raw = await failed(cycle)
    preview = await call(cycle, req)
    assert preview["next_action"] == "fresh_codex_review" and len(cycle.calls) == 1
    req.expected_snapshot = preview["snapshot"]
    accepted = await call(cycle, req, accept=True)
    assert accepted["created"] is True
    assert (await call(cycle, req, accept=True))["created"] is False
    assert cycle.store._read(f"TENANT#{ORG}", f"EXEC#{req.expected_run_id}") == raw
    assert (await tick(cycle)).effects_succeeded == 1, (await state(cycle))[0].block_detail
    execution, claim, node, actions = await state(cycle)
    assert len(cycle.calls) == 2 and claim.active_run_id != req.expected_run_id
    assert node.attempts == 1 and execution.phase == "awaiting_review" and execution.status != "concluded"
    assert actions[-1].detail["protected_recovery_decision_id"] == accepted["decision_id"]
    assert cycle.calls[-1]["review_expect"]["author_run_id"] == cycle.root
    assert cycle.calls[-1]["review_cycle_input"]["remaining_attempts"] == 7
    await review(cycle, findings=[{"finding_id": "still-blocked", "summary": "Real unresolved finding"}])
    await tick(cycle)
    assert len(cycle.calls) == 2


@pytest.mark.parametrize("change", ["head", "active", "revoked", "plan", "binding", "actor", "terminal"])
async def test_recovery_refuses_changed_preview_or_missing_authority(cycle, change, monkeypatch):
    req, raw = await failed(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    actor = "human"
    if change == "head":
        cycle.head = "d" * 40
    elif change in {"active", "revoked", "terminal"}:
        raw["terminal_outcome" if change == "terminal" else "status"] = {"S": "complete" if change == "terminal" else change}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    elif change == "plan":
        req.expected_plan_version = 2
    elif change == "binding":
        from src.orchestration.models import OrchestrationPullRequestBinding

        async with cycle.factory() as db:
            binding = await db.get(OrchestrationPullRequestBinding, cycle.binding.id)
            binding.revision += 1
            await db.commit()
    elif change == "actor":
        actor = "other-owner"
        from unittest.mock import AsyncMock

        monkeypatch.setattr("src.orchestration.protected_review_recovery.policy_owner_matches", AsyncMock(return_value=False))
    with pytest.raises(CycleBlockedError):
        await call(cycle, req, accept=True, actor_id=actor)
    async with cycle.factory() as db:
        assert not list(await db.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == KIND)))
    assert len(cycle.calls) == 1


async def test_recovery_rechecks_head_before_dispatch(cycle):
    req, _ = await failed(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    cycle.head = "e" * 40
    assert (await tick(cycle)).effects_attempted == 0
    assert len(cycle.calls) == 1


async def test_existing_recovery_entrypoint_selects_protected_plan(cycle, monkeypatch):
    from src.orchestration.review_recovery import request_review_recovery

    req, _ = await failed(cycle)
    monkeypatch.setattr("src.orchestration.review_cycle_dispatch.get_engine_authority_writer", lambda: cycle.service.writer)
    async with cycle.factory() as db:
        preview = await request_review_recovery(db, org_id=ORG, node_id=cycle.node.id, actor_id="human", actor_role="owner", request=req)
        req.expected_snapshot = preview["snapshot"]
        accepted = await request_review_recovery(
            db, org_id=ORG, node_id=cycle.node.id, actor_id="human", actor_role="owner", request=req, accept=True
        )
        await db.commit()
    assert accepted["contract"] == "protected-review-recovery/v1"
    assert (await tick(cycle)).effects_succeeded == 1


@pytest.mark.parametrize("change", ["revoked", "budget"])
async def test_recovery_does_not_override_later_revocation_or_spend_limit(cycle, change):
    from decimal import Decimal

    req, raw = await failed(cycle)
    req.expected_snapshot = (await call(cycle, req))["snapshot"]
    await call(cycle, req, accept=True)
    if change == "revoked":
        raw["status"] = {"S": "revoked"}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    else:
        cycle.spend = Decimal(100)
    assert (await tick(cycle)).effects_attempted == 0
    assert len(cycle.calls) == 1
