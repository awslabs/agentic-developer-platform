"""Revision-guarded campaign quota edits with transactional recovery receipts."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.config import Permission
from src.admin.ratelimit_cli import Dimension, revision_matches, saved, serialize, target
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.organization import Organization
from src.shared.models.usage import RateLimitConfig
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/organizations/{org_id}/ratelimit-cli", route_class=AuditedAdminRoute)


class CampaignQuotaValue(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rpm: Dimension | None
    tpm: Dimension | None
    concurrent_requests: Dimension | None


class CampaignQuotaEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: datetime | None
    desired: CampaignQuotaValue | None


async def receipt(db, operation_id, org_id, key):
    record = await db.get(AuditLog, str(operation_id))
    if record is None:
        return None
    if record.event_type != "campaign_quota_receipt" or record.org_id != org_id or record.details.get("entity_id") != key:
        raise HTTPException(409, "Campaign operation belongs to another resource")
    return record.details


@router.get("/user/{key}/campaign/{operation_id}")
async def get_campaign_receipt(
    org_id: str,
    key: str,
    operation_id: UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TokenContext, Depends(get_current_user)],
):
    key, _ = await target(db, context, org_id, "user", key, Permission.RATELIMIT_UPDATE)
    result = await receipt(db, operation_id, org_id, key)
    if result is None:
        raise HTTPException(404, "Campaign operation not committed")
    return result


@router.put("/user/{key}/campaign/{operation_id}")
async def edit_campaign_quota(
    org_id: str,
    key: str,
    operation_id: UUID,
    body: CampaignQuotaEdit,
    db: Annotated[AsyncSession, Depends(get_db)],
    context: Annotated[TokenContext, Depends(get_current_user)],
):
    key, _ = await target(db, context, org_id, "user", key, Permission.RATELIMIT_UPDATE)
    await db.execute(select(Organization.id).where(Organization.id == org_id).with_for_update())
    request = body.model_dump(mode="json")
    existing = await receipt(db, operation_id, org_id, key)
    if existing is not None:
        if existing["request"] != request:
            raise HTTPException(409, "Campaign operation was reused with different input")
        await write_admin_audit(db, actor=context, action="update_ratelimit", target_type="ratelimit", target_id=f"user/{key}", org_id=org_id)
        await db.commit()
        return existing
    row = await saved(db, org_id, "user", key, lock=True)
    if (body.expected_revision is None and row is not None) or (
        body.expected_revision is not None and (body.expected_revision.tzinfo is None or not revision_matches(row, body.expected_revision))
    ):
        raise HTTPException(409, "Rate-limit revision changed; inspect again")
    before = serialize(row)
    mark_admin_effects()
    if body.desired is None:
        if row is not None:
            await db.delete(row)
        after = None
    else:
        if row is None:
            row = RateLimitConfig(org_id=org_id, entity_type="user", entity_id=key)
            db.add(row)
        for name, value in body.desired.model_dump().items():
            setattr(row, name, value)
        row.updated_at = datetime.now(UTC)
        await db.flush()
        after = serialize(row)
    result = {"operation_id": str(operation_id), "entity_id": key, "request": request, "before": before, "after": after}
    db.add(AuditLog(id=str(operation_id), org_id=org_id, actor_id=context.user_id, event_type="campaign_quota_receipt", details=result))
    await write_admin_audit(db, actor=context, action="update_ratelimit", target_type="ratelimit", target_id=f"user/{key}", org_id=org_id)
    await db.commit()
    return result
