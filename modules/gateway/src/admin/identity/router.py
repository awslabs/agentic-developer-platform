"""FastAPI router for tenant-identity admin API.

Issue #387: Mounted at /api/admin/identity. All endpoints require adp-platform-admins
group membership (enforced via require_admin dependency).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.exceptions import BedrockGatewayError
from src.shared.schemas.auth import TokenContext

from .identities_service import IdentitiesService
from .identity_index_writer import IdentityIndexWriter
from .organizations_service import OrganizationsService
from .schemas import (
    IdentityCreateRequest,
    IdentityListResponse,
    IdentityResponse,
    OrganizationCreateRequest,
    OrganizationListResponse,
    OrganizationResponse,
    OrganizationUpdateRequest,
    UserCreateRequest,
    UserListResponse,
    UserResponse,
)
from .users_service import UsersService

logger = logging.getLogger(__name__)

router = APIRouter(
    route_class=AuditedAdminRoute,
    prefix="/api/admin/identity",
    tags=["identity-admin"],
    dependencies=[Depends(require_admin)],
)


# ---------------------------------------------------------------------------
# Organizations
# ---------------------------------------------------------------------------


@router.post("/organizations", response_model=OrganizationResponse, status_code=201)
async def create_organization(
    req: OrganizationCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Create a new organization with default dept, team, and channel mappings.

    - 409 if the id or name is already taken (a genuine conflict).
    - 500 for anything else.

    Issue #4842: this handler used to map EVERY exception to 409. A conflict tells
    the caller "your input collides with existing state, change it and retry" —
    so a DB outage, a bug in the service, or a failed Cognito call all arrived
    looking like the caller's fault, and an operator retrying with a different id
    got the same 409 forever with nothing pointing at the real cause. Only an
    integrity violation is a conflict; everything else is ours and must surface
    as a 5xx so it is visible as a server error.
    """
    svc = OrganizationsService(db)
    try:
        mark_admin_effects()
        result = await svc.create_organization(req)
        await write_admin_audit(
            db,
            actor=current_user,
            action="identity_create_organization",
            target_type="organization",
            target_id=req.id,
            org_id=req.id,
        )
        return result
    except IntegrityError as e:
        # Unique/PK violation — the id or name is taken. The one genuine 409.
        logger.warning("Conflict creating organization %s: %s", req.id, e)
        raise HTTPException(
            status_code=409,
            detail=f"Organization {req.id} conflicts with an existing organization (duplicate id or name).",
        ) from e
    except BedrockGatewayError:
        # Carries its own status code; app.py's registered handler renders it.
        # Re-raised untouched so an InstallationClaimError stays a 409/403 with
        # its own message rather than being flattened into this route's 409.
        raise
    except Exception as e:
        logger.exception("Failed to create organization %s", req.id)
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create organization {req.id}.",
        ) from e


