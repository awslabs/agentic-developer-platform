"""PostgreSQL route evidence for persisted, attributed archive transitions."""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.shared.models.base import Base
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401
from tests.orchestration import test_flow_archive as shared
from tests.orchestration import test_flow_archive_durable as durable

controls = shared.controls


@pytest.fixture
async def session(pg_url):  # noqa: F811 - imported pytest fixture
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as connection:
            yield connection
    finally:
        await engine.dispose()


@pytest.mark.integration
async def test_archive_rejected_flow_records_decision_postgres(session, controls):
    await shared.test_archive_rejected_flow_records_decision(session, controls)


@pytest.mark.integration
@pytest.mark.parametrize("state", ["running", "awaiting_merge", "awaiting_gate"])
async def test_archive_refuses_active_node_postgres(session, controls, state):
    await shared.test_archive_refuses_active_node_without_writing(session, controls, state)


@pytest.mark.integration
@pytest.mark.parametrize("effect", ["deployment", "prepared", "dispatched", "unknown", "claim", "lease"])
async def test_archive_refuses_durable_work_postgres(session, controls, effect):
    await durable.test_archive_refuses_durable_work_after_node_passed(session, controls, effect)
