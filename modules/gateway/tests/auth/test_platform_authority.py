"""An old signed admin claim cannot undo a committed global demotion."""

from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError
from src.admin.platform_authority import apply_platform_admin_authority, current_cognito_admin, set_platform_admin_grant
from src.admin.service import AdminService
from src.auth import dependencies, middleware
from src.auth.auth_service import AuthService
from src.auth.cognito_jwt import CognitoTokenClaims
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, User
from src.shared.models.platform_admin import PlatformAdminGrant
from src.shared.schemas.admin import UserUpdateRequest
from src.shared.schemas.auth import TokenContext


@pytest.fixture
def admin_context():
    return TokenContext(
        user_id="login-sub",
        cognito_username="login-name",
        org_id="one",
        team_id="",
        department_id="",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )


@pytest.fixture
async def admin_user(db_session):
    db_session.add(Organization(id="one", name="One"))
    await db_session.flush()
    user = User(id="person", org_id="one", team_id="", email="admin@example.test", cognito_sub="login-sub", role="platform_admin")
    db_session.add(user)
    await db_session.flush()
    db_session.add(TenantMembership(user_id=user.id, tenant_id="one", role="org_admin", is_active=True))
    await db_session.commit()
    return user


async def test_demotion_revokes_old_token_even_when_cognito_sync_fails(db_session, admin_user, admin_context, monkeypatch):
    lookup = Mock(return_value=True)
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", lookup)
    monkeypatch.setattr("src.admin.service.sync_cognito_role_claims", Mock(return_value=False))
    monkeypatch.setattr("src.admin.service.project_member_org_ids", AsyncMock())
    before = await apply_platform_admin_authority(admin_context, db_session)
    assert await AccessControl(db_session).check_permission(before, Permission.ORG_DELETE, target_org_id="other")

    await AdminService(db_session).update_user("one", admin_user.id, UserUpdateRequest(role="member"))

    # The exact same token context and a freshly minted stale IdP claim are both
    # fenced by the durable decision, even though IdP synchronization failed.
    for context in (admin_context, admin_context.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(hours=1)})):
        after = await apply_platform_admin_authority(context, db_session)
        assert not after.is_admin
        with pytest.raises(AccessDeniedError):
            await AccessControl(db_session).check_permission(after, Permission.ORG_DELETE, target_org_id="other")
    assert (await db_session.get(PlatformAdminGrant, "login-sub")).enabled is False
    assert lookup.call_count == 1


@pytest.mark.parametrize("path", ["dependency", "middleware", "auth_service"])
async def test_every_authentication_path_resolves_the_same_unexpired_claim(db_session, admin_context, monkeypatch, path):
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", Mock(return_value=True))

    @asynccontextmanager
    async def session():
        yield db_session

    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: session)
    claims = CognitoTokenClaims(
        sub=admin_context.user_id,
        username=admin_context.cognito_username,
        org_id="one",
        role="platform_admin",
        iss="https://cognito-idp.us-east-1.amazonaws.com/us-east-1_example",
        token_use="access",
        client_id="client",
        exp=int(admin_context.expires_at.timestamp()),
        iat=int(datetime.now(UTC).timestamp()),
    )
    validator = SimpleNamespace(validate_token=Mock(return_value=claims))
    monkeypatch.setattr(dependencies, "_get_cognito_validator", lambda: validator)
    monkeypatch.setattr(middleware, "get_cognito_validator", lambda: validator)
    service = AuthService(cognito_validator=validator)
    service._cognito_enabled = True

    async def authenticate():
        if path == "dependency":
            request = Request({"type": "http", "method": "GET", "path": "/admin/organizations", "headers": []})
            return await dependencies.get_current_user(request, authorization="Bearer same-token")
        if path == "middleware":
            return await middleware.validate_cognito_jwt("Bearer same-token")
        return await service.validate_token("same-token", db_session)

    await set_platform_admin_grant(db_session, "login-sub", enabled=True)
    assert (await authenticate()).is_admin
    await set_platform_admin_grant(db_session, "login-sub", enabled=False)
    assert not (await authenticate()).is_admin


