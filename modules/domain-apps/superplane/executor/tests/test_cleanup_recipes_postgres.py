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
    assert await observe_release(
        runtime.provider, operation, runtime.plan, recipe, authorize
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
