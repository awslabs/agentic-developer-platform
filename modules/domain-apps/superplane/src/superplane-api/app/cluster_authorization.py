"""Live cluster scopes for an exact strictly authenticated, bound caller.

This is domain grant resolution, not ADP membership introspection. Shared
execution remains disabled until current upstream identity is composed.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import VerifiedCaller
from app.models.cluster_grant_scope import (
    CLUSTER_PERMISSIONS,
    OrganizationGrantClusterScope,
)
from app.models.organization import Organization
from app.models.organization_grant import OrganizationGrantRecord
from app.services.provisioning import ProvisioningRefused

REFUSAL = (
    "no eligible shared cluster matches the selected identifier for this organization"
)


async def authorized_cluster_ids(
    db: AsyncSession,
    org_id: uuid.UUID,
    caller: VerifiedCaller | None,
    permission: str,
) -> frozenset[uuid.UUID]:
    """Resolve live scopes; no admin, workspace, role, or legacy fallback."""
    if (
        not isinstance(caller, VerifiedCaller)
        or caller.principal.account_type not in {"human", "service"}
        or not caller.principal.subject
        or caller.principal.org_id != str(org_id)
        or not caller.source_org_id
        or permission not in CLUSTER_PERMISSIONS
    ):
        raise ProvisioningRefused(REFUSAL)
    binding = await db.scalar(
        select(Organization.id).where(
            Organization.id == org_id,
            Organization.adp_org_id == caller.source_org_id,
        )
    )
    if binding is None:
        raise ProvisioningRefused(REFUSAL)
    scopes = await db.scalars(
        select(OrganizationGrantClusterScope)
        .execution_options(populate_existing=True)
        .join(
            OrganizationGrantRecord,
            (OrganizationGrantRecord.id == OrganizationGrantClusterScope.grant_id)
            & (OrganizationGrantRecord.org_id == OrganizationGrantClusterScope.org_id),
        )
        .where(
            OrganizationGrantClusterScope.org_id == org_id,
            OrganizationGrantClusterScope.revoked_at.is_(None),
            OrganizationGrantRecord.org_id == org_id,
            OrganizationGrantRecord.principal == caller.principal.subject,
            OrganizationGrantRecord.principal_type == caller.principal.account_type,
            OrganizationGrantRecord.revoked_at.is_(None),
        )
    )
    return frozenset(
        scope.cluster_id for scope in scopes if permission in scope.permissions.split()
    )
