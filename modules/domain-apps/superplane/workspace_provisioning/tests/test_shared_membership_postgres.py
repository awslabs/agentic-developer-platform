"""Real domain reservation consumed before shared workspace provider effects."""
# ruff: noqa: F811 - imported pytest fixtures are parameter names

from uuid import UUID, uuid4

import pytest
from superplane_bootstrap.membership import SharedMembership
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.shared_membership import reserve, verify

from workspace_bootstrap.tests import conftest as identities
from .test_bootstrap_runtime_postgres import bootstrap_harness  # noqa: F401
from .postgres_bridge import requires_harness_postgres

pytestmark = requires_harness_postgres


def test_shared_reservation_replays_without_rebinding_and_preserves_peers(
    bootstrap_harness,
):
    harness = bootstrap_harness
    cluster_id = str(uuid4())
    arn, endpoint = (
        "arn:aws:eks:us-east-1:123456789012:cluster/shared",
        "https://shared.example",
    )
    first = SharedMembership.create(
        org_id=identities.ORG_ID,
        workspace_id=str(uuid4()),
        cluster_id=cluster_id,
        request_id=str(uuid4()),
        cluster_arn=arn,
        endpoint=endpoint,
    )
    second = SharedMembership.create(
        org_id=identities.ORG_ID,
        workspace_id=str(uuid4()),
        cluster_id=cluster_id,
        request_id=str(uuid4()),
        cluster_arn=arn,
        endpoint=endpoint,
    )

    async def run():
        async with harness.connect() as c:
            await c.execute(
                "INSERT INTO clusters(id,org_id,name,status,eks_cluster_arn,endpoint,sharing_enabled) VALUES($1,$2,'shared','Ready',$3,$4,true)",
                UUID(cluster_id),
                UUID(identities.ORG_ID),
                arn,
                endpoint,
            )
            with pytest.raises(
                LifecycleRefused, match="requires the workspace transaction"
            ):
                await reserve(c, first)
            for value in (first, second):
                await c.execute(
                    "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) VALUES($1,$2,$3,'namespace','Provisioning',false)",
                    UUID(value.workspace_id),
                    UUID(value.org_id),
                    value.namespace,
                )
                async with c.transaction():
                    await reserve(c, value)
                async with c.transaction():
                    await reserve(c, value)
                await verify(c, value, states={"reserved"})
            assert await c.fetchval("SELECT count(*) FROM cluster_memberships") == 2
            await c.execute(
                "UPDATE cluster_memberships SET state='removed' WHERE workspace_id=$1",
                UUID(first.workspace_id),
            )
            with pytest.raises(LifecycleRefused, match="withdrawn"):
                await verify(c, first, states={"reserved", "active"})
            with pytest.raises(LifecycleRefused, match="withdrawn"):
                async with c.transaction():
                    await reserve(c, first)
            await verify(c, second, states={"reserved"})
            await c.execute(
                "UPDATE clusters SET sharing_enabled=false WHERE id=$1",
                UUID(cluster_id),
            )
            with pytest.raises(LifecycleRefused, match="withdrawn"):
                await verify(c, second, states={"reserved"})
            await c.execute(
                "UPDATE clusters SET sharing_enabled=true,status='Deleting' WHERE id=$1",
                UUID(cluster_id),
            )
            with pytest.raises(LifecycleRefused, match="withdrawn"):
                await verify(c, second, states={"reserved"})

    harness.run(run())
