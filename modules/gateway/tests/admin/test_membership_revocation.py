"""Removing a canonical membership keeps login identity and blocks stale authority."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from src.admin import membership_revocation as removal
from src.admin.exceptions import ResourceConflictError
from src.admin.memberships import upsert_tenant_membership
from src.shared.identity.workspaces import memberships_for_login, workspace_user
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def seeded(db_session, monkeypatch):
    monkeypatch.setattr(removal, "project_member_org_ids", AsyncMock())
    db_session.add_all([Organization(id="one", name="One"), Organization(id="two", name="Two")])
    await db_session.flush()
    db_session.add(User(id="person", org_id="one", team_id="", email="person@example.test", cognito_sub="login"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id="person", tenant_id="two", role="org_admin", is_active=False))
    await db_session.commit()


async def test_legacy_canonical_revocation_retains_login_and_other_membership(db_session, seeded):
    _, before = await memberships_for_login(db_session, "login")
    assert set(before) == {"one", "two"}
    await removal.revoke_membership(db_session, org_id="one", user_id="person")
    user, after = await memberships_for_login(db_session, "login")
    assert user.cognito_sub == "login" and set(after) == {"two"}
    assert await workspace_user(db_session, "login", "one") is None
    assert await db_session.get(User, "person") is not None
    tombstone = await db_session.scalar(select(TenantMembership).where(TenantMembership.tenant_id == "one"))
    assert tombstone.revoked_at is not None and not tombstone.is_active
    await removal.revoke_membership(db_session, org_id="one", user_id="person")
    _, again = await memberships_for_login(db_session, "login")
    assert set(again) == {"two"}


async def test_ordinary_upsert_cannot_restore_removed_access(db_session, seeded):
    await removal.revoke_membership(db_session, org_id="one", user_id="person")
    with pytest.raises(ResourceConflictError):
        await upsert_tenant_membership(db_session, user_id="person", tenant_id="one", role="org_admin", joined_via="test")
    await removal.reactivate_membership(db_session, org_id="one", user_id="person", role="member")
    await upsert_tenant_membership(db_session, user_id="person", tenant_id="one", role="member", joined_via="test")
    await db_session.commit()
    _, memberships = await memberships_for_login(db_session, "login")
    assert memberships["one"][1].role == "member"


async def test_stale_native_context_is_refused(db_session, seeded):
    ctx = TokenContext(
        user_id="login", org_id="one", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    await removal.require_not_revoked_context(ctx, db_session)
    await removal.revoke_membership(db_session, org_id="one", user_id="person")
    with pytest.raises(HTTPException) as exc:
        await removal.require_not_revoked_context(ctx, db_session)
    assert exc.value.status_code == 403
    await removal.require_not_revoked_context(ctx.model_copy(update={"org_id": "two"}), db_session)


async def test_raw_dependency_proxy_and_lease_refuse_removed_member(db_session, seeded, monkeypatch):
    from types import SimpleNamespace

    from starlette.requests import Request

    from src.auth import dependencies, middleware, tenant_context

    ctx = TokenContext(
        user_id="login", org_id="one", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    monkeypatch.setattr(
        tenant_context, "get_settings", lambda: SimpleNamespace(token_secret_key="offline-secret-only-for-lease-tests", cognito_user_pool_id="pool")
    )
    lease = await tenant_context.issue_context(db_session, ctx, "one")
    validator = SimpleNamespace(validate_token=lambda _: object())
    monkeypatch.setattr(dependencies, "_get_cognito_validator", lambda: validator)
    monkeypatch.setattr(middleware, "get_cognito_validator", lambda: validator)
    monkeypatch.setattr(dependencies, "_cognito_claims_to_context", lambda _: ctx)
    monkeypatch.setattr(middleware, "_cognito_claims_to_context", lambda _: ctx)
    original = removal.require_not_revoked_context
    monkeypatch.setattr(removal, "require_not_revoked_context", lambda context: original(context, db_session))
    await removal.revoke_membership(db_session, org_id="one", user_id="person")
    with pytest.raises(HTTPException):
        await tenant_context.apply_context(ctx, lease["context_token"], db_session)
    request = Request({"type": "http", "method": "GET", "path": "/models", "headers": []})
    with pytest.raises(HTTPException) as dependency:
        await dependencies.get_current_user(request, "Bearer offline")
    with pytest.raises(HTTPException) as proxy:
        await middleware.validate_cognito_jwt("Bearer offline")
    assert dependency.value.status_code == proxy.value.status_code == 403
    # Bootstrap discovery remains available to select a surviving workspace.
    discovery = Request({"type": "http", "method": "GET", "path": "/workspaces", "headers": []})
    assert (await dependencies.get_current_user(discovery, "Bearer offline")).user_id == "login"


@pytest.mark.parametrize("credential_path", ["cognito", "legacy"])
async def test_vault_and_sso_refuse_existing_and_refreshed_tokens_after_removal(db_session, seeded, monkeypatch, credential_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.auth import dependencies, gitlab_sso, middleware, vault_routes
    from src.shared.database import get_db

    context = TokenContext(
        user_id="login", org_id="one", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    service = middleware.auth_service
    monkeypatch.setattr(service, "_cognito_enabled", credential_path == "cognito")
    validate = AsyncMock(side_effect=lambda *args: context.model_copy())
    legacy = validate if credential_path == "legacy" else AsyncMock(side_effect=AssertionError("Revoked Cognito identity must not fall back"))
    monkeypatch.setattr(service, "_validate_cognito_token", validate)
    monkeypatch.setattr(service.token_manager, "validate_token", legacy)
    # Product and GitLab routes share Cognito auth. Exercise its real revocation
    # guard alongside the vault's legacy/Cognito service paths.
    monkeypatch.setattr(dependencies, "_get_cognito_validator", lambda: SimpleNamespace(validate_token=lambda _: object()))
    monkeypatch.setattr(dependencies, "_cognito_claims_to_context", lambda _: context.model_copy())
    original_guard = removal.require_not_revoked_context
    monkeypatch.setattr(removal, "require_not_revoked_context", lambda ctx, db=None: original_guard(ctx, db or db_session))
    listings = AsyncMock(return_value=[])
    discovery = Mock(return_value=None)
    monkeypatch.setattr(vault_routes, "list_credentials", listings)
    monkeypatch.setattr(gitlab_sso, "_discover_gitlab_url", discovery)
    app = FastAPI()
    app.include_router(vault_routes.router)
    app.include_router(gitlab_sso.router)

    async def session():
        yield db_session

    app.dependency_overrides[get_db] = session
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        assert (await client.get("/auth/credentials", headers={"Authorization": "Bearer existing"})).status_code == 200
        assert (await client.get("/auth/gitlab-sso", headers={"Authorization": "Bearer existing"})).status_code == 404
        listings.reset_mock()
        discovery.reset_mock()
        await removal.revoke_membership(db_session, org_id="one", user_id="person")
        for token in ("existing", "refreshed"):
            for path in ("/auth/credentials", "/auth/gitlab-sso"):
                response = await client.get(path, headers={"Authorization": f"Bearer {token}"})
                assert response.status_code == 403
                assert response.json()["detail"]["error"] == "tenant_membership_revoked"
        listings.assert_not_awaited()
        discovery.assert_not_called()
        # The same person can still select another organization where membership survives.
        context.org_id = "two"
        assert (await client.get("/auth/credentials", headers={"Authorization": "Bearer other-workspace"})).status_code == 200
        # Service principals retain their existing, separate authorization semantics.
        context.account_type = "service"
        context.org_id = "one"
        assert (await service.validate_token("service-token", db_session)).account_type == "service"


async def test_membership_storage_failure_denies_required_and_optional_auth(db_session, monkeypatch):
    from fastapi.security import HTTPAuthorizationCredentials
    from starlette.requests import Request

    from src.auth import middleware

    context = TokenContext(
        user_id="login", org_id="one", team_id="", department_id="", account_type="human", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )
    monkeypatch.setattr(middleware.auth_service, "_cognito_enabled", True)
    monkeypatch.setattr(middleware.auth_service, "_validate_cognito_token", AsyncMock(return_value=context))
    fallback = AsyncMock()
    monkeypatch.setattr(middleware.auth_service.token_manager, "validate_token", fallback)
    monkeypatch.setattr(removal, "is_revoked", AsyncMock(side_effect=RuntimeError("database unavailable")))
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="existing")
    with pytest.raises(HTTPException) as required:
        await middleware.get_current_user_context(credentials, db_session)
    request = Request({"type": "http", "headers": [(b"authorization", b"Bearer existing")]})
    with pytest.raises(HTTPException) as optional:
        await middleware.get_optional_user_context(request, db_session)
    assert required.value.status_code == optional.value.status_code == 503
    assert "database unavailable" not in str(required.value.detail)
    fallback.assert_not_awaited()
