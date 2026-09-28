"""``POST /admin/organizations/{org_id}/members`` — placing a person into an org (Issue #4943).

The defect this route closes. A platform admin used the members panel's add-member
modal, picked a person from the platform-wide roster and a team, and the server
correctly refused with 404 three times: the add-team-member route resolves a user by
``(users.id, org_id)``, and the person was not in the org yet. There was no route for
"bring this person into this org", so the modal could only ever succeed for people
who were already members — the picker offered exactly the population the write had to
reject.

The properties pinned here are the ones whose failure is silent or expensive:

1. **Only a platform admin may do this.** The body names a person from a
   platform-wide roster that is ``require_platform_admin`` for tenant-isolation
   reasons (#4827). Gating on ``ORG_UPDATE`` like the sibling team routes would let
   an org admin pull any user id they guessed into their own tenant.
2. **A bad ``user_id`` is 422, not 404.** The sibling team-add route returns 404 to
   mean "not in this org" — the exact condition this route fixes. Sharing one status
   code across two diagnoses on a flow that chains them makes the frontend unable to
   tell "add them to the org first" from "that person does not exist".
3. **It is idempotent.** A retried admin action (or a re-added returning member) must
   heal, not mint a second member row the org then sees twice.
4. **The follow-up team add succeeds against the returned id.** This is the whole
   user-visible fix, and it is why the route returns a user rather than 204: for
   somebody who came from another org the org-scoped id is NOT the one submitted.
5. **The membership is projected.** ``member_org_ids`` is what the platform-mode
   sign-in gate reads; a member who is not projected is a member who cannot log in.
6. **A cross-org team id still 404s.** The refusal being fixed was correct for what
   it was checking, and must stay in force.

``AccessControl`` is real throughout and roles come from ``tenant_memberships``, never
from a claim — a mocked authority check asserts a guarantee it never exercised.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin import org_members
from src.admin.config import AdminConfig, set_admin_config
from src.admin.exceptions import ResourceConflictError, ResourceNotFoundError, UnknownPlatformUserError
from src.admin.routes import get_current_user, router
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.models.vault import UserIdentity
from src.shared.schemas.auth import TokenContext

HOME_ORG = "org-4943-home"
OTHER_ORG = "org-4943-other"

TEAM_HOME = "team-4943-app-dev"
TEAM_HOME_2 = "team-4943-platform-admin"
TEAM_OTHER = "team-4943-other"

# The person in the live repro: on the platform roster, in another org, picked out of
# the modal for a team in HOME_ORG.
OUTSIDER_ID = "pg-4943-outsider"
OUTSIDER_GITHUB_ID = "49430001"

# Somebody already in the target org — the regression case that must stay one-step.
INSIDER_ID = "pg-4943-insider"

PLATFORM_ADMIN_SUB = "sub-4943-platform"
ORG_ADMIN_SUB = "sub-4943-orgadmin"


@pytest.fixture(autouse=True)
def least_privilege_config():
    """Least-privilege default so the permission gates below are effective."""
    set_admin_config(AdminConfig(rbac_least_privilege_default=True, rbac_role_cache_ttl_seconds=30.0))
    yield
    set_admin_config(AdminConfig())


def _context(sub: str, org_id: str, *, is_admin: bool = False) -> TokenContext:
    return TokenContext(
        user_id=sub,
        org_id=org_id,
        team_id=TEAM_HOME,
        department_id="dept-4943",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def _seed(db: AsyncSession) -> None:
    """Two orgs; the target has two teams. An outsider, an insider, and two admins."""
    db.add_all([Organization(id=HOME_ORG, name="Home Org"), Organization(id=OTHER_ORG, name="Other Org")])
    await db.flush()
    db.add_all(
        [
            Department(id="dept-4943", org_id=HOME_ORG, name="Dept"),
            Department(id="dept-4943-other", org_id=OTHER_ORG, name="Dept"),
        ]
    )
    await db.flush()
    db.add_all(
        [
            Team(id=TEAM_HOME, org_id=HOME_ORG, department_id="dept-4943", name="App-Dev"),
            Team(id=TEAM_HOME_2, org_id=HOME_ORG, department_id="dept-4943", name="Platform-admin"),
            Team(id=TEAM_OTHER, org_id=OTHER_ORG, department_id="dept-4943-other", name="Other"),
        ]
    )
    await db.flush()
    db.add_all(
        [
            User(id=OUTSIDER_ID, org_id=OTHER_ORG, team_id=TEAM_OTHER, email="marc@example.test", name="Marc McGinnis"),
            User(id=INSIDER_ID, org_id=HOME_ORG, team_id=TEAM_HOME, email="insider@example.test", name="Ina Sider"),
            User(id="pg-4943-platform", org_id=HOME_ORG, team_id=TEAM_HOME, email="platform@example.test", cognito_sub=PLATFORM_ADMIN_SUB),
            User(id="pg-4943-orgadmin", org_id=HOME_ORG, team_id=TEAM_HOME, email="orgadmin@example.test", cognito_sub=ORG_ADMIN_SUB),
        ]
    )
    await db.flush()
    db.add(
        UserIdentity(
            id="ui-4943-outsider",
            user_id=OUTSIDER_ID,
            org_id=OTHER_ORG,
            team_id=TEAM_OTHER,
            provider="github",
            provider_user_id=OUTSIDER_GITHUB_ID,
            provider_username="marcmcginnis",
            verification_method="oauth",
        )
    )
    db.add_all(
        [
            TenantMembership(user_id=OUTSIDER_ID, tenant_id=OTHER_ORG, role="member", is_active=True, joined_via="org_membership"),
            TenantMembership(user_id=INSIDER_ID, tenant_id=HOME_ORG, role="member", is_active=True, joined_via="org_membership"),
            TenantMembership(user_id="pg-4943-platform", tenant_id=HOME_ORG, role="platform_admin", is_active=True, joined_via="org_membership"),
            TenantMembership(user_id="pg-4943-orgadmin", tenant_id=HOME_ORG, role="org_admin", is_active=True, joined_via="org_membership"),
        ]
    )
    await db.commit()


@pytest.fixture
def seeded_db(db_session: AsyncSession) -> AsyncSession:
    """tests/admin/conftest.py's session, seeded. Sync so route tests stay sync."""
    asyncio.get_event_loop().run_until_complete(_seed(db_session))
    return db_session


