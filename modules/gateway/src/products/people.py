"""Current Product people, using the existing organization identity and team model."""

from sqlalchemy import func, select

from src.admin.access_control import AccessControl
from src.admin.config import Permission
from src.admin.exceptions import AccessDeniedError
from src.shared.identity.verification import PROVEN_METHODS
from src.shared.models.organization import Team, TeamMembership, User
from src.shared.models.vault import UserIdentity


async def product_people(db, actor, member, product, *, offset=0, limit=50):
    github = (
        select(UserIdentity.provider_username)
        .where(
            UserIdentity.user_id == User.id,
            UserIdentity.org_id == actor.org_id,
            func.lower(UserIdentity.provider) == "github",
            UserIdentity.verification_method.in_(PROVEN_METHODS),
        )
        .order_by(UserIdentity.is_primary.desc(), UserIdentity.provider_user_id)
        .limit(1)
        .correlate(User)
        .scalar_subquery()
    )
    people = select(User, github.label("github_username")).where(User.org_id == actor.org_id, User.user_kind == "human", User.is_shadow.is_(False))

    def person(row):
        user, username = row
        return {"id": user.id, "name": user.name or username or "Organization member", "github_username": username}

    owner = (await db.execute(people.where(User.id == product.owner_user_id))).first()
    team = await db.scalar(select(Team).where(Team.id == product.team_id, Team.org_id == actor.org_id)) if product.team_id else None
    members = []
    has_more = False
    if team:
        memberships = select(TeamMembership.id).where(TeamMembership.user_id == User.id, TeamMembership.org_id == actor.org_id)
        # Match Product authorization: only current membership rows confer access.
        rows = (
            await db.execute(
                people.where(memberships.where(TeamMembership.team_id == team.id).exists()).order_by(User.id).offset(offset).limit(limit + 1)
            )
        ).all()
        members = [person(row) for row in rows[:limit]]
        has_more = len(rows) > limit

    access = AccessControl(db)
    permissions = {}
    for key, permission in (("manage_team", Permission.ORG_UPDATE), ("manage_people", Permission.USER_MANAGE)):
        try:
            await access.check_permission(actor, permission, target_org_id=actor.org_id)
            permissions[key] = product.archived_at is None
        except AccessDeniedError:
            permissions[key] = False
    permissions["enroll_github"] = bool(actor.is_admin) and product.archived_at is None
    return {
        "product_id": product.id,
        "revision": product.revision,
        "owner": person(owner) if owner else {"id": product.owner_user_id, "name": "Product owner", "github_username": None},
        "team": {"id": team.id, "name": team.name} if team else None,
        "members": members,
        "next_offset": offset + limit if has_more else None,
        "is_owner": product.owner_user_id == member.id,
        "permissions": permissions,
    }
