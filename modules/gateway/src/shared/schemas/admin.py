"""Schemas for admin onboarding operations (departments, teams, users, service accounts)."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, EmailStr, Field


# Department Schemas
class DepartmentCreateRequest(BaseModel):
    """Request schema for creating a department."""

    name: str = Field(..., min_length=1, max_length=255, description="Department name")
    budget_limit: Decimal | None = Field(None, ge=0, description="Monthly budget limit in USD")
    description: str | None = Field(None, max_length=1024, description="Department description")


class DepartmentUpdateRequest(BaseModel):
    """Request schema for updating a department."""

    name: str | None = Field(None, min_length=1, max_length=255, description="Department name")
    budget_limit: Decimal | None = Field(None, ge=0, description="Monthly budget limit in USD")
    description: str | None = Field(None, max_length=1024, description="Department description")


class DepartmentResponse(BaseModel):
    """Response schema for department data."""

    id: str
    org_id: str
    name: str
    budget_limit: Decimal | None = None
    description: str | None = None
    cognito_group_name: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class DepartmentListResponse(BaseModel):
    """Response schema for listing departments."""

    items: list[DepartmentResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


# Team Schemas
class TeamCreateRequest(BaseModel):
    """Request schema for creating a team."""

    name: str = Field(..., min_length=1, max_length=255, description="Team name")
    description: str | None = Field(None, max_length=1024, description="Team description")


class TeamUpdateRequest(BaseModel):
    """Request schema for updating a team."""

    name: str | None = Field(None, min_length=1, max_length=255, description="Team name")
    description: str | None = Field(None, max_length=1024, description="Team description")


class TeamResponse(BaseModel):
    """Response schema for team data."""

    id: str
    org_id: str
    department_id: str
    name: str
    description: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class TeamListResponse(BaseModel):
    """Response schema for listing teams."""

    items: list[TeamResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


# User Schemas
class UserCreateRequest(BaseModel):
    """Request schema for creating/adding a user."""

    email: EmailStr = Field(..., description="User email address")
    name: str = Field(..., min_length=1, max_length=255, description="User full name")
    role: str = Field(default="user", description="User role (admin, user)")


class UserUpdateRequest(BaseModel):
    """Request schema for updating a user."""

    name: str | None = Field(None, min_length=1, max_length=255, description="User full name")
    role: str | None = Field(None, description="User role (admin, user)")


class UserResponse(BaseModel):
    """Response schema for user data."""

    id: str
    org_id: str
    team_id: str
    email: str
    name: str | None = None
    cognito_sub: str | None = None
    cognito_username: str | None = None
    role: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class UserListResponse(BaseModel):
    """Response schema for listing users."""

    items: list[UserResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


# Service Account Schemas
class ServiceAccountCreateRequest(BaseModel):
    """Request schema for creating a service account."""

    name: str = Field(..., min_length=1, max_length=255, description="Service account name")
    description: str | None = Field(None, max_length=1024, description="Service account description")
    iam_role_arn: str | None = Field(None, description="IAM role ARN for the service account")


class ServiceAccountResponse(BaseModel):
    """Response schema for service account data."""

    id: str
    org_id: str
    department_id: str
    team_id: str
    name: str
    description: str | None = None
    iam_role_arn: str
    created_at: datetime

    class Config:
        from_attributes = True


class ServiceAccountListResponse(BaseModel):
    """Response schema for listing service accounts."""

    items: list[ServiceAccountResponse]
    total: int
    page: int
    page_size: int
    has_more: bool


# Cognito User Info Schema (for internal use)
class CognitoUserInfo(BaseModel):
    """Schema for Cognito user information."""

    sub: str
    username: str
    email: str
    email_verified: bool = False
    org_id: str | None = None
    department_id: str | None = None
    team_id: str | None = None
    role: str | None = None
