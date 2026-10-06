"""Mounted human grant transactions against the existing disposable CI PostgreSQL fixture."""

import asyncio
import json
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import event, select, text
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
        if subject == "other-org-person":
            return CurrentIdentity(subject, "human", "different-adp-org", "other-membership", True, True)
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
    first, second = await asyncio.wait_for(
        asyncio.gather(
            client.post(url, json=body, headers=token()),
            client.post(url, json=body, headers=token()),
        ),
        timeout=15,
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


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_update_and_retry_postgres(postgres_access):
    client, sessions, workspace_id, _, token = postgres_access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    created = await client.post(url, json=request(), headers=token())
    assert created.status_code == 200, created.text
    replacement = request(expected_revision=1, permissions=["workspace:spend"])

    def refuse_grant_event(mapper, connection, instance):
        if instance.event_type == "workspace_access":
            raise RuntimeError("test event store unavailable")

    event.listen(Event, "before_insert", refuse_grant_event)
    try:
        with pytest.raises(RuntimeError, match="test event store unavailable"):
            await client.post(url, json=replacement, headers=token())
    finally:
        event.remove(Event, "before_insert", refuse_grant_event)
    async with sessions() as session:
        target = await session.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == workspace_id,
            WorkspaceGrantRecord.principal == "approver",
        ))
        assert target.permissions == "workspace:read"
        assert target.revision == 1
        assert len((await session.scalars(select(WorkspaceGrantChange))).all()) == 1
        assert len((await session.scalars(select(Event).where(Event.event_type == "workspace_access"))).all()) == 1
    updated = await client.post(url, json=replacement, headers=token())
    assert updated.status_code == 200, updated.text
    assert updated.json()["revision"] == 2
    async with sessions() as session:
        changes = (await session.scalars(select(WorkspaceGrantChange).order_by(WorkspaceGrantChange.revision))).all()
        assert len(changes) == 2
        audit = await session.get(Event, changes[-1].event_id)
        details = json.loads(audit.details_json)
        assert audit.org_id == (await session.get(Workspace, workspace_id)).org_id
        assert audit.principal == "owner"
        assert audit.created_at is not None
        assert audit.resource_id == uuid.UUID(updated.json()["grant_id"])
        assert audit.request_path == f"/workspaces/{workspace_id}/access/v1/grants"
        assert details["before"] == ["workspace:read"]
        assert details["after"] == ["workspace:read", "workspace:spend"]
        assert details["target"] == "approver"
        assert details["target_type"] == details["actor_type"] == "human"
        assert details["reason"] == "approver_setup"
        assert details["request_id"] == replacement["request_id"]
        event_id = str(audit.id)
    stream = await client.get(f"/events/workspaces/{workspace_id}", headers=token("approver"))
    assert stream.status_code == 200, stream.text
    assert event_id in [row["id"] for row in stream.json()["events"]]


@pytest.mark.asyncio
async def test_cross_tenant_and_removed_target_membership_postgres(postgres_access):
    client, sessions, workspace_id, members, token = postgres_access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    assert (await client.post(url, json=request(target_subject="other-org-person"), headers=token())).status_code == 403
    members.active.remove("approver")
    assert (await client.post(url, json=request(), headers=token())).status_code == 403
    async with sessions() as session:
        assert (await session.scalars(select(WorkspaceGrantChange))).all() == []
    members.active.add("approver")
    granted = await client.post(url, json=request(), headers=token())
    assert granted.status_code == 200, granted.text
    assert granted.json()["subject"] == "approver"


