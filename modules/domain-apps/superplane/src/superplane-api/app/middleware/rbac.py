"""RBAC middleware — role-based access control for API endpoints.

Provides FastAPI dependencies that enforce role requirements on endpoints.

Roles (ordered by privilege):
- developer: deploy, view workspaces/deployments
- workspace-admin: manage workspaces, credentials (includes developer perms)
- org-admin: manage org, billing, users (includes all perms)
"""

import logging
from typing import Callable

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session

from app.middleware.auth import get_current_user_context

logger = logging.getLogger(__name__)

# Role hierarchy — higher index = more privilege
ROLE_HIERARCHY = {
    "developer": 0,
    "workspace-admin": 1,
    "org-admin": 2,
}


def _has_permission(user_role: str, required_role: str) -> bool:
    """Check if user_role meets or exceeds the required_role in the hierarchy."""
    user_level = ROLE_HIERARCHY.get(user_role, -1)
    required_level = ROLE_HIERARCHY.get(required_role, 99)
    return user_level >= required_level


def require_role(minimum_role: str) -> Callable:
    """FastAPI dependency factory — require at least the given role.

    Usage:
        @router.post("/users/invite", dependencies=[Depends(require_role("org-admin"))])
        async def invite_user(...):
            ...
    """

    async def _check_role(
        request: Request,
        user_ctx: dict = Depends(get_current_user_context),
        db: AsyncSession = Depends(get_session),
    ) -> dict:
        caller = getattr(request.state, "caller", None)
        if caller is not None:
            # Display roles are not ADP permissions. User-management mutations
            # require a current human organization grant, never a service's grant
            # or the initiating human's role carried by a delegated service.
            if minimum_role != "org-admin" or caller.principal.account_type != "human":
                raise HTTPException(403, "human organization administration required")
            from app.auth import Permission, authorize_organization_operation

            await authorize_organization_operation(db, caller, Permission.ADMINISTER)
            return user_ctx
        user_role = user_ctx.get("role", "")
        if not _has_permission(user_role, minimum_role):
            logger.warning(
                "RBAC denied: user=%s role=%s required=%s",
                user_ctx.get("user_id"),
                user_role,
                minimum_role,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions. Required role: {minimum_role}, your role: {user_role}",
            )
        return user_ctx

    return _check_role
