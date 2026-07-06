"""FastAPI router for the connections module.

Issue #465: GitHub App install + connection management.
Issue #2593: Platform-admin GitHub App registration via manifest conversion flow.
Issue #2595: GitHub App lifecycle endpoints (status, rotate-key, disconnect).

Endpoints:
    POST   /admin/connections/github/install-start
    GET    /admin/connections/github/install-callback
    GET    /admin/connections
    DELETE /admin/connections/github/{installation_id}
    POST   /admin/connections/github/app/register-start    (platform_admin only)
    GET    /admin/connections/github/app/register-callback  (platform_admin via state nonce)
    GET    /admin/connections/github/app/status             (platform_admin only)
    POST   /admin/connections/github/app/rotate-key         (platform_admin only)
    POST   /admin/connections/github/app/disconnect         (platform_admin only)
"""

from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user, require_admin
from src.auth.magic_link import (
    NonceAlreadyConsumedError,
    NonceNotFoundError,
    TargetUserMismatchError,
    TokenExpiredError,
)
from src.auth.org_id_resolver import resolve_effective_org_id
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .schemas import (
    AppStatusResponse,
    ConnectionsListResponse,
    DeleteConnectionResponse,
    DisconnectAppResponse,
    InstallStartResponse,
    RegisterAppStartRequest,
    RegisterAppStartResponse,
    RotateKeyResponse,
)
from .service import (
    delete_connection,
    disconnect_app,
    get_app_status,
    install_callback,
    install_start,
    list_connections,
    register_app_callback,
    register_app_start,
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


def _redirect_success(installation_id: int) -> RedirectResponse:
    params = urllib.parse.urlencode({"success": "1", "installation_id": str(installation_id)})
    return RedirectResponse(
        url=f"{_FRONTEND_CONNECTIONS_PATH}?{params}",
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
        return await install_start(
            cognito_sub=current_user.user_id,
            user_id=current_user.user_id,
            db=db,
        )
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
        # Issue #2952: No-nonce path returns a generic HTML success page
        # (no redirect — the user has no ADP session to redirect into).
        if result.get("no_nonce"):
            from fastapi.responses import HTMLResponse

            return HTMLResponse(
                content=(
                    "<html><body><h1>Installation complete</h1>"
                    "<p>The GitHub App has been installed successfully. "
                    "Sign in to ADP to get started.</p></body></html>"
                ),
                status_code=200,
            )
        # Issue #2952 (D9): Redirect to connections page with install success.
        return _redirect_success(installation_id)

    except (NonceNotFoundError, TokenExpiredError) as exc:
        logger.warning("install-callback invalid state jti=%s: %s", state, exc)
        return _redirect_error("invalid_state", "Installation link has expired or is invalid. Please try again.")

    except NonceAlreadyConsumedError as exc:
        logger.warning("install-callback replayed state jti=%s: %s", state, exc)
        return _redirect_error("state_replayed", "Installation link already used. Please start a new install.")

    except TargetUserMismatchError as exc:
        logger.warning("install-callback unresolved user jti=%s: %s", state, exc)
        return _redirect_error("unauthorized", "Installation link was not issued for a known user.")

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
    """
    from sqlalchemy import select

    from src.shared.models.onboarding import TenantMembership
    from src.shared.models.organization import User

    try:
        effective_org_id = await resolve_effective_org_id(current_user, db)

        # Issue #3018: Resolve the Postgres users.id from the Cognito sub.
        # current_user.user_id is the Cognito 'sub' claim, but TenantMembership.user_id
        # FKs to users.id (a Postgres UUID). We must resolve via cognito_sub.
        # Graceful fallback: if resolution fails, proceed with single-org behavior.
        member_tenant_ids: list[str] | None = None

        try:
            user_stmt = select(User.id).where(User.cognito_sub == current_user.user_id)
            pg_user_id = (await db.execute(user_stmt)).scalar_one_or_none()

            if pg_user_id:
                membership_stmt = select(TenantMembership.tenant_id).where(
                    TenantMembership.user_id == pg_user_id,
                )
                tenant_ids = [row[0] for row in (await db.execute(membership_stmt))]
                if len(tenant_ids) > 1:
                    member_tenant_ids = tenant_ids
        except Exception as exc:
            # Non-fatal: fall back to single-org behavior if membership lookup fails
            logger.debug("multi-tenant membership lookup failed (falling back to single-org): %s", exc)

        return await list_connections(
            caller_org_id=effective_org_id,
            caller_user_id=current_user.user_id,
            db=db,
            member_tenant_ids=member_tenant_ids,
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("list-connections failed for org=%s: %s", current_user.org_id, exc)
        raise HTTPException(status_code=500, detail="Failed to list connections") from exc


@router.delete("/github/{installation_id}", response_model=DeleteConnectionResponse)
async def disconnect_github(
    installation_id: int,
    current_user: TokenContext = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> DeleteConnectionResponse:
    """Disconnect a GitHub App installation from the caller's ADP tenant.

    Requires admin role. Revokes the GitHub App installation and removes
    the local tenant mapping.
    """
    try:
        return await delete_connection(
            installation_id=installation_id,
            caller_org_id=current_user.org_id,
            db=db,
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
            user_id=current_user.user_id,
            db=db,
        )
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

    except HTTPException as exc:
        logger.error("register-app-callback HTTP error: %s", exc.detail)
        return _redirect_error("github_error", str(exc.detail))

    except Exception as exc:
        logger.error("register-app-callback unexpected error: %s", exc)
        return _redirect_error("internal_error", "An unexpected error occurred during App registration.")


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