def _client(caller: TokenContext, db: AsyncSession) -> TestClient:
    """Test app wired with the REAL AccessControl (resolved from the real get_db)."""
    app = FastAPI()
    app.include_router(router)

    @app.exception_handler(BedrockGatewayError)
    async def gateway_error_handler(request: Request, exc: BedrockGatewayError):
        content = {"error": exc.error, "message": exc.message}
        if exc.details:
            content["details"] = exc.details
        return JSONResponse(status_code=exc.status_code, content=content)

    async def _get_db():
        yield db

    app.dependency_overrides[get_db] = _get_db
    app.dependency_overrides[get_current_user] = lambda: caller
    # get_access_control is deliberately NOT overridden — a mocked authority check
    # asserts a guarantee it never exercised.
    return TestClient(app, raise_server_exceptions=False)


def _writer() -> MagicMock:
    writer = MagicMock()
    writer.update_user_membership_orgs = AsyncMock(return_value=True)
    writer.put_user_identity = AsyncMock(return_value=True)
    return writer


def _projected_orgs(writer: MagicMock) -> list[list[str]]:
    """Every ``member_org_ids`` list passed to the projection write."""
    return [sorted(c.kwargs["member_org_ids"]) for c in writer.update_user_membership_orgs.await_args_list]


async def _rows_in_org(db: AsyncSession, org_id: str, email: str) -> list[User]:
    return list((await db.execute(sa.select(User).where(User.org_id == org_id, User.email == email))).scalars().all())


async def _memberships(db: AsyncSession, user_id: str) -> list[TenantMembership]:
    return list((await db.execute(sa.select(TenantMembership).where(TenantMembership.user_id == user_id))).scalars().all())


# ---------------------------------------------------------------------------
# Authz — the reason this route is not ORG_UPDATE like its siblings
# ---------------------------------------------------------------------------


