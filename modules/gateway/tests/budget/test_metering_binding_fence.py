"""Production identity writers share the durable person-admission fence."""

import asyncio
from datetime import UTC, datetime, timedelta

import jwt
import psycopg2
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.auth import vault_routes
from src.auth.cognito_jwt import CognitoTokenClaims
from src.auth.dependencies import get_current_user
from src.auth.exceptions import TokenValidationError
from src.budget.metering_admission import MeteringAccountingUnresolvedError, MeteringAdmissionError
from src.shared.database import get_db
from src.shared.models.metering import MeteringScopeAlias
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext
from tests.admin import test_user_role_update as role_update_fixtures
from tests.admin.test_user_role_update import client_for, make_context
from tests.budget import test_metering_scope_admission as scope_fixtures
from tests.budget.test_metering_person_scopes import activate_person_scopes, person_key, rooted_context, seed_person
from tests.budget.test_metering_scope_admission import admission_for, prepare

pytest_plugins = ("tests.budget.test_metering_scope_admission",)


@pytest.fixture
def metering_db(pg_head_url):
    return scope_fixtures.metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def journal():
    yield from scope_fixtures.journal.__wrapped__()


@pytest.fixture
def cognito_env():
    yield from role_update_fixtures.cognito_env.__wrapped__()


async def test_membership_writer_waits_for_admission_without_changing_historical_attribution(metering_db, journal, monkeypatch, cognito_env):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "123")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123"],
    )
    admission, engine = admission_for(metering_db, journal)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as db:
        db.add(User(id="admin-b", org_id="tenant-2", team_id="", email="admin-b@example.test", cognito_sub="admin-sub", role="platform_admin"))
        user = await db.get(User, "root-b")
        user.cognito_sub = "target-sub"
        user.role = "member"
        await db.flush()
        db.add(TenantMembership(user_id="admin-b", tenant_id="tenant-2", role="org_admin", is_active=True))
        await db.commit()
    entered, release = asyncio.Event(), asyncio.Event()
    admission_pid = None

    class PausedAdmissionSession(AsyncSession):
        async def commit(self):
            nonlocal admission_pid
            await self.flush()
            if not entered.is_set():
                admission_pid = await self.scalar(text("SELECT pg_backend_pid()"))
                entered.set()
                await asyncio.wait_for(release.wait(), 3)
            await super().commit()

    monkeypatch.setattr(admission, "_session_factory", async_sessionmaker(engine, class_=PausedAdmissionSession, expire_on_commit=False))

    client = client_for(engine, make_context("admin-sub", "tenant-2", is_admin=True))
    client._transport.app.dependency_overrides.pop(get_current_user)

    class ControlledValidator:
        def validate_token(self, token):
            if token not in {"admin-fixture", "unprivileged-fixture"}:
                raise jwt.InvalidTokenError
            current = int(datetime.now(UTC).timestamp())
            return CognitoTokenClaims(
                sub="admin-sub" if token == "admin-fixture" else "target-sub",
                iss="https://fixture.invalid",
                client_id="fixture-web",
                token_use="access",
                exp=current + 3600,
                iat=current - 60,
                username="admin-sub" if token == "admin-fixture" else "target-sub",
                org_id="tenant-2",
                role="platform_admin" if token == "admin-fixture" else "member",
                account_type="human",
            )

    monkeypatch.setattr("src.auth.dependencies._get_cognito_validator", lambda: ControlledValidator())
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: sessions)
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", lambda subject, username="": subject == "admin-sub")

    async def change_membership():
        return await client.put(
            "/admin/organizations/tenant-2/users/root-b",
            json={"role": "dept_admin"},
            headers={"Authorization": "Bearer admin-fixture"},
        )

    admission_task = mutation_task = None
    try:
        async with client:
            for headers in ({}, {"Authorization": "Bearer invalid-fixture"}, {"Authorization": "Bearer unprivileged-fixture"}):
                denied = await client.put("/admin/organizations/tenant-2/users/root-b", json={"role": "dept_admin"}, headers=headers)
                assert denied.status_code in {401, 403}
        client = client_for(engine, make_context("admin-sub", "tenant-2", is_admin=True))
        client._transport.app.dependency_overrides.pop(get_current_user)
        admission_task = asyncio.create_task(prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "before-membership"))
        await asyncio.wait_for(entered.wait(), 3)
        async with client, engine.connect() as observer:
            await observer.execution_options(isolation_level="AUTOCOMMIT")
            mutation_task = asyncio.create_task(change_membership())

            async def wait_for_membership_lock():
                while not await observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                        "AND query LIKE 'INSERT INTO tenant_memberships%' "
                        "AND :admission_pid = ANY(pg_blocking_pids(pid)))"
                    ),
                    {"admission_pid": admission_pid},
                ):
                    if mutation_task.done():
                        response = await mutation_task
                        pytest.fail(f"membership write completed without waiting: {response.status_code}")
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_membership_lock(), 3)
            assert not mutation_task.done()
            release.set()
            await asyncio.wait_for(admission_task, 3)
            assert (await asyncio.wait_for(mutation_task, 3)).status_code == 200
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT tenant_id, request_id FROM metering_attempts")
            assert cursor.fetchall() == [("tenant-1", "before-membership")]
            cursor.execute(
                "SELECT scope_key FROM metering_attempt_scopes WHERE request_id='before-membership' AND scope_key=%s", (person_key("github:123"),)
            )
            assert cursor.fetchall() == [(person_key("github:123"),)]
            cursor.execute("SELECT role FROM tenant_memberships WHERE user_id='root-b'")
            assert cursor.fetchall() == [("dept_admin",)]
    finally:
        release.set()
        tasks = [task for task in (admission_task, mutation_task) if task is not None]
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await engine.dispose()


