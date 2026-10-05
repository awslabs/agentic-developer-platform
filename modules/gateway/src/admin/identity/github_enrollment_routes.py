"""GitHub assignments use the canonical origin path (without front-door /api)."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .github_enrollment import GitHubEnrollmentRequest, enroll_github_user

router = APIRouter(prefix="/admin/identity", tags=["identity-admin"], route_class=AuditedAdminRoute, dependencies=[Depends(require_admin)])


@router.post("/organizations/{org_id}/github-members", status_code=201)
async def add_github_member(
    org_id: str,
    req: GitHubEnrollmentRequest,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Platform-admin assignment to a personal GitHub broker identity."""
    mark_admin_effects()
    result = await enroll_github_user(db, org_id, req)
    await write_admin_audit(
        db,
        actor=current_user,
        action="github_member_assignment",
        target_type="user",
        target_id=result["id"],
        extra={"org_id": org_id, "team_id": req.team_id, "github_id": result["github_id"], "role": req.role},
    )
    return result
