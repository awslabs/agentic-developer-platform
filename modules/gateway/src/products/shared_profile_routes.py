"""Product-level Profile API; project-scoped execution contracts remain intact."""

import asyncio
import logging

from fastapi import APIRouter, Depends, Header, Response
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from src.auth.dependencies import get_current_user
from src.knowledge.product_access import require_product_access
from src.products.models import ProductProjectBinding
from src.products.routes import ProductPatch, require_profile_admission, update_product
from src.products.shared_profile import shared_profile
from src.shared.database import get_db
from src.shared.database_agent_context import get_agent_context_db

router = APIRouter(prefix="/v1/products", tags=["products"])
logger = logging.getLogger(__name__)


async def base_project(db, product):
    return await db.scalar(
        select(ProductProjectBinding.project_id).where(
            ProductProjectBinding.tenant_id == product.tenant_id,
            ProductProjectBinding.product_id == product.id,
            ProductProjectBinding.valid_to.is_(None),
            ProductProjectBinding.is_base.is_(True),
        )
    )


@router.get("/{product_id}/profile", operation_id="getSharedProductProfile", dependencies=[Depends(require_profile_admission)])
async def get_shared_profile(
    product_id: str, response: Response, db=Depends(get_db), actor=Depends(get_current_user), knowledge_db=Depends(get_agent_context_db)
):
    access = await require_product_access(db, actor, product_id)
    knowledge_count = None
    try:
        async with asyncio.timeout(3):
            knowledge_count = await knowledge_db.scalar(
                text("SELECT count(*) FROM knowledge_assets WHERE tenant_id = :tenant AND product_id = :product AND status != 'removed'"),
                {"tenant": access.product.tenant_id, "product": product_id},
            )
    except (SQLAlchemyError, TimeoutError, OSError):
        # Knowledge is a separate store. An outage must not erase saved Profile configuration.
        logger.warning("Product Profile knowledge count unavailable")
    result = await shared_profile(db, actor, access.product, knowledge_count=knowledge_count)
    result["base_project_id"] = await base_project(db, access.product)
    result["can_edit"] = access.product.archived_at is None
    result["knowledge_count"] = knowledge_count
    response.headers["Cache-Control"] = "no-store"
    return result


@router.patch("/{product_id}/profile", operation_id="updateSharedProductProfile", dependencies=[Depends(require_profile_admission)])
async def patch_shared_profile(
    product_id: str,
    patch: ProductPatch,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    expected_revision: int | None = Header(default=None, alias="If-Profile-Revision"),
    db=Depends(get_db),
    actor=Depends(get_current_user),
):
    access = await require_product_access(db, actor, product_id)
    project_id = await base_project(db, access.product)
    # Reuse compare-and-swap, idempotency receipts and product-wide change history.
    return await update_product(product_id, project_id, patch, response, idempotency_key, expected_revision, db, actor)
