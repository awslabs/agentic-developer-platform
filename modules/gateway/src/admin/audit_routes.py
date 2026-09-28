"""Operator-facing admin audit retrieval endpoint.

Issue #6037 (S13): Supported retrieval/view for durable admin audit events.

Platform-admin only.  Returns paginated, filterable audit events from the
``security_audit_logs`` table for admin mutation actions (event_type starting
with ``admin_``).
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import and_, exists, func, not_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from src.admin.access_control import AccessControl
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.schemas.auth import TokenContext

router = APIRouter(prefix="/audit-events", tags=["admin-audit"])


def _get_access_control(db: AsyncSession = Depends(get_db)) -> AccessControl:
    return AccessControl(db=db)


# ── Response schemas ──────────────────────────────────────────────────────


class AuditEventResponse(BaseModel):
    id: str
    event_type: str
    actor_id: str | None
    org_id: str
    details: dict | None
    created_at: datetime | None


class AuditEventListResponse(BaseModel):
    events: list[AuditEventResponse]
    total: int
    page: int
    page_size: int


# ── Endpoint ──────────────────────────────────────────────────────────────


@router.get("", response_model=AuditEventListResponse)
async def list_admin_audit_events(
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
    action: str | None = Query(default=None, description="Filter by action (without admin_ prefix)"),
    actor_id: str | None = Query(default=None, description="Filter by actor user_id"),
    target_type: str | None = Query(default=None, description="Filter by target_type in details"),
    org_id: str | None = Query(default=None, description="Filter by org_id"),
    since: datetime | None = Query(default=None, description="Events after this timestamp"),
    until: datetime | None = Query(default=None, description="Events before this timestamp"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    unresolved_only: bool = Query(default=False, description="Pending operation intents without an acknowledged success or refusal"),
) -> AuditEventListResponse:
    """List admin audit events with optional filtering.

    Platform-admin only.  Returns events whose ``event_type`` starts with
    ``admin_`` — the prefix used by the S13 admin audit writer.
    """
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        from fastapi import HTTPException

        raise HTTPException(status_code=403, detail="Platform administrator privileges required")

    # Base query: only admin-prefixed events
    stmt = select(AuditLog).where(AuditLog.event_type.startswith("admin_"))
    count_stmt = select(func.count()).select_from(AuditLog).where(AuditLog.event_type.startswith("admin_"))

    if unresolved_only:
        terminal = aliased(AuditLog)
        finished = exists(
            select(terminal.id).where(
                terminal.details["operation_id"].as_string() == AuditLog.details["operation_id"].as_string(),
                terminal.event_type != "admin_operation_started",
                terminal.details["outcome"].as_string().in_(["success", "denied"]),
            )
        )
        condition = and_(AuditLog.event_type == "admin_operation_started", not_(finished))
        stmt = stmt.where(condition)
        count_stmt = count_stmt.where(condition)

    if action:
        full_type = f"admin_{action}"
        stmt = stmt.where(AuditLog.event_type == full_type)
        count_stmt = count_stmt.where(AuditLog.event_type == full_type)
    if actor_id:
        stmt = stmt.where(AuditLog.actor_id == actor_id)
        count_stmt = count_stmt.where(AuditLog.actor_id == actor_id)
    if org_id:
        stmt = stmt.where(AuditLog.org_id == org_id)
        count_stmt = count_stmt.where(AuditLog.org_id == org_id)
    if since:
        stmt = stmt.where(AuditLog.created_at >= since)
        count_stmt = count_stmt.where(AuditLog.created_at >= since)
    if until:
        stmt = stmt.where(AuditLog.created_at <= until)
        count_stmt = count_stmt.where(AuditLog.created_at <= until)

    # JSON field filtering for target_type
    if target_type:
        stmt = stmt.where(AuditLog.details["target_type"].as_string() == target_type)
        count_stmt = count_stmt.where(AuditLog.details["target_type"].as_string() == target_type)

    total_result = await db.execute(count_stmt)
    total = total_result.scalar_one()

    offset = (page - 1) * page_size
    stmt = stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(page_size)
    result = await db.execute(stmt)
    rows = result.scalars().all()

    return AuditEventListResponse(
        events=[
            AuditEventResponse(
                id=row.id,
                event_type=row.event_type,
                actor_id=row.actor_id,
                org_id=row.org_id,
                details=row.details,
                created_at=row.created_at,
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )
