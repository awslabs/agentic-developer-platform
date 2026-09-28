"""Issue #4072 (#5), item I5: the install-membership audit reports, and only reports.

Two properties matter, and they pull in opposite directions:

1. It must **surface** the forensic signature of a pre-#4072 cross-tenant install
   (an admin-level ``joined_via='app_install'`` membership in a tenant that is not
   the member's ``users.org_id``), so an operator can adjudicate it.
2. It must **change nothing** — decision D3. The signature is ambiguous: the exact
   same row is produced by the legitimate #2952 flow this issue deliberately
   preserved. Auto-revoking would strip real org admins of workspaces they own,
   repeating the #4006 mistake where an ``--apply`` mode inferred a tenant from
   ambiguous state and escalated privilege by itself.

So the report-only property is asserted directly, not assumed from the absence of
an ``--apply`` flag: the tests compare the full membership row set before and after.
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
_SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "audit_install_admin_memberships.py"
_spec = importlib.util.spec_from_file_location("audit_install_admin_memberships", _SCRIPT_PATH)
_module = importlib.util.module_from_spec(_spec)
sys.modules["audit_install_admin_memberships"] = _module
_spec.loader.exec_module(_module)

run_audit = _module.run_audit

pytestmark = pytest.mark.asyncio


async def _seed_org(db: AsyncSession, org_id: str, *, github_org_id: str | None = None) -> str:
    db.add(
        Organization(
            id=org_id,
            name=org_id,
            aws_accounts=[],
            role_mappings={},
            settings={},
            github_installation_ids=[],
            cognito_client_ids=[],
            github_org_id=github_org_id,
        )
    )
    dept = Department(id=new_uuid(), org_id=org_id, name="Default")
    db.add(dept)
    team = Team(id=new_uuid(), org_id=org_id, department_id=dept.id, name="Default")
    db.add(team)
    await db.flush()
    return team.id


async def _seed_user(db: AsyncSession, *, user_id: str, org_id: str, team_id: str, role: str = "user") -> User:
    user = User(
        id=user_id,
        org_id=org_id,
        team_id=team_id,
        email=f"{user_id}@test.com",
        name=user_id,
        cognito_sub=f"sub-{user_id}",
        role=role,
    )
    db.add(user)
    await db.flush()
    return user


async def _snapshot(db: AsyncSession) -> list[tuple]:
    """Every membership row, as comparable tuples."""
    rows = list((await db.execute(select(TenantMembership).order_by(TenantMembership.id))).scalars().all())
    return [(r.user_id, r.tenant_id, r.role, r.is_active, r.joined_via) for r in rows]


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


async def test_cross_tenant_install_admin_row_is_reported(db_session: AsyncSession):
    """The signature the pre-#4072 takeover leaves behind is surfaced."""
    home_team = await _seed_org(db_session, "attacker-home")
    await _seed_org(db_session, "victim-corp", github_org_id="98765432")
    await _seed_user(db_session, user_id="installer", org_id="attacker-home", team_id=home_team)
    db_session.add(
        TenantMembership(
            user_id="installer",
            tenant_id="victim-corp",
            role="org_admin",
            is_active=True,
            joined_via="app_install",
        )
    )
    await db_session.commit()

    # 1 == "a human should look at this", not "the deploy is unsafe".
    assert await run_audit(db_session) == 1


async def test_install_admin_row_in_own_tenant_is_not_reported(db_session: AsyncSession):
    """The overwhelmingly common case — installing for your own tenant — is quiet.

    Without this the audit would report every ordinary installer and be useless.
    """
    team = await _seed_org(db_session, "own-org", github_org_id="11111111")
    await _seed_user(db_session, user_id="normal-installer", org_id="own-org", team_id=team)
    db_session.add(
        TenantMembership(
            user_id="normal-installer",
            tenant_id="own-org",
            role="org_admin",
            is_active=True,
            joined_via="app_install",
        )
    )
    await db_session.commit()

    assert await run_audit(db_session) == 0


async def test_non_admin_cross_tenant_row_is_not_reported(db_session: AsyncSession):
    """A plain member row grants no authority, so it is not the escalation signature."""
    home_team = await _seed_org(db_session, "member-home")
    await _seed_org(db_session, "shared-workspace")
    await _seed_user(db_session, user_id="plain-member", org_id="member-home", team_id=home_team)
    db_session.add(
        TenantMembership(
            user_id="plain-member",
            tenant_id="shared-workspace",
            role="member",
            is_active=False,
            joined_via="app_install",
        )
    )
    await db_session.commit()

    assert await run_audit(db_session) == 0


async def test_rows_from_other_provenances_are_not_reported(db_session: AsyncSession):
    """Only ``app_install`` rows are in scope.

    A cross-tenant admin row created by an operator (``admin_create``) or an
    approved access request (``onboarding_approval``) went through an authenticated,
    audited path — it is not evidence of the install-callback defect, and reporting
    it would dilute the signal this audit exists to produce.
    """
    home_team = await _seed_org(db_session, "other-home")
    await _seed_org(db_session, "operator-granted")
    await _seed_user(db_session, user_id="operator-granted-user", org_id="other-home", team_id=home_team)
    db_session.add(
        TenantMembership(
            user_id="operator-granted-user",
            tenant_id="operator-granted",
            role="org_admin",
            is_active=True,
            joined_via="admin_create",
        )
    )
    await db_session.commit()

    assert await run_audit(db_session) == 0


async def test_clean_database_reports_nothing(db_session: AsyncSession):
    assert await run_audit(db_session) == 0


# ---------------------------------------------------------------------------
# Report-only (decision D3)
# ---------------------------------------------------------------------------


async def test_audit_makes_no_writes(db_session: AsyncSession):
    """The D3 guarantee, asserted on state rather than on the absence of a flag.

    The reported cohort deliberately mixes legitimate #2952 onboarding with any
    real takeover, and Postgres cannot tell them apart — the deciding fact (was
    this person an admin of that GitHub org?) lives in GitHub. So the audit must
    leave every row exactly as it found it.
    """
    home_team = await _seed_org(db_session, "attacker-home")
    await _seed_org(db_session, "victim-corp", github_org_id="98765432")
    await _seed_user(db_session, user_id="installer", org_id="attacker-home", team_id=home_team)
    db_session.add(
        TenantMembership(
            user_id="installer",
            tenant_id="victim-corp",
            role="org_admin",
            is_active=True,
            joined_via="app_install",
        )
    )
    await db_session.commit()

    before = await _snapshot(db_session)
    assert await run_audit(db_session) == 1
    after = await _snapshot(db_session)

    assert after == before


async def test_audit_exposes_no_apply_mode(db_session: AsyncSession):
    """There is no supported way to make this script mutate (D3).

    Complements the state assertion above: that one proves the current code path
    writes nothing, this one prevents a well-meaning ``--apply`` from being added
    without revisiting D3 and this test's rationale.
    """
    assert not hasattr(_module, "upsert_admin_membership")
    assert "apply" not in run_audit.__code__.co_varnames
