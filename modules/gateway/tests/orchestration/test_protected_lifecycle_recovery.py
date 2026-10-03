"""Protected reviewer timeout recovery preserves delivery and retry limits."""

from types import SimpleNamespace

import pytest

from src.orchestration.lifecycle_recovery import RecoveryRefusedError
from tests.orchestration import test_review_cycle as protocol
from tests.orchestration.test_review_cycle import cycle, pg_server, pg_url, store  # noqa: F401
from tests.orchestration.test_shared_lifecycle_recovery import recover, request, timeout


async def test_protected_completed_reviewer_resumes_same_assignment(cycle, monkeypatch):  # noqa: F811
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: SimpleNamespace(store=cycle.store))
    run = await timeout(cycle)
    await cycle.finish(run)
    prior = await protocol.state(cycle)
    async with cycle.factory() as db:
        result = await recover(db, cycle, request(run))
        assert result["resumed"]
        await db.commit()
    async with cycle.factory() as db:
        assert not (await recover(db, cycle, request(run)))["resumed"]
    after = await protocol.state(cycle)
    assert after[2].state == "running" and after[2].attempts == prior[2].attempts
    assert after[1].active_run_id == run and after[1].generation == prior[1].generation
    assert len(cycle.calls) == 1
    await protocol.review(cycle, approve=True)
    result = await protocol.tick(cycle)
    assert (await protocol.state(cycle))[0].phase == "merge_ready", result


@pytest.mark.parametrize("change", ["aborted", "active", "claim", "capability", "revoked"])
async def test_protected_timeout_recovery_refuses_unverified_or_changed_run(cycle, monkeypatch, change):  # noqa: F811
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: SimpleNamespace(store=cycle.store))
    run = await timeout(cycle)
    await cycle.finish(run)
    raw = cycle.store._read(f"TENANT#{protocol.ORG}", f"EXEC#{run}")
    if change == "aborted":
        raw["terminal_outcome"] = {"S": "aborted"}
    elif change == "active":
        raw["status"] = {"S": "active"}
    elif change == "capability":
        raw.pop("orchestration_review_repairs")
    elif change == "revoked":
        authority = cycle.store._read(f"TENANT#{protocol.ORG}", f"AUTHORITY#{cycle.approval.id}")
        authority["status"] = {"S": "revoked"}
        cycle.store.client.put_item(TableName=cycle.store.table, Item=authority)
    cycle.store.client.put_item(TableName=cycle.store.table, Item=raw)
    async with cycle.factory() as db:
        if change == "claim":
            claim = await db.get(protocol.OrchestrationWorkClaim, cycle.identity.claim_id)
            claim.active_run_id = "another-run"
            await db.flush()
        with pytest.raises(RecoveryRefusedError):
            await recover(db, cycle, request(run))
    assert (await protocol.state(cycle))[2].state == "failed"


@pytest.mark.parametrize("allowance", [1, 2])
async def test_protected_failed_reviewer_resumes_only_with_remaining_retry_allowance(cycle, monkeypatch, allowance):  # noqa: F811
    import json

    from src.orchestration.models import OrchestrationAcceptedPlan

    ctx = cycle
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: ctx.service.writer)
    run = await timeout(ctx)
    await ctx.finish(run)
    raw = ctx.store._read(f"TENANT#{protocol.ORG}", f"EXEC#{run}")
    raw["terminal_outcome"] = {"S": "failed"}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    before = await protocol.state(ctx)
    async with ctx.factory() as db:
        plan = await db.get(OrchestrationAcceptedPlan, ctx.plan.id)
        document = json.loads(json.dumps(plan.plan_document))
        document["execution_policy"]["limits"]["max_attempts_per_node"] = allowance
        plan.plan_document = document
        result = await recover(db, ctx, request(run))
        assert result["resumed"] and result["attempt"] == 1
        await db.commit()
    resumed = await protocol.state(ctx)
    assert resumed[1].generation == before[1].generation
    assert resumed[1].active_run_id == run
    assert resumed[2].state == "running" and resumed[2].attempts == 1
    result = await protocol.tick(ctx)
    execution, claim, node, actions = await protocol.state(ctx)
    assert ctx.store._read(f"TENANT#{protocol.ORG}", f"EXEC#{run}")["terminal_outcome"] == {"S": "failed"}
    assert node.attempts == 1 and claim.generation == before[1].generation
    if allowance == 1:
        assert result.effects_succeeded == 0
        assert execution.block_code == "attempts_exhausted"
        assert len(ctx.calls) == 1
    else:
        assert result.effects_succeeded == 1
        assert len(ctx.calls) == 2
        assert actions[-1].detail["review_retry_of"] == run
        assert ctx.calls[-1]["review_expect"]["author_run_id"] == ctx.root
        assert ctx.calls[-1]["review_cycle_input"]["reviewer_owned_delivery"] is True


@pytest.mark.parametrize("status,outcome", [("active", None), ("completed", None), ("cancelled", "failed"), ("completed", "aborted")])
async def test_protected_recovery_requires_verified_terminal_outcome(cycle, monkeypatch, status, outcome):  # noqa: F811
    ctx = cycle
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: ctx.service.writer)
    run = await timeout(ctx)
    raw = ctx.store._read(f"TENANT#{protocol.ORG}", f"EXEC#{run}")
    raw["status"] = {"S": status}
    if outcome:
        raw["terminal_outcome"] = {"S": outcome}
    ctx.store.client.put_item(TableName=ctx.store.table, Item=raw)
    async with ctx.factory() as db:
        with pytest.raises(RecoveryRefusedError, match="reviewer_exit_unverified"):
            await recover(db, ctx, request(run))
    assert (await protocol.state(ctx))[2].state == "failed"
    assert len(ctx.calls) == 1
