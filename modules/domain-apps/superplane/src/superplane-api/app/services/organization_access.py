"""Current-human organization reads without role or workspace inheritance."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.current_identity import IdentityUnavailable, require_current_identity
from app.models.organization import Organization
from app.models.organization_grant import ORGANIZATION_ADMINISTER, ORGANIZATION_READ, OrganizationGrantRecord
from app.schemas.organization_access import (
    OrganizationAccessResponse, OrganizationAssignmentResponse, OrganizationAssignmentsResponse,
)


def _permissions(grant: OrganizationGrantRecord) -> set[str]:
    return grant.permission_values() & {ORGANIZATION_READ, ORGANIZATION_ADMINISTER}


def _assignment(grant: OrganizationGrantRecord) -> OrganizationAssignmentResponse:
    return OrganizationAssignmentResponse(
        organization_id=grant.org_id, grant_id=grant.id, principal_type=grant.principal_type,
        subject=grant.principal, assigned_permissions=sorted(_permissions(grant)),
        revoked_at=grant.revoked_at, granted_by=grant.granted_by, granted_at=grant.created_at,
    )


async def _authorized_grant(db: AsyncSession, caller, reader, permission: str) -> OrganizationGrantRecord:
    if caller.principal.account_type != "human" or not caller.source_org_id:
        raise HTTPException(403, "current human ADP organization binding required")
    if reader is None:
        raise HTTPException(503, "current ADP identity reader unavailable")
    try:
        await require_current_identity(
            reader, subject=caller.principal.subject, principal_type="human",
            adp_org_id=caller.source_org_id, membership_id=caller.identity_evidence,
        )
    except IdentityUnavailable:
        raise HTTPException(403, "current human membership required") from None
    org_id = uuid.UUID(caller.principal.org_id)
    organization = await db.scalar(select(Organization).where(
        Organization.id == org_id, Organization.adp_org_id == caller.source_org_id,
    ))
    if organization is None:
        raise HTTPException(403, "current ADP organization binding required")
    grant = await db.scalar(select(OrganizationGrantRecord).where(
        OrganizationGrantRecord.org_id == org_id,
        OrganizationGrantRecord.principal == caller.principal.subject,
        OrganizationGrantRecord.principal_type == "human",
        OrganizationGrantRecord.revoked_at.is_(None),
    ).with_for_update(read=True).execution_options(populate_existing=True))
    if grant is None or not (_permissions(grant) & {permission, ORGANIZATION_ADMINISTER}):
        raise HTTPException(403, "explicit live organization grant required")
    return grant


async def read_my_organization_access(db: AsyncSession, caller, reader) -> OrganizationAccessResponse:
    grant = await _authorized_grant(db, caller, reader, ORGANIZATION_READ)
    effective = _permissions(grant)
    if ORGANIZATION_ADMINISTER in effective:
        effective.add(ORGANIZATION_READ)
    return OrganizationAccessResponse(
        **_assignment(grant).model_dump(), effective_permissions=sorted(effective),
    )


async def list_organization_assignments(
    db: AsyncSession, caller, reader, *, limit: int = 50, after: uuid.UUID | None = None,
) -> OrganizationAssignmentsResponse:
    actor = await _authorized_grant(db, caller, reader, ORGANIZATION_ADMINISTER)
    query = select(OrganizationGrantRecord).where(
        OrganizationGrantRecord.org_id == actor.org_id,
    ).order_by(OrganizationGrantRecord.id)
    if after is not None:
        query = query.where(OrganizationGrantRecord.id > after)
    rows = list((await db.scalars(query.limit(limit + 1))).all())
    page = rows[:limit]
    return OrganizationAssignmentsResponse(
        organization_id=actor.org_id, assignments=[_assignment(grant) for grant in page],
        next_after=page[-1].id if len(rows) > limit else None,
    )