@router.get("/organizations", response_model=OrganizationListResponse)
async def list_organizations(
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """List all organizations."""
    svc = OrganizationsService(db)
    orgs = await svc.list_organizations()
    return OrganizationListResponse(organizations=orgs, total=len(orgs))


@router.get("/organizations/{org_id}", response_model=OrganizationResponse)
async def get_organization(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Get a single organization by ID."""
    svc = OrganizationsService(db)
    org = await svc.get_organization(org_id)
    if org is None:
        raise HTTPException(status_code=404, detail=f"Organization {org_id} not found")
    return org


@router.patch("/organizations/{org_id}", response_model=OrganizationResponse)
async def update_organization(
    org_id: str,
    req: OrganizationUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Update an organization. Platform-admin only.

    - 403 if the caller is not a platform admin.
    - 404 if the organization does not exist.
    - 409 if the request claims a GitHub installation owned by another tenant.
    """
    # Issue #4072 (#11, HIGH), decision D4 — defense in depth, stated honestly:
    # the router-level ``require_admin`` dependency already checks
    # TokenContext.is_admin, which is PLATFORM admin only (auth/dependencies.py
    # deliberately excludes org_admin per #3981), so this is NOT closing a live
    # escalation the way the #5 fix is. It is here because this route mints tenant
    # identity — it rebinds github_installation_ids AND rewrites the org's
    # channel_tenant_map rows, which is what decides where a GitHub or Slack event
    # gets routed — and a blast radius that large should not rest on a gate that
    # lives only in the mount, one refactor away from silently disappearing. Every
    # other identity-minting route in this file re-checks in the handler.
    #
    # Mirrors admin/tenants/routes.py::link_org_to_tenant (the pre-existing
    # platform-admin-only org-linking endpoint) rather than Permission.ORG_UPDATE:
    # in-repo precedent (#3981/#4018) holds ORG_UPDATE insufficient for
    # identity-minting writes because a tenant's own org_admin satisfies it.
    try:
        AccessControl(db).require_platform_admin(current_user)
    except AccessDeniedError:
        logger.warning(
            "event=identity_org_update_denied org=%s caller=%s reason=not_platform_admin",
            org_id,
            current_user.user_id,
        )
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        ) from None

    svc = OrganizationsService(db)
    mark_admin_effects()
    org = await svc.update_organization(org_id, req)
    if org is None:
        raise HTTPException(status_code=404, detail=f"Organization {org_id} not found")
    await write_admin_audit(
        db,
        actor=current_user,
        action="identity_update_organization",
        target_type="organization",
        target_id=org_id,
        org_id=org_id,
    )
    return org


@router.delete("/organizations/{org_id}", status_code=204)
async def delete_organization(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Soft-delete (archive) an organization."""
    svc = OrganizationsService(db)
    mark_admin_effects()
    deleted = await svc.delete_organization(org_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Organization {org_id} not found")
    await write_admin_audit(
        db,
        actor=current_user,
        action="identity_delete_organization",
        target_type="organization",
        target_id=org_id,
        org_id=org_id,
    )


# ---------------------------------------------------------------------------
# Users within an organization
# ---------------------------------------------------------------------------


@router.post("/organizations/{org_id}/users", response_model=UserResponse, status_code=201)
async def create_user(
    org_id: str,
    req: UserCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Create a user within an organization and optionally send a Cognito invite."""
    # Issue #4006: UserCreateRequest.role is a free-form string, so guard the
    # role-assignment ceiling the same way the equivalent route in admin/routes.py
    # does — a caller must not be able to grant a role above its own privilege.
    # The router-level require_admin dependency gates *who* may call this; this
    # gates *which role* they may grant. Raised as-is (BedrockGatewayError carries
    # its own status code) so it is never swallowed by the 409 handler below.
    await AccessControl(db).require_assignable_role(current_user, req.role, target_org_id=org_id)
    if req.cognito_identity:
        AccessControl(db).require_platform_admin(current_user)

    svc = UsersService(db, identity_writer=IdentityIndexWriter())
    try:
        mark_admin_effects()
        result = await svc.create_user(org_id, req)
        await write_admin_audit(
            db,
            actor=current_user,
            action="identity_create_user",
            target_type="user",
            target_id=result.id if hasattr(result, "id") else str(req.email),
            org_id=org_id,
        )
        return result
    except BedrockGatewayError:
        raise
    except IntegrityError as e:
        raise HTTPException(status_code=409, detail="User conflicts with an existing identity") from e
    except Exception as e:
        logger.exception("Failed to create user in org %s", org_id)
        raise HTTPException(status_code=500, detail="Failed to create user") from e


@router.get("/organizations/{org_id}/users", response_model=UserListResponse)
async def list_users(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """List all users in an organization."""
    svc = UsersService(db)
    users = await svc.list_users(org_id)
    return UserListResponse(users=users, total=len(users))


@router.delete("/organizations/{org_id}/users/{user_id}", status_code=204)
async def delete_user(
    org_id: str,
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Delete a user from an organization."""
    svc = UsersService(db, identity_writer=IdentityIndexWriter())
    mark_admin_effects()
    deleted = await svc.delete_user(org_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found in org {org_id}")
    await write_admin_audit(
        db,
        actor=current_user,
        action="identity_delete_user",
        target_type="user",
        target_id=user_id,
        org_id=org_id,
    )


# ---------------------------------------------------------------------------
# Identity linkage (cross-channel merge)
# ---------------------------------------------------------------------------


@router.post("/users/{user_id}/identities", response_model=IdentityResponse, status_code=201)
async def add_identity(
    user_id: str,
    req: IdentityCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Add a new provider identity to an existing user (cross-channel linkage)."""
    svc = IdentitiesService(db, identity_writer=IdentityIndexWriter())
    mark_admin_effects()
    result = await svc.add_identity(user_id, req)
    if result is None:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
    await write_admin_audit(
        db,
        actor=current_user,
        action="identity_add_identity",
        target_type="identity",
        target_id=result.id if hasattr(result, "id") else user_id,
        extra={"user_id": user_id, "provider": req.provider if hasattr(req, "provider") else None},
    )
    return result


@router.get("/users/{user_id}/identities", response_model=IdentityListResponse)
async def list_identities(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """List all identities for a user."""
    svc = IdentitiesService(db)
    identities = await svc.list_identities(user_id)
    return IdentityListResponse(identities=identities, total=len(identities))


@router.delete("/users/{user_id}/identities/{identity_id}", status_code=204)
async def delete_identity(
    user_id: str,
    identity_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Remove a provider identity from a user."""
    svc = IdentitiesService(db, identity_writer=IdentityIndexWriter())
    mark_admin_effects()
    deleted = await svc.delete_identity(user_id, identity_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found for user {user_id}")
    await write_admin_audit(
        db,
        actor=current_user,
        action="identity_delete_identity",
        target_type="identity",
        target_id=identity_id,
        extra={"user_id": user_id},
    )
