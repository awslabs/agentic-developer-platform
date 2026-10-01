"""Proof preservation and atomic confirmation against real PostgreSQL.

Private-delivery nonces below are synthetic consumer-contract fixtures. Production
internal issuance uses shared-channel delivery; no private provider is contacted.
Authentication is a trusted test dependency, while routes, ORM writes, row locks,
nonce consumption, and audit transactions run normally.
"""

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from fastapi import FastAPI, Request
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.auth import vault_routes
from src.auth.magic_link import issue_token, store_nonce
from src.auth.middleware import get_current_user_context
from src.internal import routes as internal_routes
from src.shared.database import get_db
from src.shared.identity.verification import PROVEN_METHODS, is_proven
from src.shared.models.audit import AuditLog
from src.shared.models.base import Base
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import ChannelTenantMap, MagicLinkNonce, UserIdentity
from src.shared.schemas.auth import TokenContext

_SECRET = "local-magic-link-proof-test-secret-32chars"
_PROVED_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture(scope="module")
def local_postgres():
    pgserver = pytest.importorskip("pgserver")
    # A short socket path also works on macOS, whose Unix socket limit is 104.
    with TemporaryDirectory(prefix="mlpg-", dir="/tmp") as data:
        server = pgserver.get_server(data)
        try:
            yield server.get_uri().replace("postgresql://", "postgresql+asyncpg://", 1)
        finally:
            server.cleanup()


@pytest.fixture
async def rig(local_postgres, monkeypatch):
    schema = "magic_" + uuid.uuid4().hex
    engine = create_async_engine(local_postgres, connect_args={"server_settings": {"search_path": schema}})
    tables = [model.__table__ for model in (Organization, Department, Team, User, UserIdentity, MagicLinkNonce, AuditLog, ChannelTenantMap)]
    async with engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        await conn.run_sync(lambda sync: Base.metadata.create_all(sync, tables=tables))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add_all(
            [
                Organization(id="org", name="Local test"),
                Department(id="dept", org_id="org", name="Department"),
                Team(id="team", org_id="org", department_id="dept", name="Team"),
                User(id="alice", org_id="org", team_id="team", email="alice@example.test"),
                User(id="bob", org_id="org", team_id="team", email="bob@example.test"),
            ]
        )
        await db.commit()
    monkeypatch.setattr(vault_routes, "_get_magic_link_secret", lambda: _SECRET)
    monkeypatch.setattr(internal_routes, "_get_magic_link_secret", lambda: _SECRET)
    monkeypatch.setattr(internal_routes, "_build_magic_link_url", lambda token: "https://local.invalid/link?token=" + token)

    def client(session_type=AsyncSession):
        app = FastAPI()
        app.include_router(vault_routes.router)
        app.include_router(internal_routes.router)
        request_factory = async_sessionmaker(engine, class_=session_type, expire_on_commit=False)

        async def database():
            async with request_factory() as session:
                yield session

        async def caller(request: Request):
            return TokenContext(
                user_id=request.headers.get("x-test-user", "alice"),
                org_id="org",
                team_id="team",
                department_id="dept",
                account_type="human",
                is_admin=False,
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )

        app.dependency_overrides[get_db] = database
        app.dependency_overrides[get_current_user_context] = caller
        app.dependency_overrides[vault_routes.get_secrets_manager] = lambda: MagicMock()

        async def internal_caller(request: Request):
            from types import SimpleNamespace

            request.state.token_context = SimpleNamespace(
                user_id="iam-agent:ingress",
                auth_source="iam",
                scope="internal",
                org_id="__platform__",
                credential_scopes=["internal:identity:resolve", "internal:identity:link", "internal:cross-tenant"],
            )

        app.dependency_overrides[internal_routes.verify_internal_or_irsa] = internal_caller
        return AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://local.invalid")

    async def synthetic_private_nonce(user="alice"):
        token = issue_token(provider="slack", provider_user_id="external-1", channel_context=None, target_user_id=user, secret_key=_SECRET)
        async with factory() as db:
            await store_nonce(
                jti=token["jti"],
                provider="slack",
                provider_user_id="external-1",
                channel_context=None,
                target_user_id=user,
                expires_at=token["expires_at"],
                delivery_method="provider_dm",
                db=db,
            )
        return token

    async def holder(method="self_asserted"):
        async with factory() as db:
            row = UserIdentity(
                org_id="org",
                team_id="team",
                user_id="alice",
                provider="slack",
                provider_user_id="external-1",
                verification_method=method,
                verified_at=_PROVED_AT if is_proven(method) else None,
            )
            db.add(row)
            await db.commit()
            return row.id

    try:
        yield factory, client, synthetic_private_nonce, holder
    finally:
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await engine.dispose()


