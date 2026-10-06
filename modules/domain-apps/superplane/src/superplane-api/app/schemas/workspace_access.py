"""Explicit human workspace access API contract."""

import uuid
from datetime import datetime
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


class RevokeHumanAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_subject: str = Field(min_length=1, max_length=255)
    principal_type: Literal["human"]
    reason: Literal["access_revocation"]
    expected_revision: int = Field(ge=1, strict=True)
    request_id: uuid.UUID

    @field_validator("target_subject")
    @classmethod
    def require_immutable_subject(cls, value: str) -> str:
        return GrantHumanAccessRequest.require_immutable_subject(value)


class WorkspaceRevocationResponse(BaseModel):
    workspace_id: uuid.UUID
    grant_id: uuid.UUID
    revision: int
    principal_type: Literal["human"]
    subject: str
    effective_permissions: list[Permission] = Field(max_length=0)
    revoked_at: datetime
    revoked_by: str
    reason: Literal["access_revocation"]
    request_id: uuid.UUID
    source: Literal["explicit_revocation"] = "explicit_revocation"
    revocation_effect: Literal["future_authority_only"] = "future_authority_only"


class WorkspaceAssignmentResponse(BaseModel):
    """Stored assignment evidence, not current membership or effective authority."""

    workspace_id: uuid.UUID
    grant_id: uuid.UUID
    revision: int
    principal_type: str
    subject: str
    assigned_permissions: list[Permission]
    revoked_at: datetime | None
    source: Literal["explicit_assignment", "explicit_revocation", "preexisting_grant"]
    changed_by: str | None = None
    reason: str | None = None
    request_id: uuid.UUID | None = None


class WorkspaceAssignmentsResponse(BaseModel):
    workspace_id: uuid.UUID
    assignments: list[WorkspaceAssignmentResponse]
    next_after: uuid.UUID | None = None
