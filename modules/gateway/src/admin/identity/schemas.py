"""Pydantic models for the tenant-identity admin API.

Issue #387: Request/response schemas for /api/admin/identity/* endpoints.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


class ChannelEntry(BaseModel):
    """A single channel binding (e.g. a GitHub installation)."""

    installation_id: str | None = None
    org_login: str | None = None
    workspace_id: str | None = None
    workspace_name: str | None = None


class ChannelsConfig(BaseModel):
    """Multi-channel configuration for an organization."""

    github: list[ChannelEntry] = Field(default_factory=list)
    slack: list[ChannelEntry] = Field(default_factory=list)
    whatsapp: list[ChannelEntry] = Field(default_factory=list)


class AwsAccountEntry(BaseModel):
    """AWS account configuration for hosted agents."""

    account_id: str
    role_arn: str
    external_id: str | None = None


class OrgSettings(BaseModel):
    """Organization-level settings."""

    user_auto_provision_mode: str = "github_oauth"


class OrganizationCreateRequest(BaseModel):
    """POST /api/admin/identity/organizations request body."""

    id: str = Field(..., min_length=1, max_length=255)
    name: str = Field(..., min_length=1, max_length=255)
    plan: str = "free"
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    aws_accounts: list[AwsAccountEntry] = Field(default_factory=list)
    settings: OrgSettings = Field(default_factory=OrgSettings)


class OrganizationUpdateRequest(BaseModel):
    """PATCH /api/admin/identity/organizations/{id} request body."""

    name: str | None = None
    plan: str | None = None
    channels: ChannelsConfig | None = None
    aws_accounts: list[AwsAccountEntry] | None = None
    settings: OrgSettings | None = None


class OrganizationResponse(BaseModel):
    """Organization response payload."""

    id: str
    name: str
    plan: str | None = None
    channels: ChannelsConfig | None = None
    aws_accounts: list[AwsAccountEntry] = Field(default_factory=list)
    settings: dict = Field(default_factory=dict)
    github_installation_ids: list[str] = Field(default_factory=list)
    cognito_client_ids: list[str] = Field(default_factory=list)
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class OrganizationListResponse(BaseModel):
    """GET /api/admin/identity/organizations response."""

    organizations: list[OrganizationResponse]
    total: int


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


class UserIdentityInput(BaseModel):
    """Identity to link to a user at creation time."""

    provider: str = Field(..., max_length=20)
    provider_user_id: str = Field(..., max_length=255)
    provider_username: str | None = None


class UserCreateRequest(BaseModel):
    """POST /api/admin/identity/organizations/{id}/users request body."""

    email: EmailStr
    name: str | None = None
    role: str = "member"
    team_id: str | None = None
    identities: list[UserIdentityInput] = Field(default_factory=list)
    send_invite: bool = True


class UserResponse(BaseModel):
    """User response payload."""

    id: str
    org_id: str
    team_id: str
    email: str
    name: str | None = None
    role: str | None = None
    cognito_sub: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class UserListResponse(BaseModel):
    """GET /api/admin/identity/organizations/{id}/users response."""

    users: list[UserResponse]
    total: int


# ---------------------------------------------------------------------------
# Identities (cross-channel linkage)
# ---------------------------------------------------------------------------


class IdentityCreateRequest(BaseModel):
    """POST /api/admin/identity/users/{user_id}/identities request body."""

    provider: str = Field(..., max_length=20)
    provider_user_id: str = Field(..., max_length=255)
    provider_username: str | None = None


class IdentityResponse(BaseModel):
    """Identity response payload."""

    id: str
    user_id: str
    org_id: str
    team_id: str
    provider: str
    provider_user_id: str
    provider_username: str | None = None
    verification_method: str
    verified_at: datetime | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class IdentityListResponse(BaseModel):
    """List of identities for a user."""

    identities: list[IdentityResponse]
    total: int
