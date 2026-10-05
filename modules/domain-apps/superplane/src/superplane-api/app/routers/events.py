"""Audit events API — filterable event listing for compliance officers.

Routes:
    GET  /events      — list events (filterable by resource_type, user, time range)
    GET  /events/{id} — event detail
"""

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.event import Event
from app.schemas.event import EventListResponse, EventResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/events", tags=["events"])


@router.get("", response_model=EventListResponse)
async def list_events(
    resource_type: str | None = Query(
        default=None, description="Filter by resource type (e.g. workspace, credential)"
    ),
    user: str | None = Query(
        default=None, alias="user", description="Filter by user ID"
    ),
    action: str | None = Query(
        default=None, description="Filter by action (created, updated, deleted, read)"
    ),
    event_type: str | None = Query(
        default=None,
        description="Filter by event type (api_call, credential_access, lifecycle)",
    ),
    start_time: datetime | None = Query(
        default=None, description="Start of time range (ISO 8601)"
    ),
    end_time: datetime | None = Query(
        default=None, description="End of time range (ISO 8601)"
    ),
    limit: int = Query(default=50, ge=1, le=500, description="Max events to return"),
    offset: int = Query(default=0, ge=0, description="Pagination offset"),
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> EventListResponse:
    """List audit events for the current organization.

    Supports filtering by:
    - resource_type: workspace, credential, deployment, etc.
    - user: user ID who performed the action
    - action: created, updated, deleted, read
    - event_type: api_call, credential_access, lifecycle
    - start_time / end_time: ISO 8601 time range

    Results are ordered by created_at descending (newest first).
    Corresponds to CLI: `superplane events --type workspace --user pranav`
    """
    # Base query scoped to org
    query = select(Event).where(Event.org_id == org_id)
    count_query = select(func.count(Event.id)).where(Event.org_id == org_id)

    # Apply filters
    if resource_type:
        query = query.where(Event.resource_type == resource_type)
        count_query = count_query.where(Event.resource_type == resource_type)

    if user:
        query = query.where(func.coalesce(Event.principal, Event.user_id) == user)
        count_query = count_query.where(
            func.coalesce(Event.principal, Event.user_id) == user
        )

    if action:
        query = query.where(Event.action == action)
        count_query = count_query.where(Event.action == action)

    if event_type:
        query = query.where(Event.event_type == event_type)
        count_query = count_query.where(Event.event_type == event_type)

    if start_time:
        query = query.where(Event.created_at >= start_time)
        count_query = count_query.where(Event.created_at >= start_time)

    if end_time:
        query = query.where(Event.created_at <= end_time)
        count_query = count_query.where(Event.created_at <= end_time)

    # Get total count
    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    # Fetch paginated results
    query = query.order_by(Event.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(query)
    events = result.scalars().all()

    return EventListResponse(
        events=[EventResponse.model_validate(e) for e in events],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{event_id}", response_model=EventResponse)
async def get_event(
    event_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> EventResponse:
    """Get a single audit event by ID.

    Only returns events belonging to the authenticated organization.
    """
    result = await db.execute(
        select(Event).where(Event.id == event_id, Event.org_id == org_id)
    )
    event = result.scalar_one_or_none()

    if event is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Event not found",
        )

    return EventResponse.model_validate(event)


@router.get("/workspaces/{workspace_id}")
async def workspace_events(
    workspace_id: uuid.UUID,
    after: str | None = Query(default=None, max_length=512),
    limit: int = Query(default=50, ge=1, le=100),
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
):
    """Stable bounded stream of audit records attributable to this exact workspace."""
    import base64
    import json
    from sqlalchemy import and_, or_
    from app.models.workspace import Workspace

    workspace = await db.scalar(
        select(Workspace.id).where(
            Workspace.id == workspace_id, Workspace.org_id == org_id
        )
    )
    if workspace is None:
        raise HTTPException(404, "Workspace not found")
    path = "/workspaces/" + str(workspace_id)
    statement = select(Event).where(
        Event.org_id == org_id,
        or_(
            and_(Event.resource_type == "workspace", Event.resource_id == workspace_id),
            Event.request_path == path,
            Event.request_path.like(path + "/%"),
        ),
    )
    if after:
        try:
            value = json.loads(base64.urlsafe_b64decode(after.encode()))
            if set(value) != {"workspace", "time", "id"} or value["workspace"] != str(
                workspace_id
            ):
                raise ValueError
            instant, event_id = (
                datetime.fromisoformat(value["time"]),
                uuid.UUID(value["id"]),
            )
            if instant.tzinfo is None:
                raise ValueError
        except Exception:
            raise HTTPException(422, "Invalid workspace event cursor") from None
        statement = statement.where(
            or_(
                Event.created_at > instant,
                and_(Event.created_at == instant, Event.id > event_id),
            )
        )
    rows = (
        (
            await db.execute(
                statement.order_by(Event.created_at, Event.id).limit(limit + 1)
            )
        )
        .scalars()
        .all()
    )
    emitted = rows[:limit]
    cursor = after
    if emitted:
        from datetime import timezone

        last = emitted[-1]
        instant = last.created_at
        instant = (
            instant.astimezone(timezone.utc)
            if instant.tzinfo
            else instant.replace(tzinfo=timezone.utc)
        )
        cursor = base64.urlsafe_b64encode(
            json.dumps(
                {
                    "workspace": str(workspace_id),
                    "time": instant.isoformat(),
                    "id": str(last.id),
                }
            ).encode()
        ).decode()
    return {
        "workspace_id": str(workspace_id),
        "events": [
            EventResponse.model_validate(row).model_dump(mode="json") for row in emitted
        ],
        "next_cursor": cursor,
        "has_more": len(rows) > limit,
        "coverage": "Workspace resource and exact workspace-path audit records; not a complete provider-event feed.",
    }
