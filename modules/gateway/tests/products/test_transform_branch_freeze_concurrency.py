"""Actual concurrent SQL writers retain exactly one branch occurrence source."""

import asyncio
from datetime import UTC, datetime

import pytest
from l5_services.models.event import EventReceipt, semantic_key
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.auth.l5_transform_branch_scope import OCCURRENCE
from src.l5_transform_schedule import retain_first_branch_source
from tests.migrations import conftest_postgres as postgres

pg_server = postgres.pg_server
pg_url = postgres.pg_url


@pytest.mark.parametrize("database", ["sqlite", "postgres"])
async def test_first_committed_freeze_wins_concurrent_writers(request, tmp_path, database):
    url = (
        make_url(request.getfixturevalue("pg_url")).set(drivername="postgresql+asyncpg")
        if database == "postgres"
        else f"sqlite+aiosqlite:///{tmp_path / 'occurrences.db'}"
    )
    engine = create_async_engine(url)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(EventReceipt.__table__.create)
    key = ("tenant", "product", "project", "transform", "trigger", "schedule:2026-10-31T00:00:00Z:revision:1")
    try:
        async with sessions() as db:
            receipt = EventReceipt(
                **dict(zip(("tenant_id", "product_id", "project_id", "service_id", "trigger_id", "event_id"), key, strict=True)),
                event_kind="schedule",
                configuration_id="configuration",
                configuration_revision=1,
                target={"source_policy": "branch"},
                run_input=None,
                state="pending",
                received_at=datetime.now(UTC),
            )
            receipt.idempotency_key = semantic_key(receipt)
            db.add(receipt)
            await db.commit()
        ready = asyncio.Barrier(2)

        async def write(revision):
            async with sessions() as db:
                receipt = await db.get(EventReceipt, key)
                assert receipt.run_input is None
                await ready.wait()
                source = {"record_revision": revision}
                won = await retain_first_branch_source(db, receipt, {"sources": [source]}, {"source_ref": source})
                return won, receipt.run_input, receipt.target

        first, second = await asyncio.gather(write("b" * 40), write("c" * 40))
        assert int(first[0]) + int(second[0]) == 1
        assert first[1:] == second[1:]
        assert first[2][OCCURRENCE]["source_ref"] == first[1]["sources"][0] == first[2]["pinned_source"]
        async with sessions() as db:
            receipt = await db.get(EventReceipt, key)
            assert not await retain_first_branch_source(db, receipt, {"sources": [{"record_revision": "d" * 40}]}, {})
            assert receipt.run_input == first[1]
    finally:
        await engine.dispose()
