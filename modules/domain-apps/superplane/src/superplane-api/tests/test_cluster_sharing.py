"""Shared-cluster eligibility resolution — issue #6048.

Exercises `app/services/cluster_sharing.py` directly against the offline SQLite
double, following the existing `test_deployment_quota_and_isolation.py` pattern
of seeding rows through the ORM rather than the HTTP layer, because the
behaviour under test is server-side resolution logic rather than the route.
"""

from __future__ import annotations

import uuid

import pytest
from app.models.cluster import Cluster
from app.models.cluster_membership import ClusterMembership
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.services.cluster_sharing import (
    list_eligible_clusters,
    namespace_conflicts,
    resolve_shared_target,
)
from app.services.provisioning import ProvisioningRefused

from tests.conftest import async_session_test

ORG_A = uuid.UUID("aaaaaaaa-0000-0000-0000-00000000000a")
ORG_B = uuid.UUID("bbbbbbbb-0000-0000-0000-00000000000b")
SHARED_CLUSTER = uuid.UUID("cccccccc-0000-0000-0000-00000000000c")
DEDICATED_CLUSTER = uuid.UUID("dddddddd-0000-0000-0000-00000000000d")
NOT_READY_CLUSTER = uuid.UUID("eeeeeeee-0000-0000-0000-00000000000e")
GEN = "a" * 64


async def _seed():
    async with async_session_test() as session:
        session.add_all(
            [
                Organization(id=ORG_A, name="org-a"),
                Organization(id=ORG_B, name="org-b"),
            ]
        )
        await session.flush()
        session.add_all(
            [
                # Explicitly shareable, healthy, in org A.
                Cluster(
                    id=SHARED_CLUSTER,
                    org_id=ORG_A,
                    name="shared-a",
                    status="Ready",
                    sharing_enabled=True,
                    eks_cluster_arn="arn:aws:eks:us-east-1:000000000000:cluster/shared-a",
                    endpoint="https://shared-a.example",
                ),
                # A cluster that exists, is healthy, but never opted into sharing.
                Cluster(
                    id=DEDICATED_CLUSTER,
                    org_id=ORG_A,
                    name="dedicated-a",
                    status="Ready",
                    sharing_enabled=False,
                ),
                # Explicitly shareable but not yet Ready.
                Cluster(
                    id=NOT_READY_CLUSTER,
                    org_id=ORG_A,
                    name="pending-a",
                    status="Pending",
                    sharing_enabled=True,
                ),
            ]
        )
        await session.commit()


class TestListEligibleClusters:
    @pytest.mark.asyncio
    async def test_only_explicitly_shared_ready_clusters_in_caller_org(self):
        await _seed()
        async with async_session_test() as session:
            eligible = await list_eligible_clusters(session, ORG_A)
        assert [c.id for c in eligible] == [SHARED_CLUSTER]

    @pytest.mark.asyncio
    async def test_never_lists_another_organizations_clusters(self):
        await _seed()
        async with async_session_test() as session:
            eligible = await list_eligible_clusters(session, ORG_B)
        assert eligible == []

    @pytest.mark.asyncio
    async def test_member_count_reflects_live_memberships_only(self):
        await _seed()
        workspace_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=workspace_id,
                    org_id=ORG_A,
                    name="member-ws",
                    isolation_mode="dedicated",
                    status="active",
                )
            )
            other_workspace_id = uuid.uuid4()
            session.add(
                Workspace(
                    id=other_workspace_id,
                    org_id=ORG_A,
                    name="removed-ws",
                    isolation_mode="dedicated",
                    status="active",
                )
            )
            await session.flush()
            session.add_all(
                [
                    ClusterMembership(
                        id=uuid.uuid4(),
                        org_id=ORG_A,
                        workspace_id=workspace_id,
                        cluster_id=SHARED_CLUSTER,
                        generation=GEN,
                        namespace="ns-live",
                        state="active",
                    ),
                    ClusterMembership(
                        id=uuid.uuid4(),
                        org_id=ORG_A,
                        workspace_id=other_workspace_id,
                        cluster_id=SHARED_CLUSTER,
                        generation=GEN,
                        namespace="ns-removed",
                        state="removed",
                    ),
                ]
            )
            await session.commit()
        async with async_session_test() as session:
            eligible = await list_eligible_clusters(session, ORG_A)
        assert eligible[0].member_count == 1


