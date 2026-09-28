"""A protected reviewer timeout resumes retained work, never failed execution."""

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


@pytest.mark.parametrize("change", ["failed", "active", "claim", "capability", "revoked"])
async def test_protected_timeout_recovery_refuses_unverified_or_changed_run(cycle, monkeypatch, change):  # noqa: F811
    monkeypatch.setattr("src.agentauth.engine.get_engine_authority_writer", lambda: SimpleNamespace(store=cycle.store))
    run = await timeout(cycle)
    await cycle.finish(run)
    raw = cycle.store._read(f"TENANT#{protocol.ORG}", f"EXEC#{run}")
    if change == "failed":
        raw["terminal_outcome"] = {"S": "failed"}
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