async def test_explicit_regrant_is_required_to_clear_a_revocation(db_session, admin_context, monkeypatch):
    lookup = Mock(side_effect=AssertionError("A token must not restore a revoked grant"))
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", lookup)
    await set_platform_admin_grant(db_session, "login-sub", enabled=False)
    assert not (await apply_platform_admin_authority(admin_context, db_session)).is_admin
    await set_platform_admin_grant(db_session, "login-sub", enabled=True)
    lookup.side_effect = None
    lookup.return_value = True
    assert (await apply_platform_admin_authority(admin_context, db_session)).is_admin
    lookup.assert_called_once_with("login-sub", "login-name")


async def test_database_grant_does_not_override_idp_offboarding(db_session, admin_context, monkeypatch):
    await set_platform_admin_grant(db_session, "login-sub", enabled=True)
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", Mock(return_value=False))
    assert not (await apply_platform_admin_authority(admin_context, db_session)).is_admin


async def test_existing_admin_uses_current_idp_state_without_a_positive_cache(db_session, admin_context, monkeypatch):
    lookup = Mock(side_effect=[True, False])
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", lookup)
    assert (await apply_platform_admin_authority(admin_context, db_session)).is_admin
    assert not (await apply_platform_admin_authority(admin_context, db_session)).is_admin
    assert await db_session.get(PlatformAdminGrant, "login-sub") is None


async def test_authority_outage_cannot_fall_back_to_the_claim(db_session, admin_context, monkeypatch):
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", Mock(side_effect=OSError("unavailable")))
    with pytest.raises(HTTPException) as error:
        await apply_platform_admin_authority(admin_context, db_session)
    assert error.value.status_code == 503
    broken_db = SimpleNamespace(scalar=AsyncMock(side_effect=OSError("database unavailable")))
    with pytest.raises(HTTPException) as error:
        await apply_platform_admin_authority(admin_context, broken_db)
    assert error.value.status_code == 503


@pytest.mark.parametrize("changes", [{"is_admin": False}, {"account_type": "service"}, {"auth_source": "iam"}])
async def test_non_admin_and_service_callers_do_not_acquire_global_authority(admin_context, changes):
    db = SimpleNamespace(scalar=AsyncMock(side_effect=AssertionError("Unexpected authority lookup")))
    assert not (await apply_platform_admin_authority(admin_context.model_copy(update=changes), db)).is_admin
    db.scalar.assert_not_called()


async def test_legacy_exchange_admin_claim_never_grants_platform_authority(db_session, admin_context):
    manager = SimpleNamespace(validate_token=AsyncMock(return_value=admin_context))
    service = AuthService(token_manager=manager)
    service._cognito_enabled = False
    assert not (await service.validate_token("legacy-token", db_session)).is_admin


@pytest.mark.parametrize(
    "enabled,subject,role,groups,expected",
    [
        (True, "login-sub", "platform_admin", [], True),
        (True, "login-sub", "org_admin", [], False),
        (True, "login-sub", "member", ["admins"], True),
        (False, "login-sub", "platform_admin", ["admins"], False),
        (True, "other-sub", "platform_admin", ["admins"], False),
    ],
)
def test_existing_idp_authority_requires_the_same_enabled_login(monkeypatch, enabled, subject, role, groups, expected):
    client = Mock()
    client.admin_get_user.return_value = {
        "Username": "login-name",
        "Enabled": enabled,
        "UserAttributes": [{"Name": "sub", "Value": subject}, {"Name": "custom:role", "Value": role}],
    }
    client.admin_list_groups_for_user.side_effect = [
        {"Groups": [], "NextToken": "next"},
        {"Groups": [{"GroupName": group} for group in groups]},
    ]
    monkeypatch.setattr("src.admin.platform_authority.get_settings", lambda: SimpleNamespace(cognito_user_pool_id="pool", aws_region="us-east-1"))
    monkeypatch.setattr("src.admin.platform_authority.boto3.client", lambda *a, **kw: client)
    assert current_cognito_admin("login-sub", "login-name") is expected


