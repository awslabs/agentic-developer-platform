"""#5010/#5011: real DB/router/workspace behavior with only AWS calls replaced.

These are offline integration tests, not proof of deployed IAM or live sign-in.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from src.admin.cognito_service import CognitoServiceError, UserAlreadyExistsError
from src.admin.exceptions import MemberRemovalConflictError
from src.admin.identity.cognito_sync import CognitoSyncService
from src.admin.identity.organizations_service import OrganizationsService
from src.admin.identity.schemas import CognitoLinkRequest, OrganizationCreateRequest, UserCreateRequest
from src.admin.identity.users_service import UsersService
from src.admin.service import AdminService
from src.auth.dependencies import get_current_user, require_admin
from src.auth.workspaces import list_workspaces, select_workspace
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio


class Pool:
    """Stateful AWS boundary: a successful create survives a lost response."""

    def __init__(self):
        self.users = {}
        self.creates = 0
        self.create_requests = []
        self.group_failure = False
        self.create_failure = False
        self.lose_response = False
        self.add_failure = False
        self.gets = []
        self.groups = set()
        self.deleted = []

    def add_existing(self, username, subject=None):
        result = {
            "Username": username,
            "UserAttributes": [{"Name": "sub", "Value": subject or str(uuid4())}],
            "Enabled": True,
        }
        self.users[username] = result
        return result

    def create_user(self, *, email, **kwargs):
        self.creates += 1
        self.create_requests.append({"email": email, **kwargs})
        if self.create_failure:
            raise CognitoServiceError("AccessDenied")
        if email in self.users:
            raise UserAlreadyExistsError(email)
        result = self.add_existing(email)
        if self.lose_response:
            raise CognitoServiceError("Response lost after AWS committed the user")
        return {"Username": result["Username"], "Attributes": result["UserAttributes"]}

    def get_user(self, username):
        self.gets.append(username)
        return self.users.get(username)

    def create_org_group(self, org_id):
        if self.group_failure:
            raise CognitoServiceError("CreateGroup AccessDenied")
        self.groups.add(f"org-{org_id}")
        return {}

    def add_user_to_group(self, *, username, group_name):
        if self.add_failure:
            raise CognitoServiceError("AdminAddUserToGroup AccessDenied")
        assert username in self.users and group_name in self.groups

    def delete_user(self, *, username):
        self.deleted.append(username)
        del self.users[username]


@pytest.fixture
async def native(db_session, monkeypatch):
    monkeypatch.setattr("src.admin.identity.cognito_sync.MAX_RETRIES", 1)
    for org_id in ("native-home", "native-work"):
        db_session.add(Organization(id=org_id, name=org_id))
        db_session.add(Department(id=f"{org_id}-dept-default", org_id=org_id, name="Default"))
        db_session.add(Team(id=f"{org_id}-team-default", org_id=org_id, department_id=f"{org_id}-dept-default", name="Default"))
    await db_session.commit()
    pool = Pool()
    sync = CognitoSyncService(pool)
    writer = AsyncMock()
    return UsersService(db_session, cognito_sync=sync, identity_writer=writer), pool, writer


def context(sub, org="native-home"):
    return TokenContext(
        user_id=sub,
        org_id=org,
        team_id="",
        department_id="",
        account_type="human",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        cognito_username="native-login",
    )


def claims():
    return MagicMock(set=AsyncMock(side_effect=lambda subject, values: ({}, values)))


def subject(pool, username):
    return pool.users[username]["UserAttributes"][0]["Value"]


async def test_two_created_native_users_have_distinct_subjects_and_select_their_workspace(db_session, native):
    service, pool, _ = native
    first = await service.create_user("native-home", UserCreateRequest(email="one@example.com", send_invite=False))
    second = await service.create_user("native-work", UserCreateRequest(email="two@example.com", send_invite=False))
    assert first.cognito_sub == subject(pool, "one@example.com")
    assert second.cognito_sub == subject(pool, "two@example.com")
    assert first.cognito_sub != second.cognito_sub
    assert first.cognito_username == "one@example.com" and second.cognito_username == "two@example.com"
    for created in (first, second):
        login = context(created.cognito_sub, created.org_id)
        listed = await list_workspaces(login, db_session)
        assert [item.org_id for item in listed.items] == [created.org_id]
        selected = await select_workspace(db_session, login, created.org_id, claims())
        assert selected.user_id == created.id and selected.role == "member"
        membership = await db_session.scalar(select(TenantMembership).where(TenantMembership.user_id == created.id))
        assert membership.role == "member" and membership.is_active
    with pytest.raises(HTTPException) as error:
        await select_workspace(db_session, context(first.cognito_sub), "native-work", claims())
    assert error.value.status_code == 403


async def test_failed_create_keeps_stable_row_and_retry_heals_it_without_duplicate(db_session, native):
    service, pool, _ = native
    pool.create_failure = True
    request = UserCreateRequest(email="retry@example.com", send_invite=False)
    with pytest.raises(BedrockGatewayError) as error:
        await service.create_user("native-home", request)
    assert error.value.status_code == 502
    user_id = error.value.details["user_id"]
    assert error.value.details["retry_path"].endswith(f"/{user_id}/provision")
    assert (await db_session.get(User, user_id)).cognito_sub is None
    assert not pool.users
    with pytest.raises(BedrockGatewayError) as duplicate:
        await service.create_user("native-home", request)
    assert duplicate.value.status_code == 409 and duplicate.value.details["user_id"] == user_id
    pool.create_failure = False
    recovered = await service.provision_user("native-home", user_id)
    assert recovered.id == user_id and recovered.cognito_sub == subject(pool, request.email)
    assert len((await db_session.scalars(select(User))).all()) == 1


async def test_lost_cognito_response_requires_explicit_verified_link(db_session, native):
    service, pool, _ = native
    pool.lose_response = True
    with pytest.raises(BedrockGatewayError) as error:
        await service.create_user("native-home", UserCreateRequest(email="partial@example.com"))
    user_id = error.value.details["user_id"]
    pool.lose_response = False
    with pytest.raises(BedrockGatewayError) as retry:
        await service.provision_user("native-home", user_id)
    assert retry.value.status_code == 409
    assert (await db_session.get(User, user_id)).cognito_sub is None
    assert (await list_workspaces(context(subject(pool, "partial@example.com")), db_session)).items == []
    linked = await service.link_cognito_user(
        "native-home", user_id, CognitoLinkRequest(username="partial@example.com", expected_sub=subject(pool, "partial@example.com"))
    )
    assert linked.cognito_sub == subject(pool, "partial@example.com")
    assert len((await db_session.scalars(select(User))).all()) == 1


async def test_group_failure_retains_immutable_link_and_retry_never_recreates_login(db_session, native):
    service, pool, _ = native
    pool.add_failure = True
    with pytest.raises(BedrockGatewayError) as error:
        await service.create_user("native-home", UserCreateRequest(email="group@example.com"))
    assert error.value.status_code == 502
    user_id = error.value.details["user_id"]
    assert (await db_session.get(User, user_id)).cognito_sub == subject(pool, "group@example.com")
    pool.add_failure = False
    recovered = await service.provision_user("native-home", user_id)
    assert recovered.cognito_username == "group@example.com"
    assert pool.creates == 1 and pool.gets == ["group@example.com"]


async def test_operator_created_login_can_use_a_different_email_without_email_matching(db_session, native):
    service, pool, _ = native
    pool.add_existing("operator-native-123")
    expected = subject(pool, "operator-native-123")
    result = await service.create_user(
        "native-home",
        UserCreateRequest(
            email="display@example.com",
            cognito_identity=CognitoLinkRequest(
                username="operator-native-123",
                expected_sub=expected,
            ),
        ),
    )
    assert pool.creates == 0 and pool.gets == ["operator-native-123"]
    assert result.cognito_sub == expected and result.cognito_username == "operator-native-123"
    assert [w.org_id for w in (await list_workspaces(context(expected), db_session)).items] == ["native-home"]


@pytest.mark.parametrize("invalid", ["subject", "disabled", "missing"])
async def test_link_rejects_unverified_subjects_and_disabled_users(db_session, native, invalid):
    service, pool, _ = native
    pool.add_existing("operator-native")
    user = User(id="unlinked", org_id="native-home", team_id="native-home-team-default", email="display@example.com")
    db_session.add(user)
    await db_session.commit()
    expected = subject(pool, "operator-native")
    if invalid == "subject":
        expected = "some-other-subject"
    elif invalid == "disabled":
        pool.users["operator-native"]["Enabled"] = False
    else:
        del pool.users["operator-native"]
    with pytest.raises(BedrockGatewayError) as error:
        await service.link_cognito_user("native-home", "unlinked", CognitoLinkRequest(username="operator-native", expected_sub=expected))
    assert error.value.status_code == 409
    assert (await db_session.get(User, "unlinked")).cognito_sub is None
    assert not (await db_session.scalars(select(UserIdentity))).all()


async def test_verified_link_cannot_replace_existing_subject(db_session, native):
    service, pool, _ = native
    created = await service.create_user("native-home", UserCreateRequest(email="owner@example.com"))
    pool.add_existing("other-login")
    with pytest.raises(BedrockGatewayError) as error:
        await service.link_cognito_user(
            "native-home",
            created.id,
            CognitoLinkRequest(
                username="other-login",
                expected_sub=subject(pool, "other-login"),
            ),
        )
    assert error.value.status_code == 409
    assert (await db_session.get(User, created.id)).cognito_sub == created.cognito_sub


async def test_existing_verified_org_link_cannot_be_replaced(db_session, native):
    service, pool, _ = native
    first = await service.create_user("native-home", UserCreateRequest(email="shared@example.com"))
    second = await service.create_user(
        "native-work",
        UserCreateRequest(
            email="same-display@example.com",
            cognito_identity=CognitoLinkRequest(
                username=first.cognito_username,
                expected_sub=first.cognito_sub,
            ),
        ),
    )
    pool.add_existing("different-login")
    with pytest.raises(BedrockGatewayError) as error:
        await service.link_cognito_user(
            "native-work",
            second.id,
            CognitoLinkRequest(
                username="different-login",
                expected_sub=subject(pool, "different-login"),
            ),
        )
    assert error.value.status_code == 409
    assert {w.org_id for w in (await list_workspaces(context(first.cognito_sub), db_session)).items} == {"native-home", "native-work"}
    assert (await list_workspaces(context(subject(pool, "different-login")), db_session)).items == []


async def test_one_login_cannot_be_bound_to_two_accounts_in_same_org(db_session, native):
    service, pool, _ = native
    first = await service.create_user("native-home", UserCreateRequest(email="same-org@example.com"))
    with pytest.raises(BedrockGatewayError) as error:
        await service.create_user(
            "native-home",
            UserCreateRequest(
                email="other-display@example.com",
                cognito_identity=CognitoLinkRequest(
                    username=first.cognito_username,
                    expected_sub=first.cognito_sub,
                ),
            ),
        )
    assert error.value.status_code == 409
    other = await db_session.get(User, error.value.details["user_id"])
    assert other.cognito_sub is None


async def test_native_multiple_orgs_select_revoke_and_remove_without_deleting_shared_login(db_session, native):
    service, pool, _ = native
    first = await service.create_user("native-home", UserCreateRequest(email="multi@example.com"))
    other = await service.create_user(
        "native-work",
        UserCreateRequest(
            email="multi@example.com",
            cognito_identity=CognitoLinkRequest(
                username=first.cognito_username,
                expected_sub=first.cognito_sub,
            ),
        ),
    )
    assert other.cognito_sub is None  # Secondary org uses the verified placement link.
    assert {w.org_id for w in (await list_workspaces(context(first.cognito_sub), db_session)).items} == {"native-home", "native-work"}
    selected = await select_workspace(db_session, context(first.cognito_sub), "native-work", claims())
    assert selected.user_id == other.id
    with pytest.raises(MemberRemovalConflictError):
        await service.delete_user("native-home", first.id)
    assert not pool.deleted
    await db_session.execute(delete(TenantMembership).where(TenantMembership.user_id == other.id))
    await db_session.commit()
    with pytest.raises(HTTPException) as error:
        await select_workspace(db_session, context(first.cognito_sub), "native-work", claims())
    assert error.value.status_code == 403
    assert await service.delete_user("native-work", other.id)
    assert not pool.deleted
    assert [w.org_id for w in (await list_workspaces(context(first.cognito_sub), db_session)).items] == ["native-home"]
    assert await service.delete_user("native-home", first.id)
    assert pool.deleted == ["multi@example.com"]


async def test_legacy_unlinked_row_gets_least_privilege_membership_not_display_role(db_session, native):
    service, pool, _ = native
    pool.add_existing("existing-user")
    db_session.add(User(id="legacy", org_id="native-home", team_id="native-home-team-default", email="old@example.com", role="org_admin"))
    await db_session.commit()
    await service.link_cognito_user(
        "native-home", "legacy", CognitoLinkRequest(username="existing-user", expected_sub=subject(pool, "existing-user"))
    )
    member = await db_session.scalar(select(TenantMembership).where(TenantMembership.user_id == "legacy"))
    assert member.role == "member"


async def test_organization_group_failure_rolls_back_and_same_request_can_retry(db_session, native):
    _, pool, writer = native
    service = OrganizationsService(db_session, identity_index=writer, cognito_sync=CognitoSyncService(pool))
    pool.group_failure = True
    request = OrganizationCreateRequest(id="retry-org", name="Retry org")
    with pytest.raises(BedrockGatewayError) as error:
        await service.create_organization(request)
    assert error.value.status_code == 502
    assert await db_session.get(Organization, "retry-org") is None
    assert await db_session.get(Team, "retry-org-team-default") is None
    writer.sync_org_channels.assert_not_awaited()
    pool.group_failure = False
    assert (await service.create_organization(request)).id == "retry-org"


async def test_legacy_team_user_create_uses_actual_subject_and_durable_failure(db_session, native):
    from src.shared.schemas.admin import UserCreateRequest as LegacyCreate

    _, pool, _ = native
    service = AdminService(db_session)
    created = await service.add_user("native-home", "native-home-team-default", LegacyCreate(email="legacy@example.com", name="Legacy"), pool)
    assert created.cognito_sub == subject(pool, "legacy@example.com")
    assert created.cognito_sub != created.cognito_username
    pool.create_failure = True
    with pytest.raises(BedrockGatewayError) as error:
        await service.add_user("native-home", "native-home-team-default", LegacyCreate(email="legacy-fail@example.com", name="Failed"), pool)
    assert error.value.status_code == 502 and error.value.details["user_id"]
    with pytest.raises(BedrockGatewayError) as duplicate:
        await service.add_user("native-home", "native-home-team-default", LegacyCreate(email="legacy-fail@example.com", name="Failed"), pool)
    assert duplicate.value.status_code == 409
    assert duplicate.value.details["user_id"] == error.value.details["user_id"]


@pytest.fixture
async def api(db_session, native, platform_admin_context, monkeypatch):
    service, _, _ = native
    from src.app import create_app

    monkeypatch.setattr("src.admin.identity.router.UsersService", lambda *args, **kwargs: service)
    monkeypatch.setattr("src.admin.identity.recovery_routes.UsersService", lambda *args, **kwargs: service)
    application = create_app()
    application.dependency_overrides[get_db] = lambda: db_session
    application.dependency_overrides[get_current_user] = lambda: platform_admin_context
    application.dependency_overrides[require_admin] = lambda: platform_admin_context

    async with AsyncClient(transport=ASGITransport(app=application, raise_app_exceptions=False), base_url="http://test") as client:
        yield client, application


async def test_http_failure_contains_recovery_paths_and_retry_returns_the_same_user(api, native):
    client, _ = api
    _, pool, _ = native
    pool.create_failure = True
    response = await client.post("/api/admin/identity/organizations/native-home/users", json={"email": "api@example.com"})
    assert response.status_code == 502
    error = response.json()
    assert error["error"] == "cognito_provisioning_failed"
    assert "Retry" in error["message"]
    pool.create_failure = False
    retried = await client.post(error["details"]["retry_path"], json={"send_invite": False})
    assert retried.status_code == 200
    assert retried.json()["id"] == error["details"]["user_id"]
    assert retried.json()["cognito_sub"] == subject(pool, "api@example.com")


async def test_http_subject_link_requires_platform_admin_and_rejects_other_org(api, native, db_session, org_admin_context):
    client, application = api
    service, pool, _ = native
    pool.add_existing("operator-user")
    pool.create_failure = True
    with pytest.raises(BedrockGatewayError) as error:
        await service.create_user("native-home", UserCreateRequest(email="link@example.com"))
    path = error.value.details["link_path"]
    body = {"username": "operator-user", "expected_sub": subject(pool, "operator-user")}
    wrong_org = await client.put(path.replace("native-home", "native-work"), json=body)
    assert wrong_org.status_code == 404
    application.dependency_overrides[get_current_user] = lambda: org_admin_context
    denied = await client.put(path, json=body)
    assert denied.status_code == 403
    assert (await db_session.get(User, error.value.details["user_id"])).cognito_sub is None


@pytest.mark.parametrize("display_role", ["org_admin", "platform_admin"])
async def test_retry_legacy_display_admin_cannot_mint_privileged_cognito_claims(db_session, native, display_role):
    service, pool, _ = native
    db_session.add(
        User(id="legacy-admin", org_id="native-home", team_id="native-home-team-default", email="legacy-admin@example.com", role=display_role)
    )
    await db_session.commit()
    await service.provision_user("native-home", "legacy-admin")
    assert pool.create_requests[0]["role"] == "member"
    membership = await db_session.scalar(select(TenantMembership).where(TenantMembership.user_id == "legacy-admin"))
    assert membership.role == "member"


async def test_explicit_admin_grant_mints_membership_role_in_cognito(native):
    service, pool, _ = native
    await service.create_user("native-home", UserCreateRequest(email="authorized-admin@example.com", role="org_admin"))
    assert pool.create_requests[0]["role"] == "org_admin"


async def test_real_app_recovery_routes_are_reachable_after_cloudfront_strip(api, native):
    client, application = api
    _, pool, _ = native
    mounted = {getattr(route, "path", "") for route in application.routes}
    prefix = "/admin/identity/organizations/{org_id}/users/{user_id}"
    assert {f"{prefix}/provision", f"{prefix}/cognito"} <= mounted
    assert f"/api{prefix}/provision" not in mounted
    assert f"/api{prefix}/cognito" not in mounted
    # Existing clients still compensate for the quarantined create endpoint.
    assert "/api/admin/identity/organizations/{org_id}/users" in mounted

    async def frontdoor(method, browser_path, body):
        assert browser_path.startswith("/api/")
        # Simulate the deployed viewer-request function stripping one leading
        # /api. ASGITransport exercises the real app registration/middleware.
        return await client.request(method, browser_path[len("/api") :], json=body)

    pool.create_failure = True
    failed = await frontdoor("POST", "/api/api/admin/identity/organizations/native-home/users", {"email": "frontdoor@example.com"})
    assert failed.status_code == 502
    details = failed.json()["details"]
    assert details["retry_path"].startswith("/admin/identity/")
    assert details["link_path"].startswith("/admin/identity/")
    pool.create_failure = False
    recovered = await frontdoor("POST", "/api" + details["retry_path"], {"send_invite": False})
    assert recovered.status_code == 200
    assert recovered.json()["id"] == details["user_id"]
    linked = await frontdoor(
        "PUT",
        "/api" + details["link_path"],
        {
            "username": "frontdoor@example.com",
            "expected_sub": recovered.json()["cognito_sub"],
        },
    )
    assert linked.status_code == 200
    assert linked.json()["id"] == details["user_id"]
