"""Real row-lock regressions. Set TEST_WORKSPACE_POSTGRES_URL to a disposable DB."""

import asyncio
import os
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.auth.workspaces import select_workspace
from src.shared.identity.workspaces import link_login_to_workspace
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from tests.auth.test_workspaces import context

pytestmark = pytest.mark.skipif(not os.environ.get("TEST_WORKSPACE_POSTGRES_URL"), reason="Requires disposable PostgreSQL")


@pytest.fixture
async def sessions():
    url = os.environ["TEST_WORKSPACE_POSTGRES_URL"]
    schema = "workspace_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as conn:
        await conn.execute(text(f"CREATE SCHEMA {schema}"))
    engine = create_async_engine(url, connect_args={"server_settings": {"search_path": schema}})
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # The production partial index lives in migration 021 rather than ORM.
            await conn.execute(text("CREATE UNIQUE INDEX workspace_one_active ON tenant_memberships(user_id) WHERE is_active"))
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add_all([Organization(id="home", name="Home"), Organization(id="work", name="Work")])
            await db.flush()
            home = User(id="login-row", org_id="home", team_id="", cognito_sub="login-sub", email="p@example.com")
            work = User(id="work-row", org_id="work", team_id="", email="p@example.com")
            db.add_all([home, work])
            await db.flush()
            await link_login_to_workspace(db, home, work)
            db.add_all(
                [
                    TenantMembership(user_id=home.id, tenant_id="home", role="org_admin", is_active=True),
                    TenantMembership(user_id=work.id, tenant_id="work", role="org_admin", is_active=False),
                ]
            )
            await db.commit()
        yield factory
    finally:
        await engine.dispose()
        async with admin.begin() as conn:
            await conn.execute(text(f"DROP SCHEMA {schema} CASCADE"))
        await admin.dispose()


@pytest.mark.asyncio
async def test_switches_serialize_through_claim_write(sessions):
    first_writing = asyncio.Event()
    release_first = asyncio.Event()
    second_writing = asyncio.Event()
    calls = []

    async def write(subject, values):
        calls.append(values["custom:org_id"])
        if len(calls) == 1:
            first_writing.set()
            await release_first.wait()
        else:
            second_writing.set()
        return {}, values

    claims = MagicMock(set=write)
    async with sessions() as first, sessions() as second:
        task1 = asyncio.create_task(select_workspace(first, context(), "work", claims))
        await asyncio.wait_for(first_writing.wait(), 5)
        task2 = asyncio.create_task(select_workspace(second, context(), "home", claims))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(second_writing.wait(), 0.1)
        finally:
            release_first.set()
        await asyncio.wait_for(asyncio.gather(task1, task2), 5)
    assert calls == ["work", "home"]
    async with sessions() as db:
        assert list(await db.scalars(select(TenantMembership.tenant_id).where(TenantMembership.is_active.is_(True)))) == ["home"]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["remove", "demote"])
async def test_waiting_switch_reloads_membership_after_concurrent_change(sessions, monkeypatch, change):
    claims = MagicMock(set=AsyncMock(side_effect=lambda subject, values: ({}, values)))
    waiting = asyncio.Event()
    async with sessions() as changing, sessions() as switching:
        membership = await changing.scalar(select(TenantMembership).where(TenantMembership.tenant_id == "work").with_for_update())
        real_scalar = switching.scalar

        async def scalar(statement, *args, **kwargs):
            if "FROM tenant_memberships" in str(statement) and "FOR UPDATE" in str(statement):
                waiting.set()
            return await real_scalar(statement, *args, **kwargs)

        monkeypatch.setattr(switching, "scalar", scalar)
        task = asyncio.create_task(select_workspace(switching, context(), "work", claims))
        await asyncio.wait_for(waiting.wait(), 5)
        if change == "remove":
            await changing.execute(delete(TenantMembership).where(TenantMembership.id == membership.id))
        else:
            membership.role = "viewer"
        await changing.commit()
        if change == "remove":
            with pytest.raises(HTTPException) as exc:
                await asyncio.wait_for(task, 5)
            assert exc.value.status_code == 403
            claims.set.assert_not_awaited()
        else:
            assert (await asyncio.wait_for(task, 5)).role == "member"