@pytest.mark.parametrize(
    "enabled,subject,role,expected",
    [
        (True, "login-sub", "platform_admin", True),
        (False, "login-sub", "platform_admin", False),
        (True, "other-sub", "platform_admin", False),
        (True, "login-sub", "member", False),
    ],
)
def test_federated_subject_without_username_retains_live_authority_checks(monkeypatch, enabled, subject, role, expected):
    class UserNotFoundError(Exception):
        pass

    client = Mock()
    client.exceptions.UserNotFoundException = UserNotFoundError
    client.admin_get_user.side_effect = [
        UserNotFoundError(),
        {
            "Username": "github_12345",
            "Enabled": enabled,
            "UserAttributes": [{"Name": "sub", "Value": subject}, {"Name": "custom:role", "Value": role}],
        },
    ]
    client.list_users.return_value = {"Users": [{"Username": "github_12345"}]}
    client.admin_list_groups_for_user.return_value = {"Groups": []}
    monkeypatch.setattr("src.admin.platform_authority.get_settings", lambda: SimpleNamespace(cognito_user_pool_id="pool", aws_region="us-east-1"))
    monkeypatch.setattr("src.admin.platform_authority.boto3.client", lambda *a, **kw: client)

    assert current_cognito_admin("login-sub") is expected
    client.list_users.assert_called_once_with(UserPoolId="pool", Filter='sub = "login-sub"', Limit=1)
    assert client.admin_get_user.call_args_list[-1].kwargs == {"UserPoolId": "pool", "Username": "github_12345"}


@pytest.mark.parametrize("failure", ["missing", "deleted_during_lookup", "lookup_unavailable", "explicit_username"])
def test_missing_federated_login_cannot_gain_authority(monkeypatch, failure):
    class UserNotFoundError(Exception):
        pass

    client = Mock()
    client.exceptions.UserNotFoundException = UserNotFoundError
    client.admin_get_user.side_effect = UserNotFoundError()
    client.list_users.return_value = {"Users": [] if failure == "missing" else [{"Username": "github_12345"}]}
    monkeypatch.setattr("src.admin.platform_authority.get_settings", lambda: SimpleNamespace(cognito_user_pool_id="pool", aws_region="us-east-1"))
    monkeypatch.setattr("src.admin.platform_authority.boto3.client", lambda *a, **kw: client)
    if failure == "lookup_unavailable":
        client.list_users.side_effect = OSError("Cognito unavailable")
        with pytest.raises(OSError):
            current_cognito_admin("login-sub")
    else:
        assert not current_cognito_admin("login-sub", "explicit-name" if failure == "explicit_username" else "")
    if failure == "explicit_username":
        client.list_users.assert_not_called()
    client.admin_list_groups_for_user.assert_not_called()


def test_global_demotion_clears_privileged_groups_for_refreshed_tokens(monkeypatch):
    from src.admin.cognito_claims import sync_cognito_role_claims

    client = Mock()
    client.list_users.return_value = {
        "Users": [
            {
                "Username": "login-name",
                "Attributes": [{"Name": "sub", "Value": "login-sub"}, {"Name": "custom:org_id", "Value": "other"}],
            }
        ]
    }
    monkeypatch.setattr("src.admin.cognito_claims.cognito_user_pool_id", lambda: "pool")
    monkeypatch.setattr("boto3.client", lambda *a, **kw: client)
    assert sync_cognito_role_claims(
        cognito_sub="login-sub",
        org_id="one",
        role="member",
        team_id="",
        previous_role="platform_admin",
        only_if_current_org=True,
        workspace_roles={"other": "member"},
    )
    assert {call.kwargs["GroupName"] for call in client.admin_remove_user_from_group.call_args_list} == {"admins", "platform-admins"}


async def test_idp_wait_releases_the_only_pool_slot_and_observes_concurrent_revocation(admin_context, monkeypatch, tmp_path):
    import asyncio
    import threading

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'authority.db'}", pool_size=1, max_overflow=0, pool_timeout=0.2)
    async with engine.begin() as connection:
        await connection.run_sync(PlatformAdminGrant.__table__.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: factory)
    entered = threading.Event()
    release = threading.Event()

    def idp(*_args):
        entered.set()
        assert release.wait(5)
        return True

    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", idp)
    check = asyncio.create_task(apply_platform_admin_authority(admin_context))
    try:
        assert await asyncio.to_thread(entered.wait, 3)
        # The demotion must acquire the same single slot while IdP is blocked.
        async with factory() as writer:
            await set_platform_admin_grant(writer, admin_context.user_id, enabled=False)
            await writer.commit()
        release.set()
        assert not (await asyncio.wait_for(check, 3)).is_admin
    finally:
        release.set()
        await asyncio.gather(check, return_exceptions=True)
        await engine.dispose()
