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


# ---------------------------------------------------------------------------
# Onboarding verification (Issue #4016)
#
# Every check is TRI-STATE: True = verified working, False = verified broken,
# None = could not determine. None must render amber/grey and NEVER red — a
# check that errored is not the same as a check that failed, and a
# false-negative red makes operators "fix" a non-problem.
#
# The split into two models is deliberate (🔴-2): the platform checks read
# deployment-global singletons with no tenant segment in their paths, so they
# are admin-gated and returned once per response. The connection checks are
# per-installation/per-tenant.
# ---------------------------------------------------------------------------


class ConnectionVerification(BaseModel):
    """Per-connection onboarding health, computed read-only at request time."""

    record_present: bool | None = Field(
        default=None,
        description=(
            "Whether a Postgres ChannelTenantMap row backs this connection. False on a "
            "synthetic entry surfaced from DynamoDB only — the install never reached the "
            "gateway callback, so the platform cannot manage it."
        ),
    )
    tenant_secret_seeded: bool | None = Field(
        default=None,
        description=(
            "Whether adp/<env>/tenants/<tenant>/github-app exists. False means the first "
            "agent worker for this tenant will die fetching its credentials."
        ),
    )
    identity_index_row: bool | None = Field(
        default=None,
        description=(
            "Whether the forward DynamoDB row (installation → tenant) exists. False means "
            "inbound webhooks for this installation are rejected as unknown_installation."
        ),
    )
    reverse_identity_row: bool | None = Field(
        default=None,
        description=(
            "Whether the reverse DynamoDB row (tenant → installation) exists. False means "
            "agent-to-agent dispatch (adp-trigger) cannot resolve this tenant. Repair is "
            "owned by issue #3860; this is observation only."
        ),
    )
    # Issue #5184: provenance of the sibling ``repositories`` list.
    #
    # ``_fetch_live_repos`` degrades to the stored metadata snapshot whenever the
    # GitHub read fails, and the two are otherwise indistinguishable in the
    # response. A caller that must PROVE access to a specific repository — the
    # CLI's ``adp github connect --repo owner/name`` — cannot treat a snapshot as
    # proof, so the provenance has to travel with the list.
    #
    # True is not weakened by the 60s repo cache: a cached list came from a real
    # GitHub read within that window. Same tri-state convention as above.
    repositories_live: bool | None = Field(
        default=None,
        description=(
            "Whether the ``repositories`` list on this connection was read live from GitHub "
            "(within the 60s cache window). False means GitHub could not be reached and the "
            "stored snapshot was served instead, so the list reflects configuration rather "
            "than confirmed current access. None when no read was attempted."
        ),
    )


class PlatformVerification(BaseModel):
    """Deployment-wide onboarding health. Admin-scoped (Issue #4016, 🔴-2).

    These read platform singletons (no tenant segment in the secret paths), so
    they are returned only to callers who can manage connections — a tenant
    member must not see, or try to "fix", global deployment state.
    """

    login_credentials: bool | None = Field(
        default=None,
        description=(
            "Whether the broker OAuth secret holds a real, non-placeholder client_id. False means 'Sign in with GitHub' is dead for everyone."
        ),
    )
    webhook_secret: bool | None = Field(
        default=None,
        description=(
            "Whether the webhook-ingress secret has been populated with a real value. "
            "False means every GitHub delivery fails signature validation with 401."
        ),
    )

    # -----------------------------------------------------------------------
    # GitHub App configuration drift (Issue #4017)
    #
    # App settings on GitHub can be edited at any time and no webhook event
    # fires when they are, so these are diffed at read time. Same tri-state
    # convention as above: None = could not determine (amber), never red.
    #
    # These are deployment-wide by construction — one App, one -meta secret, one
    # GET /app — so they live on PlatformVerification (admin-gated) rather than
    # being duplicated onto every ConnectionVerification.
    # -----------------------------------------------------------------------

    app_webhook_url_matches: bool | None = Field(
        default=None,
        description=(
            "Whether the App's webhook URL on GitHub (GET /app/hook/config) matches "
            "this deployment's webhook endpoint. False means GitHub is delivering "
            "events somewhere else, so no agent is ever triggered. None when either "
            "side could not be resolved."
        ),
    )
    app_permissions_match: bool | None = Field(
        default=None,
        description=(
            "Whether the App still grants every permission the platform requires (GET /app). False means some agent operations will fail with 403."
        ),
    )
    app_events_match: bool | None = Field(
        default=None,
        description=(
            "Whether the App is still subscribed to every event the platform needs (GET /app). False means some triggers silently never fire."
        ),
    )
    expected_callback_url: str | None = Field(
        default=None,
        description=(
            "The OAuth callback URL this deployment sends as redirect_uri. "
            "INFORMATIONAL ONLY — GitHub exposes no API to read an App's callback "
            "URL back, so this can never be diffed and must never render as a "
            "pass/fail check. It is shown for comparison against the App's "
            "settings page; a genuine mismatch surfaces at login time as "
            "redirect_uri_mismatch."
        ),
    )
    app_oauth_settings_url: str | None = Field(
        default=None,
        description="Deep-link to the App's OAuth settings page on GitHub, for comparing the callback URL by eye.",
    )
    app_config_warnings: list[str] = Field(
        default_factory=list,
        description=(
            "Human-readable detail for the App-config checks above — the same prose the manual-registration flow returns in its warnings list."
        ),
    )


