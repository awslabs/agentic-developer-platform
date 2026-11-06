"""Product home authorization shared by the browser and the provisioner."""

import hashlib
import json
import os
from datetime import UTC

from fastapi import HTTPException
from sqlalchemy import select

from src.gitlab.home_models import ProductGitlabHome
from src.products.models import Product
from src.shared.identity import workspaces
from src.shared.identity.product_scope import product_member, product_visibility_scope
from src.shared.models.base import utcnow
from src.shared.models.organization import TeamMembership, User

MAX_SYNC_AGE_SECONDS = 180


def enabled():
    return os.environ.get("FEATURE_GITLAB_ENABLED", "").lower() == "true"


async def login_identity(db, user):
    subject = await workspaces.login_subject_for_user(db, user)
    login = await workspaces.login_user(db, subject, refresh=True) if subject else None
    if login is None or login.user_kind != "human" or login.is_shadow or not login.email:
        return None
    return {"id": login.id, "subject": subject, "email": login.email, "name": login.name or login.email}


async def desired_members(db, product):
    """Current team AND workspace membership. Never grant from users.team_id/owner alone."""
    if product.archived_at is not None or product.lifecycle_status == "retired" or not product.team_id:
        return {}
    rows = (
        await db.execute(
            select(User, TeamMembership.role)
            .join(TeamMembership, TeamMembership.user_id == User.id)
            .where(
                User.org_id == product.tenant_id,
                User.user_kind == "human",
                User.is_shadow.is_(False),
                TeamMembership.org_id == product.tenant_id,
                TeamMembership.team_id == product.team_id,
            )
        )
    ).all()
    result = {}
    for user, role in rows:
        if await workspaces.workspace_user(db, user.id, product.tenant_id, refresh=True) is None:
            continue
        identity = await login_identity(db, user)
        if identity:
            # Team roles are deliberately mapped; an org display role grants nothing.
            access = {"viewer": 20, "reporter": 20, "member": 30, "maintainer": 40, "owner": 40, "lead": 40}.get(role, 20)
            previous = result.get(identity["id"], {}).get("access_level", 0)
            result[identity["id"]] = {**identity, "access_level": max(previous, access)}
    return result


def membership_digest(members):
    return hashlib.sha256(json.dumps(members, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


async def scoped_product(db, actor, product_id):
    member = await product_member(db, user_id=actor.user_id, org_id=actor.org_id, username=actor.cognito_username, account_type=actor.account_type)
    product = await db.scalar(select(Product).where(Product.id == product_id, product_visibility_scope(member, actor.org_id)))
    if product is None:
        raise HTTPException(404, detail={"code": "not_found"})
    return product, member


async def home_view(db, product, instance_url):
    if not enabled():
        return {"status": "disabled", "message": "GitLab is not enabled for this deployment."}
    if product.archived_at or product.lifecycle_status == "retired":
        return {"status": "archived", "message": "This Product is archived or retired."}
    if not instance_url:
        return {"status": "unavailable", "message": "The GitLab connection needs administrator attention."}
    home = await db.get(ProductGitlabHome, (product.tenant_id, product.id), populate_existing=True)
    if home is None:
        return {"status": "preparing", "message": "Your GitLab home is being prepared."}
    if home.instance_url != instance_url:
        return {"status": "unavailable", "message": "The GitLab connection needs administrator attention."}
    if home.status == "error":
        return {"status": "error", "message": "GitLab setup needs administrator attention. It will retry automatically."}
    current = await desired_members(db, product)
    age = (utcnow() - home.checked_at.replace(tzinfo=UTC)).total_seconds() if home.checked_at else float("inf")
    if (
        home.status != "ready"
        or not home.group_id
        or not home.full_path
        or age > MAX_SYNC_AGE_SECONDS
        or home.membership_digest != membership_digest(current)
    ):
        return {"status": "preparing", "message": "GitLab is synchronizing Product access. Please try again shortly."}
    return {
        "status": "ready",
        "url": f"{instance_url}/{home.full_path}",
        "group_id": home.group_id,
        "message": "Repositories, merge requests, pipelines, and documentation for this Product.",
    }


async def managed_login(db, actor, instance_url):
    member = await product_member(db, user_id=actor.user_id, org_id=actor.org_id, username=actor.cognito_username, account_type=actor.account_type)
    identity = await login_identity(db, member) if member else None
    if not identity:
        raise HTTPException(403, detail={"code": "gitlab_membership_required"})
    products = (await db.scalars(select(Product).where(product_visibility_scope(member, actor.org_id)))).all()
    for product in products:
        view = await home_view(db, product, instance_url)
        if view["status"] == "ready":
            home = await db.get(ProductGitlabHome, (product.tenant_id, product.id))
            if identity["id"] in home.members:
                return identity
    raise HTTPException(503, detail={"code": "gitlab_access_preparing", "message": "Your Product's GitLab access is being prepared."})
