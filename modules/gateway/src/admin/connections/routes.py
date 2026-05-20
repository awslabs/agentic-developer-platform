"""FastAPI router for the connections module.

Issue #465: GitHub App install + connection management.

Endpoints:
    POST   /api/admin/connections/github/install-start
    GET    /api/admin/connections/github/install-callback
    GET    /api/admin/connections
    DELETE /api/admin/connections/github/{installation_id}
"""

from __future__ import annotations

import logging
import urllib.parse

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from sqlalchemy.ext.asyncio import AsyncSession

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
    ConnectionsListResponse,
    DeleteConnectionResponse,
    InstallStartResponse,
)
from .service import (
    delete_connection,
    install_callback,
    install_start,
    list_connections,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/api/admin/connections",
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
    except Exception as exc:
        logger.error("install-start failed for user=%s: %s", current_user.user_id, exc)
        raise HTTPException(status_code=500, detail="Failed to initiate GitHub install flow") from exc


@router.get("/github/install-callback")
async def github_install_callback(
    installation_id: int,
    setup_action: str = "install",
    state: str = "",
    current_user: TokenContext = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Handle the GitHub redirect after app installation.

    GitHub redirects here with ?installation_id=&setup_action=&state=.
    Validates the state nonce, consumes it, and attaches the installation
    to the caller's ADP tenant. Redirects to the connections page on both
    success and failure.
    """
    if not state:
        return _redirect_error("missing_state", "Missing state parameter from GitHub redirect")

    try:
        await install_callback(
            installation_id=installation_id,
            setup_action=setup_action,
            state=state,
            caller_user_id=current_user.user_id,
            caller_org_id=current_user.org_id,
            db=db,
        )
        return _redirect_success(installation_id)

    except (NonceNotFoundError, TokenExpiredError) as exc:
        logger.warning("install-callback invalid state jti=%s: %s", state, exc)
        return _redirect_error("invalid_state", "Installation link has expired or is invalid. Please try again.")

    except NonceAlreadyConsumedError as exc:
        logger.warning("install-callback replayed state jti=%s: %s", state, exc)
        return _redirect_error("state_replayed", "Installation link already used. Please start a new install.")

    except TargetUserMismatchError as exc:
        logger.warning("install-callback cross-user attempt jti=%s user=%s: %s", state, current_user.user_id, exc)
        return _redirect_error("unauthorized", "Installation link was not issued for your account.")

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
    """List all GitHub App installations connected to the caller's ADP tenant."""
    try:
        effective_org_id = await resolve_effective_org_id(current_user, db)
        return await list_connections(
            caller_org_id=effective_org_id,
            caller_user_id=current_user.user_id,
            db=db,
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
