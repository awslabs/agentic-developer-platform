from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from src.agentauth import installation_ui
from src.agentauth.installation_reader import InstallationReader
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def caller():
    return TokenContext(
        user_id="login-a",
        org_id="tenant-a",
        team_id="team",
        department_id="dept",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def diagnostics(monkeypatch):
    reader = AsyncMock(return_value={"installation_id": 101, "status": "partial", "coverage": "runtime_receipt_only"})
    monkeypatch.setattr(installation_ui, "read_installation_status", reader)
    return reader


@pytest.fixture
async def client(db_session, caller, monkeypatch):
    monkeypatch.setenv("FEATURE_ASSISTANT_UI_ENABLED", "true")
    monkeypatch.setenv("FEATURE_CHAT_ENABLED", "true")
    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "true")
    db_session.add_all(
        [
            Organization(id="tenant-a", name="Tenant A"),
            Organization(id="tenant-b", name="Tenant B"),
            User(id="installer-a", cognito_sub="login-a", org_id="tenant-a", team_id="team", email="installer@example.test"),
            User(id="member-a", cognito_sub="member-login", org_id="tenant-a", team_id="team", email="member@example.test"),
            User(id="installer-b", org_id="tenant-b", team_id="team", email="other@example.test"),
            ChannelTenantMap(
                provider="github",
                org_id="tenant-a",
                provider_scope_id="account-a",
                installation_id="101",
                installed_by_user_id="installer-a",
            ),
            ChannelTenantMap(
                provider="github",
                org_id="tenant-b",
                provider_scope_id="account-b",
                installation_id="202",
                installed_by_user_id="installer-b",
            ),
        ]
    )
    await db_session.commit()
    app = FastAPI()
    app.include_router(installation_ui.router)
    app.dependency_overrides[installation_ui.get_current_user] = lambda: caller
    app.dependency_overrides[installation_ui.get_db] = lambda: db_session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as connection:
        yield connection


async def test_canonical_installer_gets_scoped_status_reference(client, diagnostics):
    response = await client.get("/assistant/installations/101/status")
    assert response.status_code == 200
    assert response.json() == {"installation_id": "101", "status": "partial", "coverage": "runtime_receipt_only"}
    assert response.headers["cache-control"] == "no-store"
    assert diagnostics.call_args.args[1] == InstallationReader(tenant_id="tenant-a", installation_id=101)


@pytest.mark.parametrize(
    "subject,target,admin",
    [("member-login", 101, False), ("member-login", 101, True), ("login-a", 202, True), ("login-a", 303, False)],
)
async def test_denial_precedes_diagnostics_and_ignores_supplied_identity(client, caller, diagnostics, subject, target, admin):
    caller.user_id = subject
    caller.is_admin = admin
    response = await client.get(f"/assistant/installations/{target}/status?user_id=installer-a&tenant_id=tenant-b")
    assert response.status_code == 404
    assert response.json() == {"detail": "Installation unavailable"}
    assert response.headers["cache-control"] == "no-store"
    diagnostics.assert_not_awaited()


async def test_revoked_installer_cannot_open_context(client, db_session, diagnostics):
    db_session.add(TenantMembership(user_id="installer-a", tenant_id="tenant-a", revoked_at=datetime.now(UTC)))
    await db_session.commit()
    assert (await client.get("/assistant/installations/101/status")).status_code == 404
    diagnostics.assert_not_awaited()


@pytest.mark.parametrize("flag", ["FEATURE_ASSISTANT_UI_ENABLED", "FEATURE_CHAT_ENABLED", "ADP_CHAT_DATA_ENABLED"])
async def test_disabled_capability_never_reads_diagnostics(client, diagnostics, monkeypatch, flag):
    monkeypatch.setenv(flag, "false")
    response = await client.get("/assistant/installations/101/status")
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    diagnostics.assert_not_awaited()


async def test_missing_rollout_flag_fails_closed(client, diagnostics, monkeypatch):
    monkeypatch.delenv("FEATURE_ASSISTANT_UI_ENABLED")
    assert (await client.get("/assistant/installations/101/status")).status_code == 503
    diagnostics.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("account_type", "service"), ("auth_source", "iam")])
async def test_non_browser_identity_is_denied(client, caller, diagnostics, field, value):
    setattr(caller, field, value)
    assert (await client.get("/assistant/installations/101/status")).status_code == 404
    diagnostics.assert_not_awaited()


@pytest.mark.parametrize("target", ["0", "-1", "100000000000000000000", "not-an-id"])
async def test_invalid_reference_is_rejected(client, diagnostics, target):
    assert (await client.get(f"/assistant/installations/{target}/status")).status_code == 422
    diagnostics.assert_not_awaited()


async def test_missing_runtime_evidence_is_not_reported_as_healthy(client):
    response = await client.get("/assistant/installations/101/status")
    assert response.status_code == 200
    assert response.json()["status"] == "unavailable"
    assert response.json()["reason"] == "diagnostic_record_unavailable"
