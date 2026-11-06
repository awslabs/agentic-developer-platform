"""A real PostgreSQL transaction error must not poison later tick work."""

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration import tick
from src.orchestration.models import OrchestrationNode
from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration.test_tick import ORG_A, _make_flow, _make_node


@pytest.fixture
async def factory(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def seed(factory):
    async with factory() as session:
        flow = await _make_flow(session)
        nodes = [await _make_node(session, flow, node_ref=str(i)) for i in range(3)]
        ids = sorted(node.id for node in nodes)
        await session.commit()
    return ids


@pytest.mark.parametrize("sqlstate", ["40P01", "22012"])
async def test_failed_node_rolls_back_its_writes_and_counters_only(factory, monkeypatch, sqlstate):
    ids = await seed(factory)
    advance = tick._advance_node

    async def fail_middle(session, candidate, report):
        await advance(session, candidate, report)
        if candidate.node_id == ids[1]:
            # PostgreSQL, rather than a Python mock, aborts this transaction.
            # 40P01 is the production deadlock SQLSTATE; no timing race is needed.
            await session.execute(text(f"DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = '{sqlstate}', MESSAGE = 'test node failure'; END $$"))

    monkeypatch.setattr(tick, "_advance_node", fail_middle)
    async with factory() as session:
        report = await tick.run_tick(session)
        assert not report.success and report.errors == 1
        assert report.nodes_examined == 3 and report.transitions_effected == 2
        assert report.per_org[ORG_A]["transitions_effected"] == 2
        await session.commit()
    async with factory() as session:
        states = dict((await session.execute(select(OrchestrationNode.id, OrchestrationNode.state))).all())
        assert [states[node_id] for node_id in ids] == ["ready", "pending", "ready"]


async def test_failed_page_keeps_prior_progress_committable(factory, monkeypatch):
    ids = await seed(factory)
    fetch = tick._fetch_candidate_page
    monkeypatch.setattr(tick, "_PAGE_SIZE", 2)

    async def fail_second_page(session, *, after_id, limit):
        if after_id is not None:
            await session.execute(text("SELECT 1 / 0"))
        return await fetch(session, after_id=after_id, limit=limit)

    monkeypatch.setattr(tick, "_fetch_candidate_page", fail_second_page)
    async with factory() as session:
        report = await tick.run_tick(session)
        assert not report.success and report.errors == 1
        assert report.nodes_examined == 2 and report.transitions_effected == 2
        await session.commit()
    async with factory() as session:
        states = dict((await session.execute(select(OrchestrationNode.id, OrchestrationNode.state))).all())
        assert [states[node_id] for node_id in ids] == ["ready", "ready", "pending"]