class TestRouteAuthz:
    def test_org_admin_cannot_add_a_person_to_their_own_org(self, seeded_db):
        """403 even in their OWN org: the roster they would pick from is not theirs to read."""
        client = _client(_context(ORG_ADMIN_SUB, HOME_ORG), seeded_db)
        resp = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID})
        assert resp.status_code == 403
        assert resp.json()["error"] == "access_denied"

    def test_the_org_admin_refusal_names_the_path_forward(self, seeded_db):
        """A bare 'access denied' on a button they can see reads as a bug, not a rule.

        The refusal has to distinguish what an org admin CAN do (assign existing
        members to teams) from what they cannot (bring somebody new in), or the panel
        has no message to show that is better than the dead modal being fixed.
        """
        client = _client(_context(ORG_ADMIN_SUB, HOME_ORG), seeded_db)
        message = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID}).json()["message"]
        assert "platform administrator" in message
        assert "access-request" in message

    def test_plain_member_cannot_add_a_person(self, seeded_db):
        client = _client(_context("sub-4943-nobody", HOME_ORG), seeded_db)
        resp = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID})
        assert resp.status_code == 403

    def test_platform_admin_can_add_a_person(self, seeded_db):
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        resp = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID})
        assert resp.status_code == 201, resp.text

    def test_platform_admin_can_add_into_an_org_they_are_not_in(self, seeded_db):
        """Platform authority is unscoped — that is what makes this the platform gate."""
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        resp = client.post(f"/admin/organizations/{OTHER_ORG}/members", json={"user_id": INSIDER_ID})
        assert resp.status_code == 201, resp.text


# ---------------------------------------------------------------------------
# Route behaviour
# ---------------------------------------------------------------------------


class TestRouteBehaviour:
    def test_unknown_user_is_422_not_404(self, seeded_db):
        """The distinction the frontend branches on. See the module docstring 2."""
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        resp = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": "pg-4943-nobody"})
        assert resp.status_code == 422
        assert resp.json()["error"] == "unknown_platform_user"

    def test_unknown_org_is_404(self, seeded_db):
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        resp = client.post("/admin/organizations/org-4943-nope/members", json={"user_id": OUTSIDER_ID})
        assert resp.status_code == 404
        assert resp.json()["error"] == "resource_not_found"

    def test_returns_the_row_in_the_target_org_not_the_submitted_id(self, seeded_db):
        """Why the route returns a user at all: the id changes.

        Every org-scoped write that follows — the team add in particular — resolves
        through ``(users.id, org_id)``, so a caller reusing the platform-roster id
        gets exactly the 404 this route exists to prevent.
        """
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        body = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID}).json()
        assert body["org_id"] == HOME_ORG
        assert body["id"] != OUTSIDER_ID
        assert body["email"] == "marc@example.test"

    def test_existing_member_is_returned_unchanged(self, seeded_db):
        """Idempotent for somebody already in the org — no second row, same id."""
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        body = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": INSIDER_ID}).json()
        assert body["id"] == INSIDER_ID
        rows = asyncio.get_event_loop().run_until_complete(_rows_in_org(seeded_db, HOME_ORG, "insider@example.test"))
        assert len(rows) == 1

    def test_the_team_add_then_succeeds_against_the_returned_id(self, seeded_db):
        """The user-visible fix, end to end: 404 → confirm → the person is on the team."""
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)

        refused = client.post(f"/admin/organizations/{HOME_ORG}/teams/{TEAM_HOME}/members", json={"user_id": OUTSIDER_ID})
        assert refused.status_code == 404, "the original defect: the roster id is not resolvable in this org"

        mirrored = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID}).json()
        added = client.post(f"/admin/organizations/{HOME_ORG}/teams/{TEAM_HOME}/members", json={"user_id": mirrored["id"]})
        assert added.status_code == 201, added.text
        assert added.json()["team_id"] == TEAM_HOME
        assert added.json()["is_primary"] is True, "their first team in this org is their primary"

    def test_the_chosen_team_becomes_primary_not_some_other_team(self, seeded_db):
        """The mirror row must carry NO team pointer.

        With a non-empty ``users.team_id``, ``add_membership``'s lazy-materialization
        rule would give that team the primary row and land the admin's chosen team as
        a secondary — so the person's ``custom:team_id`` claim would name a team
        nobody picked.
        """
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        mirrored = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID}).json()
        assert mirrored["team_id"] == ""

        client.post(f"/admin/organizations/{HOME_ORG}/teams/{TEAM_HOME_2}/members", json={"user_id": mirrored["id"]})
        rows = asyncio.get_event_loop().run_until_complete(
            seeded_db.execute(sa.select(TeamMembership).where(TeamMembership.user_id == mirrored["id"]))
        )
        memberships = list(rows.scalars().all())
        assert [m.team_id for m in memberships if m.is_primary] == [TEAM_HOME_2]

    def test_a_team_in_another_org_is_still_refused(self, seeded_db):
        """The refusal being fixed was CORRECT for what it checked; it stays in force."""
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        mirrored = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID}).json()

        resp = client.post(f"/admin/organizations/{HOME_ORG}/teams/{TEAM_OTHER}/members", json={"user_id": mirrored["id"]})
        assert resp.status_code == 404

    def test_org_admin_role_is_grantable_by_a_platform_admin(self, seeded_db):
        """The modal's 'Org admin' radio: the role travels on this one call."""
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        body = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID, "role": "org_admin"}).json()

        memberships = asyncio.get_event_loop().run_until_complete(_memberships(seeded_db, body["id"]))
        assert [(m.tenant_id, m.role) for m in memberships] == [(HOME_ORG, "org_admin")]

    def test_a_platform_level_role_is_stored_as_org_admin(self, seeded_db):
        """#3981: a tenant-scoped row must never confer unscoped platform authority."""
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        body = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID, "role": "platform_admin"}).json()

        memberships = asyncio.get_event_loop().run_until_complete(_memberships(seeded_db, body["id"]))
        assert [m.role for m in memberships] == ["org_admin"]


