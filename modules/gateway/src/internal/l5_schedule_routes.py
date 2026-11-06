"""IAM-only dispatch of existing schedule receipts through common L5 admission."""

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from l5_services.events.observations import observe
from l5_services.events.sources import admit_source_receipt
from l5_services.models.event import EventReceipt
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.l5_schedule_scope import schedule_worker_id
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.l5_product_routes import service_actor
from src.l5_integration import backend
from src.l5_security_schedule import schedule_authority as security_schedule_authority
from src.l5_service_identity import worker_id
from src.l5_transform_schedule import freeze_branch_occurrence
from src.l5_transform_schedule import schedule_authority as transform_schedule_authority
from src.l5_wa_schedule import schedule_authority
from src.shared.database import get_db

router = APIRouter(prefix="/internal/v1/l5", tags=["internal-l5"], dependencies=[Depends(verify_internal_or_irsa)])


class ScheduleReceiptKey(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    tenant_id: str = Field(min_length=1, max_length=255)
    product_id: str = Field(min_length=1, max_length=255)
    project_id: str = Field(min_length=1, max_length=255)
    trigger_id: str = Field(min_length=1, max_length=255)
    event_id: str = Field(min_length=1, max_length=255)


@router.post("/well-architected/schedule-admissions", operation_id="admitScheduledWellArchitectedReview")
async def admit_schedule(body: ScheduleReceiptKey, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    return await _admit_schedule("well_architected", schedule_authority, body, request, response, db)


@router.post("/transform/schedule-admissions", operation_id="admitScheduledTransform")
async def admit_transform_schedule(body: ScheduleReceiptKey, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    return await _admit_schedule("transform", transform_schedule_authority, body, request, response, db)


@router.post("/security/schedule-admissions", operation_id="admitScheduledSecurityAssessment")
async def admit_security_schedule(body: ScheduleReceiptKey, request: Request, response: Response, db: AsyncSession = Depends(get_db)):
    return await _admit_schedule("security", security_schedule_authority, body, request, response, db)


async def _admit_schedule(service_id, authority, body, request, response, db):
    actor = service_actor(request)
    if schedule_worker_id(actor) != worker_id(service_id):
        raise HTTPException(403, detail={"code": "access_denied"})
    response.headers["Cache-Control"] = "no-store"
    key = (body.tenant_id, body.product_id, body.project_id, service_id, body.trigger_id, body.event_id)
    receipt = await db.get(EventReceipt, key)
    if receipt is None or receipt.event_kind != "schedule":
        raise HTTPException(404, detail={"code": "not_found"})
    selected = backend(service_id, request=request, db=db, actor=actor, x_l5_delegation_id=None)
    if not callable(selected.publish):
        raise HTTPException(503, detail={"code": "work_queue_unavailable"})

    async def verify_target(current):
        return await authority(selected, current)

    now = datetime.now(UTC)
    try:
        if service_id == "transform":
            await freeze_branch_occurrence(selected, receipt)
            now = datetime.now(UTC)
        run = await admit_source_receipt(
            db, selected.sessions, receipt, verify_target, selected.publish, now=now, budget_guard=selected._require_devops_budget
        )
    except HTTPException as error:
        await db.rollback()
        receipt = await db.get(EventReceipt, key, populate_existing=True)
        code = error.detail.get("code") if isinstance(error.detail, dict) else None
        observe(db, receipt, "admission_denied", now, reason=code if isinstance(code, str) and len(code) <= 64 else "admission_unavailable")
        await db.commit()
        raise
    observe(db, receipt, "admitted", now)
    await db.commit()
    return {"event_id": receipt.event_id, "state": "admitted", "run_id": run.run_id}
