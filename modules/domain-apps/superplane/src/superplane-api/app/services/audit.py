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

# Issue #5673 (A17). The value recorded in `principal` when a request was rejected
# before any identity was established. A distinct sentinel rather than NULL, because
# NULL on that column already means "row written before #5673" -- overloading it would
# make "nobody was identified" indistinguishable from "this predates the fix".
PRINCIPAL_UNRESOLVED = "unresolved"

OUTCOME_ALLOWED = "allowed"
OUTCOME_DENIED = "denied"


class AuditWriteFailures:
    """Counter for audit records that could NOT be persisted.

    WHY A COUNTER EXISTS AT ALL. The defect this answers is not "writes fail" -- it is
    that they failed INVISIBLY. The previous middleware returned without a row in three
    situations (no resolvable org, a non-2xx response, a raised exception) and only the
    third logged anything. An operator had no way to tell a quiet period from a broken
    audit path, which is strictly worse than a known outage because the system appears
    to be recording.

    WHY IT IS AN IN-PROCESS COUNTER AND NOT A METRIC CLIENT. This service has no metrics
    SDK and no StatsD/CloudWatch client anywhere in `app/` (checked). Adding one for this
    story would be a new dependency and a new failure mode on the request hot path.
    Instead the count is held here and emitted as a WARNING log line, which the existing
    log pipeline already ships; the alert is configured on that line. The counter is
    what the tests assert against, so the invariant "no silent return" is enforced
    mechanically rather than by reading the code.

    Deliberately NOT reset anywhere but in tests: a monotonic process-lifetime count is
    what makes "is this number growing" answerable.
    """

    def __init__(self) -> None:
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def record_failure(self, *, method: str, path: str, reason: str) -> None:
        """Count one unpersisted audit record and say so in the log.

        `reason` is a fixed internal string chosen by the caller, never an exception
        message or any request-derived text. An audit-failure log line is emitted on the
        path where a request was ALREADY rejected, so it is precisely where malformed or
        attacker-supplied material would be in scope; interpolating the cause would
        forward it into the log the alert reads.
        """
        self._count += 1
        logger.warning(
            "audit record NOT persisted: method=%s path=%s reason=%s total_failures=%d",
            method,
            path,
            reason,
            self._count,
        )

    def reset(self) -> None:
        """Test-only. Production has no reason to forget a failure count."""
        self._count = 0


# One counter per process, imported by the middleware.
audit_write_failures = AuditWriteFailures()


async def log_event(
    db: AsyncSession,
    *,
    org_id: uuid.UUID | None,
    user_id: str | None = None,
    principal: str | None = None,
    outcome: str | None = None,
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
        org_id: Tenant the action targeted. None only when no identity was established
            (an unauthenticated attempt), which is recorded rather than dropped.
        user_id: Legacy actor column, retained for the non-middleware writers that
            already populate it. New middleware rows use `principal` instead.
        principal: WHO acted -- the verified subject, or PRINCIPAL_UNRESOLVED.
        outcome: OUTCOME_ALLOWED or OUTCOME_DENIED.
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
        principal=principal,
        outcome=outcome,
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
        "Audit event: %s %s %s outcome=%s by principal=%s org=%s",
        action,
        resource_type,
        resource_id or "",
        outcome or "unrecorded",
        principal or user_id or "system",
        org_id if org_id is not None else "unattributed",
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
