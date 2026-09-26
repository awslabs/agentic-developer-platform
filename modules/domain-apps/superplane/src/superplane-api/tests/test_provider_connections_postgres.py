"""Connection/grant/registry races against an isolated local PostgreSQL schema."""

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from superplane_contracts.connections import CredentialReference, VaultOwnership

from app.database import Base
from app.models.credential import CredentialRegistry
from app.models.organization import Organization
from app.models.provider_connection import ProviderConnection
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.routers import accounts
from app.routers import provider_connections as router
from app.services import provider_connections as service
from app.services.credential_evidence import VerifiedCredentialEvidence
from tests.test_installation_postgres import (
    installation_postgres_url as installation_postgres_url,
)
from tests.test_installation_postgres import pytestmark as postgres_available
from tests.test_provider_handles_postgres import wait_for_database_lock

# CI installs pgserver and must execute these races. A missing/broken disposable
# server must fail fixture setup there, rather than turn the lane green with skips.
pytestmark = [] if os.environ.get("CI") else postgres_available


@pytest.fixture
async def connection_db(installation_postgres_url):  # noqa: F811 - pytest fixture injection
    url = installation_postgres_url
    schema = "connection_test_" + uuid.uuid4().hex
    admin = create_async_engine(url)
    async with admin.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        url,
        connect_args={
            "server_settings": {"search_path": schema, "statement_timeout": "10000"}
        },
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    org, workspace, registry_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    reference = CredentialReference("pg-credential", "nebius", "test")
    request = SimpleNamespace(
        state=SimpleNamespace(
            caller=SimpleNamespace(
                principal=SimpleNamespace(
                    subject="pg-owner",
                    org_id=str(org),
                    account_type="human",
                )
            )
        )
    )
    evidence = VerifiedCredentialEvidence(
        org_id=str(org),
        workspace_id=str(workspace),
        reference=reference,
        ownership=VaultOwnership(reference.credential_id, "pg-owner"),
        expires_at=datetime.now(UTC) + timedelta(minutes=5),
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with sessions() as session:
            session.add(Organization(id=org, name="connection-test"))
            await session.flush()
            session.add(
                Workspace(
                    id=workspace, org_id=org, name="test", isolation_mode="dedicated"
                )
            )
            await session.flush()
            session.add(
                WorkspaceGrantRecord(
                    workspace_id=workspace,
                    org_id=org,
                    principal="pg-owner",
                    permissions="workspace:renew_credential workspace:read",
                )
            )
            session.add(
                CredentialRegistry(
                    id=registry_id,
                    org_id=org,
                    provider="nebius",
                    friendly_name="test",
                    credential_type="api_key",
                    adp_credential_id=reference.credential_id,
                )
            )
            await session.commit()
        yield SimpleNamespace(
            sessions=sessions,
            org=org,
            workspace=workspace,
            registry_id=registry_id,
            reference=reference,
            request=request,
            evidence=evidence,
        )
    finally:
        await engine.dispose()
        async with admin.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin.dispose()


def authority(ctx, session):
    return lambda: router._mutation_authority(
        session, ctx.request, ctx.org, ctx.workspace, "nebius", ctx.evidence
    )


async def register(ctx, session):
    return await service.register(
        session,
        org_id=ctx.org,
        workspace_id=ctx.workspace,
        reference=ctx.reference,
        provider="nebius",
        owner_principal="pg-owner",
        bound_by="pg-owner",
        verify_authority=authority(ctx, session),
    )


@pytest.mark.parametrize("revoke", [True, False])
async def test_grant_change_while_waiting_for_connection_rolls_back(
    connection_db, revoke
):
    ctx = connection_db
    async with ctx.sessions() as session:
        connection, _ = await register(ctx, session)
        connection_id = connection.id
    async with ctx.sessions() as blocker, ctx.sessions() as writer:
        await blocker.execute(
            select(ProviderConnection)
            .where(ProviderConnection.id == connection_id)
            .with_for_update()
        )
        pid = await writer.scalar(text("SELECT pg_backend_pid()"))

        async def disable_after_wait():
            connection, binding = await service.load(
                writer, org_id=ctx.org, connection_id=connection_id
            )
            with pytest.raises(HTTPException) as denied:
                await service.record_disablement(
                    writer,
                    connection=connection,
                    binding=binding,
                    verify_authority=authority(ctx, writer),
                )
            assert denied.value.status_code == 403

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(disable_after_wait())
            try:
                await wait_for_database_lock(ctx.sessions, pid)
                async with ctx.sessions() as revoker:
                    values = (
                        {"revoked_at": datetime.now(UTC)}
                        if revoke
                        else {"permissions": "workspace:read"}
                    )
                    await revoker.execute(update(WorkspaceGrantRecord).values(**values))
                    await revoker.commit()
            finally:
                await blocker.rollback()
    async with ctx.sessions() as session:
        assert (
            await session.get(ProviderConnection, connection_id)
        ).status == "pending"


async def test_grant_lock_is_held_until_mutation_commit(connection_db, monkeypatch):
    ctx = connection_db
    ready, proceed = asyncio.Event(), asyncio.Event()
    async with ctx.sessions() as writer, ctx.sessions() as revoker:
        commit = writer.commit

        async def pause_before_commit():
            ready.set()
            await asyncio.wait_for(proceed.wait(), 5)
            await commit()

        monkeypatch.setattr(writer, "commit", pause_before_commit)
        pid = await revoker.scalar(text("SELECT pg_backend_pid()"))

        async def revoke():
            await revoker.execute(
                update(WorkspaceGrantRecord).values(revoked_at=datetime.now(UTC))
            )
            await revoker.commit()

        async with asyncio.TaskGroup() as tasks:
            registration = tasks.create_task(register(ctx, writer))
            await asyncio.wait_for(ready.wait(), 5)
            tasks.create_task(revoke())
            try:
                await wait_for_database_lock(ctx.sessions, pid)
            finally:
                proceed.set()
        assert registration.result()[0].status == "pending"


async def test_deregistered_reference_cannot_commit_waiting_registration(connection_db):
    ctx = connection_db
    async with ctx.sessions() as remover, ctx.sessions() as writer:
        await remover.execute(
            select(CredentialRegistry)
            .where(CredentialRegistry.id == ctx.registry_id)
            .with_for_update()
        )
        pid = await writer.scalar(text("SELECT pg_backend_pid()"))

        async def waiting_registration():
            with pytest.raises(HTTPException) as denied:
                await register(ctx, writer)
            assert denied.value.status_code == 404

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(waiting_registration())
            try:
                await wait_for_database_lock(ctx.sessions, pid)
                await accounts.delete_credential(
                    ctx.registry_id, ctx.request, ctx.org, remover
                )
            finally:
                await remover.rollback()
    async with ctx.sessions() as session:
        assert (
            await session.get(CredentialRegistry, ctx.registry_id)
        ).status == "Deregistered"
        assert (await session.execute(select(ProviderConnection))).scalars().all() == []


async def test_concurrent_operation_registration_returns_one_exact_connection(
    connection_db, monkeypatch
):
    ctx = connection_db
    operation = uuid.uuid4()
    payload = {
        "operation_id": str(operation),
        "credential_id": ctx.reference.credential_id,
        "service": ctx.reference.service,
        "label": ctx.reference.label,
        "provider": "nebius",
    }

    async def body():
        return dict(payload)

    async def stream():
        import json

        yield json.dumps(payload).encode()

    ctx.request.json = body
    ctx.request.stream = stream
    ctx.request.state.grant = SimpleNamespace(
        permissions=frozenset({"workspace:renew_credential", "workspace:read"})
    )

    async def evidence(*args, **kwargs):
        return ctx.evidence

    monkeypatch.setattr(router, "_vault_evidence", evidence)
    async with ctx.sessions() as first, ctx.sessions() as second:
        replies = await asyncio.wait_for(
            asyncio.gather(
                router.register_connection(ctx.request, ctx.workspace, ctx.org, first),
                router.register_connection(ctx.request, ctx.workspace, ctx.org, second),
            ),
            timeout=10,
        )
    assert replies[0] == replies[1]
    assert replies[0]["connection_id"] == str(operation)
    assert replies[0]["status"] == "pending"
    async with ctx.sessions() as read:
        rows = (
            await read.scalars(
                select(ProviderConnection).where(ProviderConnection.org_id == ctx.org)
            )
        ).all()
        assert len(rows) == 1 and rows[0].id == operation


async def test_operation_replay_rechecks_owner_and_current_authority(connection_db):
    ctx = connection_db
    operation = uuid.uuid4()
    kwargs = dict(
        org_id=ctx.org,
        workspace_id=ctx.workspace,
        reference=ctx.reference,
        provider="nebius",
        owner_principal="pg-owner",
        bound_by="pg-owner",
        operation_id=operation,
    )
    async with ctx.sessions() as session:
        await service.register(
            session, verify_authority=authority(ctx, session), **kwargs
        )
    async with ctx.sessions() as session:
        with pytest.raises(service.RegistrationConflict):
            await service.register(
                session,
                verify_authority=authority(ctx, session),
                **{**kwargs, "owner_principal": "different-owner"},
            )
    async with ctx.sessions() as revoked:
        await revoked.execute(
            update(WorkspaceGrantRecord)
            .where(WorkspaceGrantRecord.workspace_id == ctx.workspace)
            .values(permissions="workspace:read")
        )
        await revoked.commit()
    async with ctx.sessions() as session:
        with pytest.raises(HTTPException) as caught:
            await service.register(
                session, verify_authority=authority(ctx, session), **kwargs
            )
        assert caught.value.status_code == 403