@pytest.mark.asyncio
async def test_current_viewer_service_target_and_sole_admin_self_change_denied_postgres(postgres_access):
    client, sessions, workspace_id, members, token = postgres_access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    members.active.update({"viewer", "service-shadow"})
    async with sessions() as session:
        org_id = (await session.get(Workspace, workspace_id)).org_id
        session.add(WorkspaceGrantRecord(
            workspace_id=workspace_id, org_id=org_id,
            principal="viewer", principal_type="human", permissions="workspace:read",
        ))
        session.add(WorkspaceGrantRecord(
            workspace_id=workspace_id, org_id=org_id,
            principal="service-shadow", principal_type="service", permissions="workspace:read",
        ))
        await session.commit()
    assert (await client.post(url, json=request(target_subject="viewer"), headers=token("viewer"))).status_code == 403
    assert (await client.post(url, json=request(principal_type="service"), headers=token())).status_code == 422
    assert (await client.post(url, json=request(target_subject="service-shadow"), headers=token())).status_code == 409
    assert (await client.post(url, json=request(permissions=["cluster:administer"]), headers=token())).status_code == 422
    assert (await client.post(url, json=request(target_subject="owner", expected_revision=1), headers=token())).status_code == 403
    async with sessions() as session:
        assert (await session.scalars(select(WorkspaceGrantChange))).all() == []


async def test_assignment_listing_is_scoped_paginated_and_sanitized_postgres(postgres_access):
    client, sessions, workspace_id, members, token = postgres_access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    body = request()
    assigned = await client.post(url, json=body, headers=token())
    assert assigned.status_code == 200, assigned.text
    foreign_org_id, foreign_workspace_id, sibling_workspace_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with sessions() as session:
        org_id = (await session.get(Workspace, workspace_id)).org_id
        session.add(Organization(id=foreign_org_id, name=f"foreign-{foreign_org_id}", adp_org_id="foreign-adp-org"))
        await session.flush()
        session.add_all([
            Workspace(id=foreign_workspace_id, org_id=foreign_org_id, name="foreign", isolation_mode="research"),
            Workspace(id=sibling_workspace_id, org_id=org_id, name="sibling", isolation_mode="research"),
        ])
        await session.flush()
        session.add_all([
            WorkspaceGrantRecord(workspace_id=workspace_id, org_id=org_id, principal="revoked-human",
                                 principal_type="human", permissions="workspace:spend", revoked_at=datetime.now(UTC)),
            WorkspaceGrantRecord(workspace_id=workspace_id, org_id=org_id, principal="service-record",
                                 principal_type="service", permissions="workspace:read unknown-permission"),
            WorkspaceGrantRecord(workspace_id=foreign_workspace_id, org_id=foreign_org_id, principal="foreign-human",
                                 principal_type="human", permissions="workspace:read"),
            WorkspaceGrantRecord(workspace_id=sibling_workspace_id, org_id=org_id, principal="sibling-human",
                                 principal_type="human", permissions="workspace:read"),
            WorkspaceGrantRecord(workspace_id=workspace_id, org_id=foreign_org_id, principal="wrong-org-row",
                                 principal_type="human", permissions="workspace:read"),
        ])
        audit = await session.scalar(select(Event).where(Event.event_type == "workspace_access"))
        audit.details_json = json.dumps({**json.loads(audit.details_json), "private_note": "synthetic-private-audit-value"})
        owner = await session.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == workspace_id, WorkspaceGrantRecord.principal == "owner",
        ))
        foreign_audit = Event(
            org_id=foreign_org_id, principal="foreign-actor", outcome="allowed", action="assigned",
            resource_type="workspace_grant", resource_id=owner.id, event_type="workspace_access",
        )
        session.add(foreign_audit)
        await session.flush()
        session.add(WorkspaceGrantChange(
            workspace_id=workspace_id, request_id=uuid.uuid4(), grant_id=owner.id,
            event_id=foreign_audit.id, fingerprint="synthetic-foreign-evidence", revision=owner.revision,
        ))
        await session.commit()
    members.active.remove("approver")
    response = await client.get(url, headers=token())
    assert response.status_code == 200, response.text
    assert response.json()["workspace_id"] == str(workspace_id)
    rows = response.json()["assignments"]
    by_subject = {row["subject"]: row for row in rows}
    assert set(by_subject) == {"owner", "approver", "revoked-human", "service-record"}
    assert by_subject["approver"] == {
        "workspace_id": str(workspace_id), "grant_id": assigned.json()["grant_id"], "revision": 1,
        "principal_type": "human", "subject": "approver", "assigned_permissions": ["workspace:read"],
        "revoked_at": None, "source": "explicit_assignment", "changed_by": "owner",
        "reason": "approver_setup", "request_id": body["request_id"],
    }
    assert by_subject["owner"]["assigned_permissions"] == ["workspace:administer"]
    assert by_subject["owner"]["source"] == "preexisting_grant"
    assert by_subject["owner"]["changed_by"] is None
    assert by_subject["revoked-human"]["revoked_at"] is not None
    assert by_subject["service-record"]["principal_type"] == "service"
    assert by_subject["service-record"]["assigned_permissions"] == ["workspace:read"]
    assert all("effective_permissions" not in row for row in rows)
    assert "synthetic-private-audit-value" not in response.text
    assert response.json()["next_after"] is None
    collected, after = [], None
    for _ in rows:
        params = {"limit": 1, **({"after": after} if after else {})}
        page = await client.get(url, params=params, headers=token())
        assert page.status_code == 200, page.text
        collected.extend(page.json()["assignments"])
        after = page.json()["next_after"]
    assert collected == rows
    assert after is None
    exhausted = await client.get(url, params={"after": rows[-1]["grant_id"]}, headers=token())
    assert exhausted.status_code == 200, exhausted.text
    assert exhausted.json()["assignments"] == []
    assert exhausted.json()["next_after"] is None
    for inaccessible in (foreign_workspace_id, sibling_workspace_id):
        denied = await client.get(f"/workspaces/{inaccessible}/access/v1/grants", headers=token())
        assert denied.status_code == 403
        assert "assignments" not in denied.json()
    for params in ({"limit": 0}, {"limit": 101}, {"after": "invalid"}):
        assert (await client.get(url, params=params, headers=token())).status_code == 422


