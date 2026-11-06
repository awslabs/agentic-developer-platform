"""Protected, evaluation-only retirement of one manifest-owned Product."""

import os
import re
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.agent_registry import parse_assumed_role_arn
from src.auth.caller_provenance import verified_caller_identity
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.service_authorization import require_service_operation
from src.products.evaluation_cleanup import FixtureOwnershipError, retire_run_product
from src.shared.database import get_db

router = APIRouter(prefix="/internal/v1/product-evaluation", tags=["product-evaluation"])
EVALUATOR_REGISTRY_ID = "00000000-0000-0000-0000-000000008243"
RECOVERY_REGISTRY_ID = "00000000-0000-0000-0000-000008243001"
BROWSER_REGISTRY_ID = "00000000-0000-0000-0000-000008243002"
CAPABILITY = "internal:product-fixture:retire"


class OwnedProductManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    actor_id: str
    run_id: str
    source_revision: str
    idempotency_key: str
    product_id: str | None = None
    project_id: str | None = None
    scope: dict | None = None
    scope_fixture_mode: Literal["connected_scope"] | None = None
    fixture_editor_id: str | None = None


@router.post("/retire")
async def retire_owned_product(
    request: Request,
    manifest: OwnedProductManifest,
    _: None = Depends(verify_internal_or_irsa),
    db: AsyncSession = Depends(get_db),
) -> dict:
    principal = require_service_operation(request, CAPABILITY)
    role_setting = {
        EVALUATOR_REGISTRY_ID: "ADP_PRODUCT_EVALUATOR_ROLE_ARN",
        RECOVERY_REGISTRY_ID: "ADP_PRODUCT_RECOVERY_ROLE_ARN",
        BROWSER_REGISTRY_ID: "ADP_PRODUCT_BROWSER_RETIREMENT_ROLE_ARN",
    }.get(getattr(principal, "agent_registry_id", None))
    if role_setting is None:
        raise HTTPException(403, "protected evaluator identity required")
    role = os.environ.get(role_setting, "")
    caller = verified_caller_identity(request)
    if not re.fullmatch(r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@-]+", role) or not caller or parse_assumed_role_arn(caller) != role:
        raise HTTPException(403, "configured evaluator role required")
    try:
        async with db.begin():
            return await retire_run_product(db, manifest.model_dump(exclude_none=True))
    except FixtureOwnershipError:
        raise HTTPException(409, "Product fixture ownership could not be verified") from None
