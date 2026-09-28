"""Pydantic schemas for user management endpoints."""

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field


class InviteUserRequest(BaseModel):
    """POST /users/invite — invite a user to the organization."""

    email: EmailStr = Field(..., description="Email address of the user to invite")
    role: str = Field(
        default="developer",
        pattern="^(developer|workspace-admin|org-admin)$",
        description="RBAC role: developer, workspace-admin, or org-admin",
    )


class UserResponse(BaseModel):
    """Single user representation."""

    id: uuid.UUID
    org_id: uuid.UUID
    email: str
    role: str
    status: str
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class UserListResponse(BaseModel):
    """GET /users — list response."""

    users: list[UserResponse]
    total: int


class UpdateUserRoleRequest(BaseModel):
    """PATCH /users/{id}/role — change user role."""

    role: str = Field(
        ...,
        pattern="^(developer|workspace-admin|org-admin)$",
        description="New RBAC role",
    )


class UserDeleteResponse(BaseModel):
    """DELETE /users/{id} response."""

    id: uuid.UUID
    status: str = "disabled"
    message: str = "User removed from organization"
