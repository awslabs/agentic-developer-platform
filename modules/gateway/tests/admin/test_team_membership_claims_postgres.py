"""Real PostgreSQL locks with stateful Cognito and actual admin HTTP writers."""

import asyncio

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.routes import router
from src.auth.dependencies import get_current_user
from src.auth.workspaces import CognitoWorkspaceClaims, select_workspace
from src.shared.database import get_db
from src.shared.models.base import Base
from tests.admin.test_team_membership_claims import actor, cognito, refreshed, replace, seed  # noqa: F401
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401


@pytest.fixture
async def pg_sessions(pg_url):  # noqa: F811
    engine = create_async_engine(to_async_url(pg_url))
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("CREATE UNIQUE INDEX one_active_org ON tenant_memberships(user_id) WHERE is_active"))
        await conn.execute(text("CREATE UNIQUE INDEX one_primary_team ON team_memberships(user_id, org_id) WHERE is_primary"))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await seed(db)
    try:
        yield factory
    finally:
        await engine.dispose()


def http_client(db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor(admin=True)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


async def test_switch_waits_for_team_claim_reconciliation(pg_sessions, cognito, monkeypatch):  # noqa: F811
    reconciling, release = asyncio.Event(), asyncio.Event()
    real_set_team = CognitoWorkspaceClaims.set_team

    async def pause(self, *args):
        reconciling.set()
        await release.wait()
        await real_set_team(self, *args)

    monkeypatch.setattr(CognitoWorkspaceClaims, "set_team", pause)
    async with pg_sessions() as changing, pg_sessions() as switching, http_client(changing) as client:
        change = asyncio.create_task(replace(client))
        await asyncio.wait_for(reconciling.wait(), 5)
        switch = asyncio.create_task(select_workspace(switching, actor(), "b", CognitoWorkspaceClaims()))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(switch), 0.15)
        finally:
            release.set()
        assert (await asyncio.wait_for(change, 5)).status_code == 200
        assert (await asyncio.wait_for(switch, 5)).org_id == "b"
    assert (refreshed(cognito).org_id, refreshed(cognito).team_id) == ("b", "b1")


async def test_team_write_waits_for_switch_then_respects_new_org(pg_sessions, cognito, monkeypatch):  # noqa: F811
    selecting, release = asyncio.Event(), asyncio.Event()
    real_set = CognitoWorkspaceClaims.set

    async def pause(self, *args):
        selecting.set()
        await release.wait()
        return await real_set(self, *args)

    monkeypatch.setattr(CognitoWorkspaceClaims, "set", pause)
    async with pg_sessions() as changing, pg_sessions() as switching, http_client(changing) as client:
        switch = asyncio.create_task(select_workspace(switching, actor(), "b", CognitoWorkspaceClaims()))
        await asyncio.wait_for(selecting.wait(), 5)
        change = asyncio.create_task(replace(client))
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(change), 0.15)
        finally:
            release.set()
        assert (await asyncio.wait_for(switch, 5)).org_id == "b"
        assert (await asyncio.wait_for(change, 5)).status_code == 200
    assert (refreshed(cognito).org_id, refreshed(cognito).team_id) == ("b", "b1")


async def test_older_reconciliation_reloads_newer_committed_primary(pg_sessions, cognito, monkeypatch):  # noqa: F811
    committed, release = asyncio.Event(), asyncio.Event()
    async with pg_sessions() as older, pg_sessions() as newer, http_client(older) as old_client, http_client(newer) as new_client:
        real_commit = older.commit
        first = True

        async def pause_after_commit():
            nonlocal first
            await real_commit()
            if first:
                first = False
                committed.set()
                await release.wait()

        monkeypatch.setattr(older, "commit", pause_after_commit)
        old = asyncio.create_task(replace(old_client))
        await asyncio.wait_for(committed.wait(), 5)
        try:
            assert (await replace(new_client, primary="a1")).status_code == 200
        finally:
            release.set()
        assert (await asyncio.wait_for(old, 5)).status_code == 200
    assert (refreshed(cognito).team_id, refreshed(cognito).department_id) == ("a1", "d-a1")
    assert cognito.writes == []


@pytest.mark.parametrize("later_org", ["b", "a"])
async def test_failed_switch_compensation_preserves_later_selection_and_primary_including_aba(pg_sessions, cognito, monkeypatch, later_org):  # noqa: F811
    rolled_back, release = asyncio.Event(), asyncio.Event()
    async with pg_sessions() as failing, pg_sessions() as later, http_client(later) as client:
        real_rollback = failing.rollback
        real_commit = failing.commit
        first_rollback = True
        first_commit = True

        async def fail_commit_once():
            nonlocal first_commit
            if first_commit:
                first_commit = False
                raise RuntimeError("database commit failed")
            await real_commit()

        async def pause_after_rollback():
            nonlocal first_rollback
            await real_rollback()
            if first_rollback:
                first_rollback = False
                rolled_back.set()
                await release.wait()

        monkeypatch.setattr(failing, "commit", fail_commit_once)
        monkeypatch.setattr(failing, "rollback", pause_after_rollback)
        failed = asyncio.create_task(select_workspace(failing, actor(), "b", CognitoWorkspaceClaims()))
        await asyncio.wait_for(rolled_back.wait(), 5)
        try:
            await select_workspace(later, actor(), "b", CognitoWorkspaceClaims())
            if later_org == "a":
                await select_workspace(later, actor("b"), "a", CognitoWorkspaceClaims())
            user = "login" if later_org == "a" else "local-b"
            assert (await replace(client, later_org, user, (later_org + "1", later_org + "2"), later_org + "2")).status_code == 200
            await later.commit()
        finally:
            release.set()
        with pytest.raises(HTTPException) as error:
            await asyncio.wait_for(failed, 5)
        assert error.value.status_code == 503
    fresh = refreshed(cognito)
    assert (fresh.org_id, fresh.team_id, fresh.department_id) == (later_org, later_org + "2", "d-" + later_org + "2")
