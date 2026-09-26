"""The API adapter reserves on the actual workspace insertion transaction."""
# ruff: noqa: F811

import uuid

import pytest
from sqlalchemy import select

from app.adapters.shared_membership import reserve_workspace_membership
from app.models.cluster import Cluster
from app.models.cluster_membership import ClusterMembership
from app.models.organization import Organization
from app.models.workspace import Workspace
from superplane_bootstrap.membership import SharedMembership

from tests.test_onboarding_postgres import database, pytestmark  # noqa: F401
from tests.test_installation_postgres import installation_postgres_url  # noqa: F401


@pytest.mark.parametrize("commit", [True, False])
async def test_reservation_commits_or_rolls_back_with_workspace(database, commit):
    org_id, cluster_id, workspace_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    binding = SharedMembership.create(
        org_id=str(org_id),
        workspace_id=str(workspace_id),
        cluster_id=str(cluster_id),
        request_id=str(uuid.uuid4()),
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
        endpoint="https://shared.example",
    )
    async with database.sessions() as session:
        session.add(Organization(id=org_id, name="shared transaction"))
        await session.flush()
        session.add(
            Cluster(
                id=cluster_id,
                org_id=org_id,
                name="shared",
                status="Ready",
                sharing_enabled=True,
                eks_cluster_arn=binding.cluster_arn,
                endpoint=binding.endpoint,
            )
        )
        await session.commit()
    async with database.sessions() as session:
        session.add(
            Workspace(
                id=workspace_id,
                org_id=org_id,
                name="member",
                status="Provisioning",
                isolation_mode="namespace",
                is_default=False,
            )
        )
        await reserve_workspace_membership(session, binding)
        # Another connection cannot observe a half-committed reservation.
        async with database.sessions() as observer:
            assert await observer.get(Workspace, workspace_id) is None
            assert await observer.scalar(select(ClusterMembership)) is None
        if commit:
            await session.commit()
        else:
            await session.rollback()
    await database.restart()
    async with database.sessions() as session:
        workspace = await session.get(Workspace, workspace_id)
        membership = await session.scalar(select(ClusterMembership))
        if not commit:
            assert workspace is membership is None
        else:
            assert workspace.cluster_id == cluster_id
            assert workspace.namespace_name == binding.namespace
            assert membership.workspace_id == workspace_id
            assert membership.generation == binding.generation
            assert membership.namespace == binding.namespace
            assert membership.namespace_uid is None
            assert membership.state == "reserved"