# ---------------------------------------------------------------------------
# Service: idempotency, identity mirroring, the membership row
# ---------------------------------------------------------------------------


class TestService:
    @pytest.mark.asyncio
    async def test_placement_preserves_the_person_budget_anchor(self, db_session):
        from src.shared.identity.person_anchor import resolve_caller_person_anchor

        await _seed(db_session)
        primary = await db_session.get(UserIdentity, "ui-4943-outsider")
        primary.is_primary = True
        db_session.add(
            UserIdentity(
                user_id=OUTSIDER_ID,
                org_id=OTHER_ORG,
                team_id=TEAM_OTHER,
                provider="github",
                provider_user_id="100",
                verification_method="oauth",
                is_primary=False,
            )
        )
        await db_session.commit()
        source_anchor, _ = await resolve_caller_person_anchor(db_session, OUTSIDER_ID)
        placed = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()
        target_anchor, _ = await resolve_caller_person_anchor(db_session, placed.id)

        assert source_anchor == f"github:{OUTSIDER_GITHUB_ID}"
        assert target_anchor == source_anchor

    @pytest.mark.asyncio
    async def test_placement_keeps_an_existing_destination_primary(self, db_session):
        """Copying linked accounts cannot re-key a destination cap or add a second primary."""
        from src.shared.identity.person_anchor import resolve_caller_person_anchor

        await _seed(db_session)
        # Exercise the production uniqueness constraint even on SQLite, where
        # create_all() omits migration 042's Postgres-only index.
        await db_session.execute(sa.text("CREATE UNIQUE INDEX test_one_identity_primary ON user_identities (user_id, provider) WHERE is_primary"))
        source_primary = await db_session.get(UserIdentity, "ui-4943-outsider")
        source_primary.is_primary = True
        for user_id, org_id, team_id, is_primary in [
            (OUTSIDER_ID, OTHER_ORG, TEAM_OTHER, False),
            (INSIDER_ID, HOME_ORG, TEAM_HOME, True),
        ]:
            db_session.add(
                UserIdentity(
                    user_id=user_id,
                    org_id=org_id,
                    team_id=team_id,
                    provider="github",
                    provider_user_id="100",
                    verification_method="oauth",
                    is_primary=is_primary,
                )
            )
        await db_session.commit()

        placed = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()

        assert placed.id == INSIDER_ID
        target_anchor, _ = await resolve_caller_person_anchor(db_session, placed.id)
        assert target_anchor == "github:100"
        identities = (await db_session.execute(sa.select(UserIdentity).where(UserIdentity.user_id == placed.id))).scalars().all()
        assert {identity.provider_user_id: identity.is_primary for identity in identities} == {"100": True, OUTSIDER_GITHUB_ID: False}

    @pytest.mark.asyncio
    async def test_conflicting_identity_links_require_explicit_resolution(self, db_session):
        """Two destination people must never be picked arbitrarily as the incoming person."""
        await _seed(db_session)
        db_session.add_all(
            [
                UserIdentity(
                    user_id=OUTSIDER_ID, org_id=OTHER_ORG, team_id=TEAM_OTHER, provider="github", provider_user_id="100", verification_method="oauth"
                ),
                UserIdentity(
                    user_id=INSIDER_ID,
                    org_id=HOME_ORG,
                    team_id=TEAM_HOME,
                    provider="github",
                    provider_user_id=OUTSIDER_GITHUB_ID,
                    verification_method="oauth",
                ),
                UserIdentity(
                    user_id="pg-4943-orgadmin",
                    org_id=HOME_ORG,
                    team_id=TEAM_HOME,
                    provider="github",
                    provider_user_id="100",
                    verification_method="oauth",
                ),
            ]
        )
        await db_session.commit()
        with pytest.raises(ResourceConflictError):
            await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("existing_github_id", [None, "49439999"])
    async def test_shared_email_never_absorbs_a_github_identity(self, db_session, existing_github_id):
        """A shared mailbox or placeholder must not inherit the incoming identity."""
        await _seed(db_session)
        resident = await db_session.get(User, INSIDER_ID)
        resident.email = "marc@example.test"
        membership = (await _memberships(db_session, INSIDER_ID))[0]
        membership.role = "org_admin"
        if existing_github_id:
            db_session.add(
                UserIdentity(
                    user_id=resident.id,
                    org_id=HOME_ORG,
                    team_id=TEAM_HOME,
                    provider="github",
                    provider_user_id=existing_github_id,
                    verification_method="oauth",
                )
            )
        await db_session.commit()

        placed = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()

        assert placed.id != resident.id
        assert [m.role for m in await _memberships(db_session, placed.id)] == ["member"]
        assert [m.role for m in await _memberships(db_session, resident.id)] == ["org_admin"]
        assert (
            await db_session.scalar(
                sa.select(UserIdentity.user_id).where(
                    UserIdentity.org_id == HOME_ORG,
                    UserIdentity.provider == "github",
                    UserIdentity.provider_user_id == OUTSIDER_GITHUB_ID,
                )
            )
            == placed.id
        )
        # The stable identity still finds the same placement after an email change.
        source = await db_session.get(User, OUTSIDER_ID)
        source.email = "changed@example.test"
        await db_session.commit()
        repeated = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        assert repeated.id == placed.id

    @pytest.mark.asyncio
    async def test_membership_is_created_with_admin_create_provenance(self, db_session):
        await _seed(db_session)
        user = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()

        memberships = await _memberships(db_session, user.id)
        assert [(m.tenant_id, m.joined_via) for m in memberships] == [(HOME_ORG, "admin_create")]

    @pytest.mark.asyncio
    async def test_the_other_orgs_membership_is_untouched(self, db_session):
        """Adding somebody to a second org must not remove them from the first."""
        await _seed(db_session)
        await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()

        assert [m.tenant_id for m in await _memberships(db_session, OUTSIDER_ID)] == [OTHER_ORG]

    @pytest.mark.asyncio
    async def test_calling_twice_creates_one_row_and_one_membership(self, db_session):
        """The retried admin action heals rather than duplicating the member."""
        await _seed(db_session)
        first = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()
        second = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()

        assert first.id == second.id
        assert len(await _rows_in_org(db_session, HOME_ORG, "marc@example.test")) == 1
        assert len(await _memberships(db_session, first.id)) == 1

    @pytest.mark.asyncio
    async def test_re_adding_via_the_new_org_scoped_id_is_also_idempotent(self, db_session):
        """The second call may legitimately name the MIRROR row, not the roster row."""
        await _seed(db_session)
        first = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()
        second = await org_members.add_user_to_org(db_session, user_id=first.id, org_id=HOME_ORG)
        await db_session.commit()

        assert first.id == second.id
        assert len(await _rows_in_org(db_session, HOME_ORG, "marc@example.test")) == 1

    @pytest.mark.asyncio
    async def test_the_github_identity_is_mirrored_into_the_new_org(self, db_session):
        """Without this the member is invisible to the sign-in projection.

        ``project_member_org_ids`` fans out over ``user_identities`` per GitHub
        account, so a member row carrying no identity projects nothing.
        """
        await _seed(db_session)
        user = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()

        identities = list((await db_session.execute(sa.select(UserIdentity).where(UserIdentity.user_id == user.id))).scalars().all())
        assert [(i.provider, i.provider_user_id, i.org_id) for i in identities] == [("github", OUTSIDER_GITHUB_ID, HOME_ORG)]

    @pytest.mark.asyncio
    async def test_the_identity_is_not_duplicated_on_a_second_call(self, db_session):
        """``user_identities`` is unique per (provider, provider_user_id, org_id)."""
        await _seed(db_session)
        user = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()
        await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()

        identities = list((await db_session.execute(sa.select(UserIdentity).where(UserIdentity.user_id == user.id))).scalars().all())
        assert len(identities) == 1

    @pytest.mark.asyncio
    async def test_the_mirror_row_carries_no_cognito_sub(self, db_session):
        """``uq_users_cognito_sub`` is a partial unique index SQLite never builds.

        Copying the sub would pass this suite and raise ``IntegrityError`` in
        production, so the absence is asserted here rather than trusted.
        """
        await _seed(db_session)
        await db_session.execute(sa.update(User).where(User.id == OUTSIDER_ID).values(cognito_sub="sub-4943-outsider"))
        await db_session.flush()

        user = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()

        assert user.cognito_sub is None

    @pytest.mark.asyncio
    async def test_a_person_with_no_github_identity_can_still_be_added(self, db_session):
        """Email-onboarded people are a permanent, legitimate population.

        For them the identity-based idempotency key does not exist, so the email
        fallback is what keeps a second call from minting a duplicate.
        """
        await _seed(db_session)
        await db_session.execute(sa.delete(UserIdentity).where(UserIdentity.user_id == OUTSIDER_ID))
        await db_session.flush()

        first = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()
        second = await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id=HOME_ORG)
        await db_session.commit()

        assert first.id == second.id
        assert len(await _rows_in_org(db_session, HOME_ORG, "marc@example.test")) == 1

    @pytest.mark.asyncio
    async def test_unknown_user_raises_the_422_error(self, db_session):
        await _seed(db_session)
        with pytest.raises(UnknownPlatformUserError) as exc:
            await org_members.add_user_to_org(db_session, user_id="pg-4943-nobody", org_id=HOME_ORG)
        assert exc.value.status_code == 422

    @pytest.mark.asyncio
    async def test_unknown_org_raises_not_found(self, db_session):
        await _seed(db_session)
        with pytest.raises(ResourceNotFoundError):
            await org_members.add_user_to_org(db_session, user_id=OUTSIDER_ID, org_id="org-4943-nope")


