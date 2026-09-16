"""Actual membership HTTP writers -> Cognito attributes -> refresh -> gateway scope.

Cognito is a stateful service double; no AWS calls or real identities are used.
The original token is retained to show this does not revoke issued JWTs.
"""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from src.admin.config import AdminConfig, set_admin_config
from src.admin.routes import router
from src.auth.cognito_jwt import CognitoTokenClaims
from src.auth.dependencies import _cognito_claims_to_context, get_current_user
from src.auth.workspaces import CognitoWorkspaceClaims, select_workspace
from src.budget.enforcement_service import BudgetEnforcementService
from src.proxy.bedrock_routing import BedrockRoutingResolver
from src.ratelimit.service import RateLimitService
from src.shared.database import get_db
from src.shared.identity.workspaces import link_login_to_workspace
from src.shared.models.bedrock_routing import BedrockAccountMapping, BedrockDestinationRegistry
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext


class CognitoState:
    def __init__(self):
        self.attributes = {
            "sub": "login-sub",
            "custom:org_id": "a",
            "custom:team_id": "a1",
            "custom:department_id": "d-a1",
            "custom:role": "member",
            "custom:account_type": "human",
            "email": "unchanged@example.test",
            "custom:github_username": "unchanged",
            "custom:member_org_ids": "a,b",
        }
        self.writes = []
        self.failure = None
        self.list_attributes = None

    def list_users(self, **kwargs):
        assert kwargs["Filter"] == 'sub = "login-sub"'
        attrs = self.list_attributes if self.list_attributes is not None else self.attributes
        return {"Users": [{"Username": "canonical-login", "Attributes": [{"Name": k, "Value": v} for k, v in attrs.items()]}]}

    def admin_get_user(self, **kwargs):
        assert kwargs["Username"] == "canonical-login"
        return {"Username": "canonical-login", "UserAttributes": [{"Name": k, "Value": v} for k, v in self.attributes.items()]}

    def admin_update_user_attributes(self, **kwargs):
        assert kwargs["Username"] == "canonical-login"
        if self.failure == "before":
            raise RuntimeError("Cognito unavailable")
        values = {a["Name"]: a["Value"] for a in kwargs["UserAttributes"]}
        self.attributes.update(values)
        self.writes.append(values)
        if self.failure == "after":
            raise RuntimeError("Cognito response lost after update")


