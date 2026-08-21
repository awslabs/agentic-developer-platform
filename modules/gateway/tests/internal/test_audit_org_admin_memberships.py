"""Issue #4006: the --apply gap-fill must be tenant-scoped and cover the no-row cohort.

The pre-#4006 gap-fill UPDATE carried NO ``tenant_id`` predicate, so it promoted
whichever membership row happened to be ``is_active`` — and ``is_active`` is
per-user session state flipped by ``switch_tenant``. Running it against a user who
earned org_admin in org A while actively browsing org B granted them org_admin *in
org B*: a cross-tenant privilege escalation performed by the remediation script
itself. It also could not help the cohort the bug actually produced — admins with
no membership row at all.

These tests pin both properties, including the explicit cross-tenant-escalation
guard the issue's Validation section names.

Postgres-vs-SQLite caveat: this suite is SQLite (``tests/conftest.py``), which
builds neither migration 021's PostgreSQL-only partial unique index
``uq_tenant_memberships_one_active`` nor the composite unique (neither is declared
on the model). The tests therefore prove the *portable* semantics the script
implements — one row per (user, tenant), ``is_active`` only when the user holds no
other active row — which is what keeps those Postgres constraints satisfied. They
cannot prove the constraints themselves; that requires Postgres.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.base import new_uuid
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User

# The script lives outside the importable package (it is deliberately free of
# `src.` imports because the Docker image does not ship scripts/).
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audit_org_admin_memberships.py"
_spec = importlib.util.spec_from_file_location("audit_org_admin_memberships", _SCRIPT_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["audit_org_admin_memberships"] = _module
_spec.loader.exec_module(_module)

run_audit = _module.run_audit

pytestmark = pytest.mark.asyncio


async def _seed_org(db: AsyncSession, org_id: str) -> str:
    """Create an org with the default department/team; returns the team id."""
    db.add(
        Organization(
            id=org_id,
            name=org_id,
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
        )
    )
    dept = Department(id=new_uuid(), org_id=org_id, name="Default")
    db.add(dept)
    team = Team(id=new_uuid(), org_id=org_id, department_id=dept.id, name="Default")
    db.add(team)
    await db.flush()
    return team.id


async def _seed_user(
    db: AsyncSession,
    *,
    user_id: str,
    org_id: str,
    team_id: str,
    role: str,
    is_shadow: bool = False,
) -> User:
    user = User(
        id=user_id,
        org_id=org_id,
        team_id=team_id,
        email=f"{user_id}@test.com",
        name=user_id,
        cognito_sub=f"sub-{user_id}",
        role=role,
        is_shadow=is_shadow,
    )
    db.add(user)
    await db.flush()
    return user


async def _memberships(db: AsyncSession, user_id: str) -> list[TenantMembership]:
    return list((await db.execute(select(TenantMembership).where(TenantMembership.user_id == user_id))).scalars().all())


# ---------------------------------------------------------------------------
# The no-row cohort — what the onboarding bug actually produced
# ---------------------------------------------------------------------------


async def test_no_row_admin_gets_active_org_admin_row_in_users_org(db_session: AsyncSession):
    """The core backfill: a no-row admin gets one active org_admin row, and it
    lands in users.org_id — the only tenant users.role authority can be inferred for."""
    team_id = await _seed_org(db_session, "backfill-org")
    await _seed_user(db_session, user_id="norow-admin", org_id="backfill-org", team_id=team_id, role="org_admin")
    await db_session.commit()

    assert await _memberships(db_session, "norow-admin") == []
    assert await run_audit(db_session, apply=True) == 0

    rows = await _memberships(db_session, "norow-admin")
    assert len(rows) == 1
    assert rows[0].tenant_id == "backfill-org"
    assert rows[0].role == "org_admin"
    assert rows[0].is_active is True
    assert rows[0].joined_via == "backfill"


async def test_platform_admin_user_gets_org_admin_membership_not_platform_admin(db_session: AsyncSession):
    """A tenant-scoped row must never carry platform authority (#3981)."""
    team_id = await _seed_org(db_session, "plat-org")
    await _seed_user(db_session, user_id="plat-admin", org_id="plat-org", team_id=team_id, role="platform_admin")
    await db_session.commit()

    await run_audit(db_session, apply=True)

    rows = await _memberships(db_session, "plat-admin")
    assert len(rows) == 1
    assert rows[0].role == "org_admin"


async def test_non_admin_user_gets_no_membership(db_session: AsyncSession):
    """Only admin-level users.role values are in scope — no blanket elevation."""
    team_id = await _seed_org(db_session, "member-org")
    await _seed_user(db_session, user_id="plain-user", org_id="member-org", team_id=team_id, role="member")
    await db_session.commit()

    assert await run_audit(db_session, apply=True) == 0
    assert await _memberships(db_session, "plain-user") == []


async def test_shadow_users_are_skipped(db_session: AsyncSession):
    """Shadow users are placeholders, not principals — they must not be granted rows."""
    team_id = await _seed_org(db_session, "shadow-org")
    await _seed_user(db_session, user_id="shadow-admin", org_id="shadow-org", team_id=team_id, role="org_admin", is_shadow=True)
    await db_session.commit()

    assert await run_audit(db_session, apply=True) == 0
    assert await _memberships(db_session, "shadow-admin") == []


# ---------------------------------------------------------------------------
# The cross-tenant escalation guard (the bug in the pre-#4006 gap-fill)
# ---------------------------------------------------------------------------


async def test_active_row_in_other_tenant_is_not_elevated(db_session: AsyncSession):
    """THE regression guard: a user whose active row belongs to another tenant must
    NOT be promoted there. They get an inactive admin row in users.org_id instead,
    and their active row in the other tenant keeps its original role."""
    home_team = await _seed_org(db_session, "home-org")
    await _seed_org(db_session, "other-org")
    await _seed_user(db_session, user_id="multi-admin", org_id="home-org", team_id=home_team, role="org_admin")
    db_session.add(
        TenantMembership(
            user_id="multi-admin",
            tenant_id="other-org",
            role="member",
            is_active=True,
            joined_via="org_membership",
        )
    )
    await db_session.commit()

    # Still a gap afterwards (the home-org row is inactive), so exit code is 1.
    assert await run_audit(db_session, apply=True) == 1

    rows = {m.tenant_id: m for m in await _memberships(db_session, "multi-admin")}
    assert set(rows) == {"home-org", "other-org"}

    # The browsed tenant is untouched — this is the escalation that used to happen.
    assert rows["other-org"].role == "member"
    assert rows["other-org"].is_active is True

    # The home tenant gets the authority, but inactive: only one row may be active.
    assert rows["home-org"].role == "org_admin"
    assert rows["home-org"].is_active is False
    assert rows["home-org"].joined_via == "backfill"


async def test_existing_home_tenant_row_is_promoted_in_place(db_session: AsyncSession):
    """A stale 'member' row in the admin's own tenant is promoted, not duplicated."""
    team_id = await _seed_org(db_session, "promote-org")
    await _seed_user(db_session, user_id="stale-admin", org_id="promote-org", team_id=team_id, role="org_admin")
    db_session.add(
        TenantMembership(
            user_id="stale-admin",
            tenant_id="promote-org",
            role="member",
            is_active=True,
            joined_via="org_membership",
        )
    )
    await db_session.commit()

    assert await run_audit(db_session, apply=True) == 0

    rows = await _memberships(db_session, "stale-admin")
    assert len(rows) == 1
    assert rows[0].role == "org_admin"
    assert rows[0].is_active is True
    # joined_via is preserved — the row's provenance is not rewritten.
    assert rows[0].joined_via == "org_membership"


# ---------------------------------------------------------------------------
# Dry-run and idempotency
# ---------------------------------------------------------------------------


async def test_dry_run_makes_no_writes(db_session: AsyncSession):
    """The default mode reports and returns 1 without touching the database."""
    team_id = await _seed_org(db_session, "dry-org")
    await _seed_user(db_session, user_id="dry-admin", org_id="dry-org", team_id=team_id, role="org_admin")
    await db_session.commit()

    assert await run_audit(db_session) == 1
    assert await _memberships(db_session, "dry-admin") == []


async def test_apply_is_idempotent(db_session: AsyncSession):
    """Re-running --apply is a no-op: no second row, no churn."""
    team_id = await _seed_org(db_session, "idem-org")
    await _seed_user(db_session, user_id="idem-admin", org_id="idem-org", team_id=team_id, role="org_admin")
    await db_session.commit()

    await run_audit(db_session, apply=True)
    first = await _memberships(db_session, "idem-admin")
    assert len(first) == 1

    assert await run_audit(db_session, apply=True) == 0
    second = await _memberships(db_session, "idem-admin")
    assert len(second) == 1
    assert second[0].id == first[0].id
