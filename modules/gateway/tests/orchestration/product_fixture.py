"""Accessible Product prerequisites for new-acceptance tests."""

from sqlalchemy import select

from src.products.models import Product
from src.shared.models.organization import Team, TeamMembership, User


async def seed_product(session, *, org_id, actor_id, product_id=None, user_id=None):
    user_id = user_id or actor_id
    if await session.get(User, user_id) is None:
        session.add(User(id=user_id, org_id=org_id, team_id="binding-team", email=f"{user_id}@example.test", cognito_sub=actor_id))
    team_id = await seed_product_membership(session, org_id=org_id, user_id=user_id)
    product_id = product_id or f"product-{org_id}"
    session.add(
        Product(
            id=product_id,
            tenant_id=org_id,
            name="Test Product",
            purpose="Binding fixture",
            intended_users=["team"],
            owner_user_id=user_id,
            team_id=team_id,
            starting_point="running_product",
            created_by=user_id,
        )
    )
    await session.flush()


async def seed_product_membership(session, *, org_id, user_id):
    """Real membership prerequisites; ownership alone does not reveal a Product."""
    team_id = f"product-team-{org_id}"
    if await session.get(User, user_id) is None:
        session.add(User(id=user_id, org_id=org_id, team_id=team_id, email=f"{user_id}@example.test"))
    if await session.get(Team, team_id) is None:
        session.add(Team(id=team_id, org_id=org_id, department_id="fixture", name="Product team"))
    await session.flush()
    if not await session.scalar(select(TeamMembership.id).where(TeamMembership.user_id == user_id, TeamMembership.team_id == team_id)):
        session.add(TeamMembership(user_id=user_id, team_id=team_id, org_id=org_id, is_primary=True))
        await session.flush()
    return team_id
