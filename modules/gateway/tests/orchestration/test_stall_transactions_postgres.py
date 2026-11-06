"""Stall diagnostics must not roll back already observed delivery results."""

import pytest
from sqlalchemy import select, text, update

from src.orchestration import stall
from src.orchestration.models import OrchestrationNode
from src.orchestration.run_reports import OrchestrationRunReport  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url  # noqa: F401
from tests.orchestration.test_stall import NOW, ORG_A, PAST_THRESHOLD, _make_flow, _make_node
from tests.orchestration.test_tick_transactions_postgres import factory as factory_fixture

factory = factory_fixture


async def seed(factory):
    async with factory() as session:
        flow = await _make_flow(session)
        nodes = [await _make_node(session, flow, node_ref=str(i), running_for=PAST_THRESHOLD) for i in range(3)]
        ids = sorted(node.id for node in nodes)
        await session.commit()
    return ids


@pytest.mark.parametrize("sqlstate", ["40P01", "22012"])
async def test_failed_stall_check_preserves_prior_results_and_later_nodes(factory, monkeypatch, sqlstate):
    ids = await seed(factory)
    examine = stall._examine

    async def fail_middle(session, candidate, report, **kwargs):
        if candidate.node_id == ids[1]:
            await session.execute(update(OrchestrationNode).where(OrchestrationNode.id == candidate.node_id).values(state="failed"))
            report.record(candidate.org_id, "stalls_detected")
            # A real PostgreSQL error aborts the transaction; a Python exception
            # would not reproduce the production deadlock's poisoned session.
            await session.execute(text(f"DO $$ BEGIN RAISE EXCEPTION USING ERRCODE = '{sqlstate}', MESSAGE = 'stall check failure'; END $$"))
        await examine(session, candidate, report, **kwargs)

    monkeypatch.setattr(stall, "_examine", fail_middle)
    async with factory() as session:
        # Represents a delivery result recorded earlier in the caller's pass.
        await session.execute(
            update(OrchestrationNode)
            .where(OrchestrationNode.id == ids[0])
            .values(title="Result already observed", updated_at=OrchestrationNode.updated_at)
        )
        report = await stall.detect_stalls(session, NOW)
        assert report.errors == 1 and not report.success
        assert report.nodes_examined == 3 and report.stalls_detected == 2
        assert report.per_org[ORG_A]["stalls_detected"] == 2
        await session.commit()
    async with factory() as session:
        rows = {row.id: row for row in (await session.execute(select(OrchestrationNode.id, OrchestrationNode.state, OrchestrationNode.title)))}
        assert [rows[node_id].state for node_id in ids] == ["failed", "running", "failed"]
        assert rows[ids[0]].title == "Result already observed"


async def test_failed_stall_page_keeps_prior_detections_committable(factory, monkeypatch):
    ids = await seed(factory)
    fetch = stall._fetch_candidate_page
    monkeypatch.setattr(stall, "_PAGE_SIZE", 2)

    async def fail_second_page(session, *, after_id, limit):
        if after_id is not None:
            await session.execute(text("SELECT 1 / 0"))
        return await fetch(session, after_id=after_id, limit=limit)

    monkeypatch.setattr(stall, "_fetch_candidate_page", fail_second_page)
    async with factory() as session:
        report = await stall.detect_stalls(session, NOW)
        assert report.errors == 1 and not report.success
        assert report.nodes_examined == 2 and report.stalls_detected == 2
        await session.commit()
    async with factory() as session:
        states = dict((await session.execute(select(OrchestrationNode.id, OrchestrationNode.state))).all())
        assert [states[node_id] for node_id in ids] == ["failed", "failed", "running"]
