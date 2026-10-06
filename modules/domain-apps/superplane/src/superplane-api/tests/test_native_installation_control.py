"""Native installation checks current organization authority without workspace grants."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from superplane_auth.policy import Permission

from app import auth
from app.current_identity import CurrentIdentity
from app.endpoint_inventory import PRIVATE_DOMAIN_ROUTES, Scope
from app.routers import installation


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "race", [None, "grant-revoked", "membership-replaced", "disabled"]
)
async def test_bootstrap_control_rechecks_current_human_authority(monkeypatch, race):
    org = uuid4()
    caller = SimpleNamespace(
        principal=SimpleNamespace(
            org_id=str(org), account_type="human", subject="person"
        ),
        source_org_id="adp-org",
        identity_evidence="membership",
    )
    reader = SimpleNamespace(
        read=AsyncMock(
            return_value=CurrentIdentity(
                subject="person",
                principal_type="human",
                adp_org_id="adp-org",
                membership_id="replacement"
                if race == "membership-replaced"
                else "membership",
                active=True,
                enabled=race != "disabled",
            )
        )
    )
    request = SimpleNamespace(
        state=SimpleNamespace(caller=caller),
        app=SimpleNamespace(state=SimpleNamespace(current_identity_reader=reader)),
    )
    db = SimpleNamespace(rollback=AsyncMock())
    authorization = AsyncMock(
        side_effect=[None, HTTPException(403, "revoked")]
        if race == "grant-revoked"
        else None
    )
    monkeypatch.setattr(auth, "authorize_organization_operation", authorization)
    monkeypatch.setattr(installation, "management_only", lambda: True)
    from app import operation_activation

    monkeypatch.setattr(operation_activation, "dispatch_enabled", lambda: False)
    monkeypatch.setattr(
        installation,
        "capabilities_async",
        AsyncMock(return_value={str(i): True for i in range(4)}),
    )
    if race:
        with pytest.raises(HTTPException) as denied:
            await installation.installation_organization_bootstrap(
                request, org_id=org, db=db
            )
        assert denied.value.status_code == 403
    else:
        report = await installation.installation_organization_bootstrap(
            request, org_id=org, db=db
        )
        assert report["organization_authority_verified"] is True
        assert report["credential_metadata_verified"] is False
        assert report["raw_material_returned"] is False
        assert "workspace_id" not in report
        assert authorization.await_count == 2
        authorization.assert_awaited_with(db, caller, Permission.ADMINISTER)
    db.rollback.assert_awaited_once()
    reader.read.assert_awaited_once()


def test_bootstrap_control_is_private_human_organization_route():
    assert PRIVATE_DOMAIN_ROUTES[
        ("GET", "/internal/installation/organization-bootstrap")
    ] == (Scope.ORGANIZATION, Permission.ADMINISTER)
