"""Organization access and typed mutation contracts, separate from workspace authority."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

OrganizationPermission = Literal["organization:read", "organization:administer"]


class OrganizationMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_subject: str = Field(min_length=1, max_length=255)
    principal_type: Literal["human"]
    expected_revision: int = Field(ge=0, strict=True)
    request_id: uuid.UUID

    @field_validator("target_subject")
    @classmethod
    def immutable_subject(cls, value: str) -> str:
        if "@" in value or value != value.strip():
            raise ValueError("target_subject must be an immutable subject, not an email address")
        return value


class AssignOrganizationAccessRequest(OrganizationMutationRequest):
    permissions: list[OrganizationPermission] = Field(min_length=1)
    reason: Literal["access_assignment"]

    @field_validator("permissions")
    @classmethod
    def unique_permissions(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate organization permission")
        return value


class RevokeOrganizationAccessRequest(OrganizationMutationRequest):
    expected_revision: int = Field(ge=1, strict=True)
    reason: Literal["access_revocation"]


class OrganizationAssignmentResponse(BaseModel):
    organization_id: uuid.UUID
    grant_id: uuid.UUID
    revision: int
    principal_type: str
    subject: str
    assigned_permissions: list[OrganizationPermission]
    revoked_at: datetime | None
    granted_by: str
    granted_at: datetime
    source: Literal["stored_organization_grant", "explicit_assignment", "explicit_revocation"] = "stored_organization_grant"
    changed_by: str | None = None
    changed_at: datetime | None = None
    reason: Literal["access_assignment", "access_revocation"] | None = None
    request_id: uuid.UUID | None = None


class OrganizationAccessResponse(OrganizationAssignmentResponse):
    effective_permissions: list[OrganizationPermission]
    revocation_effect: Literal["future_authority_only"] | None = None


class OrganizationAssignmentsResponse(BaseModel):
    organization_id: uuid.UUID
    assignments: list[OrganizationAssignmentResponse]
    next_after: uuid.UUID | None = None