@pytest.mark.parametrize("method", sorted(PROVEN_METHODS))
async def test_shared_confirmation_preserves_same_holder_proof_and_resolution(rig, method):
    factory, client, _, holder = rig
    identity_id = await holder(method)
    async with client() as http:
        resolved = await http.post("/internal/v1/resolve-user", json={"provider": "slack", "provider_user_id": "external-1", "org_id": "org"})
        assert resolved.status_code == 200, resolved.text
        issued = await http.post("/internal/v1/issue-magic-link", json={"provider": "slack", "provider_user_id": "external-1"})
        assert issued.status_code == 201, issued.text
        token = parse_qs(urlsplit(issued.json()["magic_link_url"]).query)["token"][0]
        response = await http.post("/auth/link/magic", params={"token": token})
        assert response.status_code == 201, response.text
        assert response.json() == {
            "status": "linked",
            "identity_id": identity_id,
            "provider": "slack",
            "provider_user_id": "external-1",
            "verification_method": method,
            "verified_at": _PROVED_AT.isoformat(),
            "next_step": None,
        }
        resolved = await http.post("/internal/v1/resolve-user", json={"provider": "slack", "provider_user_id": "external-1", "org_id": "org"})
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["user_id"] == "alice"
    async with factory() as db:
        row = await db.get(UserIdentity, identity_id)
        assert (row.verification_method, row.verified_at) == (method, _PROVED_AT)
        audit = (await db.scalars(select(AuditLog).where(AuditLog.event_type == "identity_linked"))).one()
        assert audit.details["verification_method"] == method
        # Existing proof survived; this shared nonce did not supply new proof.
        assert audit.details["ownership_proven"] is False


