"""Pure cleanup graph compilation over real original network journal writes."""

# ruff: noqa: F811
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
import asyncpg

from harness_jobs.identity import OperationRefused
from superplane_executor.cleanup_recipes import network_recipes
from superplane_executor.network_inventory import rows
from test_network import network as network, pytestmark as pytestmark
from network_support import REMOTE
from harness_jobs import OperationStore
from harness_jobs.identity import OperationRequest
from harness_jobs.leases import acquire, lock_lease
from tests.conftest import admit_paid
from tests.test_admission_postgres import principal


@pytest.mark.parametrize("interrupt", [False, True])
async def test_already_absent_network_release_publishes_both_facts_atomically(
    network, pool, interrupt
):
    from superplane_executor.cleanup_network import observe as observe_release

    runtime, aws = network
    await runtime.establish(REMOTE)
    recipe = next(
        r
        for r in network_recipes(
            runtime.plan, await rows(runtime.provider, runtime.operation)
        )
        if r["reference"]["kind"] == "security-rule"
    )
    aws.rules.pop(recipe["reference"]["id"])
    if interrupt:
        async with pool.acquire() as c:
            await c.execute("""CREATE FUNCTION interrupt_member_release() RETURNS trigger AS $$
            BEGIN RAISE EXCEPTION 'interrupted member release'; END; $$ LANGUAGE plpgsql;
            CREATE TRIGGER interrupt_member_release BEFORE UPDATE ON controller_network_members
            FOR EACH ROW EXECUTE FUNCTION interrupt_member_release();""")

    async def already_absent(_):
        return None

    async def never_delete(_):
        raise AssertionError("already absent target must not submit a deletion")

    calls = list(aws.calls)
    if interrupt:
        with pytest.raises(asyncpg.RaiseError, match="interrupted member release"):
            await runtime.journal.release(
                recipe["key"],
                observe=already_absent,
                delete=never_delete,
                expected=recipe,
            )
    else:
        await runtime.journal.release(
            recipe["key"], observe=already_absent, delete=never_delete, expected=recipe
        )
    async with pool.acquire() as c:
        assert await c.fetchval(
            "SELECT state FROM controller_network_resources WHERE resource_key=$1",
            recipe["key"],
        ) == ("delete_intended" if interrupt else "absent")
        assert (
            await c.fetchval(
                "SELECT released_at FROM controller_network_members WHERE resource_key=$1 AND allocation_id=$2",
                recipe["key"],
                runtime.journal.allocation,
            )
            is None
        ) is interrupt
        assert not await c.fetchval(
            "SELECT 1 FROM controller_network_effects WHERE resource_key=$1 AND action='delete'",
            recipe["key"],
        )
    assert aws.calls == calls
    if interrupt:
        async with pool.acquire() as c:
            await c.execute(
                "DROP TRIGGER interrupt_member_release ON controller_network_members"
            )
    actor = principal(
        org=runtime.operation.grant.lease.org_id,
        workspace=runtime.operation.grant.lease.workspace_id,
    )
    request = OperationRequest(
        action="teardown",
        idempotency_key=str(uuid4()),
        parameters={
            "allocation_id": runtime.journal.allocation,
            "controller_source_operation_id": runtime.operation.grant.lease.operation_id,
            "execution_steps": json.dumps(
                [
                    {
                        "step_id": "network:0",
                        "provider": "aws",
                        "operation_kind": "delete_cluster",
                        "target": "capacity",
                    }
                ]
            ),
        },
    )
    async with pool.acquire() as c:
        admitted = await admit_paid(OperationStore(), c, actor, request)
        lease = await acquire(
            c,
            operation_id=admitted.record.operation_id,
            holder=actor.subject,
            attempt_id=str(uuid4()),
        )
    operation = SimpleNamespace(grant=SimpleNamespace(lease=lease), request=request)
    runtime.provider.execution_pool = pool

    async def session_for(*_):
        return aws, None

    async def authorize():
        async with pool.acquire() as c, c.transaction():
            assert await lock_lease(c, lease)

    runtime.provider.session_for = session_for
    await register_network_authority(pool, runtime, lease)
    assert await observe_release(
        runtime.provider, operation, runtime.target, runtime.plan, recipe, authorize
    )
    async with pool.acquire() as c:
        assert await c.fetchval(
            "SELECT released_at IS NOT NULL FROM controller_network_members WHERE resource_key=$1 AND allocation_id=$2",
            recipe["key"],
            runtime.journal.allocation,
        )
        assert not await c.fetchval(
            "SELECT 1 FROM controller_network_effects WHERE resource_key=$1 AND action='delete'",
            recipe["key"],
        )
    assert all(
        name.startswith(("describe_", "get_", "search_"))
        for _, name, _ in aws.calls[len(calls) :]
    )


