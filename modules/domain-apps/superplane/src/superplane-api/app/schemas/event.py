"""Pydantic schemas for audit event endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class EventResponse(BaseModel):
    """Single audit event representation."""

    id: uuid.UUID
    org_id: uuid.UUID
    user_id: str | None = None
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