@pytest.mark.parametrize("consumer", ["alice", "bob"])
async def test_current_holder_proof_overrides_cached_unproven_classification(rig, consumer):
    factory, client, nonce, holder = rig
    identity_id = await holder()
    issued = await nonce(consumer)
    cached, resume = asyncio.Event(), asyncio.Event()

    class CachedHolderSession(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            if str(statement).startswith("SELECT user_identities.") and not cached.is_set():
                # Keep a strong ORM reference loaded before another transaction
                # proves the row. The route's locked read must also refresh it.
                self.cached_holder = await self.get(UserIdentity, identity_id)
                assert self.cached_holder.verification_method == "self_asserted"
                cached.set()
                await asyncio.wait_for(resume.wait(), 10)
            return await super().execute(statement, *args, **kwargs)

    async with client(CachedHolderSession) as http:
        task = asyncio.create_task(http.post("/auth/link/magic", params={"token": issued["token"]}, headers={"x-test-user": consumer}))
        try:
            await asyncio.wait_for(cached.wait(), 10)
            async with factory() as writer:
                await writer.execute(
                    update(UserIdentity).where(UserIdentity.id == identity_id).values(verification_method="oauth", verified_at=_PROVED_AT)
                )
                await writer.commit()
        finally:
            resume.set()
        response = await asyncio.wait_for(task, 10)
    assert response.status_code == (201 if consumer == "alice" else 409), response.text
    async with factory() as db:
        rows = (await db.scalars(select(UserIdentity))).all()
        assert [(row.id, row.user_id, row.verification_method, row.verified_at) for row in rows] == [(identity_id, "alice", "oauth", _PROVED_AT)]
        assert (await db.get(MagicLinkNonce, issued["jti"])).consumed_at is not None
        assert not (await db.scalars(select(AuditLog).where(AuditLog.event_type == "identity_claim_reclaimed"))).all()
    if consumer == "alice":
        assert response.json()["verification_method"] == "oauth"
        assert response.json()["status"] == "linked"
        assert response.json()["next_step"] is None


async def test_recovery_holds_holder_lock_until_final_transaction_completes(rig):
    factory, client, nonce, holder = rig
    identity_id = await holder()
    issued = await nonce("bob")
    read, resume = asyncio.Event(), asyncio.Event()

    class PauseAfterRead(AsyncSession):
        async def execute(self, statement, *args, **kwargs):
            result = await super().execute(statement, *args, **kwargs)
            if str(statement).startswith("SELECT user_identities.") and not read.is_set():
                read.set()
                await asyncio.wait_for(resume.wait(), 10)
            return result

    async with client(PauseAfterRead) as http:
        task = asyncio.create_task(http.post("/auth/link/magic", params={"token": issued["token"]}, headers={"x-test-user": "bob"}))
        try:
            await asyncio.wait_for(read.wait(), 10)
            async with factory() as writer:
                # An attempted proof write must not commit between the holder
                # classification and deletion. The DB supplies the lock evidence.
                await writer.execute(text("SET LOCAL lock_timeout = '200ms'"))
                with pytest.raises(DBAPIError) as failure:
                    await writer.execute(
                        update(UserIdentity).where(UserIdentity.id == identity_id).values(verification_method="oauth", verified_at=_PROVED_AT)
                    )
                assert failure.value.orig.sqlstate == "55P03"
                await writer.rollback()
        finally:
            resume.set()
        response = await asyncio.wait_for(task, 10)
    assert response.status_code == 201, response.text
    async with factory() as db:
        row = (await db.scalars(select(UserIdentity))).one()
        assert row.id != identity_id
        assert (row.user_id, row.verification_method) == ("bob", "magic_link_confirmed")
        assert (await db.get(MagicLinkNonce, issued["jti"])).consumed_at is not None
        events = [audit.event_type for audit in (await db.scalars(select(AuditLog))).all()]
        assert sorted(events) == ["identity_claim_reclaimed", "identity_linked", "magic_link_consumed"]


@pytest.mark.parametrize("recover", [False, True])
async def test_failed_final_commit_preserves_nonce_holder_and_audit_for_retry(rig, recover):
    factory, client, nonce, holder = rig
    identity_id = await holder() if recover else None
    issued = await nonce("bob")

    class FailCommit(AsyncSession):
        async def commit(self):
            raise RuntimeError("test final commit failure")

    async with client(FailCommit) as http:
        response = await http.post("/auth/link/magic", params={"token": issued["token"]}, headers={"x-test-user": "bob"})
    assert response.status_code == 409, response.text
    async with factory() as db:
        rows = (await db.scalars(select(UserIdentity))).all()
        assert [row.id for row in rows] == ([identity_id] if recover else [])
        assert (await db.get(MagicLinkNonce, issued["jti"])).consumed_at is None
        assert (await db.scalars(select(AuditLog))).all() == []
    async with client() as http:
        response = await http.post("/auth/link/magic", params={"token": issued["token"]}, headers={"x-test-user": "bob"})
    assert response.status_code == 201, response.text


async def test_two_consumers_of_one_nonce_have_one_transaction_winner(rig):
    factory, client, nonce, _ = rig
    issued = await nonce()
    async with client() as http:
        responses = await asyncio.gather(*(http.post("/auth/link/magic", params={"token": issued["token"]}) for _ in range(2)))
    assert sorted(response.status_code for response in responses) == [201, 400], [response.text for response in responses]
    async with factory() as db:
        assert len((await db.scalars(select(UserIdentity))).all()) == 1
        assert (await db.get(MagicLinkNonce, issued["jti"])).consumed_at is not None
        consumed = (await db.scalars(select(AuditLog).where(AuditLog.event_type == "magic_link_consumed"))).all()
        assert len(consumed) == 1
