"""Org removal must delete dependent memberships without nulling their foreign keys."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, select, text

from src.admin.exceptions import MemberRemovalConflictError, ResourceNotFoundError
from src.admin.routes import get_cognito_service, get_current_user, router
from src.admin.service import AdminService
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.provenance import ActionProvenance
from src.shared.models.vault import UserCredential, UserIdentity
from src.shared.schemas.auth import TokenContext

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def projection_writer():
    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(return_value=True)
    with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer):
        yield writer


async def _seed(db, foreign_keys=True):
    await db.execute(text(f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}"))
    assert await db.scalar(text("PRAGMA foreign_keys")) == int(foreign_keys)
    for org_id in ("home", "other"):
        db.add(Organization(id=org_id, name=org_id))
        await db.flush()
        db.add(Department(id=f"dept-{org_id}", org_id=org_id, name="Department"))
        await db.flush()
        db.add(Team(id=f"team-{org_id}", org_id=org_id, department_id=f"dept-{org_id}", name="Team"))
        await db.flush()
        db.add(User(id=f"user-{org_id}", org_id=org_id, team_id=f"team-{org_id}", email="person@example.test"))
        await db.flush()
        db.add_all(
            [
                UserIdentity(
                    user_id=f"user-{org_id}",
                    org_id=org_id,
                    team_id=f"team-{org_id}",
                    provider="github",
                    provider_user_id="123",
                    verification_method="oauth",
                ),
                TenantMembership(user_id=f"user-{org_id}", tenant_id=org_id, role="member", is_active=True, joined_via="admin_create"),
                TeamMembership(user_id=f"user-{org_id}", org_id=org_id, team_id=f"team-{org_id}", is_primary=True),
                UserCredential(
                    user_id=f"user-{org_id}",
                    org_id=org_id,
                    service="github",
                    credential_type="pat",
                    label="private",
                    secret_arn=f"arn:example:{org_id}",
                ),
                UserCredential(org_id=org_id, service="github", credential_type="pat", label="shared", secret_arn=f"arn:example:shared-{org_id}"),
            ]
        )
    await db.commit()


@pytest.mark.parametrize("foreign_keys", [True, False])
async def test_remove_org_member_cascades_without_touching_other_org(db_session, admin_service: AdminService, projection_writer, foreign_keys):
    await _seed(db_session, foreign_keys)
    assert await admin_service.remove_user("other", "user-other")
    db_session.expire_all()

    assert await db_session.get(User, "user-other") is None
    assert await db_session.get(User, "user-home") is not None
    for model in (UserIdentity, TenantMembership, TeamMembership, UserCredential):
        assert await db_session.scalar(select(model).where(model.user_id == "user-other")) is None
        assert await db_session.scalar(select(model).where(model.user_id == "user-home")) is not None
    assert len((await db_session.execute(select(UserCredential).where(UserCredential.user_id.is_(None)))).scalars().all()) == 2
    projection_writer.update_user_membership_orgs.assert_awaited_once_with(provider_user_id="123", member_org_ids=["home"], provider="github")


async def test_last_membership_projects_an_empty_org_list(db_session, admin_service, projection_writer):
    await _seed(db_session)
    await admin_service.remove_user("other", "user-other")
    await admin_service.remove_user("home", "user-home")
    assert projection_writer.update_user_membership_orgs.await_args.kwargs["member_org_ids"] == []


async def test_an_unproven_identity_still_gets_its_projection_refreshed(db_session, admin_service, projection_writer):
    """#5664 (A10): the projection snapshot is unfiltered, the trust guard is not.

    `member_org_ids` answers "which orgs does this GitHub ACCOUNT hold memberships
    in", recomputed through proven bindings across surviving users. The set of
    external IDs needing a refresh must still include unproven removed claims,
    since their stale memberships need clearing. A PROVEN-only snapshot made the refresh a
    silent no-op for accounts whose only row is unproven (auto-provisioned
    `channel_placement` rows, which were `admin_manual` and therefore proven before
    this issue). The stale row then kept advertising an org whose membership was
    just deleted — permissive staleness for a fail-closed reader.

    `user-home` keeps a proven row on the same GitHub id so the recomputed union is
    non-trivial, which is what distinguishes "refreshed" from "never written".
    """
    await _seed(db_session)
    identity = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == "user-other"))
    identity.verification_method = "channel_placement"
    await db_session.commit()

    assert await admin_service.remove_user("other", "user-other")

    projection_writer.update_user_membership_orgs.assert_awaited_once_with(provider_user_id="123", member_org_ids=["home"], provider="github")


async def test_unproven_identity_does_not_block_removal_as_a_shared_login(db_session, admin_service, projection_writer):
    """The other half of the split snapshot: the guard stays proven-only.

    The shared-login interlock refuses a deletion that would strand another
    tenant's sign-in. That is an authority question, so an unproven claim on the
    same GitHub id must not be able to manufacture the conflict and block an
    administrator from removing an account.
    """
    await _seed(db_session)
    for user_id in ("user-home", "user-other"):
        row = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == user_id))
        row.verification_method = "channel_placement"
    user = await db_session.get(User, "user-other")
    user.cognito_username = "own-login"
    await db_session.commit()

    assert await admin_service.remove_user("other", "user-other")


async def test_removing_last_proven_binding_clears_membership_projection(db_session, admin_service, projection_writer):
    await _seed(db_session)
    unproven = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == "user-other"))
    unproven.verification_method = "channel_placement"
    await db_session.commit()

    assert await admin_service.remove_user("home", "user-home")

    projection_writer.update_user_membership_orgs.assert_awaited_once_with(provider_user_id="123", member_org_ids=[], provider="github")
    assert await db_session.get(User, "user-other") is not None
    assert await db_session.scalar(select(TenantMembership).where(TenantMembership.user_id == "user-other")) is not None


async def test_projection_failure_does_not_undo_removal(db_session, admin_service, projection_writer):
    await _seed(db_session)
    projection_writer.update_user_membership_orgs.side_effect = RuntimeError("DynamoDB unavailable")
    assert await admin_service.remove_user("other", "user-other")
    db_session.expire_all()
    assert await db_session.get(User, "user-other") is None


async def test_wrong_org_cannot_remove_a_user(db_session, admin_service, projection_writer):
    await _seed(db_session)
    with pytest.raises(ResourceNotFoundError):
        await admin_service.remove_user("home", "user-other")
    assert await db_session.get(User, "user-other") is not None
    projection_writer.update_user_membership_orgs.assert_not_awaited()


async def test_shared_cognito_account_cannot_be_deleted(db_session, admin_service, projection_writer):
    await _seed(db_session)
    user = await db_session.get(User, "user-home")
    user.cognito_sub = "shared-sub"
    user.cognito_username = "shared-login"
    await db_session.commit()
    cognito = MagicMock()
    with pytest.raises(MemberRemovalConflictError, match="sign-in used by another organization"):
        await admin_service.remove_user("home", "user-home", cognito)
    assert await db_session.scalar(select(TenantMembership).where(TenantMembership.user_id == user.id)) is not None
    cognito.delete_user.assert_not_called()
    projection_writer.update_user_membership_orgs.assert_not_awaited()


async def test_legacy_user_row_with_multiple_org_memberships_is_not_deleted(db_session, admin_service):
    await _seed(db_session)
    db_session.add(TenantMembership(user_id="user-other", tenant_id="home", role="member", is_active=False, joined_via="test"))
    await db_session.commit()
    with pytest.raises(MemberRemovalConflictError, match="membership in another organization"):
        await admin_service.remove_user("other", "user-other")
    assert len((await db_session.execute(select(TenantMembership).where(TenantMembership.user_id == "user-other"))).scalars().all()) == 2


async def test_retained_audit_record_rolls_back_all_deletes_before_cognito(db_session, admin_service, projection_writer):
    await _seed(db_session)
    identity = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == "user-other"))
    identity.provider_user_id = "999"
    user = await db_session.get(User, "user-other")
    user.cognito_username = "separate-login"
    db_session.add(
        ActionProvenance(org_id="other", actor_user_id=user.id, root_human_id=user.id, action_kind="test", source_event={}, correlation_id="test")
    )
    await db_session.commit()
    cognito = MagicMock()
    with pytest.raises(MemberRemovalConflictError, match="related records"):
        await admin_service.remove_user("other", "user-other", cognito)
    assert await db_session.get(User, "user-other") is not None
    for model in (UserIdentity, TenantMembership, TeamMembership, UserCredential):
        assert await db_session.scalar(select(model).where(model.user_id == "user-other")) is not None
    cognito.delete_user.assert_not_called()
    projection_writer.update_user_membership_orgs.assert_not_awaited()


async def test_cognito_cleanup_happens_after_commit(db_session, admin_service, projection_writer):
    await _seed(db_session)
    identity = await db_session.scalar(select(UserIdentity).where(UserIdentity.user_id == "user-other"))
    identity.provider_user_id = "999"
    user = await db_session.get(User, "user-other")
    user.cognito_username = "separate-login"
    await db_session.commit()
    cognito = MagicMock()
    events = []
    event.listen(db_session.sync_session, "after_commit", lambda _: events.append("commit"))
    cognito.delete_user.side_effect = lambda **kwargs: events.append("cognito")
    assert await admin_service.remove_user("other", "user-other", cognito)
    assert events == ["commit", "cognito"]


def _client(db, caller_id="sub-caller", *, is_admin=False):
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(BedrockGatewayError)
    async def handle_error(request: Request, exc: BedrockGatewayError):
        return JSONResponse(status_code=exc.status_code, content={"error": exc.error, "message": exc.message})

    async def override_db():
        yield db

    app.dependency_overrides[get_db] = override_db
    app.dependency_overrides[get_cognito_service] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: TokenContext(
        user_id=caller_id,
        org_id="home",
        team_id="team-home",
        department_id="dept-home",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    # Exercise real AccessControl against authoritative memberships.
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture
async def route_db(db_session):
    await _seed(db_session)
    db_session.add(User(id="caller", org_id="home", team_id="team-home", email="admin@example.test", cognito_sub="sub-caller"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id="caller", tenant_id="home", role="org_admin", is_active=True, joined_via="test"))
    await db_session.commit()
    return db_session


async def test_route_removes_member_and_returns_204(route_db):
    async with _client(route_db) as client:
        response = await client.delete("/admin/organizations/home/users/user-home")
    assert response.status_code == 204, response.text
    assert response.content == b""
    route_db.expire_all()
    assert await route_db.get(User, "user-home") is None
    assert await route_db.get(User, "user-other") is not None


@pytest.mark.parametrize("caller_id", ["sub-caller", "caller"])
async def test_route_refuses_self_removal_for_both_identifier_forms(route_db, caller_id):
    async with _client(route_db, caller_id) as client:
        response = await client.delete("/admin/organizations/home/users/caller")
    assert response.status_code == 403, response.text
    assert "Cannot remove your own account" in response.json()["message"]
    assert await route_db.get(User, "caller") is not None


@pytest.mark.parametrize("role_store", ["user", "membership"])
async def test_route_org_admin_cannot_remove_platform_admin(route_db, role_store):
    if role_store == "user":
        target = await route_db.get(User, "user-home")
    else:
        target = await route_db.scalar(select(TenantMembership).where(TenantMembership.user_id == "user-home"))
    target.role = "platform_admin"
    await route_db.commit()
    async with _client(route_db) as client:
        response = await client.delete("/admin/organizations/home/users/user-home")
    assert response.status_code == 403, response.text
    assert "Cannot modify a platform administrator" in response.json()["message"]
    assert await route_db.get(User, "user-home") is not None


@pytest.mark.parametrize("org_id,expected_status", [("home", 404), ("other", 403)])
async def test_route_org_admin_cannot_remove_a_foreign_org_user(route_db, org_id, expected_status):
    async with _client(route_db) as client:
        response = await client.delete(f"/admin/organizations/{org_id}/users/user-other")
    assert response.status_code == expected_status, response.text
    assert await route_db.get(User, "user-other") is not None


async def test_route_returns_readable_conflict_when_canonical_login_is_shared(route_db, projection_writer):
    target = await route_db.get(User, "user-home")
    target.cognito_sub = "sub-member"
    await route_db.commit()
    async with _client(route_db) as client:
        response = await client.delete("/admin/organizations/home/users/user-home")
    assert response.status_code == 409, response.text
    assert response.json()["error"] == "member_removal_conflict"
    assert "sign-in used by another organization" in response.json()["message"]
    assert await route_db.get(User, "user-home") is not None
    projection_writer.update_user_membership_orgs.assert_not_awaited()