class GitHubConnectionItem(BaseModel):
    """A single GitHub App installation connected to the caller's ADP tenant."""

    revocation_pending: bool = False
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
    # Issue #4016: per-connection onboarding verification
    verification: ConnectionVerification | None = Field(
        default=None,
        description="Read-only onboarding health checks for this connection.",
    )


class ConnectionsListResponse(BaseModel):
    connections: list[GitHubConnectionItem]
    # Issue #4016: admin-scoped platform checks — omitted entirely for callers
    # who cannot manage connections.
    platform_verification: PlatformVerification | None = Field(
        default=None,
        description=("Deployment-wide onboarding health. Present only for callers who can manage connections."),
    )


# ---------------------------------------------------------------------------
# switch-tenant (Issue #3071: one-click workspace switching)
# ---------------------------------------------------------------------------


class SwitchTenantRequest(BaseModel):
    """Request body for POST /admin/connections/switch-tenant."""

    tenant_id: str = Field(..., description="Target tenant (organization) ID to switch to")


class SwitchTenantResponse(BaseModel):
    """Response from POST /admin/connections/switch-tenant."""

    active_tenant_id: str = Field(..., description="The newly active tenant ID after the switch")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


class DeleteConnectionResponse(BaseModel):
    """Local denial is durable; provider uninstall and cleanup can remain pending."""

    deleted: bool
    installation_id: int
    local_revoked: bool = True
    provider_uninstall_requested: bool = False
    provider_revoked: bool = False
    residual: list[str] = Field(default_factory=list)
    warning: str | None = None


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


class AppSetupGuideResponse(BaseModel):
    """Public configuration values for manually creating an App; never credentials."""

    homepage_url: str
    callback_url: str
    setup_url: str
    webhook_url: str
    permissions: dict[str, str]
    events: list[str]


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


class RevalidateAppResponse(BaseModel):
    """Response from POST /api/admin/connections/github/app/revalidate (Issue #4017).

    The explicit "re-check the App's configuration" action. Read-only against
    GitHub; the only thing it writes is the expected-config record in the App
    ``-meta`` secret (never credentials, never Lambda environment).
    """

    checked: bool = Field(..., description="Whether the App's live configuration could be read from GitHub")
    app_webhook_url_matches: bool | None = Field(default=None, description="Tri-state webhook URL check (see PlatformVerification)")
    app_permissions_match: bool | None = Field(default=None, description="Tri-state permissions check")
    app_events_match: bool | None = Field(default=None, description="Tri-state events check")
    expected_callback_url: str | None = Field(
        default=None,
        description="The callback URL this deployment sends. Informational — not verifiable via the GitHub API.",
    )
    app_oauth_settings_url: str | None = Field(
        default=None,
        description="Deep-link to the App's OAuth settings page for comparing the callback URL by eye.",
    )
    warnings: list[str] = Field(default_factory=list, description="Human-readable detail for any check that did not pass")
    expected_config_recorded: bool = Field(
        default=False,
        description=("Whether the expected-config record in the App metadata secret was (re)written. Credentials are never touched by this action."),
    )
    message: str = Field(..., description="Human-readable status message")


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


# ---------------------------------------------------------------------------
# Manual registration (Issue #3360)
# ---------------------------------------------------------------------------


class RegisterManualRequest(BaseModel):
    """Request body for POST /admin/connections/github/app/register-manual."""

    app_id: str = Field(
        ...,
        description="GitHub App numeric ID (from the App's settings page).",
    )
    private_key: str = Field(
        ...,
        description=("GitHub App private key in PEM format. Accepts both real newlines and escaped \\n (from .env files or JSON)."),
    )
    webhook_secret: str = Field(
        default="",
        description="Webhook secret configured on the App (used for HMAC validation).",
    )
    client_id: str = Field(
        default="",
        description=("OAuth client_id for 'Sign in with GitHub'. Optional — omitting disables GitHub login until wired separately."),
    )
    client_secret: str = Field(
        default="",
        description="OAuth client_secret (paired with client_id).",
    )


class RegisterManualResponse(BaseModel):
    """Response from POST /admin/connections/github/app/register-manual."""

    registered: bool = Field(..., description="Whether the App credentials were stored successfully")
    app_id: str = Field(..., description="The registered GitHub App ID")
    app_slug: str = Field(default="", description="GitHub App slug (from GET /app)")
    app_name: str = Field(default="", description="GitHub App display name")
    login_enabled: bool = Field(
        default=False,
        description="Whether 'Sign in with GitHub' is wired (OAuth creds stored)",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description=("Non-blocking configuration warnings (e.g. webhook URL mismatch, missing permissions, missing event subscriptions)."),
    )
