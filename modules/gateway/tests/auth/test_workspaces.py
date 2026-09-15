"""Human workspace switching: login proof, token scope, claims and failure atomicity."""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError, MemberRemovalConflictError
from src.admin.org_members import add_user_to_org
from src.admin.service import AdminService
from src.auth.dependencies import get_current_user
from src.auth.workspaces import CognitoWorkspaceClaims, get_workspace_claims, list_workspaces, router, select_workspace
from src.budget.person_ledger import resolve_person_identity
from src.proxy.bedrock_routing import BedrockRoutingResolver
from src.shared.database import get_db
from src.shared.identity.resolver import resolve_user_entity_id
from src.shared.identity.workspaces import link_login_to_workspace, workspace_user
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext


def context(**kwargs):
    return TokenContext(
        **dict(
            user_id="login-sub",
            org_id="home",
            team_id="old-team",
            department_id="old-dept",
            account_type="human",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            **kwargs,
        )
    )


@pytest.fixture
async def seeded(db_session):
    db_session.add_all([Organization(id="home", name="Personal org"), Organization(id="work", name="SOPHOS-IT")])
    await db_session.flush()
    user = User(id="login-row", org_id="home", team_id="", email="person@example.com", cognito_sub="login-sub")
    db_session.add(user)
    await db_session.flush()
    db_session.add(TenantMembership(user_id=user.id, tenant_id="home", role="org_admin", is_active=True))
    await db_session.commit()
    return user


@pytest.fixture
def claims():
    previous = {"custom:org_id": "home", "custom:team_id": "old-team", "custom:department_id": "old-dept", "custom:role": "org_admin"}
    return MagicMock(set=AsyncMock(side_effect=lambda subject, values: (previous, values)))


async def place(db, user, role="member"):
    target = await add_user_to_org(db, user_id=user.id, org_id="work", role=role)
    await db.commit()
    return target


@pytest.mark.asyncio
async def test_native_placement_lists_and_switches_without_github(db_session, seeded, claims):
    target = await place(db_session, seeded)
    listed = await list_workspaces(context(), db_session)
    assert {item.name for item in listed.items} == {"Personal org", "SOPHOS-IT"}
    assert [item.org_id for item in listed.items if item.is_current] == ["home"]
    selected = await select_workspace(db_session, context(), "work", claims)
    assert selected.user_id == target.id and selected.role == "member"
    claims.set.assert_awaited_once_with(
        "login-sub", {"custom:org_id": "work", "custom:team_id": "", "custom:department_id": "", "custom:role": "member"}
    )
    assert selected.is_current
    # Cognito sub remains the spend identity; org-local row is only used for FK/role/routing.
    assert target.cognito_sub is None
    assert await resolve_user_entity_id(db_session, "work", target.id) == "login-sub"
    assert await resolve_user_entity_id(db_session, "work", "login-sub") == "login-sub"
    active = (await db_session.scalars(select(TenantMembership).where(TenantMembership.is_active.is_(True)))).all()
    assert [m.user_id for m in active] == [target.id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "can_select"),
    [
        ("GitHub_123", True),
        ("github_123", True),
        ("GITHUB_123", True),
        ("github_123_extra", False),
        ("github_", False),
        ("github_１２３", False),
        ("gitlab_123", False),
    ],
)
async def test_legacy_github_secondary_row_requires_signed_numeric_username(db_session, seeded, claims, username, can_select):
    target = User(id="github-work", org_id="work", team_id="", email="different@example.com")
    db_session.add(target)
    await db_session.flush()
    db_session.add_all(
        [
            UserIdentity(user_id=target.id, org_id="work", team_id="", provider="github", provider_user_id="123", verification_method="oauth"),
            TenantMembership(user_id=target.id, tenant_id="work", role="member"),
        ]
    )
    await db_session.commit()
    assert [item.org_id for item in (await list_workspaces(context(), db_session)).items] == ["home"]
    signed = context(cognito_username=username)
    expected_orgs = {"home", "work"} if can_select else {"home"}
    assert {item.org_id for item in (await list_workspaces(signed, db_session)).items} == expected_orgs
    if not can_select:
        with pytest.raises(HTTPException) as exc:
            await select_workspace(db_session, signed, "work", claims)
        assert exc.value.status_code == 403
        claims.set.assert_not_awaited()
        return
    selected = await select_workspace(db_session, signed, "work", claims)
    assert selected.user_id == target.id
    # The proven switch creates an explicit login link for downstream resolution.
    assert (await workspace_user(db_session, "login-sub", "work")).id == target.id


