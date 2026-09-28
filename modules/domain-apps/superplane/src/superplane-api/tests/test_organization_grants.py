"""Organization bootstrap authority works without borrowing workspace grants."""

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from superplane_auth.policy import DomainPrincipal, DomainTokenPolicy, Permission

from app import auth
from app.current_identity import CurrentIdentity
from app.main import app
from app.models.organization import Organization
from app.models.organization_grant import (
    ORGANIZATION_ADMINISTER,
    ORGANIZATION_READ,
    OrganizationGrantRecord,
)
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from tests.conftest import async_session_test


async def seed(monkeypatch, *, permission=ORGANIZATION_ADMINISTER, grant=True):
    from app.config import settings

    monkeypatch.setattr(settings, "current_identity_enforced", True)
    class Memberships:
        async def read(self, *, subject, principal_type, adp_org_id):
            return CurrentIdentity(subject, principal_type, adp_org_id, "membership-1", True, True)

    monkeypatch.setattr(app.state, "current_identity_reader", Memberships(), raising=False)
    org = uuid.uuid4()
    async with async_session_test() as db:
        db.add(Organization(id=org, name="empty", adp_org_id="selected-adp-org"))
        await db.flush()
        if grant:
            db.add(
                OrganizationGrantRecord(
                    org_id=org,
                    principal="verified-human",
                    principal_type="human",
                    permissions=permission,
                    granted_by="verified-human",
                )
            )
        await db.commit()
    claims = {
        "sub": "verified-human",
        "custom:org_id": "selected-adp-org",
        "custom:account_type": "human",
        "token_use": "access",
        "iss": "https://issuer.example",
        "client_id": "adp-client",
    }
    monkeypatch.setattr(auth, "verify_access_token", lambda token: dict(claims))
    monkeypatch.setattr(
        app.state,
        "domain_policy",
        DomainTokenPolicy(
            allowed_client_ids=["adp-client"],
            expected_issuer="https://issuer.example",
        ),
    )
    return org, claims, {"Authorization": "Bearer verified-access-token"}


async def test_empty_organization_can_be_listed_by_its_administrator(
    client, monkeypatch
):
    _org, _claims, headers = await seed(monkeypatch)
    response = await client.get("/workspaces", headers=headers)
    assert response.status_code == 200
    assert response.json() == {"workspaces": [], "total": 0}
    assert (await client.get("/workspaces")).status_code == 401


async def test_org_admin_grant_does_not_grant_workspace_access(client, monkeypatch):
    org, _claims, headers = await seed(monkeypatch)
    workspace = uuid.uuid4()
    async with async_session_test() as db:
        db.add(
            Workspace(
                id=workspace,
                org_id=org,
                name="private",
                isolation_mode="dedicated",
                status="Ready",
            )
        )
        await db.commit()
    assert (await client.get("/workspaces", headers=headers)).status_code == 200
    assert (
        await client.get(f"/workspaces/{workspace}", headers=headers)
    ).status_code == 403


@pytest.mark.parametrize("change", ["revoked", "principal", "org", "account_type"])
async def test_current_authority_is_required_each_request(client, monkeypatch, change):
    org, claims, headers = await seed(monkeypatch)
    assert (await client.get("/workspaces", headers=headers)).status_code == 200
    if change == "revoked":
        from sqlalchemy import update

        async with async_session_test() as db:
            await db.execute(
                update(OrganizationGrantRecord)
                .where(
                    OrganizationGrantRecord.org_id == org,
                )
                .values(revoked_at=datetime.now(timezone.utc))
            )
            await db.commit()
    else:
        claims[
            {
                "principal": "sub",
                "org": "custom:org_id",
                "account_type": "custom:account_type",
            }[change]
        ] = {
            "principal": "someone-else",
            "org": "another-org",
            "account_type": "service",
        }[change]
    response = await client.get(
        "/workspaces",
        headers={**headers, "X-Org-Id": str(org), "X-User-Role": "org_admin"},
    )
    assert response.status_code in {401, 403}


async def test_workspace_grants_cannot_replace_org_authority(client, monkeypatch):
    org, _claims, headers = await seed(monkeypatch, grant=False)
    workspace = uuid.uuid4()
    async with async_session_test() as db:
        db.add(
            Workspace(
                id=workspace,
                org_id=org,
                name="only",
                isolation_mode="dedicated",
                status="Ready",
            )
        )
        await db.flush()
        db.add(
            WorkspaceGrantRecord(
                workspace_id=workspace,
                org_id=org,
                principal="verified-human",
                principal_type="human",
                permissions=Permission.ADMINISTER.value,
            )
        )
        await db.commit()
    assert (
        await client.get(f"/workspaces/{workspace}", headers=headers)
    ).status_code == 200
    assert (await client.get("/workspaces", headers=headers)).status_code == 403


async def test_org_read_grant_cannot_authorize_provisioning(client, monkeypatch):
    org, _claims, headers = await seed(monkeypatch, permission=ORGANIZATION_READ)
    assert (await client.get("/workspaces", headers=headers)).status_code == 200
    caller = auth.VerifiedCaller(
        DomainPrincipal("verified-human", str(org), "adp-client", "human"), {}
    )
    async with async_session_test() as db:
        with pytest.raises(HTTPException) as refused:
            await auth.authorize_organization_operation(
                db, caller, Permission.PROVISION
            )
        assert refused.value.status_code == 403
