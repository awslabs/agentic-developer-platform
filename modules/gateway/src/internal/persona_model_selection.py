"""Producer-only preference lookup; independent of protected worker admission."""

import hashlib
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.persona_models.service import PreferenceRejectedError
from src.agentauth.launch_configuration import mapping_enabled, select_for_dispatch
from src.agentauth.work_routes import PROOF_HEADER, verify_producer
from src.shared.database import get_db

router = APIRouter(prefix="/internal/v1/agent/persona-model", tags=["persona-models"])


class DispatchSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(min_length=1, max_length=255)
    user_id: str = Field(min_length=1, max_length=255)
    persona: str = Field(min_length=1, max_length=128)
    direct_model: str | None = Field(default=None, max_length=255)


@router.post("/resolve")
async def resolve_dispatch_selection(body: DispatchSelectionRequest, request: Request, db: AsyncSession = Depends(get_db)):
    # Bind the producer signature to the exact bytes, including tenant and owner.
    # Shared worker keys, registry labels and caller-selected identity headers
    # cannot identify a producer. Never accept them in place of this STS proof.
    raw = await request.body()
    if len(raw) > 4096:
        raise HTTPException(413, "request too large")
    roles = set(filter(None, os.environ.get("PERSONA_MODEL_PRODUCER_ROLES", "").split(",")))
    await verify_producer(request.headers.get(PROOF_HEADER, ""), hashlib.sha256(raw).hexdigest(), allowed_roles=roles)
    if not mapping_enabled():
        raise HTTPException(503, "persona model mapping is not enabled")
    try:
        return await select_for_dispatch(db, org_id=body.tenant_id, user_id=body.user_id, persona=body.persona, direct_model=body.direct_model)
    except PreferenceRejectedError as exc:
        raise HTTPException(422, {"reason": exc.reason, "message": exc.message}) from None
