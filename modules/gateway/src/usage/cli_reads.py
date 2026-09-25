"""Bounded own/managed usage projections over the existing UsageLog ledger (#5628).

Legacy usage endpoints are admin-only and lose Decimal precision. This adapter
keeps server-owned scope, metadata-only projection and keyset continuation.
"""

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.config import AdminRole, Permission
from src.admin.exceptions import AccessDeniedError, InvalidScopeError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.identity.workspaces import workspace_user
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage.config import get_usage_config

router = APIRouter()
MAX_WINDOW_DAYS = 90
MAX_AGGREGATE_ROWS = 10000


class UsageCost(BaseModel):
    currency: str = "USD"
    status: Literal["estimated", "lower_bound", "unknown"]
    amount: str | None = None
    recorded_amount: str | None = None
    pricing_confidence: str | None = None
    settlement: Literal["unknown"] = "unknown"
    reason: str


class UsageMetadata(BaseModel):
    id: str
    timestamp: datetime
    request_id: str | None
    org_id: str
    user_id: str
    department_id: str
    team_id: str
    account_type: str
    model: str
    input_tokens: int
    output_tokens: int
    status_code: int
    cost: UsageCost
    invocation_id: str | None
    chain_id: str | None
    root_invocation_id: str | None
    root_human_id: str | None
    requested_model_id: str | None
    resolved_model_id: str | None
    resolution_source: str | None
    model_decision_id: str | None
    provider_request_id: str | None
    destination_region: str | None
    pricing_source_kind: str | None
    pricing_snapshot_version: str | None
    budget_debit_id: str | None = None
    linkage_status: Literal["incomplete"] = "incomplete"


class UsageReadResponse(BaseModel):
    scope: dict[str, str]
    start: datetime
    end: datetime
    items: list[UsageMetadata]
    next_cursor: str | None = None
    complete: bool
    snapshot: bool = False
    retention_days: int
    observed_at: datetime
    raw_retention_start: datetime
    window_coverage: Literal["within_retention", "overlaps_retention", "before_retention"]
    note: str = "Metadata only. Late arrivals and settlement updates may change this ledger; pagination is not a frozen snapshot."


def observation_time():
    return datetime.now(UTC)


def retention_metadata(start, end):
    observed = observation_time()
    days = get_usage_config().raw_log_retention_days
    horizon = observed - timedelta(days=days)
    coverage = "before_retention" if end <= horizon else "overlaps_retention" if start < horizon else "within_retention"
    return {"observed_at": observed, "retention_days": days, "raw_retention_start": horizon, "window_coverage": coverage}


def utc_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    if start.tzinfo is None or end.tzinfo is None:
        raise HTTPException(422, "Timezone-aware UTC bounds are required")
    start, end = start.astimezone(UTC), end.astimezone(UTC)
    if not start < end or end - start > timedelta(days=MAX_WINDOW_DAYS):
        raise HTTPException(422, "Use an increasing [start,end) range of at most 90 days")
    return start, end


def project(row: UsageLog) -> UsageMetadata:
    amount = format(row.cost_usd, "f") if row.cost_usd is not None else None
    captured = row.pricing_confidence in {"verified", "estimated"} and amount is not None
    # Pricing confidence is not evidence of budget settlement. Legacy zero may
    # be a pre-settlement placeholder and therefore must never imply free usage.
    cost = UsageCost(
        status="estimated" if captured else "unknown",
        amount=amount if captured else None,
        recorded_amount=amount,
        pricing_confidence=row.pricing_confidence,
        reason="Pricing recorded; budget debit/settlement linkage is unavailable"
        if captured
        else "Pricing provenance or settlement linkage was not captured",
    )
    return UsageMetadata(
        id=row.id,
        timestamp=row.timestamp if row.timestamp.tzinfo else row.timestamp.replace(tzinfo=UTC),
        request_id=row.request_id,
        org_id=row.org_id,
        user_id=row.user_id,
        department_id=row.department_id,
        team_id=row.team_id,
        account_type=row.account_type,
        model=row.model,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        status_code=row.status_code,
        cost=cost,
        invocation_id=row.agent_run_id,
        chain_id=row.chain_id,
        root_invocation_id=row.root_invocation_id,
        root_human_id=None,
        requested_model_id=row.requested_model_id,
        resolved_model_id=row.resolved_model_id,
        resolution_source=row.resolution_source,
        model_decision_id=row.model_decision_id,
        provider_request_id=row.provider_request_id,
        destination_region=row.destination_region,
        pricing_source_kind=row.pricing_source_kind,
        pricing_snapshot_version=row.pricing_snapshot_version,
    )


