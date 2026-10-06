"""Organization access reads through the API and disposable PostgreSQL."""

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, select

from app.models.organization import Organization
from app.models.organization_grant import ORGANIZATION_ADMINISTER, ORGANIZATION_READ, OrganizationGrantRecord
from app.models.organization_grant_change import OrganizationGrantChange
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from tests import test_workspace_access_postgres as workspace_fixtures

enforcing = workspace_fixtures.enforcing
installation_postgres_url = workspace_fixtures.installation_postgres_url
postgres_access = workspace_fixtures.postgres_access
pytestmark = workspace_fixtures.pytestmark
rsa_keys = workspace_fixtures.rsa_keys

PATH = "/orgs/current/access/v1"


@pytest.fixture
async def organization_access(postgres_access):
    client, sessions, workspace_id, members, token = postgres_access
    async with sessions() as session:
        connection = await session.connection()
        await connection.run_sync(lambda sync: OrganizationGrantRecord.__table__.create(sync))
        await connection.run_sync(lambda sync: OrganizationGrantChange.__table__.create(sync))
        org_id = (await session.get(Workspace, workspace_id)).org_id
        await session.execute(delete(WorkspaceGrantRecord).where(WorkspaceGrantRecord.workspace_id == workspace_id))
        session.add(OrganizationGrantRecord(
            org_id=org_id, principal="owner", principal_type="human",
            permissions=ORGANIZATION_ADMINISTER, granted_by="bootstrap-human",
        ))
        await session.commit()
    yield client, sessions, org_id, workspace_id, members, token


async def test_organization_self_access_never_supplies_workspace_authority(organization_access):
    client, sessions, org_id, workspace_id, _, token = organization_access
    response = await client.get(f"{PATH}/me", headers=token())
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["organization_id"] == str(org_id)
    assert result["subject"] == "owner" and result["principal_type"] == "human"
    assert result["assigned_permissions"] == [ORGANIZATION_ADMINISTER]
    assert result["effective_permissions"] == [ORGANIZATION_ADMINISTER, ORGANIZATION_READ]
    assert result["granted_by"] == "bootstrap-human" and result["granted_at"]
    assert result["source"] == "stored_organization_grant"
    assert result["revoked_at"] is None
    assert (await client.get(f"/workspaces/{workspace_id}", headers=token())).status_code == 403
    async with sessions() as session:
        await session.execute(delete(Workspace).where(Workspace.id == workspace_id))
        await session.commit()
    empty_org_access = await client.get(f"{PATH}/me", headers=token())
    assert empty_org_access.status_code == 200, empty_org_access.text
    assert empty_org_access.json() == result
    assert (await client.get(f"{PATH}/me")).status_code == 401


