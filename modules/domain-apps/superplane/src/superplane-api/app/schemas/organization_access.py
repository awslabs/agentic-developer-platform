"""Read-only organization grant evidence, separate from workspace authority."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

OrganizationPermission = Literal["organization:read", "organization:administer"]


class OrganizationAssignmentResponse(BaseModel):
    organization_id: uuid.UUID
    grant_id: uuid.UUID
    principal_type: str
    subject: str
    assigned_permissions: list[OrganizationPermission]
    revoked_at: datetime | None
    granted_by: str
    granted_at: datetime
    source: Literal["stored_organization_grant"] = "stored_organization_grant"


class OrganizationAccessResponse(OrganizationAssignmentResponse):
    effective_permissions: list[OrganizationPermission]


class OrganizationAssignmentsResponse(BaseModel):
    organization_id: uuid.UUID
    assignments: list[OrganizationAssignmentResponse]
    next_after: uuid.UUID | None = None
