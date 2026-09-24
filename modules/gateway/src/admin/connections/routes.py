"""FastAPI router for the connections module.

Issue #465: GitHub App install + connection management.
Issue #2593: Platform-admin GitHub App registration via manifest conversion flow.
Issue #2595: GitHub App lifecycle endpoints (status, rotate-key, disconnect).
Issue #3360: Manual GitHub App registration (import existing App).

Endpoints:
    POST   /admin/connections/github/install-start
    GET    /admin/connections/github/install-callback
    GET    /admin/connections
    DELETE /admin/connections/github/{installation_id}
    POST   /admin/connections/github/app/register-start    (platform_admin only)
    GET    /admin/connections/github/app/register-callback  (platform_admin via state nonce)
    POST   /admin/connections/github/app/register-manual   (platform_admin only)
    GET    /admin/connections/github/app/status             (platform_admin only)
    POST   /admin/connections/github/app/revalidate         (platform_admin only)
    POST   /admin/connections/github/app/rotate-key         (platform_admin only)
    POST   /admin/connections/github/app/disconnect         (platform_admin only)
"""

from __future__ import annotations

import logging
import urllib.parse
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.auth.magic_link import (
    NonceAlreadyConsumedError,
    NonceNotFoundError,
    TargetUserMismatchError,
    TokenExpiredError,
)
from src.auth.org_id_resolver import resolve_effective_org_id
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from . import service as connection_service
from .schemas import (
    AppStatusResponse,
    ConnectionsListResponse,
    DeleteConnectionResponse,
    DisconnectAppResponse,
    InstallStartResponse,
    RegisterAppStartRequest,
    RegisterAppStartResponse,
    RegisterManualRequest,
    RegisterManualResponse,
    RevalidateAppResponse,
    RotateKeyResponse,
    SwitchTenantRequest,
    SwitchTenantResponse,
)
from .service import (
    delete_connection,
    disconnect_app,
    get_app_status,
    install_callback,
    install_start,
    list_connections,
    register_app_callback,
    register_app_manual,
    register_app_start,
    revalidate_app_config,
    rotate_app_key,
)

logger = logging.getLogger(__name__)


async def _get_access_control(db: AsyncSession = Depends(get_db)) -> AccessControl:
    """Provide an AccessControl instance for platform_admin checks."""
    return AccessControl(db)


