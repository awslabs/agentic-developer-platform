"""Current Product authority shared by registry and Knowledge retrieval."""

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.products.models import Product
from src.shared.identity.product_scope import product_member, product_visibility_scope
from src.shared.models.onboarding import TenantMembership
from src.shared.models.organization import TeamMembership, User


@dataclass(frozen=True)
class ProductAccess:
    product: Product
    member_id: str


async def require_product_access(db: AsyncSession, current_user: Any, product_id: str, *, write: bool = False, lock: bool = False) -> ProductAccess:
    """Require current Product team membership or verified platform authority."""
    is_platform_admin = getattr(current_user, "is_admin", False) is True and getattr(current_user, "account_type", "human") == "human"
    member = await product_member(
        db,
        user_id=current_user.user_id,
        org_id=current_user.org_id,
        username=getattr(current_user, "cognito_username", ""),
        account_type=getattr(current_user, "account_type", "human"),
        is_platform_admin=is_platform_admin,
    )
    if member is None:
        raise HTTPException(404, detail="Product not found")
    if lock:
        await db.execute(select(User.id).where(User.id == member.id).with_for_update(read=True))
        await db.execute(
            select(TenantMembership.id)
            .where(TenantMembership.user_id == member.id, TenantMembership.tenant_id == current_user.org_id)
            .with_for_update(read=True)
        )
        await db.execute(
            select(TeamMembership.id)
            .where(TeamMembership.user_id == member.id, TeamMembership.org_id == current_user.org_id)
            .with_for_update(read=True)
        )
        # A revocation may have committed while waiting for a row lock.
        member = await product_member(
            db,
            user_id=current_user.user_id,
            org_id=current_user.org_id,
            username=getattr(current_user, "cognito_username", ""),
            account_type=getattr(current_user, "account_type", "human"),
            is_platform_admin=is_platform_admin,
        )
        if member is None:
            raise HTTPException(404, detail="Product not found")
    query = (
        select(Product)
        .where(Product.id == product_id, product_visibility_scope(member, current_user.org_id, is_platform_admin=is_platform_admin))
        .execution_options(populate_existing=True)
    )
    if lock:
        query = query.with_for_update(read=True)
    product = await db.scalar(query)
    if product is None:
        raise HTTPException(404, detail="Product not found")
    if write and product.archived_at is not None:
        raise HTTPException(409, detail="Archived Products are read-only")
    return ProductAccess(product=product, member_id=member.id)