async def test_identity_link_preserves_unknown_scope_for_new_and_relinked_members(metering_db, journal):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "456"), ("root-c", "tenant-2", "789")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b"), ("tenant-2", "root-c")],
        anchors=["github:123", "github:456", "github:789"],
    )
    admission, engine = admission_for(metering_db, journal)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "unknown-before-link")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='unknown-before-link'")
        async with sessions() as db:
            await db.execute(update(UserIdentity).where(UserIdentity.user_id == "root-b").values(provider_user_id="123"))
            await db.commit()
        async with sessions() as db:
            aliases = (await db.scalars(select(MeteringScopeAlias.scope_key).where(MeteringScopeAlias.stable_alias == "root-b"))).all()
            assert person_key("github:123") in aliases
        async with sessions() as db:
            await db.execute(update(UserIdentity).where(UserIdentity.user_id == "root-b").values(provider_user_id="456"))
            await db.commit()
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b", enforcement_enabled=False), "blocked-after-unlink")
        await prepare(admission, rooted_context("tenant-2", "owner-2", "root-c"), "unrelated-person")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT request_id FROM metering_attempts ORDER BY request_id")
            assert cursor.fetchall() == [("unknown-before-link",), ("unrelated-person",)]
    finally:
        await engine.dispose()


async def test_identity_mutation_commits_before_competing_admission_reloads_aliases(metering_db, journal):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "456")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123", "github:456"],
    )
    admission, engine = admission_for(metering_db, journal)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "unknown-before-race")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='unknown-before-race'")
        async with sessions() as writer:
            await writer.execute(update(UserIdentity).where(UserIdentity.user_id == "root-b").values(provider_user_id="123"))
            contender = asyncio.create_task(prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "contending-admission"))
            await asyncio.sleep(0.05)
            assert not contender.done()
            await writer.commit()
        with pytest.raises(MeteringAdmissionError):
            await asyncio.wait_for(contender, 3)
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "retry-after-identity")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT request_id FROM metering_attempts")
            assert cursor.fetchall() == [("unknown-before-race",)]
    finally:
        await engine.dispose()


async def test_person_with_unknown_exposure_cannot_be_deleted(metering_db, journal):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1")],
        roots=[("tenant-1", "root-a")],
        anchors=["github:123"],
    )
    admission, engine = admission_for(metering_db, journal)
    try:
        await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "retained-person")
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            with pytest.raises(DBAPIError, match="unsettled metering person binding"):
                await db.execute(delete(User).where(User.id == "root-a"))
            await db.rollback()
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id FROM users WHERE id='root-a'")
            assert cursor.fetchall() == [("root-a",)]
            cursor.execute("SELECT scope_key FROM metering_scope_aliases WHERE stable_alias='root-a'")
            assert cursor.fetchall() == [(person_key("github:123"),)]
    finally:
        await engine.dispose()


