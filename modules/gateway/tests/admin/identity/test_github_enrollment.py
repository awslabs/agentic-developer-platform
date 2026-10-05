from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from src.admin.identity import github_enrollment as service
from src.admin.identity.github_enrollment import GitHubEnrollmentRequest
from src.admin.identity.github_enrollment_routes import router
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity.workspaces import memberships_for_login
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def enrollment(db_session, monkeypatch):
    db_session.add(Organization(id="sophos-it", name="Sophos IT"))
    await db_session.flush()
    db_session.add(Department(id="engineering", org_id="sophos-it", name="Engineering"))
    await db_session.flush()
    db_session.add(Team(id="platform", org_id="sophos-it", department_id="engineering", name="Platform"))
    await db_session.commit()
    monkeypatch.setattr(service, "resolve_github_user", AsyncMock(return_value={"id": "123", "login": "octocat"}))
    monkeypatch.setattr(service, "ensure_broker_login", MagicMock(return_value={"sub": "github-sub", "username": "GitHub_123"}))
    monkeypatch.setattr(service, "initialize_login_claims", MagicMock())
    monkeypatch.setattr(service, "IdentityIndexWriter", MagicMock(return_value=MagicMock(put_user_identity=AsyncMock())))
    monkeypatch.setattr(service, "project_member_org_ids", AsyncMock(return_value=True))
    return GitHubEnrollmentRequest(github_username="octocat", team_id="platform")


@pytest.mark.asyncio
async def test_first_github_login_resolves_assigned_org_primary_team_and_role(db_session, enrollment):
    enrollment.role = "org_admin"
    result = await service.enroll_github_user(db_session, "sophos-it", enrollment)
    user, memberships = await memberships_for_login(db_session, "github-sub", username="github_123")
    assert user.id == result["id"]
    account, membership = memberships["sophos-it"]
    assert membership.role == "org_admin"
    assert account.team_id == "platform"
    primary = await db_session.scalar(select(TeamMembership).where(TeamMembership.user_id == user.id))
    assert primary.is_primary
    identity = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == user.id))
    assert identity.provider_user_id == "123"
    again = await service.enroll_github_user(db_session, "sophos-it", enrollment)
    assert again["id"] == result["id"]
    assert len((await db_session.scalars(select(User))).all()) == 1


@pytest.mark.asyncio
async def test_native_login_conflict_never_rebinds_or_changes_membership(db_session, enrollment):
    user = User(id="native", org_id="sophos-it", team_id="platform", email="native@example.test", cognito_sub="native-sub")
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        UserIdentity(
            user_id=user.id, org_id="sophos-it", team_id="platform", provider="github", provider_user_id="123", verification_method="admin_attested"
        )
    )
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await service.enroll_github_user(db_session, "sophos-it", enrollment)
    assert error.value.status_code == 409
    assert user.cognito_sub == "native-sub"
    service.initialize_login_claims.assert_not_called()
    assert not (await db_session.scalars(select(TenantMembership))).all()


@pytest.mark.asyncio
async def test_foreign_team_rejected_before_cognito_side_effects(db_session, enrollment):
    enrollment.team_id = "foreign-team"
    with pytest.raises(HTTPException) as error:
        await service.enroll_github_user(db_session, "sophos-it", enrollment)
    assert error.value.status_code == 404
    service.ensure_broker_login.assert_not_called()


@pytest.mark.asyncio
async def test_partial_projection_failure_can_retry_without_duplicate_user(db_session, enrollment):
    service.project_member_org_ids.side_effect = RuntimeError("unavailable")
    with pytest.raises(HTTPException) as error:
        await service.enroll_github_user(db_session, "sophos-it", enrollment)
    assert error.value.status_code == 503
    assert "Assignment saved" in error.value.detail
    service.project_member_org_ids.side_effect = None
    result = await service.enroll_github_user(db_session, "sophos-it", enrollment)
    assert result["id"] == (await db_session.scalar(select(User))).id
    assert len((await db_session.scalars(select(User))).all()) == 1