async def read_scope(db, user, org_id):
    if org_id is not None:
        try:
            access = AccessControl(db)
            await access.check_permission(user, Permission.USAGE_READ, target_org_id=org_id)
            role, _, department = await access.get_user_role(user)
            if role not in {AdminRole.PLATFORM_ADMIN, AdminRole.ORG_ADMIN, AdminRole.DEPT_ADMIN}:
                raise AccessDeniedError()
            if role == AdminRole.DEPT_ADMIN and not department:
                raise AccessDeniedError()
        except (AccessDeniedError, InvalidScopeError):
            raise HTTPException(403, "Usage scope is not accessible") from None
        scope = {"kind": "managed", "org_id": org_id}
        if role == AdminRole.DEPT_ADMIN:
            scope["department_id"] = department
        return scope, None
    if user.account_type != "human":
        raise HTTPException(403, "Own usage requires a human session")
    owner = await workspace_user(db, user.user_id, user.org_id, username=user.cognito_username)
    if owner is None:
        raise HTTPException(403, "Own usage identity is unavailable")
    # Gateway local requests can use the authenticated login subject, whereas
    # hosted records use the canonical workspace identity. Both are derived here.
    return {"kind": "own", "org_id": user.org_id, "user_id": owner.id}, {user.user_id, owner.id}


def cursor_binding(scope, start, end, request_id, run_id):
    return hashlib.sha256(json.dumps([scope, start.isoformat(), end.isoformat(), request_id, run_id], sort_keys=True).encode()).hexdigest()


def decode_cursor(cursor, binding):
    try:
        if len(cursor) > 2048:
            raise ValueError
        value = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if value["binding"] != binding or not isinstance(value["id"], str) or not 1 <= len(value["id"]) <= 255:
            raise ValueError
        timestamp = datetime.fromisoformat(value["timestamp"])
        if timestamp.tzinfo is None:
            raise ValueError
        return timestamp, value["id"]
    except (ValueError, KeyError, TypeError):
        raise HTTPException(422, "Cursor does not match the selected scope and range") from None


def encode_cursor(row, binding):
    timestamp = row.timestamp
    if timestamp.tzinfo is None:  # SQLite test transport normalizes UTC without tz
        timestamp = timestamp.replace(tzinfo=UTC)
    return base64.urlsafe_b64encode(json.dumps({"binding": binding, "timestamp": timestamp.isoformat(), "id": row.id}).encode()).decode()


async def read_records(db, user, *, org_id, start, end, request_id, cursor, limit, run_id=None):
    start, end = utc_range(start, end)
    scope, owners = await read_scope(db, user, org_id)
    root_human_id = None
    if run_id and owners is not None:
        from src.activity.service import ActivityService

        invocation = ActivityService().get_invocation(run_id, user_id=scope["user_id"], tenant_id=scope["org_id"])
        if invocation is None:
            raise HTTPException(404, "No visible usage target")
        root_human_id = invocation.root_human_id
        owners = None  # The Activity owner/tenant gate authorizes only this run.
    scope["coverage"] = "selected_run" if run_id else "managed_tenant" if org_id else "direct_identity_records"
    binding = cursor_binding(scope, start, end, request_id, run_id)
    conditions = [UsageLog.org_id == scope["org_id"], UsageLog.timestamp >= start, UsageLog.timestamp < end]
    if scope.get("department_id"):
        conditions.append(UsageLog.department_id == scope["department_id"])
    if owners is not None:
        conditions.append(UsageLog.user_id.in_(owners))
    if run_id:
        conditions.append(UsageLog.agent_run_id == run_id)
    if request_id:
        conditions.append(UsageLog.request_id == request_id)
    if cursor:
        timestamp, row_id = decode_cursor(cursor, binding)
        conditions.append(or_(UsageLog.timestamp < timestamp, and_(UsageLog.timestamp == timestamp, UsageLog.id < row_id)))
    rows = (
        (await db.execute(select(UsageLog).where(*conditions).order_by(UsageLog.timestamp.desc(), UsageLog.id.desc()).limit(limit + 1)))
        .scalars()
        .all()
    )
    more = len(rows) > limit
    page = rows[:limit]
    items = [project(row) for row in page]
    if root_human_id:
        for item in items:
            item.root_human_id = root_human_id
    return UsageReadResponse(
        scope=scope,
        start=start,
        end=end,
        items=items,
        next_cursor=encode_cursor(page[-1], binding) if more else None,
        complete=not more,
        **retention_metadata(start, end),
    )


