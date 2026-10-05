"""Mounted HTTP checks for explicit human workspace grant contract."""

import uuid

import pytest

from app.config import settings
from app.current_identity import CurrentIdentity
from app.main import app
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from tests.conftest import async_session_test
from tests.test_auth import _mint, enforcing as enforcing, rsa_keys as rsa_keys


class Memberships:
    def __init__(self, allowed):
        self.allowed = allowed

    async def read(self, *, subject, principal_type, adp_org_id):
        if subject == "other-org-person":
            return CurrentIdentity(subject, "human", "another-adp-org", "other-membership", True, True)
        if subject not in self.allowed or principal_type != "human" or adp_org_id != "adp-fixture":
            return None
        return CurrentIdentity(subject, "human", adp_org_id, f"membership-{subject}", True, True)


@pytest.fixture
async def access(enforcing, client, monkeypatch):
    monkeypatch.setattr(settings, "current_identity_enforced", True)
    membership = Memberships({"owner", "approver"})
    monkeypatch.setattr(app.state, "current_identity_reader", membership, raising=False)
    org_id = uuid.uuid4()
    workspace_id = uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=org_id, name=f"org-{org_id}", adp_org_id="adp-fixture"))
        await db.flush()
        db.add(Workspace(id=workspace_id, org_id=org_id, name="approval-demo", isolation_mode="research"))
        db.add(WorkspaceGrantRecord(
            workspace_id=workspace_id, org_id=org_id, principal="owner",
            principal_type="human", permissions="workspace:administer",
        ))
        await db.commit()

    def token(subject="owner", **claims):
        return {"Authorization": f"Bearer {_mint(enforcing, sub=subject, **{'custom:org_id': 'adp-fixture'}, **claims)}"}

    return client, workspace_id, membership, token


@pytest.mark.asyncio
async def test_mounted_grant_and_effective_access(access):
    client, workspace_id, _, token = access
    request = {
        "target_subject": "approver", "principal_type": "human",
        "permissions": ["workspace:read"], "reason": "approver_setup",
        "expected_revision": 0, "request_id": str(uuid.uuid4()),
    }
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    created = await client.post(url, json=request, headers=token())
    assert created.status_code == 200, created.text
    result = created.json()
    assert result["subject"] == "approver"
    assert result["grant_id"]
    assert result["revision"] == 1
    assert result["effective_permissions"] == ["workspace:read"]
    assert result["granted_by"] == "owner"
    assert result["request_id"] == request["request_id"]
    response = await client.get(f"/workspaces/{workspace_id}/access/v1/me", headers=token("approver"))
    assert response.status_code == 200, response.text
    assert response.json() == result
    duplicate = await client.post(url, json=request, headers=token())
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json() == result


def _grant_request(**overrides):
    body = {
        "target_subject": "approver", "principal_type": "human",
        "permissions": ["workspace:read"], "reason": "approver_setup",
        "expected_revision": 0, "request_id": str(uuid.uuid4()),
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_viewer_cannot_assign_or_self_escalate(access):
    client, workspace_id, membership, token = access
    membership.allowed.add("viewer")
    async with async_session_test() as db:
        db.add(WorkspaceGrantRecord(
            workspace_id=workspace_id, org_id=(await db.get(Workspace, workspace_id)).org_id,
            principal="viewer", principal_type="human", permissions="workspace:read",
        ))
        await db.commit()
    response = await client.post(
        f"/workspaces/{workspace_id}/access/v1/grants",
        json=_grant_request(target_subject="viewer", permissions=["workspace:administer"]),
        headers=token("viewer"),
    )
    assert response.status_code == 403
    async with async_session_test() as db:
        row = await db.get(Workspace, workspace_id)
        from sqlalchemy import select

        grant = await db.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == row.id,
            WorkspaceGrantRecord.principal == "viewer",
        ))
        assert grant.permissions == "workspace:read"


@pytest.mark.asyncio
async def test_cross_organization_and_removed_target_refused(access):
    client, workspace_id, membership, token = access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    assert (await client.post(url, json=_grant_request(target_subject="other-org-person"), headers=token())).status_code == 403
    membership.allowed.remove("approver")
    assert (await client.post(url, json=_grant_request(), headers=token())).status_code == 403
    async with async_session_test() as db:
        from sqlalchemy import select

        assert await db.scalar(select(WorkspaceGrantRecord).where(
            WorkspaceGrantRecord.workspace_id == workspace_id,
            WorkspaceGrantRecord.principal == "approver",
        )) is None


@pytest.mark.asyncio
async def test_removed_administrator_and_missing_reader_refused(access, monkeypatch):
    client, workspace_id, membership, token = access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    membership.allowed.remove("owner")
    assert (await client.post(url, json=_grant_request(), headers=token())).status_code == 403
    membership.allowed.add("owner")
    monkeypatch.delattr(app.state, "current_identity_reader")
    assert (await client.post(url, json=_grant_request(), headers=token())).status_code == 403


@pytest.mark.asyncio
async def test_service_target_and_unsupported_permissions_refused(access):
    client, workspace_id, _, token = access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    assert (await client.post(url, json=_grant_request(principal_type="service"), headers=token())).status_code == 422
    assert (await client.post(url, json=_grant_request(permissions=["cluster:administer"]), headers=token())).status_code == 422
    assert (await client.post(url, json=_grant_request(permissions=["workspace:read", "workspace:read"]), headers=token())).status_code == 422


@pytest.mark.asyncio
async def test_service_caller_and_existing_service_subject_cannot_substitute(access, monkeypatch):
    client, workspace_id, _, token = access
    url = f"/workspaces/{workspace_id}/access/v1/grants"
    async with async_session_test() as db:
        org_id = (await db.get(Workspace, workspace_id)).org_id
        db.add(WorkspaceGrantRecord(
            workspace_id=workspace_id, org_id=org_id, principal="approver",
            principal_type="service", permissions="workspace:read",
        ))
        await db.commit()
    assert (await client.post(url, json=_grant_request(), headers=token())).status_code == 409
    assert (await client.post(
        url, json=_grant_request(), headers=token("owner", **{"custom:account_type": "service"}),
    )).status_code == 403
    async with async_session_test() as db:
        org_id = (await db.get(Workspace, workspace_id)).org_id
        db.add(WorkspaceGrantRecord(
            workspace_id=workspace_id, org_id=org_id, principal="machine",
            principal_type="service", permissions="workspace:administer",
        ))
        await db.commit()
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    assert (await client.post(
        url, json=_grant_request(), headers=token("machine", **{"custom:account_type": "service"}),
    )).status_code == 403


@pytest.mark.asyncio
async def test_read_requires_current_member_even_without_guard_setting(access, monkeypatch):
    client, workspace_id, membership, token = access
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    membership.allowed.remove("owner")
    response = await client.get(f"/workspaces/{workspace_id}/access/v1/me", headers=token())
    assert response.status_code == 403