async def test_user_then_identity_writer_has_finite_fence_and_keeps_prior_alias(metering_db, journal):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "123")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123", "github:456"],
    )
    admission, engine = admission_for(metering_db, journal)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "before-user-and-link")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='before-user-and-link'")
        async with sessions() as db:
            db.add(
                UserIdentity(
                    user_id="root-a",
                    org_id="tenant-1",
                    team_id="",
                    provider="cognito",
                    provider_user_id="subject-a",
                    verification_method="org_placement",
                )
            )
            await db.commit()
        async with sessions() as writer:
            await writer.execute(update(User).where(User.id == "root-a").values(cognito_sub="subject-a"))
            contender = asyncio.create_task(prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "during-user-and-link"))
            await asyncio.sleep(0.05)
            assert not contender.done()
            await writer.execute(
                update(UserIdentity).where(UserIdentity.user_id == "root-a", UserIdentity.provider == "github").values(provider_user_id="456")
            )
            await writer.commit()
        with pytest.raises((MeteringAdmissionError, MeteringAccountingUnresolvedError)):
            await asyncio.wait_for(contender, 3)
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "after-user-and-link")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT scope_key FROM metering_scope_aliases WHERE stable_alias='root-b'")
            assert person_key("github:123") in {row[0] for row in cursor.fetchall()}
    finally:
        await engine.dispose()


async def test_disjoint_membership_writer_does_not_hold_other_person(metering_db, journal):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "456")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123", "github:456"],
    )
    admission, engine = admission_for(metering_db, journal)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as writer:
            writer.add(TenantMembership(user_id="root-a", tenant_id="tenant-1", role="member", is_active=True))
            await writer.flush()
            await asyncio.wait_for(prepare(admission, rooted_context("tenant-2", "owner-2", "root-b"), "disjoint-person"), 3)
            await writer.rollback()
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT request_id FROM metering_attempts")
            assert cursor.fetchall() == [("disjoint-person",)]
    finally:
        await engine.dispose()


async def test_authenticated_link_and_unlink_never_release_unknown_person(metering_db, journal, monkeypatch):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1")],
        roots=[("tenant-1", "root-a")],
        anchors=["github:123"],
    )
    admission, engine = admission_for(metering_db, journal)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(vault_routes.router)

    async def db():
        async with sessions() as session:
            yield session

    app.dependency_overrides[get_db] = db

    async def validate(token, session):
        if token != "owned-fixture":
            raise TokenValidationError()
        if await session.get(User, "root-a") is None:
            raise TokenValidationError()
        return TokenContext(
            user_id="root-sub",
            org_id="tenant-1",
            team_id="",
            department_id="",
            account_type="human",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )

    monkeypatch.setattr("src.auth.middleware.auth_service.validate_token", validate)
    try:
        async with sessions() as session:
            user = await session.get(User, "root-a")
            user.cognito_sub = "root-sub"
            await session.commit()
        await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "unknown-before-http-link")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='unknown-before-http-link'")
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            path = "/auth/identities/slack/link"
            body = {"provider_user_id": "fixture-slack-id"}
            assert (await client.post(path, json=body)).status_code == 401
            assert (await client.post(path, json=body, headers={"Authorization": "Bearer invalid"})).status_code == 401
            added = await client.post(path, json=body, headers={"Authorization": "Bearer owned-fixture"})
            assert added.status_code == 201
            assert added.json()["verification_method"] == "self_asserted"
            identity_id = added.json()["identity_id"]
            assert (await client.delete(f"/auth/identities/{identity_id}")).status_code == 401
            assert (await client.delete(f"/auth/identities/{identity_id}", headers={"Authorization": "Bearer owned-fixture"})).status_code == 204
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT scope_key FROM metering_scope_aliases WHERE stable_alias='root-a'")
            assert cursor.fetchall() == [(person_key("github:123"),)]
            cursor.execute("SELECT count(*) FROM user_identities WHERE id=%s", (identity_id,))
            assert cursor.fetchone() == (0,)
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a", enforcement_enabled=False), "held-after-http-unlink")
    finally:
        await engine.dispose()


async def test_verified_anchor_relink_recovers_alias_after_prior_identity_unlink(metering_db, journal):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "456")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123", "github:456"],
    )
    admission, engine = admission_for(metering_db, journal)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await prepare(admission, rooted_context("tenant-1", "owner-1", "root-a"), "before-unlink")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='before-unlink'")
        async with sessions() as db:
            await db.execute(delete(UserIdentity).where(UserIdentity.user_id == "root-a", UserIdentity.provider == "github"))
            await db.commit()
        async with sessions() as db:
            await db.execute(update(UserIdentity).where(UserIdentity.user_id == "root-b").values(provider_user_id="123"))
            await db.commit()
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT scope_key FROM metering_scope_aliases WHERE stable_alias='root-b'")
            assert person_key("github:123") in {row[0] for row in cursor.fetchall()}
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, rooted_context("tenant-2", "owner-2", "root-b", enforcement_enabled=False), "held-after-relink")
    finally:
        await engine.dispose()
