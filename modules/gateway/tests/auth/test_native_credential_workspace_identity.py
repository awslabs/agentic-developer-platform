"""Native credential authority follows proven workspace links, never aliases."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, update

from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Organization, Team, TeamMembership, User
from src.shared.models.vault import UserIdentity
from tests.auth.test_native_credentials_postgres import create_body

pytest_plugins = ("tests.auth.test_native_credentials_sqlite",)


async def place_login(native, *, provider="cognito"):
    async with native.factory() as db, db.begin():
        await db.execute(update(User).where(User.id == "owner").values(cognito_sub=None))
        db.add(Organization(id="login-home", name="Fictional login home"))
        db.add(Team(id="login-team", org_id="login-home", department_id="login-department", name="Login team"))
        await db.flush()
        db.add(
            User(
                id="login",
                cognito_sub="subject-owner",
                org_id="login-home",
                team_id="login-team",
                email="login@example.test",
                user_kind="human",
                is_shadow=False,
            )
        )
        db.add(
            UserIdentity(
                id="workspace-link",
                user_id="owner",
                org_id="tenant",
                team_id="team",
                provider=provider,
                provider_user_id="54321" if provider == "github" else "subject-owner",
                verification_method="oauth" if provider == "github" else "org_placement",
                verified_at=datetime.now(UTC),
            )
        )


@pytest.mark.parametrize("provider", ["cognito", "github"])
async def test_proven_workspace_member_owns_native_request_with_null_subject(native, provider):
    await place_login(native, provider=provider)
    bearer = native.token(username="github_54321" if provider == "github" else "owner@example.test")
    config = await native.call("GET", "/config", bearer=bearer)
    assert config.status_code == 200, config.text
    created = await native.call("POST", "/requests", create_body(), bearer=bearer)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "created"
    # The selected credential still belongs to the tenant member, not the login
    # row, and successful admission has not issued any provider credential.
    assert "secret" not in native.providers.effects
    assert "sts" not in native.providers.effects
    async with native.factory() as db:
        assert (await db.get(User, "owner")).cognito_sub is None


@pytest.mark.parametrize(
    "change",
    [
        "wrong_tenant",
        "wrong_link_tenant",
        "unverified_link",
        "deleted_link",
        "other_subject",
        "other_login_owner",
        "inactive_membership",
        "revoked_membership",
        "missing_membership",
        "missing_primary_team",
        "wrong_primary_team",
        "shadow",
        "nonhuman",
        "login_shadow",
        "login_nonhuman",
        "raw_user_id_alias",
    ],
)
async def test_workspace_links_cannot_replace_current_native_authority(native, change):
    await place_login(native)
    bearer = native.token()
    async with native.factory() as db, db.begin():
        if change == "wrong_tenant":
            bearer = native.token(**{"custom:org_id": "login-home"})
        elif change == "wrong_link_tenant":
            await db.execute(update(UserIdentity).where(UserIdentity.id == "workspace-link").values(org_id="login-home"))
        elif change == "unverified_link":
            await db.execute(update(UserIdentity).where(UserIdentity.id == "workspace-link").values(verification_method="self_asserted"))
        elif change == "deleted_link":
            await db.execute(delete(UserIdentity).where(UserIdentity.id == "workspace-link"))
        elif change == "other_subject":
            await db.execute(update(UserIdentity).where(UserIdentity.id == "workspace-link").values(provider_user_id="subject-other"))
        elif change == "other_login_owner":
            await db.execute(update(User).where(User.id == "owner").values(cognito_sub="subject-foreign"))
        elif change == "inactive_membership":
            await db.execute(update(TenantMembership).where(TenantMembership.user_id == "owner").values(is_active=False))
        elif change == "revoked_membership":
            await db.execute(update(TenantMembership).where(TenantMembership.user_id == "owner").values(revoked_at=datetime.now(UTC)))
        elif change == "missing_membership":
            await db.execute(delete(TenantMembership).where(TenantMembership.user_id == "owner"))
        elif change == "missing_primary_team":
            await db.execute(delete(TeamMembership).where(TeamMembership.user_id == "owner"))
        elif change == "wrong_primary_team":
            await db.execute(update(User).where(User.id == "owner").values(team_id="login-team"))
        elif change == "shadow":
            await db.execute(update(User).where(User.id == "owner").values(is_shadow=True))
        elif change == "nonhuman":
            await db.execute(update(User).where(User.id == "owner").values(user_kind="bot"))
        elif change == "login_shadow":
            await db.execute(update(User).where(User.id == "login").values(is_shadow=True))
        elif change == "login_nonhuman":
            await db.execute(update(User).where(User.id == "login").values(user_kind="bot"))
        elif change == "raw_user_id_alias":
            bearer = native.token(sub="login")
    response = await native.call("GET", "/config", bearer=bearer)
    assert response.status_code == 403, response.text
    assert response.json() == {"detail": {"error": "native_current_membership_required"}}
    assert native.providers.effects == []


@pytest.mark.parametrize("username", ["github_99999", "owner@example.test", "github_not-a-number"])
async def test_github_link_requires_matching_signed_numeric_identity(native, username):
    await place_login(native, provider="github")
    response = await native.call("GET", "/config", bearer=native.token(username=username))
    assert response.status_code == 403
    assert native.providers.effects == []


async def test_native_direct_subject_member_keeps_existing_authority(native):
    response = await native.call("POST", "/requests", create_body())
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "created"
    assert "secret" not in native.providers.effects


async def test_link_revocation_is_rechecked_after_successful_configuration(native):
    await place_login(native)
    assert (await native.call("GET", "/config")).status_code == 200
    before = list(native.providers.effects)
    async with native.factory() as db, db.begin():
        await db.execute(delete(UserIdentity).where(UserIdentity.id == "workspace-link"))
    response = await native.call("POST", "/requests", create_body())
    assert response.status_code == 403
    assert native.providers.effects == before


@pytest.mark.parametrize("identity", ["subject_collision", "proven_subject_collision", "ambiguous_proven_member", "revoked_alias"])
async def test_real_login_cannot_authorize_an_unrelated_or_ambiguous_workspace_member(native, identity):
    await place_login(native)
    user_id = "subject-owner" if identity in {"subject_collision", "proven_subject_collision"} else "duplicate-member"
    async with native.factory() as db, db.begin():
        db.add(User(id=user_id, org_id="tenant", team_id="team", email="duplicate@example.test", user_kind="human", is_shadow=False))
        await db.flush()
        db.add(
            TenantMembership(
                user_id=user_id, tenant_id="tenant", is_active=True, revoked_at=datetime.now(UTC) if identity == "revoked_alias" else None
            )
        )
        db.add(TeamMembership(user_id=user_id, org_id="tenant", team_id="team"))
        if identity != "subject_collision":
            db.add(
                UserIdentity(
                    user_id=user_id,
                    org_id="tenant",
                    team_id="team",
                    provider="github",
                    provider_user_id="54321",
                    verification_method="oauth",
                    verified_at=datetime.now(UTC),
                )
            )
    response = await native.call("GET", "/config", bearer=native.token(username="github_54321"))
    assert response.status_code == 403
    assert response.json() == {"detail": {"error": "native_current_membership_required"}}
    assert native.providers.effects == []
