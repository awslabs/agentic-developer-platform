"""Team-membership service + REST surface (Issue #4840).

Design note §2.1 (ruling R2), §1.5b, §1.5e.

Two halves, and the split is deliberate:

**Service tests** cover the invariants the database cannot enforce in CI. The
one-primary-per-user-per-org rule is a PostgreSQL-only partial unique index
(migration 040, `uq_team_memberships_one_primary`), and `create_all()` on SQLite
never builds it — so a blind write passes tests and raises `IntegrityError` in
production. That is exactly the trap `src/admin/memberships.py:13-21` documents one
grain up. The application-layer guard tested here is the half that CI *can* verify;
`tests/migrations/test_040_team_memberships.py` asserts the real Postgres DDL.

**Route tests** use the REAL `AccessControl` over a real seeded session, with
`rbac_least_privilege_default=True`, following
`tests/admin/test_agent_registry_write_authz.py`. This matters: `get_access_control`
depends on `get_db`, so overriding it with a mock (as some older specs do) makes
every permission assertion vacuous — a 403 that can never fire. Under the
least-privilege default a membership-less caller resolves to MEMBER, so the gates
are reachable.

What each group proves:
  - a user can hold several teams, with exactly one primary (the feature)
  - a second primary is refused with the **stable, structured code** T2b asserts on
    (`team_membership_second_primary`) — not a bare 500, not free text
  - `users.team_id` tracks the primary, because the Cognito pre-token Lambda and
    every `custom:team_id` reader depend on that pointer staying correct
  - the pointer is NOT disturbed for single-team users (the "projection unchanged"
    requirement in the issue's Validation section)
  - each endpoint enforces the same permission as its sibling team route, and
    cross-org access — a user OR a team outside `{org_id}` — is refused
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin import team_memberships
from src.admin.config import AdminConfig, set_admin_config
from src.admin.exceptions import ResourceNotFoundError, SecondPrimaryTeamError
from src.admin.routes import get_current_user, router
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, TeamMembership, User
from src.shared.schemas.auth import TokenContext

ORG_A = "org-a"
ORG_B = "org-b"

MEMBER_SUB = "sub-member"
ORG_ADMIN_SUB = "sub-orgadmin"

# users.id values (NOT Cognito subs — TeamMembership.user_id is users.id)
U_MULTI = "pg-multi"
U_OUTSIDER = "pg-outsider"

TEAM_A1 = "team-a1"
TEAM_A2 = "team-a2"
TEAM_B1 = "team-b1"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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
        team_id=TEAM_A1,
        department_id="dept-a",
        account_type="human",
        is_admin=is_admin,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def _seed(db: AsyncSession) -> None:
    """Two orgs; org-a has two teams, org-b has one. A member and an org admin."""
    db.add_all([Organization(id=ORG_A, name="Org A"), Organization(id=ORG_B, name="Org B")])
    await db.flush()
    db.add_all([Department(id="dept-a", org_id=ORG_A, name="Dept A"), Department(id="dept-b", org_id=ORG_B, name="Dept B")])
    await db.flush()
    db.add_all(
        [
            Team(id=TEAM_A1, org_id=ORG_A, department_id="dept-a", name="Platform"),
            Team(id=TEAM_A2, org_id=ORG_A, department_id="dept-a", name="On-call"),
            Team(id=TEAM_B1, org_id=ORG_B, department_id="dept-b", name="Other Org Team"),
        ]
    )
    await db.flush()
    db.add_all(
        [
            User(id="pg-member", org_id=ORG_A, team_id=TEAM_A1, email="member@a.test", cognito_sub=MEMBER_SUB),
            User(id="pg-orgadmin", org_id=ORG_A, team_id=TEAM_A1, email="orgadmin@a.test", cognito_sub=ORG_ADMIN_SUB),
            User(id=U_MULTI, org_id=ORG_A, team_id=TEAM_A1, email="sre@a.test"),
            User(id=U_OUTSIDER, org_id=ORG_B, team_id=TEAM_B1, email="outsider@b.test"),
        ]
    )
    await db.flush()
    db.add_all(
        [
            TenantMembership(user_id="pg-member", tenant_id=ORG_A, role="member", is_active=True, joined_via="org_membership"),
            TenantMembership(user_id="pg-orgadmin", tenant_id=ORG_A, role="org_admin", is_active=True, joined_via="org_membership"),
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
    # get_access_control is deliberately NOT overridden — see module docstring.
    return TestClient(app, raise_server_exceptions=False)


async def _user(db: AsyncSession, user_id: str) -> User:
    return (await db.execute(sa.select(User).where(User.id == user_id))).scalar_one()


# ---------------------------------------------------------------------------
# Service: the one-primary guard (§1.5b — the half CI can verify)
# ---------------------------------------------------------------------------


class TestOnePrimaryGuard:
    @pytest.mark.asyncio
    async def test_first_membership_becomes_primary(self, db_session):
        await _seed(db_session)
        m = await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A)
        assert m.is_primary is True, "a user's first team is their primary"

    @pytest.mark.asyncio
    async def test_second_team_is_not_primary(self, db_session):
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A)
        second = await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A)
        assert second.is_primary is False
        rows = await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A)
        assert len(rows) == 2, "the whole point of the story: a user may hold several teams"
        assert sum(1 for r in rows if r.is_primary) == 1

    @pytest.mark.asyncio
    async def test_requesting_a_second_primary_is_refused(self, db_session):
        """The guard that the SQLite suite cannot get from the database."""
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)

        with pytest.raises(SecondPrimaryTeamError) as exc:
            await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A, is_primary=True)

        assert exc.value.status_code == 409
        assert exc.value.error == "team_membership_second_primary"
        assert exc.value.details["existing_primary_team_id"] == TEAM_A1
        assert exc.value.details["requested_primary_team_id"] == TEAM_A2

    @pytest.mark.asyncio
    async def test_error_code_is_the_documented_contract(self):
        """T2b branches on this exact string; changing it is a breaking change."""
        assert SecondPrimaryTeamError.ERROR_CODE == "team_membership_second_primary"

    @pytest.mark.asyncio
    async def test_re_adding_the_same_primary_team_is_idempotent(self, db_session):
        """SELECT-then-upsert keyed on (user_id, team_id) — not a blind insert."""
        await _seed(db_session)
        first = await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        again = await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        assert first.id == again.id
        rows = await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# Service: the users.team_id pointer
# ---------------------------------------------------------------------------


class TestPrimaryPointer:
    @pytest.mark.asyncio
    async def test_set_primary_updates_users_team_id(self, db_session):
        """The pointer is a cache of this table, so it must follow the primary."""
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A)

        await team_memberships.set_primary_team(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A)
        await db_session.commit()

        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A2
        rows = await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A)
        assert [r.team_id for r in rows if r.is_primary] == [TEAM_A2]
        assert sum(1 for r in rows if r.is_primary) == 1, "the old primary must be demoted, not duplicated"

    @pytest.mark.asyncio
    async def test_single_team_user_pointer_is_untouched(self, db_session):
        """The issue's 'pre-token Lambda projection unchanged' requirement.

        Adding a SECOND team must not move the claim the Lambda projects.
        """
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        await db_session.commit()
        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A1

        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A)
        await db_session.commit()

        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A1, "adding a non-primary team must not move custom:team_id"

    @pytest.mark.asyncio
    async def test_removing_primary_promotes_remaining_and_repoints(self, db_session):
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A)
        await db_session.commit()

        await team_memberships.remove_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A)
        await db_session.commit()

        rows = await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A)
        assert [r.team_id for r in rows] == [TEAM_A2]
        assert rows[0].is_primary is True, "never leave a user with teams but no primary"
        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A2

    @pytest.mark.asyncio
    async def test_removing_primary_with_two_memberships_succeeds_and_repoints(self, db_session):
        """Pin the intent of the delete-flush-promote ordering in remove_membership.

        On Postgres, promoting the successor while the old primary row is still
        pending deletion in the same flush trips uq_team_memberships_one_primary
        (UPDATEs flush before DELETEs). SQLite has no partial index, so this test
        passes either way here — the real guard is the code order (flush right
        after db.delete, before promoted.is_primary = True); this test just keeps
        the user-visible contract honest: removing a primary with two memberships
        succeeds and repoints, it does not 500.
        """
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A)
        await db_session.commit()

        await team_memberships.remove_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A)
        await db_session.commit()

        rows = await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A)
        assert [(r.team_id, r.is_primary) for r in rows] == [(TEAM_A2, True)]
        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A2

    @pytest.mark.asyncio
    async def test_removing_last_membership_clears_pointer_to_empty_string(self, db_session):
        """'' is the existing 'no team' sentinel (shadow-user + approval paths)."""
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        await db_session.commit()

        await team_memberships.remove_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A)
        await db_session.commit()

        assert await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A) == []
        assert (await _user(db_session, U_MULTI)).team_id == ""

    @pytest.mark.asyncio
    async def test_remove_is_idempotent(self, db_session):
        await _seed(db_session)
        await team_memberships.remove_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A)
        await team_memberships.remove_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A)


# ---------------------------------------------------------------------------
# Service: lazy materialization of a pre-backfill users.team_id primary
# ---------------------------------------------------------------------------


class TestLazyPrimaryMaterialization:
    """A post-backfill user (users.team_id set, NO membership rows) must not have
    their primary — and therefore custom:team_id — hijacked by the first
    add_membership call naming a different team.

    U_MULTI is exactly that shape: seeded with team_id=TEAM_A1 and no rows.
    """

    @pytest.mark.asyncio
    async def test_nonprimary_add_materializes_existing_pointer_as_primary(self, db_session):
        await _seed(db_session)
        added = await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A, is_primary=False)
        await db_session.commit()

        assert added.is_primary is False, "the requested team must not steal the primary"
        rows = await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A)
        assert {(r.team_id, r.is_primary) for r in rows} == {(TEAM_A1, True), (TEAM_A2, False)}
        materialized = next(r for r in rows if r.team_id == TEAM_A1)
        assert materialized.source == "admin", "mirrors the backfill's semantics"
        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A1, "custom:team_id source must not move"

    @pytest.mark.asyncio
    async def test_explicit_primary_add_demotes_the_materialized_row(self, db_session):
        """Explicit is_primary=True is a deliberate move, so it wins — but the
        legacy team keeps a (non-primary) membership row rather than vanishing."""
        await _seed(db_session)
        added = await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A, is_primary=True)
        await db_session.commit()

        assert added.is_primary is True
        rows = await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A)
        assert {(r.team_id, r.is_primary) for r in rows} == {(TEAM_A1, False), (TEAM_A2, True)}
        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A2

    @pytest.mark.asyncio
    async def test_empty_team_id_keeps_the_first_membership_rule(self, db_session):
        """'' is the 'no team' sentinel — nothing to materialize; auto-promote stays."""
        await _seed(db_session)
        user = await _user(db_session, U_MULTI)
        user.team_id = ""
        await db_session.commit()

        added = await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A, is_primary=False)
        await db_session.commit()

        assert added.is_primary is True, "first membership becomes primary when there is no existing pointer"
        rows = await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A)
        assert [(r.team_id, r.is_primary) for r in rows] == [(TEAM_A2, True)]
        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A2


# ---------------------------------------------------------------------------
# Service: replace-set (the UI's save action)
# ---------------------------------------------------------------------------


class TestReplaceMemberships:
    @pytest.mark.asyncio
    async def test_replace_sets_exact_membership_set(self, db_session):
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        await db_session.commit()

        await team_memberships.replace_memberships(
            db_session,
            user_id=U_MULTI,
            org_id=ORG_A,
            desired=[{"team_id": TEAM_A2, "is_primary": True, "role": "lead"}],
        )
        await db_session.commit()

        rows = await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A)
        assert [r.team_id for r in rows] == [TEAM_A2], "teams absent from the set are removed"
        assert rows[0].role == "lead"
        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A2

    @pytest.mark.asyncio
    async def test_replace_with_two_primaries_is_refused(self, db_session):
        await _seed(db_session)
        with pytest.raises(SecondPrimaryTeamError) as exc:
            await team_memberships.replace_memberships(
                db_session,
                user_id=U_MULTI,
                org_id=ORG_A,
                desired=[{"team_id": TEAM_A1, "is_primary": True}, {"team_id": TEAM_A2, "is_primary": True}],
            )
        assert exc.value.error == "team_membership_second_primary"

    @pytest.mark.asyncio
    async def test_replace_without_a_primary_promotes_one(self, db_session):
        """A non-empty set must never leave the user with teams but no primary."""
        await _seed(db_session)
        rows = await team_memberships.replace_memberships(
            db_session,
            user_id=U_MULTI,
            org_id=ORG_A,
            desired=[{"team_id": TEAM_A1}, {"team_id": TEAM_A2}],
        )
        await db_session.commit()
        assert sum(1 for r in rows if r.is_primary) == 1

    @pytest.mark.asyncio
    async def test_replace_without_a_primary_keeps_the_current_primary(self, db_session):
        """An unflagged save whose set still contains the current primary must not
        move it (moving it re-points custom:team_id); first-entry promotion is only
        the fallback when the current primary left the set."""
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A2, org_id=ORG_A)
        await db_session.commit()

        rows = await team_memberships.replace_memberships(
            db_session,
            user_id=U_MULTI,
            org_id=ORG_A,
            # Current primary (TEAM_A1) deliberately NOT the first entry.
            desired=[{"team_id": TEAM_A2}, {"team_id": TEAM_A1}],
        )
        await db_session.commit()

        assert [r.team_id for r in rows if r.is_primary] == [TEAM_A1], "primary must stay where it was"
        assert (await _user(db_session, U_MULTI)).team_id == TEAM_A1

    @pytest.mark.asyncio
    async def test_replace_is_idempotent_and_preserves_row_identity(self, db_session):
        await _seed(db_session)
        desired = [{"team_id": TEAM_A1, "is_primary": True}, {"team_id": TEAM_A2}]
        first = await team_memberships.replace_memberships(db_session, user_id=U_MULTI, org_id=ORG_A, desired=desired)
        await db_session.commit()
        first_ids = {r.team_id: r.id for r in first}

        second = await team_memberships.replace_memberships(db_session, user_id=U_MULTI, org_id=ORG_A, desired=desired)
        await db_session.commit()

        assert {r.team_id: r.id for r in second} == first_ids, "existing rows are updated in place, not recreated"

    @pytest.mark.asyncio
    async def test_replace_with_empty_set_removes_everything(self, db_session):
        await _seed(db_session)
        await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, is_primary=True)
        await db_session.commit()

        await team_memberships.replace_memberships(db_session, user_id=U_MULTI, org_id=ORG_A, desired=[])
        await db_session.commit()

        assert await team_memberships.list_memberships(db_session, user_id=U_MULTI, org_id=ORG_A) == []
        assert (await _user(db_session, U_MULTI)).team_id == ""

    @pytest.mark.asyncio
    async def test_duplicate_team_ids_collapse(self, db_session):
        """A sloppy client must not be able to trip UNIQUE (user_id, team_id)."""
        await _seed(db_session)
        rows = await team_memberships.replace_memberships(
            db_session,
            user_id=U_MULTI,
            org_id=ORG_A,
            desired=[{"team_id": TEAM_A1}, {"team_id": TEAM_A1, "role": "lead"}],
        )
        await db_session.commit()
        assert len(rows) == 1
        assert rows[0].role == "lead", "last entry wins"


# ---------------------------------------------------------------------------
# Service: role normalization + tenant scoping
# ---------------------------------------------------------------------------


class TestRoleNormalization:
    @pytest.mark.parametrize("raw", ["org_admin", "platform_admin", "admin", "", None, "nonsense"])
    def test_unrecognized_roles_collapse_to_member(self, raw):
        """A team row must never read as a privilege grant (mirror of memberships.py)."""
        assert team_memberships.normalize_team_role(raw) == "member"

    @pytest.mark.parametrize(("raw", "expected"), [("member", "member"), ("lead", "lead"), ("LEAD", "lead"), (" Lead ", "lead")])
    def test_valid_roles_are_kept_lowercased(self, raw, expected):
        assert team_memberships.normalize_team_role(raw) == expected

    @pytest.mark.asyncio
    async def test_admin_role_is_not_stored_on_a_membership(self, db_session):
        await _seed(db_session)
        m = await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A, role="org_admin")
        assert m.role == "member"


class TestServiceTenantScoping:
    @pytest.mark.asyncio
    async def test_user_from_another_org_is_not_found(self, db_session):
        await _seed(db_session)
        with pytest.raises(ResourceNotFoundError):
            await team_memberships.add_membership(db_session, user_id=U_OUTSIDER, team_id=TEAM_A1, org_id=ORG_A)

    @pytest.mark.asyncio
    async def test_team_from_another_org_is_not_found(self, db_session):
        await _seed(db_session)
        with pytest.raises(ResourceNotFoundError):
            await team_memberships.add_membership(db_session, user_id=U_MULTI, team_id=TEAM_B1, org_id=ORG_A)

    @pytest.mark.asyncio
    async def test_replace_refuses_a_team_from_another_org(self, db_session):
        await _seed(db_session)
        with pytest.raises(ResourceNotFoundError):
            await team_memberships.replace_memberships(db_session, user_id=U_MULTI, org_id=ORG_A, desired=[{"team_id": TEAM_B1}])


# ---------------------------------------------------------------------------
# Routes: permission parity with the sibling team routes
# ---------------------------------------------------------------------------


class TestRoutePermissions:
    """Each endpoint must gate on the same permission as its sibling team route."""

    def test_member_cannot_add_a_member(self, seeded_db):
        client = _client(_context(MEMBER_SUB, ORG_A), seeded_db)
        resp = client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_A1}/members", json={"user_id": U_MULTI})
        assert resp.status_code == 403
        assert resp.json()["error"] == "access_denied"

    def test_member_cannot_replace_membership_set(self, seeded_db):
        client = _client(_context(MEMBER_SUB, ORG_A), seeded_db)
        resp = client.put(f"/admin/organizations/{ORG_A}/users/{U_MULTI}/teams", json={"memberships": [{"team_id": TEAM_A1}]})
        assert resp.status_code == 403

    def test_member_cannot_remove_a_member(self, seeded_db):
        client = _client(_context(MEMBER_SUB, ORG_A), seeded_db)
        resp = client.delete(f"/admin/organizations/{ORG_A}/teams/{TEAM_A1}/members/{U_MULTI}")
        assert resp.status_code == 403

    def test_plain_member_cannot_read_memberships(self, seeded_db):
        """Reads gate on ORG_READ — exactly the sibling list_teams route.

        `AdminRole.MEMBER` does NOT hold ORG_READ (`admin/config.py:138`: MEMBER has
        USAGE_READ + PLAN_DRAFT only), so a plain member is refused. Asserted
        explicitly because "read endpoints are open to members" is the natural
        assumption and it is wrong here; the gate is inherited from the sibling
        route rather than chosen, which is the parity the issue asked for.
        """
        client = _client(_context(MEMBER_SUB, ORG_A), seeded_db)
        resp = client.get(f"/admin/organizations/{ORG_A}/users/{U_MULTI}/teams")
        assert resp.status_code == 403

    def test_org_admin_can_read_memberships(self, seeded_db):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.get(f"/admin/organizations/{ORG_A}/users/{U_MULTI}/teams")
        assert resp.status_code == 200

    def test_org_admin_can_read_org_teams(self, seeded_db):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.get(f"/admin/organizations/{ORG_A}/teams")
        assert resp.status_code == 200

    def test_org_admin_can_write(self, seeded_db):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_A1}/members", json={"user_id": U_MULTI})
        assert resp.status_code == 201, resp.text


class TestRouteCrossOrgRefusal:
    """A caller must not reach across tenants, even holding ORG_UPDATE at home."""

    def test_cannot_write_into_another_org(self, seeded_db):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.post(f"/admin/organizations/{ORG_B}/teams/{TEAM_B1}/members", json={"user_id": U_OUTSIDER})
        assert resp.status_code == 403

    def test_cannot_read_another_org(self, seeded_db):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.get(f"/admin/organizations/{ORG_B}/users/{U_OUTSIDER}/teams")
        assert resp.status_code == 403

    def test_user_outside_the_path_org_is_refused(self, seeded_db):
        """Tenant scoping beyond the permission check: right org, wrong user."""
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_A1}/members", json={"user_id": U_OUTSIDER})
        assert resp.status_code == 404
        assert resp.json()["error"] == "resource_not_found"

    def test_team_outside_the_path_org_is_refused(self, seeded_db):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_B1}/members", json={"user_id": U_MULTI})
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Routes: behavior
# ---------------------------------------------------------------------------


class TestRouteBehavior:
    def test_second_primary_returns_the_structured_409(self, seeded_db):
        """T2b's acceptance criterion: the UI surfaces the SERVER's message.

        So the response must carry a machine-readable code and a human message —
        not a bare 500 and not a free-text string.
        """
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        first = client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_A1}/members", json={"user_id": U_MULTI, "is_primary": True})
        assert first.status_code == 201, first.text

        resp = client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_A2}/members", json={"user_id": U_MULTI, "is_primary": True})
        assert resp.status_code == 409
        body = resp.json()
        assert body["error"] == "team_membership_second_primary"
        assert body["message"], "a human-readable message is half the contract"
        assert body["details"]["existing_primary_team_id"] == TEAM_A1

    def test_list_returns_memberships_primary_first(self, seeded_db):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_A1}/members", json={"user_id": U_MULTI, "is_primary": True})
        client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_A2}/members", json={"user_id": U_MULTI})

        resp = client.get(f"/admin/organizations/{ORG_A}/users/{U_MULTI}/teams")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert body["items"][0]["is_primary"] is True
        assert body["items"][0]["team_id"] == TEAM_A1
        assert {i["team_id"] for i in body["items"]} == {TEAM_A1, TEAM_A2}
        assert body["items"][0]["source"] == "admin"

    def test_replace_set_round_trips(self, seeded_db):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.put(
            f"/admin/organizations/{ORG_A}/users/{U_MULTI}/teams",
            json={"memberships": [{"team_id": TEAM_A1, "is_primary": True}, {"team_id": TEAM_A2, "role": "lead"}]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        by_team = {i["team_id"]: i for i in body["items"]}
        assert by_team[TEAM_A1]["is_primary"] is True
        assert by_team[TEAM_A2]["role"] == "lead"

    def test_delete_removes_the_membership(self, seeded_db):
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_A1}/members", json={"user_id": U_MULTI, "is_primary": True})
        client.post(f"/admin/organizations/{ORG_A}/teams/{TEAM_A2}/members", json={"user_id": U_MULTI})

        resp = client.delete(f"/admin/organizations/{ORG_A}/teams/{TEAM_A2}/members/{U_MULTI}")
        assert resp.status_code == 204

        listed = client.get(f"/admin/organizations/{ORG_A}/users/{U_MULTI}/teams").json()
        assert [i["team_id"] for i in listed["items"]] == [TEAM_A1]

    def test_overlong_source_is_rejected_422(self, seeded_db):
        """The column is String(32); without a schema bound an over-long source
        would reach Postgres and 500 with StringDataRightTruncation."""
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.post(
            f"/admin/organizations/{ORG_A}/teams/{TEAM_A1}/members",
            json={"user_id": U_MULTI, "source": "x" * 33},
        )
        assert resp.status_code == 422

    def test_org_wide_team_list_spans_departments(self, seeded_db):
        """The gap this closes: only a per-department listing existed before."""
        client = _client(_context(ORG_ADMIN_SUB, ORG_A), seeded_db)
        resp = client.get(f"/admin/organizations/{ORG_A}/teams")
        assert resp.status_code == 200
        body = resp.json()
        assert {t["id"] for t in body["items"]} == {TEAM_A1, TEAM_A2}
        assert TEAM_B1 not in {t["id"] for t in body["items"]}, "must not leak another org's teams"


class TestModelRoundTrip:
    @pytest.mark.asyncio
    async def test_defaults(self, db_session):
        await _seed(db_session)
        db_session.add(TeamMembership(user_id=U_MULTI, team_id=TEAM_A1, org_id=ORG_A))
        await db_session.commit()

        row = (await db_session.execute(sa.select(TeamMembership).where(TeamMembership.user_id == U_MULTI))).scalar_one()
        assert row.role == "member"
        assert row.is_primary is False
        assert row.source == "admin"
        assert row.external_id is None
        assert row.synced_at is None
        assert row.created_at is not None
