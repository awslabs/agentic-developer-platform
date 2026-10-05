"""Explicit human workspace access API contract."""

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from superplane_auth.policy import Permission


class GrantHumanAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_subject: str = Field(min_length=1, max_length=255)
    principal_type: Literal["human"]
    permissions: list[Permission] = Field(min_length=1)
    reason: Literal["approver_setup"]
    expected_revision: int = Field(ge=0)
    request_id: uuid.UUID

    @field_validator("target_subject")
    @classmethod
    def require_immutable_subject(cls, value: str) -> str:
        if "@" in value or value != value.strip():
            raise ValueError("target_subject must be an immutable subject, not an email address")
        return value


class WorkspaceAccessResponse(BaseModel):
    workspace_id: uuid.UUID
    grant_id: uuid.UUID
    revision: int
    principal_type: Literal["human"]
    subject: str
    effective_permissions: list[Permission]
    source: Literal["explicit_assignment", "preexisting_grant"]
    granted_by: str | None = None
    reason: str | None = None
    request_id: uuid.UUID | None = None