# ---------------------------------------------------------------------------
# The projection call site (the pattern of test_member_org_ids_projection_call_sites)
# ---------------------------------------------------------------------------


class TestProjection:
    def test_the_route_projects_the_new_org(self, seeded_db):
        """``member_org_ids`` is what the platform-mode sign-in gate reads.

        A member who is not projected is a member who cannot log in — and both orgs
        must appear, because the projection is keyed per GitHub ACCOUNT and unions
        across every ``users`` row holding it. Projecting only the new org would
        clobber the person out of the org they came from.
        """
        writer = _writer()
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer):
            resp = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID})
        assert resp.status_code == 201, resp.text

        assert _projected_orgs(writer) == [sorted([HOME_ORG, OTHER_ORG])]

    def test_a_projection_failure_does_not_fail_the_add(self, seeded_db):
        """Postgres is authoritative and already committed; reconciliation is the repair path."""
        writer = _writer()
        writer.update_user_membership_orgs = AsyncMock(side_effect=RuntimeError("dynamo down"))
        client = _client(_context(PLATFORM_ADMIN_SUB, HOME_ORG, is_admin=True), seeded_db)
        with patch("src.admin.identity.identity_index_writer.IdentityIndexWriter", return_value=writer):
            resp = client.post(f"/admin/organizations/{HOME_ORG}/members", json={"user_id": OUTSIDER_ID})

        assert resp.status_code == 201, resp.text
        assert len(asyncio.get_event_loop().run_until_complete(_memberships(seeded_db, resp.json()["id"]))) == 1
