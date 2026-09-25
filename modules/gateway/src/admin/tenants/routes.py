"""FastAPI router for tenant org-linking (rule 3).

Issue #2954: Platform-admin can link multiple GitHub orgs to one tenant
(many:many, attach-forward-only). Endpoints:
    POST   /admin/tenants/{tenant_id}/orgs         — link an org to a tenant
    DELETE /admin/tenants/{tenant_id}/orgs/{github_org_id} — unlink an org
    GET    /admin/tenants/{tenant_id}/orgs         — list linked orgs
"""

from __future__ import annotations

import hashlib
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.access_control import AccessControl
from src.admin.audit import write_admin_audit
from src.admin.audit_operation import AuditedAdminRoute, mark_admin_effects
from src.admin.exceptions import AccessDeniedError
from src.auth.dependencies import get_current_user
from src.shared.database import get_db
from src.shared.models.organization import Organization
from src.shared.schemas.auth import TokenContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class LinkOrgRequest(BaseModel):
    """Request body for linking an org to a tenant."""

    github_org_id: str
    expected_revision: str | None = None


class LinkOrgResponse(BaseModel):
    """Response for a successful link operation."""

    linked: bool
    tenant_id: str
    github_org_id: str
    org_name: str


class UnlinkOrgResponse(BaseModel):
    """Response for a successful unlink operation."""

    unlinked: bool
    tenant_id: str
    github_org_id: str


class LinkedOrgItem(BaseModel):
    """A single linked org in the list response."""

    org_id: str
    org_name: str
    github_org_id: str | None


class LinkedOrgsListResponse(BaseModel):
    """Response listing all orgs linked to a tenant."""

    tenant_id: str
    linked_orgs: list[LinkedOrgItem]


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = APIRouter(
    route_class=AuditedAdminRoute,
    prefix="/admin/tenants",
    tags=["tenant-org-links"],
)


async def _get_access_control(db: AsyncSession = Depends(get_db)) -> AccessControl:
    """Provide an AccessControl instance for platform_admin checks."""
    return AccessControl(db)


# ---------------------------------------------------------------------------
# POST /admin/tenants/{tenant_id}/orgs — link an org to a tenant
# ---------------------------------------------------------------------------