def aggregate(result, group):
    buckets = {}
    for item in result.items:
        if group == "summary":
            key = "all"
        elif group == "timeline":
            key = item.timestamp.astimezone(UTC).date().isoformat()
        else:
            key = getattr(item, {"models": "model", "users": "user_id", "departments": "department_id"}[group])
        buckets.setdefault(key, []).append(item)
    items = []
    for key, rows in sorted(buckets.items()):
        known = [Decimal(row.cost.amount) for row in rows if row.cost.amount is not None]
        unknown = len(rows) - len(known)
        status = "unknown" if not known else "lower_bound" if unknown else "estimated"
        items.append(
            {
                "group": key,
                "requests": len(rows),
                "input_tokens": sum(row.input_tokens for row in rows),
                "output_tokens": sum(row.output_tokens for row in rows),
                "cost": {
                    "currency": "USD",
                    "status": status,
                    "amount": format(sum(known, Decimal(0)), "f") if known else None,
                    "unknown_records": unknown,
                    "settlement": "unknown",
                },
            }
        )
    return {
        "scope": result.scope,
        "start": result.start,
        "end": result.end,
        "items": items,
        "complete": True,
        "snapshot": False,
        "cost_status": "unknown" if not items else "see_groups",
        "observed_at": result.observed_at,
        "retention_days": result.retention_days,
        "raw_retention_start": result.raw_retention_start,
        "window_coverage": result.window_coverage,
        "note": "No rows does not prove zero spend. Totals describe recorded usage only, not settled budget debits.",
    }


async def dispatch_read(db, user, view, org_id, start, end, request_id, cursor, limit, run_id=None):
    if view != "requests" and cursor:
        raise HTTPException(422, "Aggregate views do not accept cursors")
    result = await read_records(
        db,
        user,
        org_id=org_id,
        start=start,
        end=end,
        request_id=request_id,
        cursor=cursor,
        limit=limit if view == "requests" else MAX_AGGREGATE_ROWS,
        run_id=run_id,
    )
    if view == "requests":
        return result
    if not result.complete:
        raise HTTPException(422, "Aggregate exceeds 10000 records; narrow the time range or page requests")
    return aggregate(result, view)


@router.get("/me/{view}")
async def own_read(
    view: Literal["summary", "timeline", "models", "requests"],
    request: Request,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    start: datetime,
    end: datetime,
    request_id: Annotated[str | None, Query(max_length=255)] = None,
    run_id: Annotated[str | None, Query(max_length=128)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    if set(request.query_params) - {"start", "end", "request_id", "run_id", "cursor", "limit"}:
        raise HTTPException(422, "Unsupported usage filter or scope override")
    return await dispatch_read(db, current_user, view, None, start, end, request_id, cursor, limit, run_id)


@router.get("/managed/{org_id}/{view}")
async def managed_read(
    org_id: str,
    view: Literal["summary", "users", "departments", "requests"],
    request: Request,
    current_user: Annotated[TokenContext, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    start: datetime,
    end: datetime,
    request_id: Annotated[str | None, Query(max_length=255)] = None,
    run_id: Annotated[str | None, Query(max_length=128)] = None,
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
):
    if set(request.query_params) - {"start", "end", "request_id", "run_id", "cursor", "limit"}:
        raise HTTPException(422, "Unsupported usage filter or scope override")
    return await dispatch_read(db, current_user, view, org_id, start, end, request_id, cursor, limit, run_id)
