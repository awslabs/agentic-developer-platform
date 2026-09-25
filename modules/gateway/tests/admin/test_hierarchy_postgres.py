"""Concurrent guarded updates serialize on the real PostgreSQL target row."""

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin import hierarchy, routes
from src.shared.models.organization import Department, Organization
from src.shared.schemas.auth import TokenContext
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url, upgrade  # noqa: F401


async def test_stale_concurrent_patch_cannot_replace_winner(pg_url, monkeypatch):  # noqa: F811
    upgrade(pg_url, "head")
    engine = create_async_engine(to_async_url(pg_url))
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    actor = TokenContext(
        user_id="admin",
        org_id="owned",
        team_id="",
        department_id="",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    monkeypatch.setattr(routes, "write_admin_audit", AsyncMock())
    async with sessions() as db:
        db.add(Organization(id="owned", name="Owned"))
        await db.flush()
        db.add(Department(id="dept", org_id="owned", name="Before"))
        await db.commit()
        before = await hierarchy.read("owned", "department", "dept", db, actor)
    entered, release = asyncio.Event(), asyncio.Event()
    original = routes.update_department

    async def pause(*args, **kwargs):
        entered.set()
        await release.wait()
        return await original(*args, **kwargs)

    monkeypatch.setattr(routes, "update_department", pause)

    async def change(name):
        async with sessions() as db:
            return await hierarchy.patch(
                "owned", "department", "dept", hierarchy.Change(expected_revision=before["revision"], patch={"name": name}), db, actor
            )

    first = asyncio.create_task(change("Winner"))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(change("Stale"))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.15)
    finally:
        release.set()
    assert (await asyncio.wait_for(first, 5))["resource"]["name"] == "Winner"
    with pytest.raises(HTTPException) as stale:
        await asyncio.wait_for(second, 5)
    assert stale.value.status_code == 409
    await engine.dispose()
