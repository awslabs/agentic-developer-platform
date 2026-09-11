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
    # Issue #4019: this schema now backs PUT /organizations/{org_id}/users/{user_id},
    # so the description is user-visible in the OpenAPI docs. The accepted values
    # are ASSIGNABLE_ROLES (admin/config.py); "admin, user" was never accurate.
    role: str | None = Field(
        None,
        description="User role: member, dept_admin, org_admin, or platform_admin",
    )


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
    # The linked GitHub login, for the members panel's person label (Issue #4847).
    #
    # Optional and defaulted because only ``list_users_org`` fills it: it is a
    # per-row identity lookup, and the write-shaped endpoints sharing this schema
    # (create/update user) have no reason to pay for it.
    #
    # NOT ``cognito_username``, which is already on this payload and looks like the
    # shortcut: that column is only written on the admin-invite path, so it is NULL
    # for exactly the GitHub-onboarded population the label is for (#4687).
    github_username: str | None = None
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


# Team Membership Schemas (Issue #4840)
# Many-to-many user<->team membership. Before this, the only write path touching a
# user's team was UserUpdateRequest, which carries name/role and no team field at
# all — i.e. there was no way to move a user between teams over the API.
class TeamMembershipRequest(BaseModel):
    """One desired membership, used by the add and replace-set endpoints."""

    team_id: str = Field(..., min_length=1, max_length=255, description="Team the user is a member of")
    role: str | None = Field(None, description="Role within the team: member or lead. Anything else is stored as member.")
    is_primary: bool = Field(
        default=False,
        description=(
            "Whether this is the user's primary team. At most one per user per org — the primary is what projects into the custom:team_id claim."
        ),
    )
    source: str | None = Field(None, max_length=32, description="Provenance of the membership: admin (default) or a directory-sync tag")
    external_id: str | None = Field(None, max_length=255, description="Directory-system identity for synced memberships")


class TeamMemberAddRequest(BaseModel):
    """Add one user to one team.

    Separate from ``TeamMembershipRequest`` because on this endpoint the team comes
    from the path and the user from the body, which is the mirror image of the
    replace-set endpoint (user in path, teams in body).
    """

    user_id: str = Field(..., min_length=1, max_length=255, description="User to add to the team")
    role: str | None = Field(None, description="Role within the team: member or lead. Anything else is stored as member.")
    is_primary: bool = Field(
        default=False,
        description="Whether this becomes the user's primary team. Refused if they already have a different primary.",
    )
    source: str | None = Field(None, max_length=32, description="Provenance of the membership: admin (default) or a directory-sync tag")
    external_id: str | None = Field(None, max_length=255, description="Directory-system identity for synced memberships")


class TeamMembershipSetRequest(BaseModel):
    """Replace a user's entire membership set (the admin UI's save action)."""

    memberships: list[TeamMembershipRequest] = Field(
        ...,
        description=("The full intended set, not a diff. Teams absent from this list are removed. An empty list removes every membership."),
    )


class TeamMembershipResponse(BaseModel):
    """Response schema for one team membership."""

    id: str
    user_id: str
    team_id: str
    org_id: str
    role: str
    is_primary: bool
    source: str
    external_id: str | None = None
    created_at: datetime
    updated_at: datetime | None = None

    class Config:
        from_attributes = True


class TeamMembershipListResponse(BaseModel):
    """Response schema for a user's team memberships."""

    items: list[TeamMembershipResponse]
    total: int


class PlatformUserResponse(BaseModel):
    """One person in the platform-wide member picker (Issue #4827).

    Deliberately narrower than ``UserResponse``: this feeds an admin picker for
    person-scoped rules, so it carries what an operator needs to *recognise* somebody
    plus the id a rule must be stored under — and nothing else. No ``cognito_sub``,
    no ``cognito_username``, no ``role``: a platform-wide listing is the widest
    read of the member table in the API, and the fields it does not return cannot
    leak from it.

    ``id`` is the canonical ``users.id``. That is the column the routing resolver and
    ``bedrock_routing.service.require_scope_exists`` both compare against (#4647), so
    a picker that submitted anything else — a Cognito sub, a GitHub login — would
    produce a rule that stores cleanly and governs nobody.

    ``github_username`` is ``None`` for a member with no linked GitHub identity, which
    is a legitimate permanent state (email/invite onboarding), never an error. The
    caller renders the fallback label rather than hiding the row.
    """

    id: str
    org_id: str
    email: str
    name: str | None = None
    github_username: str | None = None


class PlatformUserListResponse(BaseModel):
    """Response schema for the platform-wide member listing (Issue #4827)."""

    items: list[PlatformUserResponse]
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
