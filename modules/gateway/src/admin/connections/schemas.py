"""Pydantic schemas for the connections module.

Issue #465: GitHub App install + connection management API.
Issue #2593: Platform-admin GitHub App registration via manifest conversion flow.
Issue #2595: GitHub App lifecycle endpoints (status, rotate-key, disconnect).
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
        description="Accessible repo full names (owner/repo); fetched live from GitHub with 60s cache.",
    )
    installed_at: datetime | None = None
    configure_url: str = Field(..., description="Deep-link to GitHub App settings for this installation")
    manage_url: str = Field(
        default="",
        description="Deep-link to GitHub's installation repository management page. Visible to all members.",
    )
    # Issue #3073: Per-connection management authorization
    can_manage: bool = Field(
        default=False,
        description=(
            "Whether the caller can manage (disconnect) this connection. True if the caller is a workspace admin or the user who installed it."
        ),
    )
    # Issue #3018: Multi-tenant visibility fields
    tenant_id: str | None = Field(
        default=None,
        description="Tenant (organization) ID that owns this connection.",
    )
    tenant_name: str | None = Field(
        default=None,
        description="Display name of the tenant that owns this connection.",
    )
    is_active_tenant: bool | None = Field(
        default=None,
        description="Whether this connection belongs to the caller's currently active tenant.",
    )


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
    app_name: str | None = Field(
        default=None,
        description=(
            "Optional custom GitHub App name. If omitted, defaults to "
            "'<owner>-adp-agent-platform' (org-prefixed to avoid global name collisions). "
            "GitHub App names are globally unique across all of GitHub."
        ),
    )
    visibility: str = Field(
        default="private",
        description=(
            "App visibility: 'private' (only your org can install) or 'public' "
            "(other orgs + personal accounts can install via the link). "
            "Private→public is flippable later in GitHub settings; "
            "public→private only while ≤1 install."
        ),
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
    suggested_app_name: str | None = Field(
        default=None,
        description=(
            "The resolved App name used in the manifest (only when status='ready'). Useful for the frontend to display what name will be registered."
        ),
    )
    app_slug: str | None = Field(
        default=None,
        description="Existing App slug (only when status='already_registered').",
    )
    app_id: str | None = Field(
        default=None,
        description="Existing App ID (only when status='already_registered').",
    )


# ---------------------------------------------------------------------------
# App lifecycle (Issue #2595)
# ---------------------------------------------------------------------------


class AppStatusResponse(BaseModel):
    """Response from GET /api/admin/connections/github/app/status."""

    registered: bool = Field(..., description="Whether a GitHub App is registered for this deployment")
    install_ready: bool = Field(
        default=False,
        description=(
            "Whether the install flow is usable — true iff the App slug resolves "
            "(directly, via env var, or via self-heal). False means install-start "
            "will 503 until the slug is recoverable (Issue #2700)."
        ),
    )
    login_enabled: bool = Field(
        default=False,
        description=(
            "Whether 'Sign in with GitHub' is wired — true iff the broker OAuth "
            "secret (adp/<env>/cognito/github-oauth-credentials) holds a real, "
            "non-placeholder client_id. False means the login button is dead until "
            "the App is (re-)registered or credentials are seeded (Issue #2708)."
        ),
    )
    app_slug: str | None = Field(default=None, description="GitHub App slug (if registered)")
    app_id: str | None = Field(default=None, description="GitHub App numeric ID (if registered)")
    owner_type: str | None = Field(
        default=None,
        description="'Organization' or 'User' — where the App was created on GitHub (if known)",
    )
    created_at: str | None = Field(
        default=None,
        description="ISO-8601 timestamp when the App secret was last written (if available)",
    )


class RotateKeyResponse(BaseModel):
    """Response from POST /api/admin/connections/github/app/rotate-key."""

    rotated: bool = Field(..., description="Whether the key was successfully rotated")
    app_id: str | None = Field(default=None, description="GitHub App ID the key was rotated for")
    message: str = Field(..., description="Human-readable status message")


class DisconnectAppResponse(BaseModel):
    """Response from POST /api/admin/connections/github/app/disconnect."""

    disconnected: bool = Field(..., description="Whether the App was successfully disconnected")
    app_id: str | None = Field(default=None, description="The App ID that was disconnected")
    message: str = Field(..., description="Human-readable status message")
    affected_installations: int = Field(
        default=0,
        description="Number of tenant installations that will stop working",
    )
