"""Registered producer reads current, selected human membership, not token roles."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.internal import domain_current_identity, domain_operation_routes
from src.internal.auth_deps import verify_internal_or_irsa
from src.shared.database import get_db
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import UserIdentity


@pytest.fixture
async def identity_client(db_session, monkeypatch):
    db_session.add_all([Organization(id="O1", name="First"), Organization(id="O2", name="Second")])
    await db_session.flush()
    first = User(id="member-one", org_id="O1", team_id="", email="same@example.test", cognito_sub="immutable-sub")
    second = User(id="member-two", org_id="O2", team_id="", email="same@example.test")
    db_session.add_all([first, second])
    await db_session.flush()
    first_member = TenantMembership(user_id=first.id, tenant_id="O1", is_active=True)
    second_member = TenantMembership(user_id=second.id, tenant_id="O2", is_active=False)
    db_session.add_all(
        [
            first_member,
            second_member,
            UserIdentity(
                user_id=second.id,
                org_id="O2",
                team_id="",
                provider="cognito",
                provider_user_id="immutable-sub",
                verification_method="org_placement",
            ),
        ]
    )
    await db_session.commit()

    state = {"selected": "O1", "enabled": True, "registry": "registered"}
    cognito = MagicMock()
    cognito.list_users.return_value = {"Users": [{"Username": "login"}]}
    cognito.admin_get_user.side_effect = lambda **kwargs: {
        "Enabled": state["enabled"],
        "UserAttributes": [
            {"Name": "sub", "Value": "immutable-sub"},
            {"Name": "custom:org_id", "Value": state["selected"]},
        ],
    }
    monkeypatch.setattr(domain_current_identity, "cognito_user_pool_id", lambda: "pool-test")
    monkeypatch.setattr(domain_current_identity, "aws_client", lambda service: cognito)
    monkeypatch.setattr(
        domain_operation_routes,
        "binding_for",
        lambda domain, org_id: SimpleNamespace(adp_org_id=org_id, producer_registry_id="registered"),
    )
    monkeypatch.setattr(domain_operation_routes, "current_registry", lambda request, scope: state["registry"])

    app = FastAPI()
    app.include_router(domain_operation_routes.router)
    app.dependency_overrides[verify_internal_or_irsa] = lambda: None

    async def database():
        yield db_session

    app.dependency_overrides[get_db] = database
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client, state, first_member, second_member, cognito


def payload(org_id="O1", *, subject="immutable-sub", principal_type="human"):
    return {"domain": "superplane", "org_id": org_id, "subject": subject, "principal_type": principal_type}


async def test_selected_organization_only_and_switch_does_not_revoke_membership(identity_client):
    client, state, first, second, cognito = identity_client
    endpoint = "/internal/v1/controller-execution/current-identity"
    response = await client.post(endpoint, json=payload())
    assert response.status_code == 200
    assert response.json() == {
        "version": 1,
        "subject": "immutable-sub",
        "principal_type": "human",
        "adp_org_id": "O1",
        "membership_id": first.id,
        "active": True,
        "enabled": True,
    }
    assert (await client.post(endpoint, json=payload("O2"))).status_code == 403
    state["selected"] = "O2"
    result = await client.post(endpoint, json=payload("O2"))
    assert result.status_code == 200 and result.json()["membership_id"] == second.id
    assert not second.is_active
    assert (await client.post(endpoint, json=payload())).status_code == 403
    assert cognito.admin_get_user.call_count == 4


@pytest.mark.parametrize("change", ["revoked", "disabled", "no-membership", "service", "substituted", "registry", "absent-pool", "unavailable"])
async def test_no_unproven_authority(identity_client, monkeypatch, change):
    client, state, first, _, cognito = identity_client
    if change == "revoked":
        first.revoked_at = datetime.now(UTC)
    elif change == "disabled":
        state["enabled"] = False
    elif change == "no-membership":
        first.tenant_id = "O2"
    elif change == "registry":
        state["registry"] = "different"
    elif change == "absent-pool":
        monkeypatch.setattr(domain_current_identity, "cognito_user_pool_id", lambda: "")
    elif change == "unavailable":
        cognito.admin_get_user.side_effect = RuntimeError("unavailable")
    request = payload(
        subject="different-sub" if change == "substituted" else "immutable-sub",
        principal_type="service" if change == "service" else "human",
    )
    response = await client.post("/internal/v1/controller-execution/current-identity", json=request)
    assert response.status_code == (503 if change in {"absent-pool", "unavailable"} else 403)
    if change in {"service", "registry"}:
        cognito.admin_get_user.assert_not_called()
