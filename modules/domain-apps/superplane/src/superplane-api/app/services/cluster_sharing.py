"""Resolve and reserve shared cluster placement for workspace creation (issue #6048).

DESIGN.md §2.5 step 2 requires workspace creation to preview "dedicated/shared
placement, exact cluster identity". This module is the server-side resolver for
the "shared" branch: it never trusts a caller's cluster identity claim beyond a
selection, and it never infers eligibility from a cluster's name, AWS account, or
having a dedicated member already — only the explicit `sharing_enabled` (and, for
ADP's own management cluster, `platform_eligible`) flags make a cluster eligible.

## Why this is a separate module rather than a branch inside `onboarding.py`

`onboarding.py::preview` already resolves account/mode/region for the *dedicated*
path against `LifecyclePolicy` and the account-factory validators, none of which
know about cluster membership. Shared placement resolves a different kind of
target — an existing cluster this organization already owns eligibility over —
so it gets its own resolution function with its own refusal vocabulary, and
`onboarding.py` calls into it rather than growing a second, parallel meaning for
its existing checks.

## Cross-organization isolation

Every query here filters on `Cluster.org_id == org_id` using the SERVER-RESOLVED
organization (the caller's verified token), never a value from the request body.
A `shared_cluster_id` naming another organization's cluster resolves to "not
found" — the same refusal as a nonexistent id — so this cannot be used to probe
which cluster ids exist in another tenant, mirroring the non-enumeration
discipline `superplane_contracts.scoping` already establishes for observations.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import VerifiedCaller
from app.cluster_authorization import REFUSAL, authorized_cluster_ids
from app.models.cluster import Cluster
from app.models.cluster_grant_scope import CLUSTER_USE
from app.models.cluster_membership import (
    STATE_ACTIVE,
    STATE_RESERVED,
    ClusterMembership,
)
from app.models.workspace import Workspace
from app.services.provisioning import ProvisioningRefused

# Cluster statuses eligible for a new member. Mirrors the existing dedicated-path
# readiness check in `controller_deployments.py` (`cluster.status not in {"Ready",
# "Active"}` refuses deployment) — a cluster that is not ready for its existing
# workload is not ready for a new member either.
ELIGIBLE_CLUSTER_STATUSES = frozenset({"Ready", "Active"})

# Membership states that occupy a cluster slot and therefore count toward its
# live membership when checking for namespace/identity conflicts. `removed` does
# not: a removed membership is a tombstone, not a current occupant.
LIVE_MEMBERSHIP_STATES = frozenset({STATE_RESERVED, STATE_ACTIVE})
RETIRING_OWNER_STATES = frozenset({"Teardown", "retired", "Deleted"})


async def _owner_accepts_members(db: AsyncSession, cluster: Cluster) -> bool:
    if cluster.workspace_id is None:
        return True
    owner_status = await db.scalar(
        select(Workspace.status).where(
            Workspace.id == cluster.workspace_id,
            Workspace.org_id == cluster.org_id,
        )
    )
    return owner_status is not None and owner_status not in RETIRING_OWNER_STATES


@dataclass(frozen=True)
class EligibleCluster:
    """One cluster this organization may place a new shared member on."""

    id: uuid.UUID
    name: str
    cluster_arn: str | None
    endpoint: str | None
    region: str | None
    platform_eligible: bool
    member_count: int


@dataclass(frozen=True)
class ResolvedSharedTarget:
    """The verified cluster identity to carry into preview/approval/execution.

    Mirrors the shape `verify_target`'s `VerifiedTarget` carries for the
    dedicated/adopted path (`workspace_bootstrap/superplane_bootstrap/target.py`):
    every field here is read from storage under the caller's organization, never
    echoed from the request, so a downstream consumer holds evidence rather than
    a claim.
    """

    cluster_id: uuid.UUID
    cluster_arn: str | None
    endpoint: str | None
    platform_eligible: bool


async def list_eligible_clusters(
    db: AsyncSession, org_id: uuid.UUID, *, caller: VerifiedCaller | None = None
) -> list[EligibleCluster]:
    """Clusters this exact caller may select with an explicit live use scope.

    Returns only clusters explicitly marked `sharing_enabled` under this exact
    organization — never another organization's, even one sharing the same AWS
    account (DESIGN.md: "An AWS Organizations identifier is not an ADP tenant
    identifier"). `member_count` counts live (non-removed) memberships so a
    caller can see how many workspaces are already there before choosing.
    """
    allowed = await authorized_cluster_ids(db, org_id, caller, CLUSTER_USE)
    result = await db.execute(
        select(Cluster).where(
            Cluster.id.in_(allowed),
            Cluster.org_id == org_id,
            Cluster.sharing_enabled.is_(True),
            Cluster.status.in_(ELIGIBLE_CLUSTER_STATUSES),
        )
    )
    clusters = result.scalars().all()
    eligible: list[EligibleCluster] = []
    for cluster in clusters:
        if not await _owner_accepts_members(db, cluster):
            continue
        member_result = await db.execute(
            select(ClusterMembership.id).where(
                ClusterMembership.cluster_id == cluster.id,
                ClusterMembership.state.in_(LIVE_MEMBERSHIP_STATES),
            )
        )
        member_count = len(member_result.scalars().all())
        eligible.append(
            EligibleCluster(
                id=cluster.id,
                name=cluster.name,
                cluster_arn=cluster.eks_cluster_arn,
                endpoint=cluster.endpoint,
                region=None,
                platform_eligible=cluster.platform_eligible,
                member_count=member_count,
            )
        )
    return eligible


async def resolve_shared_target(
    db: AsyncSession,
    org_id: uuid.UUID,
    shared_cluster_id: uuid.UUID,
    *,
    caller: VerifiedCaller | None = None,
) -> ResolvedSharedTarget:
    """Verify a shared-placement selection under the caller's organization.

    Requires the exact verified caller's live cluster-use scope.
    Refuses (never returns a partial result) for every ineligible case: cluster
    absent, cluster belongs to another organization, sharing not enabled, or
    cluster not currently `Ready`/`Active`. All four refusals use the same
    message shape as `resolve_cluster_workspace`'s ownership check in
    `observations.py` for the same reason: which specific check failed for
    another organization's cluster is itself information about that
    organization's inventory, and revealing it would make this an enumeration
    oracle. A caller sees only "no eligible shared cluster" either way.
    """
    allowed = await authorized_cluster_ids(db, org_id, caller, CLUSTER_USE)
    if shared_cluster_id not in allowed:
        raise ProvisioningRefused(REFUSAL)
    cluster = await db.scalar(
        select(Cluster)
        .where(Cluster.id == shared_cluster_id, Cluster.org_id == org_id)
        .execution_options(populate_existing=True)
    )
    if (
        cluster is None
        or cluster.org_id != org_id
        or not cluster.sharing_enabled
        or cluster.status not in ELIGIBLE_CLUSTER_STATUSES
        or not await _owner_accepts_members(db, cluster)
    ):
        raise ProvisioningRefused(
            "no eligible shared cluster matches the selected identifier for this "
            "organization"
        )
    return ResolvedSharedTarget(
        cluster_id=cluster.id,
        cluster_arn=cluster.eks_cluster_arn,
        endpoint=cluster.endpoint,
        platform_eligible=cluster.platform_eligible,
    )


async def namespace_conflicts(
    db: AsyncSession, cluster_id: uuid.UUID, namespace: str
) -> bool:
    """True if `namespace` is already live on `cluster_id`.

    A pre-check only — the database's partial unique index
    (`uq_cluster_memberships_cluster_namespace_live`) is the actual enforcement
    boundary under concurrent admission, matching the "check, then let the
    database refuse the race" discipline `superplane_bootstrap.registry` already
    uses for the reservation table. This function exists so a predictable
    conflict can be reported as a clean refusal rather than a raw integrity
    error surfacing from a commit.
    """
    result = await db.execute(
        select(ClusterMembership.id).where(
            ClusterMembership.cluster_id == cluster_id,
            ClusterMembership.namespace == namespace,
            ClusterMembership.state.in_(LIVE_MEMBERSHIP_STATES),
        )
    )
    return result.scalar_one_or_none() is not None