async def test_recipe_compilation_preserves_every_original_key_without_sdk_calls(
    network,
):
    runtime, aws = network
    await runtime.establish(REMOTE)
    original = await rows(runtime.provider, runtime.operation)
    calls = list(aws.calls)
    recipes = network_recipes(runtime.plan, original)
    assert [r["key"] for r in recipes] == list(
        dict.fromkeys(k for k, _, _ in reversed(runtime.recipes))
    )
    assert {r["key"] for r in recipes} == {r["resource_key"] for r in original}
    assert aws.calls == calls


@pytest.mark.parametrize("changed", ["missing", "descriptor", "reference", "extra"])
async def test_partial_or_changed_native_recipe_is_not_a_cleanup_graph(
    network, changed
):
    runtime, aws = network
    await runtime.establish(REMOTE)
    original = [dict(r) for r in await rows(runtime.provider, runtime.operation)]
    if changed == "missing":
        original.pop()
    elif changed == "descriptor":
        original[0]["descriptor"] = json.dumps({"foreign": True})
    elif changed == "reference":
        original[0]["provider_reference"] = None
    else:
        original.append({**original[0], "resource_key": "foreign"})
    calls = list(aws.calls)
    with pytest.raises(OperationRefused):
        network_recipes(runtime.plan, original)
    assert aws.calls == calls


@pytest.mark.parametrize(
    "sharing,change",
    [
        ("owned", c)
        for c in [None, "revoked", "moved", "generation", "revoked-during-read"]
    ]
    + [("peer", None), ("adopted", None)],
)
async def test_completed_network_stage_observation_preserves_peers_after_lost_reply(
    network, pool, sharing, change, monkeypatch
):
    from dataclasses import replace

    from harness_jobs.leases import fence_expired_lease
    from network_support import make_network
    from superplane_executor import cleanup_network

    runtime, aws = network
    await runtime.establish(REMOTE)
    recipe = next(
        r
        for r in network_recipes(
            runtime.plan, await rows(runtime.provider, runtime.operation)
        )
        if r["reference"]["kind"] == "security-rule"
    )
    peer = None
    if sharing == "peer":
        peer, _ = await make_network(
            pool,
            aws,
            org=runtime.journal.lease.org_id,
            workspace=runtime.journal.lease.workspace_id,
            cluster=runtime.target["cluster_id"],
        )
        await peer.establish(REMOTE)
    elif sharing == "adopted":
        # Simulate the retained source proof for an originally adopted native rule.
        async with pool.acquire() as c:
            await c.execute(
                "UPDATE controller_network_resources SET owned=false WHERE resource_key=$1",
                recipe["key"],
            )
        recipe["owned"] = False
    actor = principal(
        org=runtime.operation.grant.lease.org_id,
        workspace=runtime.operation.grant.lease.workspace_id,
    )
    request = OperationRequest(
        action="teardown",
        idempotency_key=str(uuid4()),
        parameters={
            "allocation_id": runtime.journal.allocation,
            "controller_source_operation_id": runtime.operation.grant.lease.operation_id,
        },
    )
    async with pool.acquire() as c:
        admitted = await admit_paid(OperationStore(), c, actor, request)
        lease = await acquire(
            c,
            operation_id=admitted.record.operation_id,
            holder=actor.subject,
            attempt_id=str(uuid4()),
        )
    operation = SimpleNamespace(grant=SimpleNamespace(lease=lease), request=request)
    runtime.provider.execution_pool = pool

    async def session_for(*_):
        return aws, None

    async def authorize():
        async with pool.acquire() as c, c.transaction():
            assert await lock_lease(c, operation.grant.lease)

    runtime.provider.session_for = session_for
    await register_network_authority(pool, runtime, lease)
    start = len(aws.calls)
    if sharing == "owned":
        aws.lost = (
            "revoke_security_group_egress"
            if recipe["reference"]["egress"]
            else "revoke_security_group_ingress"
        )
        with pytest.raises(TimeoutError):
            await cleanup_network.execute(
                runtime.provider,
                operation,
                runtime.target,
                runtime.plan,
                recipe,
                authorize,
            )
    else:
        await cleanup_network.execute(
            runtime.provider, operation, runtime.target, runtime.plan, recipe, authorize
        )
    # The worker is now lost before shared acknowledgement; only a real new
    # recovery claim may publish the observation for the same original allocation.
    async with pool.acquire() as c:
        await c.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1",
            lease.operation_id,
        )
        takeover = await fence_expired_lease(
            c,
            operation_id=lease.operation_id,
            recovery_principal=replace(
                actor, permissions=frozenset({"workspace:recover"})
            ),
        )
    assert takeover is not None
    operation.grant.lease = takeover.lease

    async def facts():
        async with pool.acquire() as c:
            return (
                await c.fetchrow(
                    "SELECT * FROM controller_network_resources WHERE resource_key=$1",
                    recipe["key"],
                ),
                await c.fetchrow(
                    "SELECT * FROM controller_network_members WHERE resource_key=$1 AND allocation_id=$2",
                    recipe["key"],
                    runtime.journal.allocation,
                ),
                await c.fetchrow(
                    "SELECT * FROM controller_network_effects WHERE resource_key=$1 AND action='delete'",
                    recipe["key"],
                ),
            )

    if change is not None:
        before = await facts()
        if change == "revoked-during-read":
            observe_native = cleanup_network.observe_native

            async def revoke_after_native_read(*args, **kwargs):
                observed = await observe_native(*args, **kwargs)
                async with pool.acquire() as c:
                    await c.execute("UPDATE cluster_memberships SET state='revoked'")
                return observed

            monkeypatch.setattr(
                cleanup_network, "observe_native", revoke_after_native_read
            )
        else:
            async with pool.acquire() as c:
                await c.execute(
                    {
                        "revoked": "UPDATE cluster_memberships SET state='revoked'",
                        "moved": "UPDATE workspaces SET cluster_id='00000000-0000-0000-0000-000000000000'",
                        "generation": "UPDATE cluster_memberships SET generation=repeat('b',64)",
                    }[change]
                )
        calls = list(aws.calls)
        with pytest.raises(OperationRefused):
            await cleanup_network.observe(
                runtime.provider,
                operation,
                runtime.target,
                runtime.plan,
                recipe,
                authorize,
            )
        assert await facts() == before
        assert all(
            name.startswith(("describe_", "get_", "search_"))
            for _, name, _ in aws.calls[len(calls) :]
        )
        return
    after_effect = len(aws.calls)
    assert await cleanup_network.observe(
        runtime.provider, operation, runtime.target, runtime.plan, recipe, authorize
    )
    assert await cleanup_network.observe(
        runtime.provider, operation, runtime.target, runtime.plan, recipe, authorize
    )
    mutations = [
        name
        for _, name, _ in aws.calls[start:]
        if not name.startswith(("describe_", "get_", "search_"))
    ]
    assert len(mutations) == int(sharing == "owned")
    assert all(
        name.startswith(("describe_", "get_", "search_"))
        for _, name, _ in aws.calls[after_effect:]
    )
    assert (recipe["reference"]["id"] in aws.rules) is (sharing != "owned")
    async with pool.acquire() as c:
        assert await c.fetchval(
            "SELECT released_at IS NOT NULL FROM controller_network_members WHERE resource_key=$1 AND allocation_id=$2",
            recipe["key"],
            runtime.journal.allocation,
        )
        if peer is not None:
            assert (
                await c.fetchval(
                    "SELECT released_at FROM controller_network_members WHERE resource_key=$1 AND allocation_id=$2",
                    recipe["key"],
                    peer.journal.allocation,
                )
                is None
            )
        assert await c.fetchval(
            "SELECT count(*) FROM controller_network_effects WHERE resource_key=$1 AND action='delete'",
            recipe["key"],
        ) == int(sharing == "owned")


