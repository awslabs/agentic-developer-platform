"""User management endpoints — invite, list, update role, remove."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org, get_current_user_context
from app.middleware.rbac import require_role
from app.models.user import User, USER_STATUS_DISABLED, USER_STATUS_INVITED
from app.models.organization import Organization
from app.schemas.user import (
    InviteUserRequest,
    UpdateUserRoleRequest,
    UserDeleteResponse,
    UserListResponse,
    UserResponse,
)
from app.services.cognito import (
    admin_create_user,
    admin_disable_user,
    admin_update_user_role,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])


async def _require_legacy_identity_mutation(db, org_id, *, email, subject=None):
    """Never turn an org-local action into a global ADP identity mutation.

    Gateway currently has no supported domain membership-mutation transport.
    Keep the unbound, single-organization Cognito path only; ADP-bound tenants
    must manage membership at its authority until that interface is available.
    """
    organization = await db.get(Organization, org_id)
    if organization is None:
        raise HTTPException(403, "organization unavailable")
    if organization.adp_org_id is not None:
        raise HTTPException(
            409,
            "Manage membership in ADP; the domain membership interface is unavailable",
        )
    identities = [User.email == email]
    if subject:
        identities.append(User.cognito_sub == subject)
    peer = await db.scalar(
        select(User.id).where(User.org_id != org_id, or_(*identities)).limit(1)
    )
    if peer is not None:
        raise HTTPException(
            409, "Global identity mutation would affect another organization"
        )


@router.post(
    "/invite",
    response_model=UserResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_role("org-admin"))],
)
async def invite_user(
    body: InviteUserRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> UserResponse:
    """Invite a user to the organization via Cognito AdminCreateUser.

    Flow:
    1. Check if user already exists in this org
    2. Call Cognito AdminCreateUser (sends invite email with temp password)
    3. Set custom attributes (org_id, role) on Cognito user
    4. Insert user row in Aurora users table
    5. Return the created user object

    Requires: org-admin role.
    """
    # Check for existing user in this org
    existing = await db.execute(
        select(User).where(User.org_id == org_id, User.email == body.email)
    )
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"User {body.email} already exists in this organization",
        )

    await _require_legacy_identity_mutation(db, org_id, email=body.email)
    # Create user in Cognito (sends invite email)
    cognito_sub = await admin_create_user(
        email=body.email,
        org_id=str(org_id),
        role=body.role,
    )

    # Insert user row in Aurora
    user = User(
        org_id=org_id,
        email=body.email,
        cognito_sub=cognito_sub,
        role=body.role,
        status=USER_STATUS_INVITED,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)

    logger.info(
        "User invited: id=%s email=%s role=%s org_id=%s",
        user.id,
        user.email,
        user.role,
        org_id,
    )

    return UserResponse(
        id=user.id,
        org_id=user.org_id,
        email=user.email,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.get("", response_model=UserListResponse)
async def list_users(
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> UserListResponse:
    """List all users in the current organization.

    Any authenticated user can list org members.
    """
    result = await db.execute(
        select(User).where(User.org_id == org_id).order_by(User.created_at.desc())
    )
    users = result.scalars().all()
    return UserListResponse(
        users=[
            UserResponse(
                id=u.id,
                org_id=u.org_id,
                email=u.email,
                role=u.role,
                status=u.status,
                created_at=u.created_at,
                updated_at=u.updated_at,
            )
            for u in users
        ],
        total=len(users),
    )


@router.patch(
    "/{user_id}/role",
    response_model=UserResponse,
    dependencies=[Depends(require_role("org-admin"))],
)
async def update_user_role(
    user_id: uuid.UUID,
    body: UpdateUserRoleRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> UserResponse:
    """Change a user's RBAC role.

    Updates both Aurora users table and Cognito custom:role attribute.
    Requires: org-admin role.
    """
    result = await db.execute(
        select(User).where(User.id == user_id, User.org_id == org_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    if user.status == USER_STATUS_DISABLED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot change role of a disabled user",
        )

    old_role = user.role

    await _require_legacy_identity_mutation(
        db, org_id, email=user.email, subject=user.cognito_sub
    )

    # Update Cognito custom attribute
    await admin_update_user_role(email=user.email, role=body.role)

    # Update Aurora
    user.role = body.role
    await db.commit()
    await db.refresh(user)

    logger.info(
        "User role updated: id=%s email=%s old_role=%s new_role=%s",
        user.id,
        user.email,
        old_role,
        body.role,
    )

    return UserResponse(
        id=user.id,
        org_id=user.org_id,
        email=user.email,
        role=user.role,
        status=user.status,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.delete(
    "/{user_id}",
    response_model=UserDeleteResponse,
    dependencies=[Depends(require_role("org-admin"))],
)
async def delete_user(
    user_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    user_ctx: dict = Depends(get_current_user_context),
    db: AsyncSession = Depends(get_session),
) -> UserDeleteResponse:
    """Remove a user from the organization.

    Disables the user in Cognito and marks as disabled in Aurora.
    Requires: org-admin role.
    """
    result = await db.execute(
        select(User).where(User.id == user_id, User.org_id == org_id)
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )

    # Prevent self-deletion
    if user_ctx.get("user_id") and user_ctx["user_id"] == user.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove yourself from the organization",
        )

    if user.status == USER_STATUS_DISABLED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already disabled",
        )

    await _require_legacy_identity_mutation(
        db, org_id, email=user.email, subject=user.cognito_sub
    )
    # Disable in Cognito
    await admin_disable_user(email=user.email)

    # Mark as disabled in Aurora
    user.status = USER_STATUS_DISABLED
    await db.commit()

    logger.info("User removed: id=%s email=%s org_id=%s", user.id, user.email, org_id)

    return UserDeleteResponse(id=user.id)
