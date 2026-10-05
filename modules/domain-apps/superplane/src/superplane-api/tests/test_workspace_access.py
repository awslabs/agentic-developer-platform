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
