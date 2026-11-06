"""Real row locks serialize cancellation reconciliation and the next retry."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine

from src.orchestration.developer_recovery import recover_developer
from src.orchestration.models import OrchestrationAction, OrchestrationDecision, OrchestrationEnvironmentLease, OrchestrationNode
from src.orchestration.operations_cancelled_release import FAILURE_KIND, KIND
from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401
from tests.orchestration.test_operations_cancelled_release import cancelled, failed_apply  # noqa: F401
from tests.orchestration.test_operations_recovery import reporting, session, session_factory, work  # noqa: F401


@pytest.fixture
async def engine(pg_url):  # noqa: F811
    database = create_async_engine(pg_url.replace("postgresql://", "postgresql+asyncpg://", 1))
    async with database.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield database
    await database.dispose()


@pytest.mark.parametrize("failed_rollout", [False, True])
async def test_two_recovery_ticks_settle_one_failed_release_and_schedule_one_retry(session_factory, cancelled, failed_rollout):  # noqa: F811
    # The public tick obtains the node lock before calling recover_developer.
    # Independent sessions here exercise that boundary against PostgreSQL.
    if failed_rollout:
        failed_apply(cancelled)
    kind = FAILURE_KIND if failed_rollout else KIND

    async def tick():
        async with session_factory() as database:
            async with database.begin():
                node = await database.scalar(select(OrchestrationNode).where(OrchestrationNode.id == cancelled.work.node.id).with_for_update())
                return await recover_developer(database, node, now=datetime.now(UTC) + timedelta(minutes=40))

    results = await asyncio.wait_for(asyncio.gather(tick(), tick()), timeout=15)
    assert sorted(results) == ["not_a_failed_developer", "scheduled"]
    async with session_factory() as database:
        action = await database.get(OrchestrationAction, cancelled.action.id)
        lease = await database.get(OrchestrationEnvironmentLease, cancelled.lease.id)
        node = await database.get(OrchestrationNode, cancelled.work.node.id)
        receipts = (await database.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == kind))).all()
        retries = (await database.scalars(select(OrchestrationDecision).where(OrchestrationDecision.kind == "operations_retry_scheduled"))).all()
        assert action.status == "failed" and lease.state == "free"
        assert node.state == "ready" and node.attempts == 1
        assert len(receipts) == len(retries) == 1
        assert json.loads(receipts[0].reason)["outcome"] == ("failure" if failed_rollout else "cancelled")