async def register_network_authority(pool, runtime, lease):
    """Use actual current registration checks; never bypass Network.authority."""
    async with pool.acquire() as c:
        await c.execute("""CREATE TABLE clusters(id uuid,org_id uuid,workspace_id uuid,eks_cluster_arn text);
            CREATE TABLE workspaces(id uuid,org_id uuid,cluster_id uuid,namespace_name text);
            CREATE TABLE cluster_memberships(workspace_id uuid,org_id uuid,cluster_id uuid,generation text,namespace text,state text);
            CREATE TABLE workspace_bootstrap_reservations(workspace_id text,state text,identity_json text,attempt_token text);
            CREATE TABLE workspace_bootstrap_authority(workspace_id text,org_id text,cluster_arn text,generation text,claim text,progress_json text,revoked boolean);""")
        await c.execute(
            "INSERT INTO clusters VALUES($1::text::uuid,$2::text::uuid,$3::text::uuid,$4)",
            runtime.target["cluster_id"],
            lease.org_id,
            lease.workspace_id,
            runtime.plan.data["cluster_arn"],
        )
        await c.execute(
            "INSERT INTO workspaces VALUES($1::text::uuid,$2::text::uuid,$3::text::uuid,'original-workspace')",
            lease.workspace_id,
            lease.org_id,
            runtime.target["cluster_id"],
        )
        await c.execute(
            "INSERT INTO cluster_memberships VALUES($1::text::uuid,$2::text::uuid,$3::text::uuid,$4,'original-workspace','active')",
            lease.workspace_id,
            lease.org_id,
            runtime.target["cluster_id"],
            runtime.plan.network["cluster"]["membership_generation"],
        )