async def test_organization_assignments_are_tenant_scoped_typed_and_paginated(organization_access):
    client, sessions, org_id, _, members, token = organization_access
    foreign_id = uuid.uuid4()
    async with sessions() as session:
        session.add(Organization(id=foreign_id, name=f"foreign-{foreign_id}", adp_org_id="foreign-adp-org"))
        await session.flush()
        session.add_all([
            OrganizationGrantRecord(org_id=org_id, principal="approver", principal_type="human",
                                    permissions=ORGANIZATION_READ, granted_by="owner"),
            OrganizationGrantRecord(org_id=org_id, principal="revoked", principal_type="human",
                                    permissions=ORGANIZATION_READ, granted_by="owner", revoked_at=datetime.now(UTC)),
            OrganizationGrantRecord(org_id=org_id, principal="service", principal_type="service",
                                    permissions=f"{ORGANIZATION_READ} unknown-permission", granted_by="owner"),
            OrganizationGrantRecord(org_id=foreign_id, principal="foreign", principal_type="human",
                                    permissions=ORGANIZATION_ADMINISTER, granted_by="foreign-actor"),
        ])
        await session.commit()
    viewer = await client.get(f"{PATH}/me", headers=token("approver"))
    assert viewer.status_code == 200, viewer.text
    assert viewer.json()["effective_permissions"] == [ORGANIZATION_READ]
    assert (await client.get(f"{PATH}/grants", headers=token("approver"))).status_code == 403
    members.active.remove("approver")
    response = await client.get(f"{PATH}/grants", headers=token())
    assert response.status_code == 200, response.text
    rows = response.json()["assignments"]
    by_subject = {row["subject"]: row for row in rows}
    assert set(by_subject) == {"owner", "approver", "revoked", "service"}
    assert all(row["organization_id"] == str(org_id) and "effective_permissions" not in row for row in rows)
    assert by_subject["revoked"]["revoked_at"] is not None
    assert by_subject["service"]["principal_type"] == "service"
    assert by_subject["service"]["assigned_permissions"] == [ORGANIZATION_READ]
    collected, after = [], None
    for _ in rows:
        params = {"limit": 1, **({"after": after} if after else {})}
        page = await client.get(f"{PATH}/grants", params=params, headers=token())
        assert page.status_code == 200, page.text
        collected.extend(page.json()["assignments"])
        after = page.json()["next_after"]
    assert collected == rows and after is None
    exhausted = await client.get(f"{PATH}/grants", params={"after": rows[-1]["grant_id"]}, headers=token())
    assert exhausted.json()["assignments"] == [] and exhausted.json()["next_after"] is None
    for params in ({"limit": 0}, {"limit": 101}, {"after": "invalid"}):
        assert (await client.get(f"{PATH}/grants", params=params, headers=token())).status_code == 422


@pytest.mark.parametrize("denial", ["workspace_only", "revoked", "removed_member", "service_substitution", "unknown_permission", "cross_org"])
async def test_organization_access_denies_ineligible_callers(organization_access, denial):
    client, sessions, org_id, workspace_id, members, token = organization_access
    async with sessions() as session:
        grant = await session.scalar(select(OrganizationGrantRecord).where(OrganizationGrantRecord.org_id == org_id))
        if denial == "workspace_only":
            await session.delete(grant)
            session.add(WorkspaceGrantRecord(workspace_id=workspace_id, org_id=org_id, principal="owner",
                                            principal_type="human", permissions="workspace:administer"))
        elif denial == "revoked":
            grant.revoked_at = datetime.now(UTC)
        elif denial == "removed_member":
            members.active.remove("owner")
        elif denial == "service_substitution":
            grant.principal_type = "service"
        elif denial == "unknown_permission":
            grant.permissions = "unknown-permission"
        else:
            foreign_id = uuid.uuid4()
            session.add(Organization(id=foreign_id, name=f"foreign-{foreign_id}", adp_org_id="foreign-adp-org"))
            await session.flush()
            grant.org_id = foreign_id
        await session.commit()
    for endpoint in ("me", "grants"):
        response = await client.get(f"{PATH}/{endpoint}", headers=token())
        assert response.status_code == 403, response.text
        assert "grant_id" not in response.json() and "assignments" not in response.json()


@pytest.mark.parametrize("endpoint", ["me", "grants"])
async def test_organization_access_rechecks_revocation_after_middleware(organization_access, monkeypatch, endpoint):
    from app.services import organization_access as service

    client, sessions, org_id, _, _, token = organization_access
    identity_read, continue_read = asyncio.Event(), asyncio.Event()
    original = service.require_current_identity

    async def delayed_identity(*args, **kwargs):
        identity = await original(*args, **kwargs)
        identity_read.set()
        await continue_read.wait()
        return identity

    monkeypatch.setattr(service, "require_current_identity", delayed_identity)
    pending = asyncio.create_task(client.get(f"{PATH}/{endpoint}", headers=token()))
    try:
        await asyncio.wait_for(identity_read.wait(), timeout=5)
        async with sessions() as session:
            grant = await session.scalar(select(OrganizationGrantRecord).where(OrganizationGrantRecord.org_id == org_id))
            grant.revoked_at = datetime.now(UTC)
            await session.commit()
    finally:
        continue_read.set()
    response = await asyncio.wait_for(pending, timeout=10)
    assert response.status_code == 403, response.text