@pytest.mark.asyncio
async def test_route_rejects_non_platform_admin(db_session, enrollment):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id="member",
        org_id="sophos-it",
        team_id="platform",
        department_id="engineering",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )

    async def db_override():
        yield db_session

    app.dependency_overrides[get_db] = db_override
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/admin/identity/organizations/sophos-it/github-members", json=enrollment.model_dump())
    assert response.status_code == 403
    service.ensure_broker_login.assert_not_called()


def test_existing_cognito_password_mfa_and_org_selection_are_untouched(monkeypatch):
    client = MagicMock()
    client.admin_get_user.return_value = {
        "Username": "github_123",
        "Enabled": True,
        "UserAttributes": [
            {"Name": "sub", "Value": "sub"},
            {"Name": "custom:org_id", "Value": "current-org"},
        ],
    }
    monkeypatch.setattr(service, "CognitoService", lambda: MagicMock(client=client, user_pool_id="pool"))
    login = service.ensure_broker_login("123")
    service.initialize_login_claims(login, "sophos-it", MagicMock(id="platform", department_id="engineering"), "member")
    client.admin_create_user.assert_not_called()
    client.admin_set_user_password.assert_not_called()
    client.admin_update_user_attributes.assert_not_called()
    client.admin_set_user_mfa_preference.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"id": 123, "login": "octocat", "type": "Organization"},
        {"id": "123", "login": "octocat", "type": "User"},
        {"id": 123, "login": "someone-else", "type": "User"},
    ],
)
async def test_provider_lookup_rejects_non_person_or_mismatched_identity(monkeypatch, payload):
    client = MagicMock(get=AsyncMock(return_value=MagicMock(status_code=200, json=lambda: payload)))
    boundary = MagicMock(__aenter__=AsyncMock(return_value=client), __aexit__=AsyncMock(return_value=False))
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **kwargs: boundary)
    with pytest.raises(HTTPException):
        await service.resolve_github_user("octocat")


@pytest.mark.asyncio
async def test_saved_assignment_projects_false_as_incomplete(db_session, enrollment):
    service.project_member_org_ids.return_value = False
    with pytest.raises(HTTPException) as error:
        await service.enroll_github_user(db_session, "sophos-it", enrollment)
    assert error.value.status_code == 503
    service.initialize_login_claims.assert_not_called()


def test_initial_claims_are_written_for_reserved_github_subject(monkeypatch):
    client = MagicMock()
    client.admin_get_user.return_value = {"Username": "github_123", "UserAttributes": [{"Name": "sub", "Value": "sub"}]}
    monkeypatch.setattr(service, "CognitoService", lambda: MagicMock(client=client, user_pool_id="pool"))
    service.initialize_login_claims(
        {"username": "github_123", "sub": "sub"}, "sophos-it", MagicMock(id="platform", department_id="engineering"), "member"
    )
    values = {v["Name"]: v["Value"] for v in client.admin_update_user_attributes.call_args.kwargs["UserAttributes"]}
    assert values == {"custom:org_id": "sophos-it", "custom:team_id": "platform", "custom:department_id": "engineering", "custom:role": "member"}
    client.admin_set_user_password.assert_not_called()


@pytest.mark.asyncio
async def test_revoked_assignment_does_not_initialize_login_claims(db_session, enrollment):
    async def revoke(*args, **kwargs):
        membership = await db_session.scalar(select(TenantMembership))
        membership.revoked_at = datetime.now(UTC)
        await db_session.commit()
        return True

    service.project_member_org_ids.side_effect = revoke
    with pytest.raises(HTTPException) as error:
        await service.enroll_github_user(db_session, "sophos-it", enrollment)
    assert error.value.status_code == 503
    service.initialize_login_claims.assert_not_called()