# NOTE: prefix is "/admin/connections", NOT "/api/admin/connections". CloudFront
# fronts the gateway with an /api/* behavior whose viewer-request function strips
# the leading /api before forwarding to the ALB — so the SPA calls
# /api/admin/connections/... and the backend must serve /admin/connections/...
# (every other admin router follows the same convention). Mounting this under
# /api/admin/... made GitHub's Setup-URL redirect and the SPA's calls 404 after
# the strip → the connections UI never populated.
router = APIRouter(
    prefix="/admin/connections",
    tags=["connections"],
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_FRONTEND_CONNECTIONS_PATH = "/settings/connections"


def _redirect_error(code: str, message: str) -> RedirectResponse:
    params = urllib.parse.urlencode({"error": code, "message": message})
    return RedirectResponse(
        url=f"{_FRONTEND_CONNECTIONS_PATH}?{params}",
        status_code=302,
    )


def _redirect_success(
    installation_id: int,
    *,
    installed: str | None = None,
    switched_from: str | None = None,
) -> RedirectResponse:
    params: dict[str, str] = {"success": "1", "installation_id": str(installation_id)}
    # Issue #3072: Pass org name + previous tenant so the frontend can show
    # the "you're now in <org>" banner with a "Switch back" action.
    if installed:
        params["installed"] = installed
    if switched_from:
        params["switched_from"] = switched_from
    return RedirectResponse(
        url=f"{_FRONTEND_CONNECTIONS_PATH}?{urllib.parse.urlencode(params)}",
        status_code=302,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post("/github/install-start", response_model=InstallStartResponse)
async def github_install_start(
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InstallStartResponse:
    """Generate a state nonce and return the GitHub App installation URL.

    The caller redirects to install_url to start the GitHub App install flow.
    """
    try:
        if current_user.account_type != "human":
            raise HTTPException(status_code=403, detail="A signed-in human identity is required")
        return await install_start(
            cognito_sub=current_user.user_id,
            org_id=current_user.org_id,
            cognito_username=current_user.cognito_username,
            db=db,
        )
    except connection_service.SetupAuthorityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except HTTPException:
        # Issue #2700: install_start raises a deliberate HTTPException(503,
        # "GitHub App not configured…") when the slug can't be resolved. The
        # blanket handler below used to rewrap it into a generic 500, hiding the
        # actionable detail. Re-raise it verbatim (same pattern as register-start).
        raise
    except Exception as exc:
        logger.error("install-start failed for user=%s: %s", current_user.user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to initiate GitHub install flow") from exc


@router.get("/github/install-callback")
async def github_install_callback(
    installation_id: int,
    setup_action: str = "install",
    state: str = "",
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Handle the GitHub redirect after app installation.

    GitHub redirects the operator's browser here as a plain GET with
    ?installation_id=&setup_action=&state= and **no Authorization header** — so
    this endpoint is intentionally NOT behind get_current_user. The single-use,
    short-TTL `state` nonce (minted by install-start for a specific signed-in
    user) is the authenticator: install_callback validates + consumes it and
    resolves the caller's user/org from it. Redirects to the connections page on
    both success and failure.
    """
    # Issue #2952: Allow empty state for public-App installs initiated from
    # GitHub by non-ADP users. The service layer handles the no-nonce path.
    try:
        result = await install_callback(
            installation_id=installation_id,
            setup_action=setup_action,
            state=state,
            db=db,
        )
        # Issue #2952: No-nonce path returns a generic HTML page (no redirect —
        # the user has no ADP session to redirect into).
        #
        # Issue #4016: the page now reports the OUTCOME. It previously said
        # "Installation complete" even when the handler had persisted nothing at
        # all, so the one person who could have escalated the problem was
        # actively told it had worked.
        if result.get("no_nonce"):
            from fastapi.responses import HTMLResponse

            # `partial` is its own signal: #2724 contracts a promotion refusal as
            # a successful-but-not-vouched-for install, so success stays True
            # there. The page must still not say "complete".
            if result.get("success") and not result.get("partial"):
                return HTMLResponse(
                    content=(
                        "<html><body><h1>Installation complete</h1>"
                        "<p>The GitHub App has been installed successfully. "
                        "Sign in to ADP to get started.</p></body></html>"
                    ),
                    status_code=200,
                )

            detail = result.get("error_message") or "The installation could not be completed."
            logger.warning(
                "event=install_callback_no_nonce_incomplete installation_id=%d error_code=%s partial=%s",
                installation_id,
                result.get("error_code") or "unknown",
                bool(result.get("partial")),
            )
            # 200, not an error status: GitHub has completed the install on its
            # side and this is the operator's browser, not an API client. The
            # honesty has to be in the page body, not the status code.
            return HTMLResponse(
                content=(
                    "<html><body><h1>Installation needs attention</h1>"
                    f"<p>{escape(detail)}</p>"
                    "<p>The app is installed on GitHub, but this ADP deployment has not "
                    "finished connecting it. Contact your platform operator and quote "
                    f"installation ID <code>{installation_id}</code>.</p></body></html>"
                ),
                status_code=200,
            )
        # Issue #2952 (D9): Redirect to connections page with install success.
        # Issue #3072: Pass org name + switched_from tenant for the auto-switch banner.
        return _redirect_success(
            installation_id,
            installed=result.get("account_login") if result.get("switched_from") else None,
            switched_from=result.get("switched_from"),
        )

    except (NonceNotFoundError, TokenExpiredError) as exc:
        logger.warning("install-callback invalid state jti=%s: %s", state, exc)
        return _redirect_error("invalid_state", "Installation link has expired or is invalid. Please try again.")

    except NonceAlreadyConsumedError as exc:
        logger.warning("install-callback replayed state jti=%s: %s", state, exc)
        return _redirect_error("state_replayed", "Installation link already used. Please start a new install.")

    except TargetUserMismatchError as exc:
        logger.warning("install-callback unresolved user jti=%s: %s", state, exc)
        return _redirect_error("unauthorized", "Installation link was not issued for a known user.")

    except connection_service.SetupAuthorityError as exc:
        return _redirect_error("github_control_required", str(exc))

    except HTTPException as exc:
        return _redirect_error("github_verification_unavailable", str(exc.detail))

    except PermissionError as exc:
        logger.warning("install-callback cross-tenant conflict installation_id=%d: %s", installation_id, exc)
        return _redirect_error("tenant_conflict", str(exc))

    except Exception as exc:
        logger.error("install-callback unexpected error installation_id=%d: %s", installation_id, exc)
        return _redirect_error("internal_error", "An unexpected error occurred. Please try again.")


@router.get("", response_model=ConnectionsListResponse)
async def get_connections(
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ConnectionsListResponse:
    """List all GitHub App installations connected to the caller's ADP tenants.

    Issue #3018: Multi-tenant visibility — returns connections from ALL tenants
    the user is a member of (via tenant_memberships). Each connection is tagged
    with tenant_id, tenant_name, and is_active_tenant. Falls back to single-org
    behavior when no membership rows exist (legacy path).

    Issue #4016: This is deliberately where onboarding verification is hosted,
    rather than on /github/app/status — status is platform-admin-only, so it
    hides the signal from the tenant admin who actually experiences the broken
    install. Per-connection checks go to every caller; the deployment-wide
    ``platform_verification`` block goes only to admins (the ``caller_is_admin``
    argument below is what gates it).
    """

    try:
        effective_org_id = await resolve_effective_org_id(current_user, db)

        from src.shared.identity.workspaces import memberships_for_login

        login, memberships = await memberships_for_login(db, current_user.user_id, username=current_user.cognito_username)
        member_tenant_ids = list(memberships) or None
        active = memberships.get(effective_org_id)
        pg_user_id = active[0].id if active else None
        # The signed token, not another session's is_active flag, pins this
        # request's workspace until the frontend refreshes its credentials.

        return await list_connections(
            caller_org_id=effective_org_id,
            caller_user_id=current_user.user_id,
            db=db,
            member_tenant_ids=member_tenant_ids,
            # Issue #3073: Pass admin status and PG user ID for can_manage computation.
            caller_is_admin=current_user.is_admin,
            caller_pg_user_id=pg_user_id,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("list-connections failed for org=%s: %s", current_user.org_id, exc)
        raise HTTPException(status_code=500, detail="Failed to list connections") from exc


@router.post("/switch-tenant", response_model=SwitchTenantResponse)
async def switch_tenant(
    body: SwitchTenantRequest,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SwitchTenantResponse:
    """Compatibility route; callers must refresh tokens after this switch."""
    from src.auth.workspaces import get_workspace_claims, select_workspace

    selected = await select_workspace(db, current_user, body.tenant_id, get_workspace_claims())
    return SwitchTenantResponse(active_tenant_id=selected.org_id)


@router.delete("/github/{installation_id}", response_model=DeleteConnectionResponse)
async def disconnect_github(
    installation_id: int,
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> DeleteConnectionResponse:
    """Disconnect a GitHub App installation from the caller's ADP tenant.

    Issue #3073: No longer requires workspace admin role. Authorization is:
    tenant ownership (unchanged) AND (workspace admin OR the user who installed
    the connection). Non-admin installers can manage their own connection.
    """
    from src.shared.identity.workspaces import workspace_user

    try:
        effective_org_id = await resolve_effective_org_id(current_user, db)
        caller = await workspace_user(db, current_user.user_id, effective_org_id, username=current_user.cognito_username)
        pg_user_id = caller.id if caller else None

        return await delete_connection(
            installation_id=installation_id,
            caller_org_id=effective_org_id,
            db=db,
            caller_user_id=pg_user_id,
            caller_is_admin=current_user.is_admin,
        )
    except HTTPException:
        # Issue #2700: surface deliberate HTTPExceptions instead of masking
        # them as a generic 500 (same audit as install-start).
        raise
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        logger.error(
            "disconnect-github failed installation_id=%d org=%s: %s",
            installation_id,
            current_user.org_id,
            exc,
        )
        raise HTTPException(status_code=500, detail="Failed to disconnect installation") from exc


# ---------------------------------------------------------------------------
# GitHub App registration (Issue #2593: manifest conversion flow)
# ---------------------------------------------------------------------------


@router.post("/github/app/register-start", response_model=RegisterAppStartResponse)
async def github_app_register_start(
    body: RegisterAppStartRequest,
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
) -> RegisterAppStartResponse:
    """Generate a GitHub App manifest for the manifest conversion flow.

    Platform-admin only. Returns a manifest to POST to GitHub, or
    'already_registered' if an App already exists for this deployment.
    """
    if current_user.account_type != "human":
        raise HTTPException(status_code=403, detail="A signed-in human identity is required")
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        )

    try:
        return await register_app_start(
            owner_type=body.owner_type,
            org=body.org,
            app_name=body.app_name,
            visibility=body.visibility,
            cognito_sub=current_user.user_id,
            cognito_username=current_user.cognito_username,
            db=db,
        )
    except connection_service.SetupAuthorityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("register-app-start failed for user=%s: %s", current_user.user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to initiate GitHub App registration") from exc


@router.get("/github/app/register-callback")
async def github_app_register_callback(
    code: str = "",
    state: str = "",
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Handle the GitHub manifest conversion callback.

    GitHub redirects the admin's browser here with ?code=&state= after
    they submit the manifest. This endpoint is NOT behind get_current_user
    because GitHub redirects without an Authorization header — the state
    nonce is the authenticator (same pattern as install-callback).

    Exchanges the code for App credentials and stores them in Secrets Manager.
    """
    # Issue #2682: Log at entry BEFORE any validation so we can confirm
    # whether GitHub's redirect is reaching the backend at all.
    logger.info(
        "register-app-callback: entry code_present=%s state_present=%s",
        bool(code),
        bool(state),
    )

    if not code:
        return _redirect_error("missing_code", "Missing code parameter from GitHub redirect")
    if not state:
        return _redirect_error("missing_state", "Missing state parameter from GitHub redirect")

    try:
        redirect_url = await register_app_callback(
            code=code,
            state=state,
            db=db,
        )
        return RedirectResponse(url=redirect_url, status_code=302)

    except (NonceNotFoundError, TokenExpiredError) as exc:
        logger.warning("register-app-callback invalid state jti=%s: %s", state, exc)
        return _redirect_error("invalid_state", "Registration link has expired or is invalid. Please try again.")

    except NonceAlreadyConsumedError as exc:
        logger.warning("register-app-callback replayed state jti=%s: %s", state, exc)
        return _redirect_error("state_replayed", "Registration link already used. Please start a new registration.")

    except connection_service.SetupAuthorityError as exc:
        # Issue #5664: the state token was valid but the principal it was issued to
        # may not replace the deployment's shared App/webhook/sign-in secrets — or
        # an App is already registered. Logged with a greppable event name because
        # this is the containment for the credential-replacement path.
        logger.warning("event=register_app_callback_denied jti=%s reason=%s", state, exc)
        return _redirect_error("not_authorized", str(exc))

    except HTTPException as exc:
        logger.error("register-app-callback HTTP error: %s", exc.detail)
        return _redirect_error("github_error", str(exc.detail))

    except Exception as exc:
        logger.error("register-app-callback unexpected error: %s", exc)
        return _redirect_error("internal_error", "An unexpected error occurred during App registration.")


# ---------------------------------------------------------------------------
# Manual GitHub App registration (Issue #3360)
# ---------------------------------------------------------------------------


@router.post("/github/app/register-manual", response_model=RegisterManualResponse)
async def github_app_register_manual(
    body: RegisterManualRequest,
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
) -> RegisterManualResponse:
    """Import an existing GitHub App by providing its credentials.

    Platform-admin only. Validates the App ID + private key against GitHub,
    stores credentials, and returns non-blocking configuration warnings.
    """
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        )

    try:
        result = await register_app_manual(
            app_id=body.app_id,
            private_key=body.private_key,
            webhook_secret=body.webhook_secret,
            client_id=body.client_id,
            client_secret=body.client_secret,
        )
        return RegisterManualResponse(**result)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("register-app-manual failed for user=%s: %s", current_user.user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to register GitHub App") from exc


# ---------------------------------------------------------------------------
# GitHub App lifecycle (Issue #2595: status, rotate-key, disconnect)
# ---------------------------------------------------------------------------


@router.get("/github/app/status", response_model=AppStatusResponse)
async def github_app_status(
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
) -> AppStatusResponse:
    """Return registration status of the deployment's GitHub App.

    Platform-admin only. Never returns the private key or client secret.
    """
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        )

    try:
        return await get_app_status()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("app-status failed for user=%s: %s", current_user.user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to retrieve App status") from exc


@router.post("/github/app/revalidate", response_model=RevalidateAppResponse)
async def github_app_revalidate(
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
) -> RevalidateAppResponse:
    """Re-check the App's live configuration on GitHub (Issue #4017).

    Platform-admin only. GitHub fires no event when an admin edits App settings,
    so this is the operator's on-demand "is my App still configured correctly?"
    action. Read-only against GitHub; the only thing it writes is the
    expected-config record in the App metadata secret — never credentials, never
    Lambda environment.
    """
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        )

    try:
        result = await revalidate_app_config(actor=current_user.user_id)
        return RevalidateAppResponse(**result)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("app-revalidate failed for user=%s: %s", current_user.user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to re-validate App configuration") from exc


@router.post("/github/app/rotate-key", response_model=RotateKeyResponse)
async def github_app_rotate_key(
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
) -> RotateKeyResponse:
    """Rotate the GitHub App's private key.

    Platform-admin only. Generates a new key via the GitHub API, stores it in
    Secrets Manager, and invalidates the credentials cache.
    """
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        )

    try:
        return await rotate_app_key()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("app-rotate-key failed for user=%s: %s", current_user.user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to rotate App key") from exc


@router.post("/github/app/disconnect", response_model=DisconnectAppResponse)
async def github_app_disconnect(
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
) -> DisconnectAppResponse:
    """Disconnect (deregister) the GitHub App from this deployment.

    Platform-admin only. Deletes App secrets from Secrets Manager and
    invalidates credentials cache. Does NOT delete the App on GitHub.
    """
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        )

    try:
        return await disconnect_app()
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("app-disconnect failed for user=%s: %s", current_user.user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to disconnect App") from exc
