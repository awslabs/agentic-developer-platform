"""An active peer outlives its historical creator without permitting deletion."""
# ruff: noqa: F811

from uuid import UUID, uuid4
from dataclasses import replace

import pytest
from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.membership import SharedMembership

from workspace_provisioning import membership_credential_journal as credentials
from workspace_provisioning.member_credentials import CredentialBinding
from workspace_provisioning.runtime_config import LifecycleRefused
from workspace_provisioning.retirement_inventory import (
    load_bootstrap_retirement_inventory,
)
from workspace_provisioning.shared_membership import reserve, verify
from workspace_bootstrap.tests import conftest as identities

from .test_bootstrap_runtime_postgres import bootstrap_harness  # noqa: F401
from .postgres_bridge import requires_harness_postgres

pytestmark = requires_harness_postgres


@pytest.mark.parametrize("owner_status", ["Teardown", "retired", "Deleted"])
def test_active_peer_renews_while_new_members_and_cluster_deletion_stay_fenced(
    bootstrap_harness, owner_status
):
    harness = bootstrap_harness
    cluster_id, owner_id = uuid4(), uuid4()
    member = SharedMembership.create(
        org_id=identities.ORG_ID,
        workspace_id=str(uuid4()),
        cluster_id=str(cluster_id),
        request_id=str(uuid4()),
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
        endpoint="https://shared.example.test",
    )
    pending = SharedMembership.create(
        org_id=member.org_id,
        workspace_id=str(uuid4()),
        cluster_id=member.cluster_id,
        request_id=str(uuid4()),
        cluster_arn=member.cluster_arn,
        endpoint=member.endpoint,
    )

    async def run():
        async with harness.connect() as c:
            for workspace_id in (
                owner_id,
                UUID(member.workspace_id),
                UUID(pending.workspace_id),
            ):
                await c.execute(
                    "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) VALUES($1,$2,$3,'namespace','Provisioning',false)",
                    workspace_id,
                    UUID(member.org_id),
                    str(workspace_id),
                )
            await c.execute(
                "INSERT INTO clusters(id,org_id,workspace_id,name,status,sharing_enabled,eks_cluster_arn,endpoint) VALUES($1,$2,$3,'shared','Ready',true,$4,$5)",
                cluster_id,
                UUID(member.org_id),
                owner_id,
                member.cluster_arn,
                member.endpoint,
            )
            await c.execute(
                "UPDATE workspaces SET cluster_id=$1,status='Ready' WHERE id=$2",
                cluster_id,
                owner_id,
            )
            async with c.transaction():
                await reserve(c, member)
                await reserve(c, pending)
            await c.execute(
                "UPDATE cluster_memberships SET state='active',namespace_uid='peer-namespace' WHERE workspace_id=$1",
                UUID(member.workspace_id),
            )
            await c.execute(
                "UPDATE workspaces SET status='Ready' WHERE id=$1",
                UUID(member.workspace_id),
            )
            await c.execute(
                "UPDATE workspaces SET status=$2 WHERE id=$1", owner_id, owner_status
            )
            observed = await verify(c, member, states={"active"})
            assert observed["state"] == "active"
            assert observed["owner_status"] == owner_status
            async with c.transaction():
                result = await credentials.reserve(
                    c, CredentialBinding(member, "peer-namespace", 1, "reader")
                )
                assert result["state"] == "reserved"
            with pytest.raises(LifecycleRefused, match="withdrawn"):
                await verify(c, pending, states={"reserved"})
            newcomer = SharedMembership.create(
                org_id=member.org_id,
                workspace_id=str(uuid4()),
                cluster_id=member.cluster_id,
                request_id=str(uuid4()),
                cluster_arn=member.cluster_arn,
                endpoint=member.endpoint,
            )
            await c.execute(
                "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) VALUES($1,$2,'newcomer','namespace','Provisioning',false)",
                UUID(newcomer.workspace_id),
                UUID(member.org_id),
            )
            with pytest.raises(LifecycleRefused, match="no longer eligible"):
                async with c.transaction():
                    await reserve(c, newcomer)
            # Cluster eligibility is still independent of historical ownership.
            await c.execute(
                "UPDATE clusters SET sharing_enabled=false WHERE id=$1", cluster_id
            )
            with pytest.raises(LifecycleRefused, match="withdrawn"):
                await verify(c, member, states={"active"})
            await c.execute(
                "UPDATE clusters SET sharing_enabled=true,status='Deleting' WHERE id=$1",
                cluster_id,
            )
            with pytest.raises(LifecycleRefused, match="withdrawn"):
                await verify(c, member, states={"active"})
            await c.execute(
                "UPDATE clusters SET status='Ready' WHERE id=$1", cluster_id
            )
            await c.execute(
                "UPDATE workspaces SET status='Teardown' WHERE id=$1",
                UUID(member.workspace_id),
            )
            with pytest.raises(LifecycleRefused, match="withdrawn"):
                await verify(c, member, states={"active"})
            await c.execute(
                "UPDATE workspaces SET status='Ready',shared_cluster_id=NULL WHERE id=$1",
                UUID(member.workspace_id),
            )
            with pytest.raises(LifecycleRefused, match="withdrawn"):
                await verify(c, member, states={"active"})

    harness.run(run())


def test_real_dedicated_retirement_still_refuses_active_peer_after_owner_retires(
    runtime,
):
    assert runtime.run().ready
    owner, peer, member_id = runtime.target.workspace_id, str(uuid4()), str(uuid4())
    db = runtime.store.store
    with db.transaction():
        db.execute(
            "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) "
            "SELECT CAST(:peer AS uuid),org_id,'active-peer','namespace','Ready',false FROM workspaces WHERE id=CAST(:owner AS uuid)",
            {"peer": peer, "owner": owner},
        )
        db.execute(
            "INSERT INTO cluster_memberships(id,org_id,workspace_id,cluster_id,generation,namespace,state) "
            "SELECT CAST(:member AS uuid),org_id,CAST(:peer AS uuid),id,:generation,'active-peer','active' "
            "FROM clusters WHERE workspace_id=CAST(:owner AS uuid)",
            {"member": member_id, "peer": peer, "owner": owner, "generation": "c" * 64},
        )
        db.execute("UPDATE clusters SET sharing_enabled=false", {})
        db.execute(
            "UPDATE workspaces SET status='Teardown' WHERE id=CAST(:owner AS uuid)",
            {"owner": owner},
        )
    with pytest.raises(BootstrapRefused, match="membership-scoped retirement"):
        load_bootstrap_retirement_inventory(
            registration_store=runtime.store,
            binding=replace(runtime.binding, action="teardown"),
        )
    # The refused inventory read did not revoke the peer or reinterpret the
    # historical creator's Terraform ownership as cluster deletion authority.
    assert (
        db.execute(
            "SELECT state FROM cluster_memberships WHERE id=CAST(:member AS uuid)",
            {"member": member_id},
        )[0]["state"]
        == "active"
    )
