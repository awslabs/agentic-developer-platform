"""Real PostgreSQL duplicate acceptance preserves idempotent results."""

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.orchestration.models import OrchestrationPendingAmendment
from src.orchestration.pending_amendments import AmendmentConflictError, accept_amendment, register_amendment_draft
from src.shared.models.base import Base
from tests.migrations import conftest_postgres as postgres

from .test_pending_amendments import AUTHOR_RUN, ORG_A, accepted_flow, amended_proposal, amender, open_request

pg_server = postgres.pg_server
pg_url = postgres.pg_url


@pytest.fixture
async def factory(pg_url):
    engine = create_async_engine(postgres.to_async_url(pg_url))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


@pytest.mark.parametrize("same_draft", [True, False])
async def test_overlapping_acceptances_refresh_state_after_the_flow_lock(factory, same_draft):
    async with factory() as session:
        flow_id = await accepted_flow(session)
        request = await open_request(session, flow_id)
        draft = await register_amendment_draft(session, org_id=ORG_A, request=request, author_run_id=AUTHOR_RUN, proposal=amended_proposal())
        other = (
            draft
            if same_draft
            else await register_amendment_draft(
                session, org_id=ORG_A, request=request, author_run_id=AUTHOR_RUN, proposal=amended_proposal(extra_gate=False)
            )
        )
        await session.commit()

    # Preload the pending draft in the second transaction, then commit the first
    # acceptance. The second must refresh that state while holding the flow lock.
    async with factory() as first, factory() as second:
        cached = await second.scalar(select(OrchestrationPendingAmendment).where(OrchestrationPendingAmendment.id == other.draft_id))
        assert cached.state == "pending"
        original = await accept_amendment(first, draft_id=draft.draft_id, actor=amender(), flow_id=flow_id)
        # The first transaction still holds the flow lock as the duplicate begins.
        repeated_task = asyncio.create_task(accept_amendment(second, draft_id=other.draft_id, actor=amender(), flow_id=flow_id))
        await first.commit()
        if not same_draft:
            with pytest.raises(AmendmentConflictError) as error:
                await asyncio.wait_for(repeated_task, timeout=10)
            assert error.value.code == "draft_not_pending"
            return
        repeated = await asyncio.wait_for(repeated_task, timeout=10)
        await second.commit()

    assert repeated.replayed
    assert (repeated.plan_version, repeated.decision_id) == (original.plan_version, original.decision_id)
