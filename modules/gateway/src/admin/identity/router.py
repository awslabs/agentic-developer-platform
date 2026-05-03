"""FastAPI router for tenant-identity admin API.

Issue #387: Mounted at /api/admin/identity. All endpoints require adp-platform-admins
group membership (enforced via require_admin dependency).
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.dependencies import get_current_user, require_admin
from src.shared.database import get_db
from src.shared.schemas.auth import TokenContext

from .identities_service import IdentitiesService
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
    """Create a new organization with default dept, team, and channel mappings."""
    svc = OrganizationsService(db)
    try:
        return await svc.create_organization(req)
    except Exception as e:
        logger.error("Failed to create organization %s: %s", req.id, e)
        raise HTTPException(status_code=409, detail=str(e)) from e


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
    """Update an organization."""
    svc = OrganizationsService(db)
    org = await svc.update_organization(org_id, req)
    if org is None:
        raise HTTPException(status_code=404, detail=f"Organization {org_id} not found")
    return org


@router.delete("/organizations/{org_id}", status_code=204)
async def delete_organization(
    org_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: TokenContext = Depends(get_current_user),
):
    """Soft-delete (archive) an organization."""
    svc = OrganizationsService(db)
    deleted = await svc.delete_organization(org_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Organization {org_id} not found")


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
    svc = UsersService(db)
    try:
        return await svc.create_user(org_id, req)
    except Exception as e:
        logger.error("Failed to create user in org %s: %s", org_id, e)
        raise HTTPException(status_code=409, detail=str(e)) from e


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
    svc = UsersService(db)
    deleted = await svc.delete_user(org_id, user_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found in org {org_id}")


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
    svc = IdentitiesService(db)
    result = await svc.add_identity(user_id, req)
    if result is None:
        raise HTTPException(status_code=404, detail=f"User {user_id} not found")
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
    svc = IdentitiesService(db)
    deleted = await svc.delete_identity(user_id, identity_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"Identity {identity_id} not found for user {user_id}")
