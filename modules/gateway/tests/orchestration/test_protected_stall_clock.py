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
