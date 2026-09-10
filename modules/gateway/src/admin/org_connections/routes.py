"""Platform-admin routes for the org GitHub-connection lifecycle.

Issue #4842 (EPIC #4839 · C3). UI surface: the Connections tab of
``docs/mockups/4839-tenancy-admin.html`` — T2a/T2b render it read-only, these
routes are what make it actionable.

Mount path
----------
``/admin/organizations/{org_id}/connections/...`` — deliberately NOT
``/api/admin/...``. CloudFront strips the first ``/api`` before the origin, so a
router carrying that prefix is unreachable through the dashboard (issue #4330,
enforced by ``tests/test_route_prefix_convention.py``, whose quarantine list must
not grow).

Authorization
-------------
Platform admin, checked twice: once on the router mount and once in each handler.
This mirrors ``admin/identity/router.py`` and ``admin/tenants/routes.py`` and the
reason is the same — these routes mint tenant identity (they decide which tenant
a GitHub org's webhooks resolve to), and a blast radius that large should not rest
on a gate that lives only in the mount, one refactor away from silently
disappearing. ``Permission.ORG_UPDATE`` is deliberately not used: in-repo
precedent (#3981/#4018) holds it insufficient for identity-minting writes because
a tenant's own org_admin satisfies it, and this route can target any org.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .schemas import (
    GitHubConnectionAttachRequest,
    GitHubConnectionDetachResponse,
    GitHubConnectionListResponse,
    GitHubConnectionResponse,
)
from .service import (
    ConnectionNotFoundError,
    OrganizationNotFoundError,
    OrgConnectionsService,
)

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/admin/organizations",
    tags=["org-connections"],
    dependencies=[Depends(require_admin)],
)


def _require_platform_admin(db: AsyncSession, current_user: TokenContext, org_id: str, action: str) -> None:
    """In-handler platform-admin re-check. See the module docstring."""
    try:
        AccessControl(db).require_platform_admin(current_user)
    except AccessDeniedError:
        logger.warning(
            "event=org_connection_denied action=%s org=%s caller=%s reason=not_platform_admin",
            action,
            org_id,
            current_user.user_id,
        )
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        ) from None


@router.get("/{org_id}/connections/github", response_model=GitHubConnectionListResponse)
async def list_github_connections(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
) -> GitHubConnectionListResponse:
    """List an organization's GitHub connections.

    - 403 if the caller is not a platform admin.
    - 404 if the organization does not exist.
    """
    _require_platform_admin(db, current_user, org_id, "list")
    try:
        return await OrgConnectionsService(db).list_connections(org_id)
    except OrganizationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.post("/{org_id}/connections/github", response_model=GitHubConnectionResponse, status_code=201)
async def attach_github_connection(
    org_id: str,
    req: GitHubConnectionAttachRequest,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
) -> GitHubConnectionResponse:
    """Attach a GitHub App installation to an organization.

    - 403 if the caller is not a platform admin, or if the installation's
      ownership cannot be verified.
    - 404 if the organization does not exist.
    - 409 if the installation is already connected to another organization, or
      its ownership is disputed.

    The 409/403 pair is raised by ``InstallationClaimError``, which carries its
    own status code and is rendered by ``app.py``'s ``BedrockGatewayError``
    handler — deliberately NOT caught here, so this route cannot soften a
    fail-closed refusal into a success or flatten it into a generic error.
    """
    _require_platform_admin(db, current_user, org_id, "attach")
    try:
        return await OrgConnectionsService(db).attach_github(org_id, req)
    except OrganizationNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e


@router.delete("/{org_id}/connections/github/{installation_id}", response_model=GitHubConnectionDetachResponse)
async def detach_github_connection(
    org_id: str,
    installation_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
) -> GitHubConnectionDetachResponse:
    """Detach a GitHub App installation from an organization.

    - 403 if the caller is not a platform admin.
    - 404 if the organization does not exist, or the installation is not
      connected to it.

    The response body carries an explicit warning that webhook dispatch for this
    GitHub organization stops. That is intended behaviour, not a side-effect:
    events that no longer resolve to a tenant are refused rather than routed to a
    guess.
    """
    _require_platform_admin(db, current_user, org_id, "detach")
    try:
        return await OrgConnectionsService(db).detach_github(org_id, installation_id)
    except (OrganizationNotFoundError, ConnectionNotFoundError) as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
