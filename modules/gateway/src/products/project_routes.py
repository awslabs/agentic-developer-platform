"""Discover governed Projects without exposing another workspace's records."""

from fastapi import APIRouter, Depends, Query, Response
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.products.models import Product, ProductProjectBinding, Project
from src.products.routes import current_owner
from src.shared.database import get_db
from src.shared.identity.product_scope import product_visibility_scope
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/v1/projects", tags=["products"])


@router.get("", operation_id="listProjects")
async def list_projects(
    response: Response,
    cursor: str | None = None,
    limit: int = Query(100, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    actor: TokenContext = Depends(get_current_user),
):
    member = await current_owner(db, actor)
    rows = (
        await db.execute(
            select(Project, Product, ProductProjectBinding.is_base)
            .outerjoin(
                ProductProjectBinding,
                (Project.tenant_id == ProductProjectBinding.tenant_id)
                & (Project.id == ProductProjectBinding.project_id)
                & ProductProjectBinding.valid_to.is_(None),
            )
            .outerjoin(
                Product,
                (Product.tenant_id == ProductProjectBinding.tenant_id) & (Product.id == ProductProjectBinding.product_id),
            )
            .where(
                Project.tenant_id == actor.org_id,
                Project.archived_at.is_(None),
                Project.id > (cursor or ""),
                or_(
                    and_(ProductProjectBinding.id.is_(None), Project.owner_user_id == member.id),
                    product_visibility_scope(member, actor.org_id, is_platform_admin=actor.is_admin),
                ),
            )
            .order_by(Project.id)
            .limit(limit + 1)
        )
    ).all()
    page = rows[:limit]
    response.headers["Cache-Control"] = "no-store"
    return {
        "items": [
            {
                "id": project.id,
                "name": project.name,
                "state": project.state,
                "product_id": product.id if product is not None else None,
                "is_base": bool(is_base),
            }
            for project, product, is_base in page
        ],
        "next_cursor": page[-1][0].id if len(rows) > limit else None,
    }
