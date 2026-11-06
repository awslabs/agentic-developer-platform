"""Explicit current team assignments for Product scenario fixtures."""

from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from src.admin.platform_authority import set_platform_admin_grant
from src.shared.models.organization import Team, TeamMembership, User


async def add_team_member(db, actor, *, team_id=None, primary=True):
    """Assign an already seeded actor; never infer membership during requests."""
    await db.flush()
    team_id = team_id or actor.team_id
    if await db.get(Team, team_id) is None:
        db.add(Team(id=team_id, org_id=actor.org_id, department_id="fixture-department", name=team_id))
        await db.flush()
    membership = await db.scalar(select(TeamMembership).where(TeamMembership.user_id == actor.user_id, TeamMembership.team_id == team_id))
    if membership is None:
        membership = TeamMembership(user_id=actor.user_id, org_id=actor.org_id, team_id=team_id, is_primary=primary)
        db.add(membership)
    await db.flush()
    return membership


async def grant_platform_admin(db, actor, monkeypatch):
    """Give a callback initiator current server authority, including IdP proof."""
    user = await db.get(User, actor.user_id)
    subject = str(uuid5(NAMESPACE_URL, actor.user_id))
    user.cognito_sub = subject
    user.cognito_username = "product-callback-admin"
    actor.cognito_username = user.cognito_username
    actor.auth_source = "jwt"
    actor.is_admin = True
    await set_platform_admin_grant(db, user.cognito_sub, enabled=True)
    await db.commit()
    monkeypatch.setattr("src.admin.platform_authority.current_cognito_admin", lambda candidate, username: candidate == subject)

    async def synchronize_team(_self, candidate, org_id, team_id, department_id):
        assert candidate == subject and org_id == actor.org_id
        assert team_id and department_id

    monkeypatch.setattr("src.admin.team_membership_claims.CognitoWorkspaceClaims.set_team", synchronize_team)


async def revoke_platform_admin(db, actor):
    user = await db.get(User, actor.user_id)
    await set_platform_admin_grant(db, user.cognito_sub, enabled=False)
    actor.is_admin = False


async def select_creation_team(db, actor, team_id):
    """Start sharing-transition scenarios with an actual separate primary team."""
    from src.admin.team_memberships import set_primary_team

    await add_team_member(db, actor, team_id=team_id, primary=False)
    await set_primary_team(db, user_id=actor.user_id, team_id=team_id, org_id=actor.org_id)
    await db.commit()