@router.post("/{tenant_id}/orgs", response_model=LinkOrgResponse)
async def link_org_to_tenant(
    tenant_id: str,
    body: LinkOrgRequest,
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
) -> LinkOrgResponse:
    """Link a GitHub org to a parent tenant. Platform-admin only.

    - 403 if caller is not platform admin.
    - 404 if tenant or org not found.
    - 409 if the org is already linked to a different tenant.
    """
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        )

    # Verify the target tenant exists
    parent_stmt = select(Organization).where(Organization.id == tenant_id).with_for_update()
    parent_org = (await db.execute(parent_stmt)).scalar_one_or_none()
    if not parent_org:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Prevent linking a tenant to itself (it's already its own tenant)
    if parent_org.github_org_id == body.github_org_id:
        raise HTTPException(
            status_code=409,
            detail="Cannot link a tenant to itself",
        )

    # Find the org by github_org_id
    child_stmt = (
        select(Organization)
        .where(
            Organization.github_org_id == body.github_org_id,
        )
        .with_for_update()
    )
    child_org = (await db.execute(child_stmt)).scalar_one_or_none()
    if not child_org:
        raise HTTPException(
            status_code=404,
            detail=f"Organization with github_org_id={body.github_org_id} not found",
        )

    if body.expected_revision is not None and body.expected_revision != _link_revision(parent_org, child_org):
        raise HTTPException(409, {"error": "stale_revision"})

    # Cannot link the parent to itself
    if child_org.id == tenant_id:
        raise HTTPException(
            status_code=409,
            detail="Cannot link a tenant to itself",
        )

    # Check if already linked to a different tenant
    if child_org.parent_tenant_id and child_org.parent_tenant_id != tenant_id:
        raise HTTPException(
            status_code=409,
            detail=(f"Organization '{child_org.name}' is already linked to tenant '{child_org.parent_tenant_id}'"),
        )

    # Prevent chains: reject if the target parent tenant is itself a child
    # (the matcher only resolves one hop, so A←B←C would silently break)
    if parent_org.parent_tenant_id:
        raise HTTPException(
            status_code=409,
            detail=(f"Tenant '{parent_org.name}' is itself linked to another tenant. Daisy-chaining is not supported (single-level linking only)."),
        )

    # Prevent cycles: reject if the child org already has children linked to it
    # (linking it as a child would orphan its own children's resolution)
    has_children_stmt = select(Organization.id).where(
        Organization.parent_tenant_id == child_org.id,
    )
    has_children = (await db.execute(has_children_stmt)).first()
    if has_children:
        raise HTTPException(
            status_code=409,
            detail=(f"Organization '{child_org.name}' is already a parent to other linked orgs. Unlink its children first before making it a child."),
        )

    # Already linked to this tenant — idempotent success
    if child_org.parent_tenant_id == tenant_id:
        await write_admin_audit(
            db,
            actor=current_user,
            action="tenant_link_org",
            target_type="tenant_org_link",
            target_id=child_org.id,
            org_id=tenant_id,
            extra={"github_org_id": body.github_org_id},
        )
        return LinkOrgResponse(
            linked=True,
            tenant_id=tenant_id,
            github_org_id=body.github_org_id,
            org_name=child_org.name,
        )

    # Perform the link (Postgres-only — no DDB write-through needed for this column)
    child_org.parent_tenant_id = tenant_id
    await write_admin_audit(
        db,
        actor=current_user,
        action="tenant_link_org",
        target_type="tenant_org_link",
        target_id=child_org.id,
        org_id=tenant_id,
        extra={"github_org_id": body.github_org_id, "child_org_name": child_org.name},
    )
    mark_admin_effects()
    await db.commit()

    logger.info(
        "Linked org to tenant",
        extra={
            "child_org_id": child_org.id,
            "child_org_name": child_org.name,
            "parent_tenant_id": tenant_id,
            "github_org_id": body.github_org_id,
            "actor": current_user.user_id,
        },
    )

    return LinkOrgResponse(
        linked=True,
        tenant_id=tenant_id,
        github_org_id=body.github_org_id,
        org_name=child_org.name,
    )


# ---------------------------------------------------------------------------
# DELETE /admin/tenants/{tenant_id}/orgs/{github_org_id} — unlink an org
# ---------------------------------------------------------------------------


@router.delete("/{tenant_id}/orgs/{github_org_id}", response_model=UnlinkOrgResponse)
async def unlink_org_from_tenant(
    tenant_id: str,
    github_org_id: str,
    expected_revision: str | None = None,
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
) -> UnlinkOrgResponse:
    """Unlink a GitHub org from a parent tenant. Platform-admin only.

    - 403 if caller is not platform admin.
    - 404 if the org is not linked to this tenant.
    """
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        )

    parent_org = await db.scalar(select(Organization).where(Organization.id == tenant_id).with_for_update())

    # Find the linked org
    stmt = (
        select(Organization)
        .where(
            Organization.github_org_id == github_org_id,
            Organization.parent_tenant_id == tenant_id,
        )
        .with_for_update()
    )
    child_org = (await db.execute(stmt)).scalar_one_or_none()
    if not child_org:
        raise HTTPException(
            status_code=404,
            detail=(f"Organization with github_org_id={github_org_id} is not linked to tenant {tenant_id}"),
        )

    if expected_revision is not None and (parent_org is None or expected_revision != _link_revision(parent_org, child_org)):
        raise HTTPException(409, {"error": "stale_revision"})

    # Perform the unlink
    child_org.parent_tenant_id = None
    await write_admin_audit(
        db,
        actor=current_user,
        action="tenant_unlink_org",
        target_type="tenant_org_link",
        target_id=child_org.id,
        org_id=tenant_id,
        extra={"github_org_id": github_org_id, "child_org_name": child_org.name},
    )
    mark_admin_effects()
    await db.commit()

    logger.info(
        "Unlinked org from tenant",
        extra={
            "child_org_id": child_org.id,
            "child_org_name": child_org.name,
            "parent_tenant_id": tenant_id,
            "github_org_id": github_org_id,
            "actor": current_user.user_id,
        },
    )

    return UnlinkOrgResponse(
        unlinked=True,
        tenant_id=tenant_id,
        github_org_id=github_org_id,
    )


