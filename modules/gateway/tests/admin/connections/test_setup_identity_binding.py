"""Offline HTTP→nonce→provider-control regressions for GitHub setup authority."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

import src.admin.connections.service as svc
from src.admin.connections.github_client import GitHubAppClient
from src.admin.connections.routes import router
from src.auth.cognito_jwt import CognitoTokenClaims
from src.shared.database import get_db
from src.shared.identity.workspaces import link_login_to_workspace
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap, MagicLinkNonce, UserIdentity
from tests.admin import test_register_app_callback_authority as fixtures

db = fixtures.db
db_engine = fixtures.db_engine


@pytest.fixture
async def setup(db, monkeypatch):
    user = await db.get(User, "user-mallory")
    user.cognito_sub = "sub-mallory"
    await db.commit()
    claims = {"subject": user.cognito_sub, "org": user.org_id, "role": "member", "username": "native-mallory"}
    metadata = {"id": 123456, "account": {"id": 999, "login": "inert-user", "type": "User"}, "permissions": {"members": "read"}}
    membership = {"state": "active", "role": "admin", "user": {"id": 999}, "organization": {"id": 4242}}
    calls = []

    def transport(request):
        calls.append(request.url.path)
        if request.url.path == "/app/installations/123456":
            return httpx.Response(200, json=metadata)
        if request.url.path == "/app/installations/123456/access_tokens":
            return httpx.Response(201, json={"token": "inert-installation-token"})
        if request.url.path == "/installation/repositories":
            return httpx.Response(200, json={"repositories": [], "total_count": 0})
        if request.url.path == "/user/999":
            return httpx.Response(200, json={"id": 999, "login": "current-provider-login", "type": "User"})
        if request.url.path == "/orgs/inert-org/memberships/current-provider-login":
            return httpx.Response(200, json=membership)
        raise AssertionError(f"Unstubbed provider request: {request.method} {request.url.path}")

    provider_http = httpx.AsyncClient(base_url="https://api.github.com", transport=httpx.MockTransport(transport))
    github = GitHubAppClient("inert", "inert", http_client=provider_http)
    monkeypatch.setattr(github, "_auth_headers", lambda: {"Authorization": "Bearer inert-app-token"})
    monkeypatch.setattr(svc, "GitHubAppClient", lambda **kwargs: github)
    monkeypatch.setattr(svc, "_get_github_app_credentials", lambda: ("inert", "inert"))
    monkeypatch.setattr(svc, "_get_github_app_slug", lambda: "inert-app")
    monkeypatch.setattr(svc, "_check_existing_app_secret", lambda: None)
    monkeypatch.setattr(svc, "_invalidate_verification_cache", lambda: None)
    monkeypatch.setattr(svc, "_invalidate_login_enabled_cache", lambda: None)
    provider = MagicMock()
    provider.get_slug.return_value = ""
    monkeypatch.setattr(svc, "get_github_app_provider", lambda: provider)
    monkeypatch.setenv("WEBHOOK_URL", "https://webhook.test/inert")
    monkeypatch.setenv("ORG_TENANT_AUTO_CREATE", "false")
    monkeypatch.setattr("boto3.client", MagicMock(side_effect=AssertionError("Live cloud access forbidden")))
    seed, index, secrets = AsyncMock(), AsyncMock(), AsyncMock(return_value=True)
    monkeypatch.setattr("src.admin.connections.tenant_secret.seed_tenant_github_app_secret", seed)
    monkeypatch.setattr(svc, "_write_installation_identity_index", index)
    monkeypatch.setattr(svc, "_store_app_credentials", secrets)
    monkeypatch.setattr(svc, "_auto_switch_active_tenant", AsyncMock(return_value=None))
    monkeypatch.setattr("src.admin.memberships.project_member_org_ids", AsyncMock())
    validator = MagicMock()

    def validate(_token):
        now = datetime.now(UTC)
        return CognitoTokenClaims(
            sub=claims["subject"],
            iss="https://cognito-idp.us-east-1.amazonaws.com/inert",
            client_id="inert-client",
            token_use="access",
            exp=int((now + timedelta(hours=1)).timestamp()),
            iat=int(now.timestamp()),
            username=claims["username"],
            org_id=claims["org"],
            team_id="team-eng",
            department_id="dept-eng",
            role=claims["role"],
            account_type="human",
        )

    validator.validate_token.side_effect = validate
    monkeypatch.setattr("src.auth.dependencies._get_cognito_validator", lambda: validator)
    app = FastAPI()
    app.include_router(router)

    async def database():
        yield db

    app.dependency_overrides[get_db] = database
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://gateway.test") as client:
        yield {
            "user": user,
            "claims": claims,
            "metadata": metadata,
            "membership": membership,
            "github": github,
            "client": client,
            "seed": seed,
            "index": index,
            "secrets": secrets,
            "calls": calls,
        }
    await provider_http.aclose()


async def prove(db, setup, *, method="oauth", provider="github", account="999", user_id=None, org_id=None):
    row = UserIdentity(
        user_id=user_id or setup["user"].id,
        org_id=org_id or setup["claims"]["org"],
        team_id="team-eng",
        provider=provider,
        provider_user_id=account,
        verification_method=method,
        verified_at=datetime.now(UTC),
    )
    db.add(row)
    await db.commit()
    return row


async def start(setup, *, register=False, owner_type="user", org=None):
    route = "/admin/connections/github/app/register-start" if register else "/admin/connections/github/install-start"
    return await setup["client"].post(
        route, headers={"Authorization": "Bearer inert-valid-signed-token"}, json={"owner_type": owner_type, "org": org} if register else None
    )


async def callback(setup, state):
    # Intentionally no browser JWT: the valid state is the setup capability.
    return await setup["client"].get("/admin/connections/github/install-callback", params={"installation_id": 123456, "state": state})


async def untouched(db, setup, state):
    assert not list(await db.scalars(select(ChannelTenantMap)))
    assert (await db.get(MagicLinkNonce, state, populate_existing=True)).consumed_at is None
    setup["seed"].assert_not_awaited()
    setup["index"].assert_not_awaited()
    setup["secrets"].assert_not_awaited()


@pytest.mark.parametrize("same_id", [False, True])
@pytest.mark.parametrize("method", ["oauth", "admin_attested", "admin_manual", "self_asserted", "magic_link", None])
async def test_real_route_personal_install_requires_bound_proven_identity(db, setup, same_id, method):
    if same_id:
        setup["user"].cognito_sub = setup["user"].id
        setup["claims"]["subject"] = setup["user"].id
        await db.commit()
    if method:
        await prove(db, setup, method=method)
    result = await start(setup)
    assert result.status_code == 200, result.text
    state = result.json()["state_token"]
    nonce = await db.get(MagicLinkNonce, state)
    assert nonce.target_user_id == setup["user"].id
    response = await callback(setup, state)
    if method in {"oauth", "admin_attested"}:
        assert "success=1" in response.headers["location"]
        mapping = (await db.scalars(select(ChannelTenantMap))).one()
        assert mapping.org_id == "org-acme" and mapping.installed_by_user_id == setup["user"].id
    else:
        assert "github_control_required" in response.headers["location"]
        await untouched(db, setup, state)


@pytest.mark.parametrize("provider,account,org", [("slack", "999", "org-acme"), ("github", "888", "org-acme"), ("github", "999", "foreign")])
async def test_personal_proof_cannot_cross_provider_account_or_tenant(db, setup, provider, account, org):
    await prove(db, setup, provider=provider, account=account, org_id=org)
    state = (await start(setup)).json()["state_token"]
    assert "github_control_required" in (await callback(setup, state)).headers["location"]
    await untouched(db, setup, state)


@pytest.mark.parametrize("method", ["oauth", "admin_attested"])
async def test_org_control_accepts_existing_proof_plus_fresh_provider_admin_membership(db, setup, method):
    setup["metadata"]["account"] = {"id": 4242, "login": "inert-org", "type": "Organization"}
    await prove(db, setup, method=method)
    state = (await start(setup)).json()["state_token"]
    response = await callback(setup, state)
    assert "success=1" in response.headers["location"]
    assert "/user/999" in setup["calls"]
    assert "/orgs/inert-org/memberships/current-provider-login" in setup["calls"]


@pytest.mark.parametrize("field,value", [("state", "pending"), ("role", "member"), ("user", {"id": 888}), ("organization", {"id": 9999})])
async def test_org_membership_must_be_active_admin_and_same_immutable_ids(db, setup, field, value):
    setup["metadata"]["account"] = {"id": 4242, "login": "inert-org", "type": "Organization"}
    setup["membership"][field] = value
    await prove(db, setup)
    state = (await start(setup)).json()["state_token"]
    assert "github_control_required" in (await callback(setup, state)).headers["location"]
    await untouched(db, setup, state)


@pytest.mark.parametrize("status", [403, 500])
async def test_org_permission_or_provider_failure_is_truthful_and_retryable(db, setup, monkeypatch, status):
    setup["metadata"]["account"] = {"id": 4242, "login": "inert-org", "type": "Organization"}
    await prove(db, setup)
    state = (await start(setup)).json()["state_token"]
    original = setup["github"].has_org_admin_membership
    fault = httpx.HTTPStatusError(
        "offline provider failure", request=httpx.Request("GET", "https://api.github.com/inert"), response=httpx.Response(status)
    )
    monkeypatch.setattr(setup["github"], "has_org_admin_membership", AsyncMock(side_effect=fault))
    response = await callback(setup, state)
    assert "github_verification_unavailable" in response.headers["location"]
    assert "members" in response.headers["location"]
    await untouched(db, setup, state)
    monkeypatch.setattr(setup["github"], "has_org_admin_membership", original)
    assert "success=1" in (await callback(setup, state)).headers["location"]


@pytest.mark.parametrize("change", ["wrong_installation", "missing_account_id", "unknown_type", "suspended"])
async def test_bad_provider_metadata_never_falls_back_to_caller_tenant(db, setup, change):
    await prove(db, setup)
    if change == "wrong_installation":
        setup["metadata"]["id"] = 222
    elif change == "missing_account_id":
        del setup["metadata"]["account"]["id"]
    elif change == "unknown_type":
        setup["metadata"]["account"]["type"] = "Bot"
    else:
        setup["metadata"]["suspended_at"] = "2026-09-24T00:00:00Z"
    state = (await start(setup)).json()["state_token"]
    assert "github_verification_unavailable" in (await callback(setup, state)).headers["location"]
    await untouched(db, setup, state)


@pytest.mark.parametrize("representation", ["foreign_home", "local"])
async def test_selected_workspace_is_bound_separately_from_canonical_home(db, setup, representation):
    db.add(Organization(id="org-work", name="Work"))
    target = setup["user"]
    if representation == "local":
        target = User(id="work-user", org_id="org-work", team_id="team-work", email="work@test.com")
        db.add(target)
        await db.flush()
        await link_login_to_workspace(db, setup["user"], target)
    db.add(TenantMembership(user_id=target.id, tenant_id="org-work", role="member", is_active=True))
    await db.commit()
    setup["claims"]["org"] = "org-work"
    await prove(db, setup, user_id=target.id, org_id="org-work")
    state = (await start(setup)).json()["state_token"]
    assert (await db.get(MagicLinkNonce, state)).target_user_id == target.id
    # A workspace selection change is not membership revocation.
    setup["claims"]["org"] = "org-acme"
    response = await callback(setup, state)
    assert "success=1" in response.headers["location"]
    assert (await db.scalars(select(ChannelTenantMap))).one().org_id == "org-work"


async def test_removed_selected_workspace_membership_revokes_pending_setup(db, setup):
    db.add(Organization(id="org-work", name="Work"))
    membership = TenantMembership(user_id=setup["user"].id, tenant_id="org-work", role="member", is_active=True)
    db.add(membership)
    await db.commit()
    setup["claims"]["org"] = "org-work"
    await prove(db, setup)
    state = (await start(setup)).json()["state_token"]
    await db.delete(membership)
    await db.commit()
    assert "unauthorized" in (await callback(setup, state)).headers["location"]
    await untouched(db, setup, state)


@pytest.mark.parametrize("failure", ["foreign_tenant", "missing_user", "ambiguous_subject", "foreign_subject"])
async def test_start_refuses_missing_ambiguous_or_foreign_claims(db, setup, failure):
    if failure == "foreign_tenant":
        setup["claims"]["org"] = "unrelated-workspace"
    elif failure == "missing_user":
        setup["claims"]["subject"] = "absent-subject"
    elif failure == "ambiguous_subject":
        db.add(User(id="sub-mallory", org_id="org-acme", team_id="team-eng", email="other@test.com", cognito_sub="other-subject"))
        await db.commit()
    else:
        setup["claims"]["subject"] = setup["user"].id
    result = await start(setup)
    assert result.status_code == 403, result.text
    assert not list(await db.scalars(select(MagicLinkNonce)))


async def test_unversioned_state_requires_restart(db, setup):
    await prove(db, setup)
    state = (await start(setup)).json()["state_token"]
    nonce = await db.get(MagicLinkNonce, state)
    nonce.channel_context = None
    await db.commit()
    assert "unauthorized" in (await callback(setup, state)).headers["location"]
    await untouched(db, setup, state)


async def test_proof_revoked_during_membership_call_is_rechecked(db, setup, monkeypatch):
    setup["metadata"]["account"] = {"id": 4242, "login": "inert-org", "type": "Organization"}
    identity = await prove(db, setup)
    state = (await start(setup)).json()["state_token"]

    async def revoke(**kwargs):
        identity.verification_method = "self_asserted"
        await db.commit()
        return True

    monkeypatch.setattr(setup["github"], "has_org_admin_membership", revoke)
    assert "github_control_required" in (await callback(setup, state)).headers["location"]
    await untouched(db, setup, state)


async def test_expiry_after_provider_work_is_rechecked(db, setup, monkeypatch):
    setup["metadata"]["account"] = {"id": 4242, "login": "inert-org", "type": "Organization"}
    await prove(db, setup)
    state = (await start(setup)).json()["state_token"]

    class ExpiredClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(minutes=16)

    async def delayed(**kwargs):
        monkeypatch.setattr(svc, "datetime", ExpiredClock)
        return True

    monkeypatch.setattr(setup["github"], "has_org_admin_membership", delayed)
    assert "invalid_state" in (await callback(setup, state)).headers["location"]
    await untouched(db, setup, state)


async def register_response(db, setup, state, *, owner=None, mutate=None):
    data = {
        "id": 444,
        "slug": "inert-new-app",
        "pem": "inert-test-pem",
        "client_id": "inert-client",
        "client_secret": "inert-secret",
        "webhook_secret": "inert-webhook",
        "owner": owner or {"id": 999, "login": "inert-user", "type": "User"},
    }

    async def exchange(*args, **kwargs):
        if mutate:
            await mutate()
        return httpx.Response(201, json=data)

    with patch("httpx.AsyncClient.post", side_effect=exchange):
        return await setup["client"].get("/admin/connections/github/app/register-callback", params={"code": "inert", "state": state})


@pytest.mark.parametrize("selected_org", ["", "org-acme", "different-selected-workspace"])
async def test_platform_register_uses_global_canonical_authority_not_workspace_role(db, setup, selected_org):
    setup["user"].role = "platform_admin"
    setup["claims"].update(role="platform_admin", org=selected_org)
    await db.commit()
    result = await start(setup, register=True)
    assert result.status_code == 200, result.text
    state = result.json()["state"]
    assert (await db.get(MagicLinkNonce, state)).target_user_id == setup["user"].id
    response = await register_response(db, setup, state)
    assert response.headers["location"] == "https://github.com/apps/inert-new-app/installations/new"
    setup["secrets"].assert_awaited_once()
    assert (await db.get(MagicLinkNonce, state)).consumed_at is not None


async def test_platform_role_revoked_during_provider_exchange_prevents_secret_write(db, setup):
    setup["user"].role = "platform_admin"
    setup["claims"]["role"] = "platform_admin"
    await db.commit()
    state = (await start(setup, register=True)).json()["state"]

    async def revoke():
        setup["user"].role = "member"
        await db.commit()

    response = await register_response(db, setup, state, mutate=revoke)
    assert "not_authorized" in response.headers["location"]
    await untouched(db, setup, state)


@pytest.mark.parametrize("owner", [{"id": 4242, "login": "other-org", "type": "Organization"}, {"id": 4242, "login": "inert-org", "type": "User"}])
async def test_registration_owner_target_is_bound(db, setup, owner):
    setup["user"].role = "platform_admin"
    setup["claims"]["role"] = "platform_admin"
    await db.commit()
    state = (await start(setup, register=True, owner_type="org", org="inert-org")).json()["state"]
    response = await register_response(db, setup, state, owner=owner)
    assert "not_authorized" in response.headers["location"]
    await untouched(db, setup, state)


async def test_foreign_installation_claim_denies_without_consuming_state(db, setup):
    await prove(db, setup)
    db.add(Organization(id="other-workspace", name="Other"))
    db.add(ChannelTenantMap(provider="github", provider_scope_id="different-account", installation_id="123456", org_id="other-workspace"))
    await db.commit()
    state = (await start(setup)).json()["state_token"]
    response = await callback(setup, state)
    assert "tenant_conflict" in response.headers["location"]
    assert (await db.get(MagicLinkNonce, state)).consumed_at is None
    setup["seed"].assert_not_awaited()
    setup["index"].assert_not_awaited()
    mapping = (await db.scalars(select(ChannelTenantMap))).one()
    assert mapping.org_id == "other-workspace"


async def test_foreign_org_standing_denial_does_not_consume_state(db, setup):
    setup["metadata"]["account"] = {"id": 4242, "login": "inert-org", "type": "Organization"}
    db.add(Organization(id="other-workspace", name="Other", github_org_id="4242"))
    await db.commit()
    await prove(db, setup)
    state = (await start(setup)).json()["state_token"]
    response = await callback(setup, state)
    assert "tenant_conflict" in response.headers["location"]
    await untouched(db, setup, state)
