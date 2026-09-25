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


@pytest.mark.parametrize("drift", [None, "generation", "operation_id", "missing"])
def test_canonical_publication_retains_the_pre_namespace_reservation(
    bootstrap_harness, drift
):
    import asyncio

    from superplane_bootstrap.errors import BootstrapRefused
    from superplane_bootstrap.membership import registration_fields
    from superplane_bootstrap.registration import WorkspaceTarget
    from superplane_bootstrap.registry import SqlRegistrationStore, _target_mapping
    from workspace_provisioning.process import AsyncBridgeStore

    harness = bootstrap_harness
    binding = SharedMembership.create(
        org_id=identities.ORG_ID,
        workspace_id=str(uuid4()),
        cluster_id=str(uuid4()),
        request_id=str(uuid4()),
        cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/shared",
        endpoint="https://shared.example",
    )
    target = WorkspaceTarget(
        workspace_id=binding.workspace_id,
        org_id=binding.org_id,
        account_id="123456789012",
        region="us-east-1",
        cluster_name="shared",
        cluster_arn=binding.cluster_arn,
        endpoint=binding.endpoint,
        namespace=binding.namespace,
        namespace_uid="discovered-namespace-uid",
        cluster_ownership="adopted",
        credential_reference_id="scoped-workspace-reference",
        contract_version="v1",
        **registration_fields(binding),
    )

    async def run():
        async with harness.connect() as connection:
            await connection.execute(
                "INSERT INTO clusters(id,org_id,name,status,eks_cluster_arn,endpoint,sharing_enabled) "
                "VALUES($1,$2,'shared','Ready',$3,$4,true)",
                UUID(binding.cluster_id),
                UUID(binding.org_id),
                binding.cluster_arn,
                binding.endpoint,
            )
            await connection.execute(
                "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) "
                "VALUES($1,$2,'member','namespace','Provisioning',false)",
                UUID(binding.workspace_id),
                UUID(binding.org_id),
            )
            async with connection.transaction():
                await reserve(connection, binding)
            assert (
                await connection.fetchval(
                    "SELECT namespace_uid FROM cluster_memberships"
                )
                is None
            )
            if drift == "generation":
                await connection.execute(
                    "UPDATE cluster_memberships SET generation=$1", "b" * 64
                )
            elif drift == "operation_id":
                await connection.execute(
                    "UPDATE cluster_memberships SET operation_id=$1", uuid4()
                )
            elif drift == "missing":
                await connection.execute("DELETE FROM cluster_memberships")

        registry = SqlRegistrationStore(
            AsyncBridgeStore(harness.connect, asyncio.get_running_loop())
        )
        claim = await asyncio.to_thread(
            registry.reserve, binding.workspace_id, _target_mapping(target)
        )
        if drift:
            with pytest.raises(
                BootstrapRefused, match="reservation is missing or changed"
            ):
                await asyncio.to_thread(
                    registry.finalize, target, claim["attempt_token"]
                )
            assert await asyncio.to_thread(registry.read, binding.workspace_id) is None
            return
        await asyncio.to_thread(registry.finalize, target, claim["attempt_token"])
        registered = await asyncio.to_thread(registry.read, binding.workspace_id)
        assert registered.membership_generation == binding.generation
        async with harness.connect() as connection:
            await verify(connection, binding, states={"active"})
            member = await connection.fetchrow(
                "SELECT generation,namespace_uid,operation_id FROM cluster_memberships"
            )
            assert member["generation"] == binding.generation
            assert member["namespace_uid"] == target.namespace_uid
            assert str(member["operation_id"]) == binding.request_id
        replay = await asyncio.to_thread(
            registry.reserve, binding.workspace_id, _target_mapping(target)
        )
        assert replay["replayed"] is True
        await _assert_shared_result_anchor(
            harness, binding, target, claim["attempt_token"]
        )

    harness.run(run())


async def _assert_shared_result_anchor(harness, binding, target, attempt_token):
    """Authority rows are fixture evidence, not a claim of live shared bootstrap."""
    import hashlib
    import json
    from types import SimpleNamespace

    from superplane_bootstrap.registry import _target_mapping
    from superplane_bootstrap.state import claim_fingerprint
    from workspace_provisioning.bootstrap_result import read_bootstrap_anchor

    operation_id = str(uuid4())
    claim = claim_fingerprint(attempt_token)
    generation = hashlib.sha256(("fixture-authority:" + claim).encode()).hexdigest()
    progress = json.dumps(
        {
            "phase": "revoked",
            "complete": True,
            "retain_workspace": True,
            "component_inventory_complete": True,
        }
    )
    peer_id = uuid4()
    metadata = json.dumps({"workspace_bootstrap": {"peer": "original-registration"}})
    async with harness.connect() as connection:
        await connection.execute(
            "INSERT INTO workspaces(id,org_id,name,isolation_mode,status,is_default) "
            "VALUES($1,$2,'original-owner','namespace','active',false)",
            peer_id,
            UUID(binding.org_id),
        )
        await connection.execute(
            "UPDATE clusters SET workspace_id=$1,actual_state_json=$2::jsonb WHERE id=$3",
            peer_id,
            metadata,
            UUID(binding.cluster_id),
        )
        await connection.execute(
            "INSERT INTO workspace_bootstrap_authority(workspace_id,generation,operation_id,org_id,cluster_arn,claim,plan_json,progress_json,revoked) "
            "VALUES($1,$2,$3,$4,$5,$6,'{}',$7,true)",
            binding.workspace_id,
            generation,
            operation_id,
            binding.org_id,
            binding.cluster_arn,
            claim,
            progress,
        )
    arguments = dict(
        context=SimpleNamespace(domain_connect=harness.connect),
        operation_id=operation_id,
        org_id=binding.org_id,
        workspace_id=binding.workspace_id,
        registration=_target_mapping(target),
        claim=claim,
    )
    anchored = await read_bootstrap_anchor(**arguments)
    assert anchored["registration"]["membership_generation"] == binding.generation
    assert anchored["current_generation"] == generation
    with pytest.raises(LifecycleRefused, match="unique original authority"):
        await read_bootstrap_anchor(**{**arguments, "claim": "b" * 64})
    async with harness.connect() as connection:
        await connection.execute(
            "UPDATE cluster_memberships SET namespace_uid='replaced-namespace'"
        )
        with pytest.raises(LifecycleRefused, match="membership changed"):
            await read_bootstrap_anchor(**arguments)
        await connection.execute(
            "UPDATE cluster_memberships SET namespace_uid=$1", target.namespace_uid
        )
        await connection.execute(
            "UPDATE workspace_bootstrap_authority SET revoked=false"
        )
        with pytest.raises(LifecycleRefused, match="recovery is still outstanding"):
            await read_bootstrap_anchor(**arguments)
        observed = await connection.fetchrow(
            "SELECT workspace_id,actual_state_json FROM clusters WHERE id=$1",
            UUID(binding.cluster_id),
        )
        assert observed["workspace_id"] == peer_id
        assert json.loads(observed["actual_state_json"]) == json.loads(metadata)