def actor(org="a", *, admin=False):
    return TokenContext(
        user_id="login-sub",
        org_id=org,
        team_id="a1",
        department_id="d-a1",
        account_type="human",
        cognito_username="canonical-login",
        auth_source="jwt",
        is_admin=admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


def refreshed(cognito):
    path = Path(__file__).parents[2] / "infra/modules/cognito/lambda/pre_token_generation.py"
    spec = importlib.util.spec_from_file_location("primary_team_pre_token", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    event = module.handle_user_token_generation(
        {"triggerSource": "TokenGeneration_RefreshTokens", "request": {"userAttributes": dict(cognito.attributes)}}
    )
    claims = event["response"]["claimsAndScopeOverrideDetails"]["accessTokenGeneration"]["claimsToAddOrOverride"]
    return _cognito_claims_to_context(
        CognitoTokenClaims(
            sub="login-sub",
            iss="test",
            client_id="test",
            token_use="access",
            exp=int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
            iat=0,
            username="canonical-login",
            **{key.removeprefix("custom:"): value for key, value in claims.items()},
        )
    )


async def seed(db):
    db.add_all([Organization(id=org, name=org) for org in ("a", "b")])
    await db.flush()
    db.add_all([Department(id="d-" + team, org_id=team[0], name=team) for team in ("a1", "a2", "b1", "b2")])
    await db.flush()
    db.add_all([Team(id=team, org_id=team[0], department_id="d-" + team, name=team) for team in ("a1", "a2", "b1", "b2")])
    await db.flush()
    login = User(id="login", org_id="a", team_id="a1", email="same@example.test", cognito_sub="login-sub", cognito_username="canonical-login")
    local = User(id="local-b", org_id="b", team_id="b1", email="same@example.test")
    db.add_all([login, local])
    await db.flush()
    await link_login_to_workspace(db, login, local)
    db.add_all(
        [
            TenantMembership(user_id="login", tenant_id="a", role="member", is_active=True),
            TenantMembership(user_id="local-b", tenant_id="b", role="member", is_active=False),
            TeamMembership(user_id="login", team_id="a1", org_id="a", role="member", is_primary=True),
            TeamMembership(user_id="local-b", team_id="b1", org_id="b", role="member", is_primary=True),
        ]
    )
    await db.commit()


@pytest.fixture
def cognito(monkeypatch):
    state = CognitoState()
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: state)
    monkeypatch.setattr("src.auth.workspaces.cognito_user_pool_id", lambda: "test-pool")
    return state


@pytest.fixture
async def client(db_session, cognito):
    set_admin_config(AdminConfig(rbac_least_privilege_default=True))
    await seed(db_session)
    # Establish selection through the real product service and claims adapter.
    await select_workspace(db_session, actor(), "a", CognitoWorkspaceClaims())
    cognito.writes.clear()
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_db] = lambda: db_session
    app.dependency_overrides[get_current_user] = lambda: actor(admin=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        yield http
    set_admin_config(AdminConfig())


async def replace(client, org="a", user="login", teams=("a1", "a2"), primary="a2"):
    return await client.put(
        f"/admin/organizations/{org}/users/{user}/teams", json={"memberships": [{"team_id": team, "is_primary": team == primary} for team in teams]}
    )


async def test_primary_change_refreshes_department_and_real_consumers_without_reselection(client, db_session, cognito):
    old = refreshed(cognito)
    preserved = {k: v for k, v in cognito.attributes.items() if k not in {"custom:team_id", "custom:department_id"}}
    for team, account in [("a1", "111111111111"), ("a2", "222222222222")]:
        db_session.add(
            BedrockDestinationRegistry(
                id="dest-" + team,
                label=team,
                account_id=account,
                owner_org_id="a",
                role_arn=f"arn:aws:iam::{account}:role/adp",
                region="us-east-1",
                routing_capable=True,
                verified_at=datetime.now(UTC),
                registered_by_user_id="admin",
            )
        )
    await db_session.flush()
    for team in ("a1", "a2"):
        db_session.add(
            BedrockAccountMapping(
                id="map-" + team, destination_id="dest-" + team, scope_type="team", scope_id_team=team, scope_id_org="a", authored_by_user_id="admin"
            )
        )
    await db_session.commit()
    response = await replace(client)
    assert response.status_code == 200, response.text
    assert (await db_session.get(User, "login")).team_id == "a2"
    fresh = refreshed(cognito)
    assert (fresh.org_id, fresh.team_id, fresh.department_id, fresh.is_admin) == ("a", "a2", "d-a2", False)
    assert {k: v for k, v in cognito.attributes.items() if k not in {"custom:team_id", "custom:department_id"}} == preserved
    assert cognito.writes == [{"custom:team_id": "a2", "custom:department_id": "d-a2"}]
    for entities in (BudgetEnforcementService._get_entity_hierarchy(None, fresh), RateLimitService._get_hierarchy_entities(None, fresh)):
        assert {entity.value: key for entity, key in entities} == {"user": "login-sub", "team": "a2", "department": "d-a2", "org": "a"}
    resolver = BedrockRoutingResolver()
    assert (await resolver.resolve(db_session, old)).account_id == "111111111111"  # No retroactive JWT revocation.
    assert (await resolver.resolve(db_session, fresh)).account_id == "222222222222"


async def test_nonprimary_addition_does_not_write_claims(client, cognito):
    response = await client.post("/admin/organizations/a/teams/a2/members", json={"user_id": "login"})
    assert response.status_code == 201, response.text
    assert cognito.writes == []
    assert refreshed(cognito).team_id == "a1"


async def test_nonselected_org_change_keeps_login_selection_even_with_stale_listusers(client, db_session, cognito):
    cognito.list_attributes = {**cognito.attributes, "custom:org_id": "b"}
    response = await replace(client, "b", "local-b", ("b1", "b2"), "b2")
    assert response.status_code == 200, response.text
    assert (await db_session.get(User, "local-b")).team_id == "b2"
    assert cognito.writes == []
    assert (refreshed(cognito).org_id, refreshed(cognito).team_id) == ("a", "a1")


async def test_selected_secondary_account_resolves_canonical_login(client, db_session, cognito):
    await select_workspace(db_session, actor(), "b", CognitoWorkspaceClaims())
    cognito.writes.clear()
    response = await replace(client, "b", "local-b", ("b1", "b2"), "b2")
    assert response.status_code == 200, response.text
    assert (refreshed(cognito).org_id, refreshed(cognito).team_id, refreshed(cognito).department_id) == ("b", "b2", "d-b2")
    assert (await db_session.get(User, "login")).team_id == "a1"
    assert (await db_session.get(User, "login")).cognito_sub == "login-sub"
    assert (await db_session.get(User, "local-b")).cognito_sub is None


async def test_primary_removal_promotes_then_clears_last_and_add_restores(client, cognito):
    assert (await client.post("/admin/organizations/a/teams/a2/members", json={"user_id": "login"})).status_code == 201
    assert (await client.delete("/admin/organizations/a/teams/a1/members/login")).status_code == 204
    assert (refreshed(cognito).team_id, refreshed(cognito).department_id) == ("a2", "d-a2")
    assert (await client.delete("/admin/organizations/a/teams/a2/members/login")).status_code == 204
    assert (refreshed(cognito).team_id, refreshed(cognito).department_id) == ("", "")
    assert (await client.post("/admin/organizations/a/teams/a1/members", json={"user_id": "login"})).status_code == 201
    assert (refreshed(cognito).team_id, refreshed(cognito).department_id) == ("a1", "d-a1")


@pytest.mark.parametrize("failure", ["before", "after"])
@pytest.mark.parametrize("operation", ["replace", "delete", "add"])
async def test_cognito_failure_is_explicit_saved_state_and_idempotent_retry_repairs(client, db_session, cognito, failure, operation):
    if operation == "add":
        assert (await client.delete("/admin/organizations/a/teams/a1/members/login")).status_code == 204

    async def write():
        if operation == "delete":
            return await client.delete("/admin/organizations/a/teams/a1/members/login")
        if operation == "add":
            return await client.post("/admin/organizations/a/teams/a2/members", json={"user_id": "login"})
        return await replace(client)

    cognito.failure = failure
    response = await write()
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["membership_saved"] is True
    expected = "" if operation == "delete" else "a2"
    assert (await db_session.get(User, "login")).team_id == expected
    cognito.failure = None
    response = await write()
    assert response.status_code in (200, 201, 204), response.text
    assert (refreshed(cognito).team_id, refreshed(cognito).department_id) == (expected, "d-" + expected if expected else "")


async def test_database_commit_failure_never_writes_cognito(client, db_session, cognito, monkeypatch):
    monkeypatch.setattr(db_session, "commit", AsyncMock(side_effect=RuntimeError("database down")))
    with pytest.raises(RuntimeError, match="database down"):
        await replace(client)
    await db_session.rollback()
    assert cognito.writes == []
    assert (await db_session.get(User, "login")).team_id == "a1"


@pytest.mark.parametrize("link", ["absent", "broken", "ambiguous"])
async def test_missing_canonical_linkage_never_guesses_from_email(client, db_session, cognito, link):
    row = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == "local-b", UserIdentity.provider == "cognito"))
    if link == "absent":
        await db_session.delete(row)
    elif link == "broken":
        row.provider_user_id = "nonexistent-sub"
    else:
        db_session.add(
            UserIdentity(
                user_id="local-b", org_id="b", team_id="b1", provider="cognito", provider_user_id="second-sub", verification_method="org_placement"
            )
        )
    await db_session.commit()
    response = await replace(client, "b", "local-b", ("b1", "b2"), "b2")
    assert response.status_code == (200 if link == "absent" else 503), response.text
    assert cognito.writes == []
    assert refreshed(cognito).org_id == "a"