@pytest.mark.asyncio
async def test_target_primary_team_and_old_session_role_stay_in_their_org(db_session, seeded, claims):
    target = await place(db_session, seeded)
    db_session.add(Department(id="dept-work", org_id="work", name="Engineering"))
    await db_session.flush()
    db_session.add_all(
        [
            Team(id="team-work", org_id="work", department_id="dept-work", name="Platform"),
            Team(id="team-other", org_id="work", department_id="dept-work", name="Other"),
        ]
    )
    await db_session.flush()
    db_session.add_all(
        [
            TeamMembership(user_id=target.id, team_id="team-work", org_id="work", is_primary=True),
            TeamMembership(user_id=target.id, team_id="team-other", org_id="work", is_primary=False),
        ]
    )
    await db_session.commit()
    old = context()
    selected = await select_workspace(db_session, old, "work", claims)
    assert (selected.team_id, selected.department_id) == ("team-work", "dept-work")
    new = old.model_copy(update={"org_id": "work", "attributed_org_id": "work", "team_id": selected.team_id, "department_id": selected.department_id})
    access = AccessControl(db_session)
    assert await access.check_permission(old, Permission.USER_MANAGE, target_org_id="home")
    with pytest.raises(AccessDeniedError):
        await access.check_permission(new, Permission.USER_MANAGE, target_org_id="work")
    with pytest.raises(InvalidScopeError):
        await access.check_permission(old, Permission.USER_MANAGE, target_org_id="work")
    assert await BedrockRoutingResolver.resolve_canonical_user_id(db_session, old) == seeded.id
    assert await BedrockRoutingResolver.resolve_canonical_user_id(db_session, new) == target.id
    assert (old.org_id, old.team_id, old.department_id) == ("home", "old-team", "old-dept")


@pytest.mark.asyncio
async def test_platform_role_preserved_without_promoting_membership(db_session, seeded, claims):
    target = await place(db_session, seeded)
    # The writer preserves the current Cognito role, independently of the token.
    claims.set.side_effect = lambda subject, values: ({}, {**values, "custom:role": "platform_admin"})
    selected = await select_workspace(db_session, context(is_admin=True), "work", claims)
    assert selected.role == "platform_admin"
    assert claims.set.await_args.args[1]["custom:role"] == "member"
    assert await db_session.scalar(select(TenantMembership.role).where(TenantMembership.user_id == target.id)) == "member"


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [{"account_type": "service"}, {"auth_source": "iam"}, {"org_id": "forged-org"}])
async def test_reject_nonhuman_and_unknown_target(db_session, seeded, claims, updates):
    await place(db_session, seeded)
    token = context().model_copy(update=updates)
    target = "forged-org" if "org_id" in updates else "work"
    with pytest.raises(HTTPException) as exc:
        await select_workspace(db_session, token, target, claims)
    assert exc.value.status_code == 403
    claims.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_mutable_email_and_self_linked_identities_do_not_grant_access(db_session, seeded, claims):
    target = User(id="other", org_id="work", team_id="", email=seeded.email)
    db_session.add(target)
    await db_session.flush()
    db_session.add_all(
        [
            TenantMembership(user_id=target.id, tenant_id="work", role="org_admin"),
            UserIdentity(
                user_id=target.id, org_id="work", team_id="", provider="cognito", provider_user_id="login-sub", verification_method="manual"
            ),
            UserIdentity(user_id=seeded.id, org_id="home", team_id="", provider="github", provider_user_id="999", verification_method="manual"),
            UserIdentity(user_id=target.id, org_id="work", team_id="", provider="github", provider_user_id="999", verification_method="manual"),
        ]
    )
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await select_workspace(db_session, context(), "work", claims)
    assert exc.value.status_code == 403
    claims.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_placement_cannot_join_accounts_owned_by_different_logins(db_session, seeded):
    target = User(id="other", org_id="work", team_id="", email=seeded.email)
    db_session.add(target)
    await db_session.flush()
    db_session.add(
        UserIdentity(
            user_id=target.id, org_id="work", team_id="", provider="cognito", provider_user_id="someone-else", verification_method="org_placement"
        )
    )
    await db_session.commit()
    with pytest.raises(ValueError, match="different login"):
        await link_login_to_workspace(db_session, seeded, target)


