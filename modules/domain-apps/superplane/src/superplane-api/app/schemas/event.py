"""Pydantic schemas for audit event endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class EventResponse(BaseModel):
    """Single audit event representation."""

    id: uuid.UUID
    # Optional since #5673 (A17): an attempt rejected before any identity was established
    # has no tenant to attribute. Such rows are not reachable through the tenant-scoped
    # `GET /events` listing, but the field is optional so the shape matches the model.
    org_id: uuid.UUID | None = None
    user_id: str | None = Field(
        default=None,
        description=(
            "Legacy actor column. On rows written before #5673 this holds an "
            "ORGANIZATION id, not a person; read `principal` for who acted."
        ),
    )
    principal: str | None = Field(
        default=None,
        description=(
            "Who acted: the verified principal, or 'unresolved' when no identity was "
            "established. Null on rows predating #5673."
        ),
    )
    outcome: str | None = Field(
        default=None,
        description=(
            "'allowed', 'denied' or 'error'. Null on rows predating #5673, which recorded "
            "successes only -- null must not be read as 'allowed'."
        ),
    )
    action: str = Field(description="Action verb: created, updated, deleted, read")
    resource_type: str = Field(
        description="Resource type: workspace, credential, deployment, etc."
    )
    resource_id: uuid.UUID | None = None
    event_type: str = Field(
        description="Event classification: api_call, credential_access, lifecycle"
    )
    message: str | None = None
    details_json: str | None = None
    source_ip: str | None = None
    request_path: str | None = None
    http_status: int | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


class EventListResponse(BaseModel):
    """GET /events — paginated list response."""

    events: list[EventResponse]
    total: int
    limit: int
    offset: int
