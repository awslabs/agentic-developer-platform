"""Real hierarchy adapters: exact scope, guarded patch and non-cascading removal."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from src.admin import hierarchy, membership_revocation
from src.admin.exceptions import ResourceNotFoundError
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import Department, Organization, Team, User
from src.shared.schemas.auth import TokenContext


@pytest.fixture
async def seeded(db_session, monkeypatch):
    from src.admin import routes

    monkeypatch.setattr(routes, "write_admin_audit", AsyncMock())
    monkeypatch.setattr(hierarchy, "write_admin_audit", AsyncMock())
    monkeypatch.setattr(membership_revocation, "project_member_org_ids", AsyncMock())
    db_session.add_all([Organization(id="one", name="One"), Organization(id="two", name="Two")])
    await db_session.flush()
    db_session.add_all(
        [Department(id="dept", org_id="one", name="Dept", description="preserve"), Department(id="other-dept", org_id="two", name="Other")]
    )
    await db_session.flush()
    db_session.add(Team(id="team", org_id="one", department_id="dept", name="Team"))
    db_session.add(User(id="person", org_id="one", team_id="", email="person@example.test", cognito_sub="person-login"))
    await db_session.flush()
    db_session.add(TenantMembership(user_id="person", tenant_id="two", role="member", is_active=False))
    await db_session.commit()
    return TokenContext(
        user_id="operator",
        org_id="one",
        team_id="",
        department_id="",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


async def test_patch_preserves_omitted_fields_and_refuses_stale_revision(db_session, seeded):
    before = await hierarchy.read("one", "department", "dept", db_session, seeded)
    changed = await hierarchy.patch(
        "one", "department", "dept", hierarchy.Change(expected_revision=before["revision"], patch={"name": "Renamed"}), db_session, seeded
    )
    assert changed["resource"]["name"] == "Renamed"
    assert changed["resource"]["description"] == "preserve"
    with pytest.raises(HTTPException) as exc:
        await hierarchy.patch(
            "one", "department", "dept", hierarchy.Change(expected_revision=before["revision"], patch={"description": "wrong"}), db_session, seeded
        )
    assert exc.value.status_code == 409


async def test_foreign_parent_is_not_a_target(db_session, seeded):
    with pytest.raises(ResourceNotFoundError):
        await hierarchy.read("one", "department", "other-dept", db_session, seeded)


async def test_populated_parent_refuses_cascade(db_session, seeded):
    before = await hierarchy.read("one", "department", "dept", db_session, seeded)
    assert "teams" in before["dependent_tables"] and not before["delete_permitted"]
    with pytest.raises(HTTPException) as exc:
        await hierarchy.remove("one", "department", "dept", before["revision"], db_session, seeded)
    assert exc.value.status_code == 409
    assert await db_session.get(Team, "team") is not None


async def test_member_remove_preserves_identity_and_unrelated_memberships(db_session, seeded):
    before = await hierarchy.read("one", "member", "person", db_session, seeded)
    result = await hierarchy.remove("one", "member", "person", before["revision"], db_session, seeded)
    assert result["resource"]["membership_status"] == "revoked"
    user = await db_session.get(User, "person")
    assert user.cognito_sub == "person-login"
    from src.shared.identity.workspaces import memberships_for_login

    _, memberships = await memberships_for_login(db_session, "person-login")
    assert set(memberships) == {"two"}


async def test_member_team_list_uses_secondary_memberships(db_session, seeded):
    from src.shared.models.organization import TeamMembership

    db_session.add(TeamMembership(user_id="person", org_id="one", team_id="team", role="member", is_primary=False, source="admin"))
    await db_session.commit()
    result = await hierarchy.listing("one", "member", db_session, seeded, team_id="team")
    assert [row["id"] for row in result["items"]] == ["person"]
