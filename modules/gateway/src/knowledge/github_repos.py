"""Repo picker API — list repos accessible to the caller's tenant GitHub App.

Issue #2045: Relocated from agent_context.api.github_repos into the gateway.
Original: Issue #1793 (Story D of E10 #1736).
Route: GET /api/agent-context/github/accessible-repos

Powers the "Add repo" UI in the knowledge-assets management page. Verifies that
the caller's tenant has a GitHub App installed, resolves credentials from Secrets
Manager, and lists accessible repos via the GitHub Installation API.

Auth (Cognito JWT) is provided by the gateway middleware stack.

Gating: this module's router is only exposed when AGENT_CONTEXT_ENABLED=true.
The UNIT_MODULES auto-discovery loop checks `hasattr(module, "router")` — when
the gate is off, `router` is not defined so the routes are silently skipped.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user
from src.knowledge.github_app_service import (
    list_accessible_repos,
    resolve_tenant_app_credentials,
)
from src.knowledge.github_repos_schemas import (
    AccessibleRepo,
    AccessibleReposResponse,
)
from src.shared.database import get_db

logger = logging.getLogger("bedrockgateway.knowledge.github_repos")

_router = APIRouter(prefix="/api/agent-context/github", tags=["github-repos"])


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@_router.get("/accessible-repos", response_model=AccessibleReposResponse)
async def get_accessible_repos(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    q: Annotated[str | None, Query(description="Search filter on repo full_name")] = None,
    page: Annotated[int, Query(ge=1, description="Page number")] = 1,
    page_size: Annotated[int, Query(ge=1, le=100, description="Results per page")] = 50,
) -> AccessibleReposResponse:
    """List repos accessible to the caller's tenant GitHub App installation.

    Resolves App credentials from Secrets Manager at
    adp/<env>/tenants/<org_id>/github-app, then uses the installation token
    to query GitHub's /installation/repositories endpoint.
    """
    org_id = current_user.org_id
    if not org_id:
        raise HTTPException(
            status_code=409,
            detail="User has no org assignment; complete onboarding first.",
        )

    # Find the tenant's GitHub App installation_id from ChannelTenantMap
    installation_id = await _resolve_installation_id(db, org_id)
    if installation_id is None:
        raise HTTPException(
            status_code=404,
            detail=("No GitHub App installed for this tenant. Install a GitHub App via Settings > Connections first."),
        )

    # Resolve App credentials from Secrets Manager
    try:
        app_id, private_key = await resolve_tenant_app_credentials(org_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # List repos
    try:
        result = await list_accessible_repos(
            installation_id=installation_id,
            app_id=app_id,
            private_key_pem=private_key,
            q=q,
            page=page,
            page_size=page_size,
        )
    except Exception as exc:
        logger.error(
            "Failed to list repos for tenant %s installation %d: %s",
            org_id,
            installation_id,
            exc,
        )
        raise HTTPException(
            status_code=502,
            detail="Failed to fetch repos from GitHub. The installation may be expired or revoked.",
        ) from exc

    repos = [AccessibleRepo(**r) for r in result["repos"]]
    return AccessibleReposResponse(
        repos=repos,
        total=result["total"],
        page=result["page"],
        has_more=result["has_more"],
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _resolve_installation_id(db: AsyncSession, org_id: str) -> int | None:
    """Resolve the GitHub App installation_id for a tenant from ChannelTenantMap.

    Looks up ChannelTenantMap rows where provider='github' and org_id matches.
    Returns the first installation_id found (from metadata JSON), or None.
    """
    import json as json_module

    result = await db.execute(
        text("""
            SELECT metadata FROM channel_tenant_map
            WHERE provider = 'github' AND org_id = :org_id
            LIMIT 1
        """),
        {"org_id": org_id},
    )
    row = result.fetchone()
    if row is None:
        return None

    metadata = row[0] if row else None
    if not metadata:
        return None

    # metadata is a JSON dict with "installation_id" key
    if isinstance(metadata, str):
        metadata = json_module.loads(metadata)

    install_id = metadata.get("installation_id")
    if install_id:
        return int(install_id)
    return None


# ---------------------------------------------------------------------------
# Conditional export — gated behind AGENT_CONTEXT_ENABLED
# ---------------------------------------------------------------------------

if os.environ.get("AGENT_CONTEXT_ENABLED", "").lower() == "true":
    router = _router