@pytest.mark.asyncio
async def test_cognito_failure_rolls_back_active_membership(db_session, seeded, claims):
    await place(db_session, seeded)
    claims.set.side_effect = RuntimeError("Cognito unavailable")
    with pytest.raises(HTTPException) as exc:
        await select_workspace(db_session, context(), "work", claims)
    assert exc.value.status_code == 503
    active = (await db_session.scalars(select(TenantMembership).where(TenantMembership.is_active.is_(True)))).all()
    # Placement creates an active membership on each separate user row. Rollback
    # restores that previous state rather than persisting the attempted switch.
    assert {m.tenant_id for m in active} == {"home", "work"}


@pytest.mark.asyncio
async def test_commit_failure_reconciles_current_committed_workspace_claims(db_session, seeded, claims):
    await place(db_session, seeded)
    with patch.object(db_session, "commit", AsyncMock(side_effect=RuntimeError("database disconnected"))):
        with pytest.raises(HTTPException) as exc:
            await select_workspace(db_session, context(), "work", claims)
    assert exc.value.status_code == 503
    assert claims.set.await_count == 2
    assert claims.set.await_args_list[-1].args == (
        "login-sub",
        {"custom:org_id": "home", "custom:team_id": "", "custom:department_id": "", "custom:role": "org_admin"},
    )


@pytest.mark.asyncio
async def test_removed_membership_cannot_be_selected(db_session, seeded, claims):
    target = await place(db_session, seeded)
    await db_session.execute(delete(TenantMembership).where(TenantMembership.user_id == target.id))
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await select_workspace(db_session, context(), "work", claims)
    assert exc.value.status_code == 403
    claims.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_person_spend_is_shared_and_login_deletion_is_protected(db_session, seeded):
    target = await place(db_session, seeded)
    home_person = await resolve_person_identity(db_session, seeded.id)
    work_person = await resolve_person_identity(db_session, target.id)
    assert home_person == work_person
    assert set(home_person[1]) == {seeded.id, target.id}
    service = AdminService(db_session)
    assert (await service.get_user_authz_state("work", target.id)).cognito_sub == "login-sub"
    with pytest.raises(MemberRemovalConflictError, match="sign-in"):
        await service.remove_user("home", seeded.id)


@pytest.mark.asyncio
async def test_http_membership_selector_and_claim_writer(db_session, seeded, claims):
    await place(db_session, seeded)
    app = FastAPI()
    app.include_router(router, prefix="/auth")
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = context
    app.dependency_overrides[get_workspace_claims] = lambda: claims
    # The fixture callable has **kwargs, so wrap to expose no request parameters.
    app.dependency_overrides[get_current_user] = lambda: context()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/auth/workspaces")
        assert response.status_code == 200 and len(response.json()["items"]) == 2
        response = await client.post("/auth/workspaces/select", json={"org_id": "work"})
        assert response.status_code == 200 and response.json()["org_id"] == "work"
        assert (await client.post("/auth/workspaces/select", json={"org_id": ""})).status_code == 422