class TestResolveSharedTarget:
    @pytest.mark.asyncio
    async def test_resolves_a_shared_eligible_cluster(self):
        await _seed()
        async with async_session_test() as session:
            resolved = await resolve_shared_target(session, ORG_A, SHARED_CLUSTER)
        assert resolved.cluster_id == SHARED_CLUSTER
        assert resolved.cluster_arn == "arn:aws:eks:us-east-1:000000000000:cluster/shared-a"

    @pytest.mark.asyncio
    async def test_refuses_a_cluster_belonging_to_another_organization(self):
        """Cross-organization selection is refused even under the correct AWS account.

        This is the acceptance criterion DESIGN.md states explicitly: "Reject
        cross-organization selection even in the same AWS account."
        """
        await _seed()
        async with async_session_test() as session:
            with pytest.raises(ProvisioningRefused):
                await resolve_shared_target(session, ORG_B, SHARED_CLUSTER)

    @pytest.mark.asyncio
    async def test_refuses_a_cluster_that_never_opted_into_sharing(self):
        """Adopting/owning a cluster is not the same as sharing it (DESIGN.md)."""
        await _seed()
        async with async_session_test() as session:
            with pytest.raises(ProvisioningRefused):
                await resolve_shared_target(session, ORG_A, DEDICATED_CLUSTER)

    @pytest.mark.asyncio
    async def test_refuses_a_cluster_that_is_not_ready(self):
        await _seed()
        async with async_session_test() as session:
            with pytest.raises(ProvisioningRefused):
                await resolve_shared_target(session, ORG_A, NOT_READY_CLUSTER)

    @pytest.mark.asyncio
    async def test_refuses_an_unknown_cluster_id(self):
        await _seed()
        async with async_session_test() as session:
            with pytest.raises(ProvisioningRefused):
                await resolve_shared_target(session, ORG_A, uuid.uuid4())

    @pytest.mark.asyncio
    async def test_cross_org_and_unknown_refusals_are_indistinguishable(self):
        """Same refusal text for both cases — neither confirms the other org's cluster exists."""
        await _seed()
        async with async_session_test() as session:
            with pytest.raises(ProvisioningRefused) as absent:
                await resolve_shared_target(session, ORG_A, uuid.uuid4())
        async with async_session_test() as session:
            with pytest.raises(ProvisioningRefused) as foreign:
                await resolve_shared_target(session, ORG_B, SHARED_CLUSTER)
        assert str(absent.value) == str(foreign.value)


class TestNamespaceConflicts:
    @pytest.mark.asyncio
    async def test_no_conflict_on_an_empty_cluster(self):
        await _seed()
        async with async_session_test() as session:
            assert not await namespace_conflicts(session, SHARED_CLUSTER, "ns-fresh")

    @pytest.mark.asyncio
    async def test_conflict_with_a_live_member_namespace(self):
        await _seed()
        workspace_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=workspace_id,
                    org_id=ORG_A,
                    name="taken-ws",
                    isolation_mode="dedicated",
                    status="active",
                )
            )
            await session.flush()
            session.add(
                ClusterMembership(
                    id=uuid.uuid4(),
                    org_id=ORG_A,
                    workspace_id=workspace_id,
                    cluster_id=SHARED_CLUSTER,
                    generation=GEN,
                    namespace="ns-taken",
                    state="active",
                )
            )
            await session.commit()
        async with async_session_test() as session:
            assert await namespace_conflicts(session, SHARED_CLUSTER, "ns-taken")

    @pytest.mark.asyncio
    async def test_no_conflict_with_a_removed_members_namespace(self):
        """A removed membership's namespace name is not reserved forever."""
        await _seed()
        workspace_id = uuid.uuid4()
        async with async_session_test() as session:
            session.add(
                Workspace(
                    id=workspace_id,
                    org_id=ORG_A,
                    name="freed-ws",
                    isolation_mode="dedicated",
                    status="active",
                )
            )
            await session.flush()
            session.add(
                ClusterMembership(
                    id=uuid.uuid4(),
                    org_id=ORG_A,
                    workspace_id=workspace_id,
                    cluster_id=SHARED_CLUSTER,
                    generation=GEN,
                    namespace="ns-freed",
                    state="removed",
                )
            )
            await session.commit()
        async with async_session_test() as session:
            assert not await namespace_conflicts(session, SHARED_CLUSTER, "ns-freed")