# ---------------------------------------------------------------------------
# GET /admin/tenants/{tenant_id}/orgs — list linked orgs
# ---------------------------------------------------------------------------


@router.get("/{tenant_id}/orgs", response_model=LinkedOrgsListResponse)
async def list_linked_orgs(
    tenant_id: str,
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
) -> LinkedOrgsListResponse:
    """List all orgs linked to a tenant. Platform-admin only."""
    try:
        access.require_platform_admin(current_user)
    except AccessDeniedError:
        raise HTTPException(
            status_code=403,
            detail="Platform administrator privileges required",
        )

    # Verify the tenant exists
    parent_stmt = select(Organization).where(Organization.id == tenant_id).with_for_update()
    parent_org = (await db.execute(parent_stmt)).scalar_one_or_none()
    if not parent_org:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Find all orgs linked to this tenant
    stmt = select(Organization).where(Organization.parent_tenant_id == tenant_id)
    linked_orgs = (await db.execute(stmt)).scalars().all()

    return LinkedOrgsListResponse(
        tenant_id=tenant_id,
        linked_orgs=[
            LinkedOrgItem(
                org_id=org.id,
                org_name=org.name,
                github_org_id=org.github_org_id,
            )
            for org in linked_orgs
        ],
    )


def _link_revision(parent, child):
    values = [parent.id, parent.parent_tenant_id, parent.github_org_id, child.id, child.parent_tenant_id, child.github_org_id]
    return hashlib.sha256(json.dumps(values, separators=(",", ":")).encode()).hexdigest()


@router.get("/{tenant_id}/orgs/{github_org_id}/preview")
async def preview_org_link(
    tenant_id: str,
    github_org_id: str,
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
):
    access.require_platform_admin(current_user)
    parent = await db.scalar(select(Organization).where(Organization.id == tenant_id))
    child = await db.scalar(select(Organization).where(Organization.github_org_id == github_org_id))
    if parent is None or child is None:
        raise HTTPException(404, "Tenant or GitHub organization not found")
    return {
        "tenant_id": tenant_id,
        "github_org_id": github_org_id,
        "org_id": child.id,
        "parent_tenant_id": child.parent_tenant_id,
        "revision": _link_revision(parent, child),
    }


@router.get("/{tenant_id}/orgs/page")
async def linked_org_page(
    tenant_id: str,
    page: int = 1,
    page_size: int = 20,
    current_user: TokenContext = Depends(get_current_user),
    access: AccessControl = Depends(_get_access_control),
    db: AsyncSession = Depends(get_db),
):
    from sqlalchemy import func

    access.require_platform_admin(current_user)
    if not 1 <= page <= 10000 or not 1 <= page_size <= 100:
        raise HTTPException(422, "Pagination is out of bounds")
    if await db.get(Organization, tenant_id) is None:
        raise HTTPException(404, "Tenant not found")
    predicate = Organization.parent_tenant_id == tenant_id
    total = await db.scalar(select(func.count()).select_from(Organization).where(predicate))
    rows = (await db.scalars(select(Organization).where(predicate).order_by(Organization.id).offset((page - 1) * page_size).limit(page_size))).all()
    return {
        "tenant_id": tenant_id,
        "linked_orgs": [{"org_id": row.id, "org_name": row.name, "github_org_id": row.github_org_id} for row in rows],
        "page": page,
        "page_size": page_size,
        "total": total,
        "has_more": page * page_size < total,
    }
