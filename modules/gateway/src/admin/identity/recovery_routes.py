"""Native Cognito recovery endpoints using the gateway's origin-path convention.

The legacy identity router retains its client-compensated /api prefix. New
endpoints use /admin so CloudFront's single /api strip reaches them directly.
"""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .schemas import CognitoLinkRequest, UserProvisionRequest, UserResponse
from .users_service import UsersService

router = APIRouter(route_class=AuditedAdminRoute, prefix="/admin/identity", tags=["identity-admin"], dependencies=[Depends(require_admin)])


@router.post("/organizations/{org_id}/users/{user_id}/provision", response_model=UserResponse)
async def provision_user(
    org_id: str,
    user_id: str,
    req: UserProvisionRequest,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Retry Cognito provisioning on a saved ADP user; do not POST another user."""
    AccessControl(db).require_platform_admin(current_user)
    mark_admin_effects()
    result = await UsersService(db).provision_user(org_id, user_id, send_invite=req.send_invite)
    await write_admin_audit(
        db,
        actor=current_user,
        action="identity_provision_user",
        target_type="user",
        target_id=user_id,
        org_id=org_id,
    )
    return result


@router.put("/organizations/{org_id}/users/{user_id}/cognito", response_model=UserResponse)
async def link_cognito_user(
    org_id: str,
    user_id: str,
    req: CognitoLinkRequest,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Link an operator-created native login after verifying its immutable sub."""
    AccessControl(db).require_platform_admin(current_user)
    mark_admin_effects()
    result = await UsersService(db).link_cognito_user(org_id, user_id, req)
    await write_admin_audit(
        db,
        actor=current_user,
        action="identity_link_cognito_user",
        target_type="user",
        target_id=user_id,
        org_id=org_id,
    )
    return result
