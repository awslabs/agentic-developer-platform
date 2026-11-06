"""PostgreSQL errors must not publish dispatches whose SQL records rolled back."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration import dispatch_pass, tick_handler
from src.orchestration.dispatch_pass import DispatchPassConfig, DispatchPassReport
from src.orchestration.tick import TickReport
from src.orchestration.tracker_projection import TrackerProjectionReport
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

pytestmark = pytest.mark.integration


@pytest.fixture
async def factory(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE dispatch_probe (id text PRIMARY KEY)"))
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def record(session, value):
    await session.execute(text("INSERT INTO dispatch_probe VALUES (:value)"), {"value": value})


async def recorded(factory):
    async with factory() as session:
        return set((await session.scalars(text("SELECT id FROM dispatch_probe"))).all())


@pytest.fixture
def dispatch_environment(monkeypatch):
    monkeypatch.setattr(dispatch_pass, "_fetch_ready_nodes", AsyncMock(return_value=[SimpleNamespace(id="new", org_id="tenant")]))
    for path in (
        "src.orchestration.protected_dispatch_recovery.recover_pending_protected",
        "src.orchestration.report_dispatch.recover_pending_reports",
    ):
        monkeypatch.setattr(path, AsyncMock())

    async def dispatch(session, node, *, config, report):
        await record(session, node.id)
        report.pending.append(SimpleNamespace(node_id=node.id))
        report.record(node.org_id, "dispatched")

    monkeypatch.setattr(dispatch_pass, "_dispatch_one", dispatch)
    return DispatchPassConfig(queue_url="https://queue.example/test.fifo", aws_region="us-east-1", repo="example/project")


@pytest.mark.parametrize("recovery", ["protected_dispatch_recovery.recover_pending_protected", "report_dispatch.recover_pending_reports"])
@pytest.mark.parametrize("swallow", [False, True])
async def test_recovery_sql_failure_preserves_new_dispatch_and_discards_failed_replays(factory, dispatch_environment, monkeypatch, recovery, swallow):
    async def fail(session, *, config, report):
        # A replay can be staged before a later row deadlocks. Its in-memory
        # publication must roll back with that recovery pass's SQL work.
        await record(session, "replay")
        report.pending.append(SimpleNamespace(node_id="replay"))
        try:
            await session.execute(text("SELECT 1 / 0"))
        except DBAPIError:
            if not swallow:
                raise

    monkeypatch.setattr("src.orchestration." + recovery, fail)
    async with factory() as session:
        report = await dispatch_pass.run_dispatch_pass(session, dispatch_environment)
        await session.commit()

    assert [pending.node_id for pending in report.pending] == ["new"]
    assert await recorded(factory) == {"new"}
    assert report.errors > 0


async def test_full_session_rollback_drops_all_pending_dispatches(factory, dispatch_environment, monkeypatch):
    original = dispatch_pass._dispatch_one

    async def dispatch(session, node, *, config, report):
        if node.id == "new":
            return await original(session, node, config=config, report=report)
        raise RuntimeError("connection lost during second dispatch")

    async def recover(session, *, node_id):
        await session.rollback()
        return True

    monkeypatch.setattr(
        dispatch_pass,
        "_fetch_ready_nodes",
        AsyncMock(return_value=[SimpleNamespace(id="new", org_id="tenant"), SimpleNamespace(id="failed", org_id="tenant")]),
    )
    monkeypatch.setattr(dispatch_pass, "_dispatch_one", dispatch)
    monkeypatch.setattr(dispatch_pass, "recover_session", recover)
    async with factory() as session:
        report = await dispatch_pass.run_dispatch_pass(session, dispatch_environment)
        await session.commit()

    assert await recorded(factory) == set()
    assert report.pending == []
    assert report.errors > 0


@pytest.mark.parametrize("swallow", [False, True])
async def test_tracker_sql_failure_keeps_dispatch_committed_before_publication(factory, monkeypatch, swallow):
    for path in (
        "src.orchestration.tick_handler.run_engine_command_pass",
        "src.orchestration.tick_handler.detect_stalls",
        "src.orchestration.tick_handler.run_execution_runner",
        "src.orchestration.tick_handler.flush_engine_commands",
        "src.orchestration.tick_handler.flush_tracker_projections",
        "src.orchestration.tick_handler.run_retirement_alert_pass",
        "src.orchestration.review_recovery.recover_stalled_stories",
        "src.orchestration.developer_recovery.recover_failed_developers",
    ):
        monkeypatch.setattr(path, AsyncMock(return_value=0))
    monkeypatch.setattr("src.orchestration.results.observe_results", AsyncMock(return_value=SimpleNamespace(errors=0)))
    monkeypatch.setattr(tick_handler, "run_tick", AsyncMock(return_value=TickReport()))

    async def dispatch(session):
        await record(session, "new")
        report = DispatchPassReport(enabled=True)
        report.pending.append(SimpleNamespace(node_id="new"))
        return report

    async def projection(session):
        await record(session, "projection")
        try:
            await session.execute(text("SELECT 1 / 0"))
        except DBAPIError:
            if not swallow:
                raise
        return TrackerProjectionReport()

    prepared = []

    async def prepare(session, report):
        # An independent connection models the worker that receives the message.
        assert await recorded(factory) == {"new"}
        prepared.extend(p.node_id for p in report.pending)

    published = []
    monkeypatch.setattr(tick_handler, "run_dispatch_pass", dispatch)
    monkeypatch.setattr(tick_handler, "run_tracker_projection_pass", projection)
    monkeypatch.setattr(tick_handler, "prepare_pending", prepare)
    monkeypatch.setattr(tick_handler, "publish_pending", lambda report: published.extend(p.node_id for p in report.pending))
    report = await tick_handler._run_locked(factory)

    assert report.tracker_projection_report.errors == 1
    assert prepared == published == ["new"]
    assert await recorded(factory) == {"new"}
