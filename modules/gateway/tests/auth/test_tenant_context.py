"""Signed leases bind the human, membership and tenant without global switches."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import delete, select

from src.auth import tenant_context as tenant
from src.auth.workspaces import TenantContextRequest
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.schemas.auth import TokenContext


def context(subject="login", org="home"):
    return TokenContext(
        user_id=subject,
        org_id=org,
        team_id="old-team",
        department_id="old-dept",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
async def seeded(db_session, monkeypatch):
    monkeypatch.setattr(
        tenant, "get_settings", lambda: SimpleNamespace(token_secret_key="test-secret-that-is-not-production", cognito_user_pool_id="pool-a")
    )
    db_session.add_all([Organization(id="home", name="Home"), Organization(id="other", name="Other")])
    await db_session.flush()
    db_session.add(User(id="user", org_id="home", team_id="", email="u@example.test", cognito_sub="login"))
    await db_session.flush()
    db_session.add_all(
        [
            TenantMembership(id="home-member", user_id="user", tenant_id="home", role="member", is_active=True),
            TenantMembership(id="other-member", user_id="user", tenant_id="other", role="member", is_active=False),
        ]
    )
    await db_session.commit()


async def test_two_leases_preserve_global_selection(db_session, seeded):
    for target in ["home", "other"]:
        lease = await tenant.issue_context(db_session, context(), target)
        result = await tenant.apply_context(context(), lease["context_token"], db_session)
        assert result.org_id == result.attributed_org_id == target
        assert result.team_id == ""
    active = (await db_session.scalars(select(TenantMembership).where(TenantMembership.is_active.is_(True)))).all()
    assert [item.id for item in active] == ["home-member"]


async def test_revocation_refuses_lease_and_refresh(db_session, seeded):
    lease = await tenant.issue_context(db_session, context(), "other")
    await db_session.execute(delete(TenantMembership).where(TenantMembership.id == "other-member"))
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await tenant.apply_context(context(), lease["context_token"], db_session)
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException):
        await tenant.issue_context(db_session, context(), "other", lease["membership_id"])


async def test_replay_other_login_and_tampered_signature_refused(db_session, seeded):
    lease = await tenant.issue_context(db_session, context(), "other")
    for who, token in [(context("attacker"), lease["context_token"]), (context(), lease["context_token"] + "bad")]:
        with pytest.raises(HTTPException) as exc:
            await tenant.apply_context(who, token, db_session)
        assert exc.value.status_code == 401


async def test_refresh_cognito_default_does_not_redirect_lease(db_session, seeded):
    lease = await tenant.issue_context(db_session, context(), "other")
    assert (await tenant.apply_context(context(org="home"), lease["context_token"], db_session)).org_id == "other"


def test_exchange_schema_forbids_authority_overrides():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TenantContextRequest(org_id="other", user_id="victim")


async def test_dependency_and_proxy_validate_same_original_cognito_and_lease(db_session, seeded, monkeypatch):
    from starlette.requests import Request

    from src.auth import dependencies, middleware

    lease = await tenant.issue_context(db_session, context(), "other")
    bearer = "adpctx1~" + lease["context_token"] + "~cognito-original"
    seen = []
    validator = SimpleNamespace(validate_token=lambda token: seen.append(token) or object())
    monkeypatch.setattr(dependencies, "_get_cognito_validator", lambda: validator)
    monkeypatch.setattr(middleware, "get_cognito_validator", lambda: validator)
    monkeypatch.setattr(dependencies, "_cognito_claims_to_context", lambda _: context())
    monkeypatch.setattr(middleware, "_cognito_claims_to_context", lambda _: context())
    original = tenant.apply_context
    monkeypatch.setattr(tenant, "apply_context", lambda value, token: original(value, token, db_session))
    request = Request({"type": "http", "headers": [], "path": "/models"})
    assert (await dependencies.get_current_user(request, "Bearer " + bearer)).org_id == "other"
    assert (await middleware.validate_cognito_jwt("Bearer " + bearer)).org_id == "other"
    assert seen == ["cognito-original", "cognito-original"]


async def test_replaced_membership_does_not_reuse_lease(db_session, seeded):
    lease = await tenant.issue_context(db_session, context(), "other")
    await db_session.execute(delete(TenantMembership).where(TenantMembership.id == "other-member"))
    db_session.add(TenantMembership(id="replacement", user_id="user", tenant_id="other", role="member", is_active=False))
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await tenant.apply_context(context(), lease["context_token"], db_session)
    assert error.value.status_code == 403


async def test_lease_cannot_cross_pool_or_outlive_expiry(db_session, seeded, monkeypatch):
    import jwt

    lease = await tenant.issue_context(db_session, context(), "other")
    key = tenant.get_settings().token_secret_key
    claims = jwt.decode(lease["context_token"], key, algorithms=["HS256"], audience=tenant.AUDIENCE)
    for changed in ({"pool": "foreign-pool"}, {"exp": datetime.now(UTC) - timedelta(seconds=1)}):
        invalid = jwt.encode({**claims, **changed}, key, algorithm="HS256")
        with pytest.raises(HTTPException) as error:
            await tenant.apply_context(context(), invalid, db_session)
        assert error.value.status_code == 401


async def test_service_identity_cannot_borrow_human_lease(db_session, seeded):
    lease = await tenant.issue_context(db_session, context(), "other")
    service = context().model_copy(update={"account_type": "service"})
    with pytest.raises(HTTPException) as error:
        await tenant.apply_context(service, lease["context_token"], db_session)
    assert error.value.status_code == 403


async def test_tenant_role_is_resolved_in_selected_membership(db_session, seeded):
    from src.admin.access_control import AccessControl
    from src.admin.config import AdminRole

    row = await db_session.get(TenantMembership, "home-member")
    row.role = "org_admin"
    await db_session.commit()
    lease = await tenant.issue_context(db_session, context(), "other")
    scoped = await tenant.apply_context(context(), lease["context_token"], db_session)
    role, org = await AccessControl(db_session)._resolve_membership_role(scoped)
    assert role == AdminRole.MEMBER
    assert org == "other"
