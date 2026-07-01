"""Pydantic schemas for the connections module.

Issue #465: GitHub App install + connection management API.
Issue #2593: Platform-admin GitHub App registration via manifest conversion flow.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# install-start
# ---------------------------------------------------------------------------


class InstallStartResponse(BaseModel):
    """Response from POST /api/admin/connections/github/install-start."""

    install_url: str = Field(..., description="GitHub App installation URL with state token embedded")
    state_token: str = Field(..., description="UUID nonce; passed back by GitHub as ?state=")
    expires_at: datetime = Field(..., description="When the state nonce expires (15 min from now)")


# ---------------------------------------------------------------------------
# connections list
# ---------------------------------------------------------------------------


class GitHubConnectionItem(BaseModel):
    """A single GitHub App installation connected to the caller's ADP tenant."""

    provider: str = Field(default="github")
    installation_id: int
    account_login: str = Field(..., description="GitHub org or user login")
    account_type: str = Field(..., description="'Organization' or 'User'")
    repository_selection: str = Field(..., description="'all' or 'selected'")
    repository_count: int = Field(default=0)
    repositories: list[str] = Field(
        default_factory=list,
        description="Accessible repo full names (owner/repo); empty for legacy rows written before names were captured.",
    )
    installed_at: datetime | None = None
    configure_url: str = Field(..., description="Deep-link to GitHub App settings for this installation")


class ConnectionsListResponse(BaseModel):
    connections: list[GitHubConnectionItem]


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class DeleteConnectionResponse(BaseModel):
    deleted: bool
    installation_id: int


# ---------------------------------------------------------------------------
# register (Issue #2593: platform-admin GitHub App registration)
# ---------------------------------------------------------------------------


class RegisterAppStartRequest(BaseModel):
    """Request body for POST /api/admin/connections/github/app/register-start."""

    owner_type: str = Field(
        default="org",
        description="'user' or 'org'. Controls whether the App is created under the caller's personal account or an org.",
    )
    org: str | None = Field(
        default=None,
        description="GitHub organization login (required when owner_type='org').",
    )


class RegisterAppStartResponse(BaseModel):
    """Response from POST /api/admin/connections/github/app/register-start."""

    status: str = Field(
        ...,
        description="'ready' (proceed with manifest POST) or 'already_registered'.",
    )
    manifest: dict[str, Any] | None = Field(
        default=None,
        description="GitHub App manifest JSON to POST to GitHub (only when status='ready').",
    )
    post_url: str | None = Field(
        default=None,
        description="GitHub URL to POST the manifest to (only when status='ready').",
    )
    state: str | None = Field(
        default=None,
        description="CSRF state nonce (only when status='ready').",
    )
    app_slug: str | None = Field(
        default=None,
        description="Existing App slug (only when status='already_registered').",
    )
    app_id: str | None = Field(
        default=None,
        description="Existing App ID (only when status='already_registered').",
    )