@pytest.mark.parametrize("denial", ["viewer", "unknown_permission", "revoked", "removed_member", "service_substitution"])
async def test_assignment_listing_denies_ineligible_administrators_postgres(postgres_access, denial):
    client, sessions, workspace_id, members, token = postgres_access
    if denial == "removed_member":
        members.active.remove("owner")
    else:
        async with sessions() as session:
            owner = await session.scalar(select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.workspace_id == workspace_id, WorkspaceGrantRecord.principal == "owner",
            ))
            if denial == "viewer":
                owner.permissions = "workspace:read"
            elif denial == "unknown_permission":
                owner.permissions = "unknown-permission"
            elif denial == "revoked":
                owner.revoked_at = datetime.now(UTC)
            else:
                owner.principal_type = "service"
            await session.commit()
    response = await client.get(f"/workspaces/{workspace_id}/access/v1/grants", headers=token())
    assert response.status_code == 403, response.text
    assert "assignments" not in response.json()


async def test_assignment_listing_rechecks_admin_after_middleware_postgres(postgres_access, monkeypatch):
    from app.services import workspace_access

    client, sessions, workspace_id, _, token = postgres_access
    identity_read, continue_read = asyncio.Event(), asyncio.Event()
    original = workspace_access.require_current_identity

    async def delayed_identity(*args, **kwargs):
        identity = await original(*args, **kwargs)
        identity_read.set()
        await continue_read.wait()
        return identity

    monkeypatch.setattr(workspace_access, "require_current_identity", delayed_identity)
    pending = asyncio.create_task(client.get(f"/workspaces/{workspace_id}/access/v1/grants", headers=token()))
    try:
        await asyncio.wait_for(identity_read.wait(), timeout=5)
        async with sessions() as session:
            owner = await session.scalar(select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.workspace_id == workspace_id, WorkspaceGrantRecord.principal == "owner",
            ))
            owner.revoked_at = datetime.now(UTC)
            await session.commit()
    finally:
        continue_read.set()
    response = await asyncio.wait_for(pending, timeout=10)
    assert response.status_code == 403, response.text
    assert "assignments" not in response.json()
