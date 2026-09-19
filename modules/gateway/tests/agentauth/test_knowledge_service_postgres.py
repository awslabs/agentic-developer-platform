"""Real PostgreSQL identity resolution and membership/reassignment locking."""

import asyncio
from dataclasses import replace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agentauth import knowledge_service as service
from src.shared.models.base import Base
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity
from tests.agentauth.test_run_services import GRANT, RECORD
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

OWNER = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
async def sessions(pg_url, monkeypatch):  # noqa: F811 - imported pytest fixture
    engine = create_async_engine(to_async_url(pg_url))
    tables = [Organization.__table__, User.__table__, TenantMembership.__table__, UserIdentity.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all([Organization(id="tenant-one", name="one"), Organization(id="tenant-two", name="two")])
        await db.flush()
        db.add_all(
            [
                User(id="human-one", org_id="tenant-one", team_id="", email="one@example.test", cognito_sub=OWNER),
                User(id="human-two", org_id="tenant-two", team_id="", email="two@example.test", cognito_sub="22222222-2222-4222-8222-222222222222"),
            ]
        )
        await db.flush()
        db.add_all(
            [
                TenantMembership(user_id="human-one", tenant_id="tenant-one", is_active=True),
                TenantMembership(user_id="human-two", tenant_id="tenant-two", is_active=True),
                UserIdentity(
                    user_id="human-one",
                    org_id="tenant-one",
                    team_id="",
                    provider="github",
                    provider_user_id="1",
                    provider_username="Actual-User",
                    verification_method="oauth",
                ),
                UserIdentity(
                    user_id="human-two",
                    org_id="tenant-two",
                    team_id="",
                    provider="github",
                    provider_user_id="2",
                    provider_username="Victim",
                    verification_method="oauth",
                ),
            ]
        )
        await db.commit()
    monkeypatch.setattr(service, "get_session_factory", lambda: factory)
    try:
        yield factory
    finally:
        await engine.dispose()


async def test_protected_subject_resolves_only_own_tenant_link(sessions):
    async with service.locked_door_identity(RECORD, GRANT) as headers:
        assert headers == {"x-github-login": "actual-user", "x-owner-sub": OWNER, "x-tenant-id": "tenant-one", "x-adp-run-service": "true"}


@pytest.mark.parametrize("attack", ["other-human", "other-tenant", "inactive", "shadow", "bot", "service"])
async def test_unrelated_or_inactive_identity_cannot_be_borrowed(sessions, attack):
    grant = GRANT
    async with sessions() as db:
        if attack == "other-human":
            grant = replace(grant, authority=replace(grant.authority, human_id="human-two"))
        elif attack == "other-tenant":
            grant = replace(grant, authority=replace(grant.authority, org_id="tenant-two"))
        elif attack == "service":
            grant = replace(grant, authority=replace(grant.authority, kind="service_policy"))
        elif attack == "inactive":
            await db.execute(update(TenantMembership).where(TenantMembership.user_id == "human-one").values(is_active=False))
        elif attack in {"shadow", "bot"}:
            values = {"is_shadow": True} if attack == "shadow" else {"user_kind": "bot"}
            await db.execute(update(User).where(User.id == "human-one").values(**values))
        await db.commit()
    with pytest.raises(HTTPException) as error:
        async with service.locked_door_identity(RECORD, grant):
            pytest.fail("identity was borrowed")
    assert error.value.status_code == 404
    assert error.value.detail == "not found"


async def test_no_github_link_retains_own_personal_identity_without_borrowing_another_tenants_link(sessions):
    async with sessions() as db:
        await db.execute(update(UserIdentity).where(UserIdentity.user_id == "human-one").values(org_id="tenant-two"))
        await db.commit()
    async with service.locked_door_identity(RECORD, GRANT) as headers:
        assert "x-github-login" not in headers
        assert headers["x-owner-sub"] == OWNER
        assert headers["x-tenant-id"] == "tenant-one"


@pytest.mark.parametrize("target", ["membership", "link", "user"])
async def test_identity_rows_cannot_change_during_upstream_use(sessions, target):
    async with service.locked_door_identity(RECORD, GRANT) as headers:
        assert headers["x-github-login"] == "actual-user"
        # PostgreSQL must reject this concurrent write with lock_timeout. SQLite
        # would silently succeed and cannot be used to establish this invariant.
        async with sessions() as db:
            await db.execute(text("SET LOCAL lock_timeout = '150ms'"))
            statement = {
                "membership": update(TenantMembership).where(TenantMembership.user_id == "human-one").values(is_active=False),
                "link": update(UserIdentity).where(UserIdentity.user_id == "human-one").values(provider_username="Victim"),
                "user": update(User).where(User.id == "human-one").values(cognito_sub="33333333-3333-4333-8333-333333333333"),
            }[target]
            from sqlalchemy.exc import DBAPIError

            with pytest.raises(DBAPIError) as error:
                await db.execute(statement)
            assert error.value.orig.sqlstate == "55P03"
            await db.rollback()
    # Lock is released after service use; removal then becomes visible next call.
    async with sessions() as db:
        await db.execute(update(TenantMembership).where(TenantMembership.user_id == "human-one").values(is_active=False))
        await db.commit()
    with pytest.raises(HTTPException):
        async with service.locked_door_identity(RECORD, GRANT):
            pytest.fail("removed membership was reused")


async def test_waiting_request_observes_committed_membership_removal(sessions):
    async with sessions() as writer:
        member = await writer.scalar(select(TenantMembership).where(TenantMembership.user_id == "human-one").with_for_update())
        started = asyncio.Event()

        async def reader():
            started.set()
            async with service.locked_door_identity(RECORD, GRANT):
                pytest.fail("stale member escaped the locked read")

        task = asyncio.create_task(reader())
        await started.wait()
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(task), 0.15)
            member.is_active = False
            await writer.commit()
            with pytest.raises(HTTPException) as error:
                await asyncio.wait_for(task, 3)
            assert error.value.status_code == 404
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