def test_strict_writer_uses_verified_sub_and_persists_refresh_claims(monkeypatch):
    client = MagicMock()
    client.list_users.return_value = {
        "Users": [{"Username": "real-login-name", "Attributes": [{"Name": "sub", "Value": "login-sub"}, {"Name": "custom:org_id", "Value": "home"}]}]
    }
    monkeypatch.setattr("src.auth.workspaces.cognito_user_pool_id", lambda: "pool")
    client.admin_get_user.return_value = {"UserAttributes": client.list_users.return_value["Users"][0]["Attributes"]}
    values = {"custom:org_id": "work", "custom:team_id": "", "custom:department_id": "", "custom:role": "member"}
    with patch("boto3.client", return_value=client):
        previous, applied = CognitoWorkspaceClaims()._set("login-sub", values)
    assert previous["custom:org_id"] == "home"
    call = client.admin_update_user_attributes.call_args.kwargs
    assert call["Username"] == "real-login-name"
    persisted = {item["Name"]: item["Value"] for item in call["UserAttributes"]}
    assert persisted == values == applied
    # Exercise the actual pre-token Lambda: refresh and later login inherit the
    # saved org/role and omit cleared team/dept claims.
    path = Path(__file__).parents[2] / "infra/modules/cognito/lambda/pre_token_generation.py"
    spec = importlib.util.spec_from_file_location("workspace_pre_token", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    event = module.handle_user_token_generation({"request": {"userAttributes": persisted}})
    token = event["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]["claimsToAddOrOverride"]
    assert token["custom:org_id"] == "work" and token["custom:role"] == "member"
    assert "custom:team_id" not in token and "custom:department_id" not in token


@pytest.mark.asyncio
async def test_refreshed_context_scopes_costs_routes_and_credentials(db_session, seeded, claims):
    from src.auth.aws_connect_routes import _resolve_user_id
    from src.auth.cognito_jwt import CognitoTokenClaims
    from src.auth.dependencies import _cognito_claims_to_context
    from src.auth.vault_routes import list_credentials_endpoint
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.ratelimit.service import RateLimitService
    from src.shared.models.bedrock_routing import BedrockAccountMapping, BedrockDestinationRegistry
    from src.shared.models.vault import UserCredential

    target = await place(db_session, seeded)
    for org, user, account in [("home", seeded, "111111111111"), ("work", target, "222222222222")]:
        db_session.add(
            UserCredential(
                id=f"cred-{org}",
                org_id=org,
                user_id=user.id,
                service="aws",
                credential_type="iam_role",
                label=org,
                secret_arn=f"arn:aws:secretsmanager:us-east-1:{account}:secret:{org}",
            )
        )
        db_session.add(
            BedrockDestinationRegistry(
                id=f"dest-{org}",
                label=org,
                account_id=account,
                owner_org_id=org,
                role_arn=f"arn:aws:iam::{account}:role/adp",
                region="us-east-1",
                routing_capable=True,
                verified_at=datetime.now(UTC),
                registered_by_user_id="admin",
            )
        )
    await db_session.flush()
    db_session.add_all(
        [
            BedrockAccountMapping(id="map-home", destination_id="dest-home", scope_type="user", scope_id_user=seeded.id, authored_by_user_id="admin"),
            BedrockAccountMapping(id="map-work", destination_id="dest-work", scope_type="user", scope_id_user=target.id, authored_by_user_id="admin"),
        ]
    )
    await db_session.commit()
    selected = await select_workspace(db_session, context(), "work", claims)
    refreshed = _cognito_claims_to_context(
        CognitoTokenClaims(
            sub="login-sub",
            iss="test",
            client_id="test",
            token_use="access",
            exp=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            iat=0,
            org_id=selected.org_id,
            role=selected.role,
            team_id=selected.team_id,
            department_id=selected.department_id,
            username="native-login",
        )
    )
    assert refreshed.user_id == "login-sub" and refreshed.attributed_org_id == "work"
    assert refreshed.cognito_username == "native-login"
    budgets = BudgetEnforcementService._get_entity_hierarchy(None, refreshed)
    rates = RateLimitService._get_hierarchy_entities(None, refreshed)
    assert {str(entity.value): key for entity, key in budgets} == {"user": "login-sub", "org": "work"}
    assert {str(entity.value): key for entity, key in rates} == {"user": "login-sub", "org": "work"}
    resolver = BedrockRoutingResolver()
    assert (await resolver.resolve(db_session, context())).account_id == "111111111111"
    assert (await resolver.resolve(db_session, refreshed)).account_id == "222222222222"
    assert await _resolve_user_id("login-sub", db_session, org_id="work") == target.id
    visible = await list_credentials_endpoint(scope="user", token_context=refreshed.model_copy(), db=db_session)
    assert [credential.id for credential in visible] == ["cred-work"]


@pytest.mark.asyncio
@pytest.mark.parametrize("username", ["GitHub_123", "github_123"])
async def test_ambiguous_login_matches_fail_closed(db_session, seeded, claims, username):
    await place(db_session, seeded)
    another = User(id="another-work", org_id="work", team_id="", email="other@example.com")
    db_session.add(another)
    await db_session.flush()
    db_session.add_all(
        [
            TenantMembership(user_id=another.id, tenant_id="work", role="org_admin"),
            UserIdentity(user_id=another.id, org_id="work", team_id="", provider="github", provider_user_id="123", verification_method="oauth"),
        ]
    )
    await db_session.commit()
    with pytest.raises(HTTPException) as exc:
        await select_workspace(db_session, context(cognito_username=username), "work", claims)
    assert exc.value.status_code == 409
    claims.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_role_updates_do_not_overwrite_selected_workspace(db_session, seeded, monkeypatch):
    from src.shared.schemas.admin import UserUpdateRequest

    target = await place(db_session, seeded)
    client = MagicMock()
    client.list_users.return_value = {
        "Users": [{"Username": "native-login", "Attributes": [{"Name": "sub", "Value": "login-sub"}, {"Name": "custom:org_id", "Value": "work"}]}]
    }
    monkeypatch.setenv("BG_COGNITO_USER_POOL_ID", "pool")
    with patch("boto3.client", return_value=client):
        # Editing the original account must not move this login back to home.
        await AdminService(db_session).update_user("home", seeded.id, UserUpdateRequest(role="member"))
        client.admin_update_user_attributes.assert_not_called()
        # A role update on the selected secondary native account reaches the
        # same Cognito login and leaves all workspace/team attributes intact.
        await AdminService(db_session).update_user("work", target.id, UserUpdateRequest(role="org_admin"))
    client.admin_update_user_attributes.assert_called_once_with(
        UserPoolId="pool", Username="native-login", UserAttributes=[{"Name": "custom:role", "Value": "org_admin"}]
    )


@pytest.mark.asyncio
async def test_legacy_membership_keeps_personal_routing_on_the_login_user(db_session, seeded):
    from src.admin.bedrock_routing.self_routes import _caller_id

    # A legacy membership grants workspace access without a local user row.
    # Workspace-scoped resolution still has no placement, while personal Bedrock
    # routing stays anchored to the login user across workspace switches (#5170).
    db_session.add(TenantMembership(user_id=seeded.id, tenant_id="work", role="member"))
    await db_session.commit()
    other = context().model_copy(update={"org_id": "work", "attributed_org_id": "work"})
    assert await BedrockRoutingResolver.resolve_canonical_user_id(db_session, other) is None
    assert await _caller_id(db_session, other) == seeded.id
    assert await BedrockRoutingResolver.resolve_canonical_user_id(db_session, context()) == seeded.id


@pytest.mark.asyncio
async def test_platform_role_edits_preserve_workspace_and_restore_its_role(db_session, seeded, monkeypatch):
    from src.shared.schemas.admin import UserUpdateRequest

    target = await place(db_session, seeded, role="org_admin")
    client = MagicMock()
    persisted = {"sub": "login-sub", "custom:org_id": "work", "custom:role": "org_admin"}
    client.list_users.side_effect = lambda **kwargs: {
        "Users": [{"Username": "native-login", "Attributes": [{"Name": key, "Value": value} for key, value in persisted.items()]}]
    }

    def update(**kwargs):
        persisted.update({item["Name"]: item["Value"] for item in kwargs["UserAttributes"]})

    client.admin_update_user_attributes.side_effect = update
    monkeypatch.setenv("BG_COGNITO_USER_POOL_ID", "pool")
    with patch("boto3.client", return_value=client):
        service = AdminService(db_session)
        # Global promotion applies even while another workspace is selected.
        await service.update_user("home", seeded.id, UserUpdateRequest(role="platform_admin"))
        assert persisted["custom:org_id"] == "work" and persisted["custom:role"] == "platform_admin"
        # An ordinary edit to the selected org's membership preserves that grant.
        await service.update_user("work", target.id, UserUpdateRequest(role="dept_admin"))
        assert persisted["custom:role"] == "platform_admin"
        # Explicit global demotion restores WORK's role, not HOME's new member role.
        await service.update_user("home", seeded.id, UserUpdateRequest(role="member"))
        assert persisted["custom:org_id"] == "work" and persisted["custom:role"] == "dept_admin"


@pytest.mark.asyncio
async def test_stale_platform_token_cannot_restore_revoked_global_role(db_session, seeded, claims):
    await place(db_session, seeded)
    selected = await select_workspace(db_session, context(is_admin=True), "work", claims)
    assert selected.role == "member"
    assert claims.set.await_args.args[1]["custom:role"] == "member"


def test_switch_writer_preserves_only_current_platform_authority(monkeypatch):
    client = MagicMock()
    attributes = [{"Name": "sub", "Value": "login-sub"}, {"Name": "custom:role", "Value": "platform_admin"}]
    client.list_users.return_value = {"Users": [{"Username": "native-login", "Attributes": attributes}]}
    client.admin_get_user.return_value = {"UserAttributes": attributes}
    monkeypatch.setattr("src.auth.workspaces.cognito_user_pool_id", lambda: "pool")
    with patch("boto3.client", return_value=client):
        _, applied = CognitoWorkspaceClaims()._set("login-sub", {"custom:org_id": "work", "custom:role": "member"})
        assert applied["custom:role"] == "platform_admin"
        attributes[1]["Value"] = "member"
        with pytest.raises(RuntimeError, match="Platform role changed"):
            CognitoWorkspaceClaims()._set("login-sub", {"custom:org_id": "home", "custom:role": "platform_admin"})
    client.admin_update_user_attributes.assert_called_once()
