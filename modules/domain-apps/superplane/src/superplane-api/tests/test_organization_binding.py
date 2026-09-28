import uuid

import pytest
from app.auth import VerifiedCaller
from app.models.organization import Organization
from app.organization_binding import bind_caller
from fastapi import HTTPException
from superplane_auth.policy import DomainPrincipal

from tests.conftest import async_session_test


def caller(org):
    return VerifiedCaller(
        DomainPrincipal("human-subject", org, "adp-client", "human"), {}
    )


async def test_explicit_slug_binding_is_isolated_and_retains_source():
    one, two = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add_all(
            [
                Organization(id=one, name="one", adp_org_id="aws-e"),
                Organization(id=two, name="two", adp_org_id="another-org"),
            ]
        )
        await db.commit()
        bound = await bind_caller(db, caller("aws-e"))
        assert bound.principal.org_id == str(one)
        assert bound.source_org_id == "aws-e"
        assert (await bind_caller(db, caller("another-org"))).principal.org_id == str(
            two
        )
        with pytest.raises(HTTPException) as denied:
            await bind_caller(db, caller("unknown-org"))
        assert denied.value.status_code == 403
        with pytest.raises(HTTPException):
            await bind_caller(db, caller(str(one)))


async def test_unbound_legacy_uuid_behavior_survives():
    identifier = uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=identifier, name="legacy"))
        await db.commit()
        original = caller(str(identifier))
        assert await bind_caller(db, original) == original


async def test_bound_adp_request_uses_server_grant_and_rejects_uuid_bypass(client, monkeypatch):
    from app import auth
    from app.main import app
    from app.models.workspace import Workspace
    from app.models.workspace_grant import WorkspaceGrantRecord
    from app.current_identity import CurrentIdentity
    from superplane_auth.policy import DomainTokenPolicy

    org, workspace = uuid.uuid4(), uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=org, name="fresh", adp_org_id="aws-e"))
        await db.flush()
        db.add(Workspace(id=workspace, org_id=org, name="fresh", isolation_mode="dedicated", status="Ready"))
        await db.flush()
        db.add(WorkspaceGrantRecord(workspace_id=workspace, org_id=org, principal="admin-subject", principal_type="human", permissions="workspace:administer"))
        await db.commit()
    claims = {"sub": "admin-subject", "custom:org_id": "aws-e", "custom:account_type": "human", "token_use": "access", "iss": "https://issuer.example", "client_id": "adp-client"}
    monkeypatch.setattr(auth, "verify_access_token", lambda token: claims)
    monkeypatch.setattr(app.state, "domain_policy", DomainTokenPolicy(allowed_client_ids=["adp-client"], expected_issuer="https://issuer.example"))
    headers = {"Authorization": "Bearer signed-token", "X-Org-Id": "spoofed"}
    from app.config import settings

    monkeypatch.delattr(app.state, "current_identity_reader", raising=False)
    monkeypatch.setattr(settings, "current_identity_enforced", False)
    # Upgrade compatibility: the existing signed-token/live-grant path still works.
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 200
    monkeypatch.setattr(settings, "current_identity_enforced", True)
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 403

    class Memberships:
        enabled = True

        async def read(self, *, subject, principal_type, adp_org_id):
            return CurrentIdentity(subject, principal_type, adp_org_id, "membership-1", True, self.enabled)
    reader = Memberships()
    monkeypatch.setattr(app.state, "current_identity_reader", reader, raising=False)
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 200
    reader.enabled = False
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 403
    reader.enabled = True
    assert (await client.get(f"/workspaces/{uuid.uuid4()}", headers=headers)).status_code == 403
    claims["custom:org_id"] = str(org)
    assert (await client.get(f"/workspaces/{workspace}", headers=headers)).status_code == 403
