"""Mounted human grant transactions against the existing disposable CI PostgreSQL fixture."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.current_identity import CurrentIdentity
from app.database import get_session
from app.main import app
from app.models.cloud_account import CloudAccount
from app.models.cluster import Cluster
from app.models.event import Event
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.models.workspace_grant_change import WorkspaceGrantChange
from tests.test_auth import _mint, enforcing as enforcing, rsa_keys as rsa_keys
from tests.test_installation_postgres import (
    installation_postgres_url as installation_postgres_url,
    pytestmark as pytestmark,
)


class CurrentMembers:
    def __init__(self):
        self.active = {"owner", "approver"}
        self.target_read = None
        self.continue_read = None

    async def read(self, *, subject, principal_type, adp_org_id):
        if subject == "approver" and self.target_read is not None:
            self.target_read.set()
            await self.continue_read.wait()
        if subject not in self.active or principal_type != "human" or adp_org_id != "adp-transaction-test":
            return None
        return CurrentIdentity(subject, "human", adp_org_id, f"membership-{subject}", True, True)


@pytest.fixture
async def postgres_access(installation_postgres_url, enforcing, client, monkeypatch):
    schema = "grant_" + uuid.uuid4().hex
    admin_engine = create_async_engine(installation_postgres_url)
    async with admin_engine.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        installation_postgres_url, connect_args={"server_settings": {"search_path": schema}},
    )
    tables = [model.__table__ for model in (
        Organization, CloudAccount, Cluster, Workspace, WorkspaceGrantRecord,
        Event, WorkspaceGrantChange,
    )]
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync: Organization.metadata.create_all(sync, tables=tables))
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def postgres_session():
        async with sessions() as session:
            yield session

    original_session = app.dependency_overrides.get(get_session)
    app.dependency_overrides[get_session] = postgres_session
    monkeypatch.setattr(settings, "current_identity_enforced", True)
    members = CurrentMembers()
    monkeypatch.setattr(app.state, "current_identity_reader", members, raising=False)
    org_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    async with sessions() as session:
        session.add(Organization(id=org_id, name=f"transaction-{org_id}", adp_org_id="adp-transaction-test"))
        await session.flush()
        session.add(Workspace(id=workspace_id, org_id=org_id, name="transaction", isolation_mode="research"))
        session.add(WorkspaceGrantRecord(
            workspace_id=workspace_id, org_id=org_id, principal="owner",
            principal_type="human", permissions="workspace:administer",
        ))
        await session.commit()

    def token(subject="owner"):
        return {"Authorization": f"Bearer {_mint(enforcing, sub=subject, **{'custom:org_id': 'adp-transaction-test'})}"}

    try:
        yield client, sessions, workspace_id, members, token
    finally:
        if original_session is None:
            app.dependency_overrides.pop(get_session, None)
        else:
            app.dependency_overrides[get_session] = original_session
        await engine.dispose()
        async with admin_engine.begin() as connection:
            await connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


def request(**changes):
    body = {
        "target_subject": "approver", "principal_type": "human",
        "permissions": ["workspace:read"], "reason": "approver_setup",
        "expected_revision": 0, "request_id": str(uuid.uuid4()),
    }
    body.update(changes)
    return body


@pytest.mark.asyncio
async def test_concurrent_duplicate_and_revision_conflict_postgres(postgres_access):
    client, sessions, workspace_id, _, token = postgres_access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    body = request()
    first, second = await asyncio.gather(
        client.post(url, json=body, headers=token()),
        client.post(url, json=body, headers=token()),
    )
    assert (first.status_code, second.status_code) == (200, 200), (first.text, second.text)
    assert first.json() == second.json()
    assert (await client.post(url, json=request(), headers=token())).status_code == 409
    async with sessions() as session:
        assert len((await session.scalars(select(WorkspaceGrantChange))).all()) == 1
        assert len((await session.scalars(select(Event).where(Event.event_type == "workspace_access"))).all()) == 1


@pytest.mark.asyncio
async def test_revocation_and_removed_admin_before_write_postgres(postgres_access):
    client, sessions, workspace_id, members, token = postgres_access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    members.target_read = asyncio.Event()
    members.continue_read = asyncio.Event()
    pending = asyncio.create_task(client.post(url, json=request(), headers=token()))
    await asyncio.wait_for(members.target_read.wait(), timeout=5)
    async with sessions() as session:
        owner = await session.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == workspace_id,
            WorkspaceGrantRecord.principal == "owner",
        ))
        owner.revoked_at = datetime.now(UTC)
        await session.commit()
    members.continue_read.set()
    refused = await asyncio.wait_for(pending, timeout=10)
    assert refused.status_code == 403, refused.text
    async with sessions() as session:
        assert (await session.scalars(select(WorkspaceGrantChange))).all() == []


@pytest.mark.asyncio
async def test_revoked_target_cannot_be_replayed_after_new_session_postgres(postgres_access):
    client, sessions, workspace_id, _, token = postgres_access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    body = request()
    assert (await client.post(url, json=body, headers=token())).status_code == 200
    async with sessions() as session:
        target = await session.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == workspace_id,
            WorkspaceGrantRecord.principal == "approver",
        ))
        target.revoked_at = datetime.now(UTC)
        await session.commit()
    assert (await client.post(url, json=body, headers=token())).status_code == 409
    assert (await client.post(url, json=request(expected_revision=1), headers=token())).status_code == 409
    assert (await client.get(
        f"/workspaces/{workspace_id}/access/v1/me", headers=token("approver"),
    )).status_code == 403
    async with sessions() as session:
        target = await session.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == workspace_id,
            WorkspaceGrantRecord.principal == "approver",
        ))
        assert target.revoked_at is not None


@pytest.mark.asyncio
async def test_concurrent_target_revoke_cannot_be_overwritten_postgres(postgres_access):
    client, sessions, workspace_id, members, token = postgres_access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    created = await client.post(url, json=request(), headers=token())
    assert created.status_code == 200, created.text
    members.target_read = asyncio.Event()
    members.continue_read = asyncio.Event()
    pending = asyncio.create_task(client.post(
        url, json=request(expected_revision=1, permissions=["workspace:spend"]), headers=token(),
    ))
    await asyncio.wait_for(members.target_read.wait(), timeout=5)
    async with sessions() as session:
        target = await session.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == workspace_id,
            WorkspaceGrantRecord.principal == "approver",
        ))
        target.revoked_at = datetime.now(UTC)
        await session.commit()
    members.continue_read.set()
    refused = await asyncio.wait_for(pending, timeout=10)
    assert refused.status_code == 409, refused.text
    async with sessions() as session:
        target = await session.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == workspace_id,
            WorkspaceGrantRecord.principal == "approver",
        ))
        assert target.revoked_at is not None
        assert target.permissions == "workspace:read"
        assert len((await session.scalars(select(WorkspaceGrantChange))).all()) == 1