async def test_platform_role_and_other_attributes_are_preserved(client, cognito):
    cognito.attributes["custom:role"] = "platform_admin"
    assert (await replace(client)).status_code == 200
    assert cognito.attributes["custom:role"] == "platform_admin"
    assert set(cognito.writes[0]) == {"custom:team_id", "custom:department_id"}


async def test_membership_primary_wins_over_disagreeing_cached_pointer(client, db_session, cognito):
    # A nonprimary addition must reconcile the actual primary, even if the old
    # denormalized pointer (and existing Cognito attribute) are already stale.
    user = await db_session.get(User, "login")
    user.team_id = "a2"
    await db_session.commit()
    cognito.attributes.update({"custom:team_id": "a2", "custom:department_id": "d-a2"})
    response = await client.post("/admin/organizations/a/teams/a2/members", json={"user_id": "login"})
    assert response.status_code == 201, response.text
    assert (refreshed(cognito).team_id, refreshed(cognito).department_id) == ("a1", "d-a1")


async def test_legacy_pointer_without_memberships_uses_existing_workspace_rule(db_session, cognito):
    from sqlalchemy import delete

    from src.admin.team_membership_claims import commit_team_memberships

    await seed(db_session)
    await db_session.execute(delete(TeamMembership).where(TeamMembership.user_id == "login"))
    user = await db_session.get(User, "login")
    user.team_id = "a2"
    await commit_team_memberships(db_session, user_id="login", org_id="a")
    assert (refreshed(cognito).team_id, refreshed(cognito).department_id) == ("a2", "d-a2")


async def test_primary_service_writer_uses_same_commit_boundary(db_session, cognito):
    from src.admin.team_membership_claims import commit_team_memberships
    from src.admin.team_memberships import set_primary_team

    await seed(db_session)
    await set_primary_team(db_session, user_id="login", org_id="a", team_id="a2")
    await commit_team_memberships(db_session, user_id="login", org_id="a")
    assert (refreshed(cognito).team_id, refreshed(cognito).department_id) == ("a2", "d-a2")


async def test_ambiguous_primary_fails_claim_reconciliation(db_session, cognito):
    from fastapi import HTTPException

    from src.admin.team_membership_claims import commit_team_memberships

    await seed(db_session)
    # SQLite permits this; deployed PostgreSQL's partial index rejects it.
    db_session.add(TeamMembership(user_id="login", org_id="a", team_id="a2", is_primary=True))
    with pytest.raises(HTTPException) as error:
        await commit_team_memberships(db_session, user_id="login", org_id="a")
    assert error.value.status_code == 503
    assert cognito.writes == []
