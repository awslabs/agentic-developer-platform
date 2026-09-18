"""Audit logging service — records platform actions to the events table."""

import json
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import Event

logger = logging.getLogger(__name__)

# Map HTTP methods to action verbs
HTTP_METHOD_TO_ACTION = {
    "POST": "created",
    "PUT": "updated",
    "PATCH": "updated",
    "DELETE": "deleted",
    "GET": "read",
}


async def log_event(
    db: AsyncSession,
    *,
    org_id: uuid.UUID,
    user_id: str | None = None,
    action: str,
    resource_type: str,
    resource_id: uuid.UUID | None = None,
    event_type: str = "api_call",
    message: str | None = None,
    details: dict | None = None,
    source_ip: str | None = None,
    request_path: str | None = None,
    http_status: int | None = None,
) -> Event:
    """Insert an audit event into the events table.

    Args:
        db: Async database session.
        org_id: Organization that owns the event.
        user_id: Authenticated user/org ID performing the action.
        action: Action verb (created, updated, deleted, read).
        resource_type: Type of resource (workspace, credential, etc.).
        resource_id: Optional UUID of the specific resource.
        event_type: Classification (api_call, credential_access, lifecycle).
        message: Human-readable description.
        details: Additional structured data (serialized to JSON).
        source_ip: Client IP address.
        request_path: Full HTTP request path.
        http_status: HTTP response status code.

    Returns:
        The created Event record.
    """
    details_json = json.dumps(details) if details else None

    event = Event(
        org_id=org_id,
        user_id=user_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        event_type=event_type,
        message=message,
        details_json=details_json,
        source_ip=source_ip,
        request_path=request_path,
        http_status=http_status,
    )
    db.add(event)
    await db.commit()
    await db.refresh(event)

    logger.info(
        "Audit event: %s %s %s by user=%s org=%s",
        action,
        resource_type,
        resource_id or "",
        user_id or "system",
        org_id,
    )
    return event


def extract_resource_from_path(path: str) -> tuple[str, uuid.UUID | None]:
    """Extract resource type and resource ID from a request path.

    Examples:
        /workspaces -> ("workspace", None)
        /workspaces/abc-123 -> ("workspace", UUID("abc-123"))
        /workspaces/abc-123/kubeconfig -> ("workspace", UUID("abc-123"))
        /auth/login -> ("auth", None)

    Returns:
        Tuple of (resource_type, resource_id or None).
    """
    # Strip leading slash and split
    parts = path.strip("/").split("/")
    if not parts or parts[0] == "":
        return ("unknown", None)

    # Resource type is the first path segment (singularized)
    resource_type = (
        parts[0].rstrip("s") if parts[0] not in ("health", "auth") else parts[0]
    )

    # Try to extract resource ID from second segment
    resource_id = None
    if len(parts) >= 2:
        try:
            resource_id = uuid.UUID(parts[1])
        except (ValueError, AttributeError):
            pass

    return (resource_type, resource_id)
