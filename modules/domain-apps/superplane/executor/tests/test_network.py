"""Database-backed network effects with native AWS request contract validation."""

import pytest
from harness_jobs.identity import OperationRefused
from network_support import HOME, REMOTE, make_network, schema

from tests.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.fixture
async def network(pool):
    await schema(pool)
    return await make_network(pool)


async def test_original_network_is_reused_and_fully_removed(network, pool):
    runtime, aws = network
    await runtime.establish(REMOTE)
    creates = len(
        [
            c
            for c in aws.calls
            if c[1].startswith(("create_", "authorize_", "associate_"))
        ]
    )
    await runtime.establish(REMOTE)
    assert (
        len(
            [
                c
                for c in aws.calls
                if c[1].startswith(("create_", "authorize_", "associate_"))
            ]
        )
        == creates
    )
    assert any(
        c[0] == REMOTE and c[1] == "accept_transit_gateway_peering_attachment"
        for c in aws.calls
    )
    assert aws.attachments and aws.peerings and aws.routes and aws.rules
    async with pool.acquire() as c:
        assert (
            await c.fetchval(
                "SELECT count(*) FROM controller_network_effects WHERE confirmed_at IS NULL"
            )
            == 0
        )
    await runtime.cleanup()
    assert (
        not aws.attachments
        and not aws.peerings
        and not aws.routes
        and not aws.rules
        and not aws.associations
    )
    await runtime.cleanup()


async def test_two_allocations_preserve_shared_network_until_last_release(
    network, pool
):
    first, aws = network
    await first.establish(REMOTE)
    second, _ = await make_network(
        pool,
        aws,
        org=first.journal.lease.org_id,
        workspace=first.journal.lease.workspace_id,
        cluster=first.target["cluster_id"],
    )
    await second.establish(REMOTE)
    await first.cleanup()
    assert aws.peerings and aws.routes
    await first.cleanup()  # a stale former member cannot retire the survivor
    assert aws.peerings
    await second.cleanup()
    assert not aws.peerings and not aws.routes


async def test_adopted_route_survives_cleanup(network):
    runtime, aws = network
    # Exact VPC route, explicitly covered by the reviewed network policy, existed
    # before this operation. It may be used but must never become owned.
    key = (HOME, "rtb-11111111", "10.2.0.0/16")
    aws.routes[key] = {
        "DestinationCidrBlock": key[2],
        "State": "active",
        "TransitGatewayId": "tgw-11111111",
    }
    await runtime.establish(REMOTE)
    await runtime.cleanup()
    assert key in aws.routes


async def test_wrong_target_route_is_refused_and_never_deleted(network):
    runtime, aws = network
    await runtime.establish(REMOTE)
    key = next(k for k in aws.routes if k[1].startswith("tgw-rtb"))
    aws.routes[key]["TransitGatewayAttachments"] = [
        {"TransitGatewayAttachmentId": "tgw-attach-ffffffff"}
    ]
    with pytest.raises(OperationRefused, match="target differs"):
        await runtime.establish(REMOTE)
    with pytest.raises(OperationRefused, match="target differs"):
        await runtime.cleanup()
    assert key in aws.routes


async def test_lost_successful_create_is_observed_without_repeating_it(network, pool):
    runtime, aws = network
    aws.lost = "create_transit_gateway_vpc_attachment"
    with pytest.raises(TimeoutError):
        await runtime.establish(REMOTE)
    async with pool.acquire() as c:
        assert (
            await c.fetchval(
                "SELECT count(*) FROM controller_network_effects WHERE confirmed_at IS NULL"
            )
            == 1
        )
    await runtime.establish(REMOTE)
    calls = [
        c
        for c in aws.calls
        if c[1] == "create_transit_gateway_vpc_attachment" and c[0] == HOME
    ]
    assert len(calls) == 1
    await runtime.cleanup()


async def test_incomplete_provider_observation_never_releases_network(network, pool):
    runtime, aws = network
    await runtime.establish(REMOTE)
    aws.denied = "describe_security_group_rules"
    with pytest.raises(PermissionError):
        await runtime.cleanup()
    async with pool.acquire() as c:
        assert (
            await c.fetchval(
                "SELECT count(*) FROM controller_network_members WHERE released_at IS NULL"
            )
            > 0
        )
    assert aws.peerings


async def test_actual_database_lock_excludes_concurrent_attach_and_teardown(
    network, pool
):
    runtime, aws = network
    await runtime.establish(REMOTE)
    second, _ = await make_network(
        pool,
        aws,
        org=runtime.journal.lease.org_id,
        workspace=runtime.journal.lease.workspace_id,
        cluster=runtime.target["cluster_id"],
    )
    key = runtime.recipes[0][0]
    async with runtime.journal.locked(key):
        with pytest.raises(OperationRefused, match="busy"):
            async with second.journal.locked(key):
                pass
    async with second.journal.locked(key):
        pass


async def test_revocation_after_intent_stops_provider_mutation(network, pool):
    runtime, _ = network
    entered = 0

    async def authority():
        nonlocal entered
        entered += 1
        if entered == 3:
            raise OperationRefused("revoked")

    runtime.journal.authorize = authority
    created = []

    async def observe(_):
        return None

    async def create(_):
        created.append(True)

    with pytest.raises(OperationRefused, match="revoked"):
        await runtime.journal.ensure(
            "test", {"kind": "fixture"}, adopted=False, observe=observe, create=create
        )
    assert not created


async def test_retired_network_can_be_recreated_for_a_new_allocation(network, pool):
    first, aws = network
    await first.establish(REMOTE)
    await first.cleanup()
    second, _ = await make_network(
        pool,
        aws,
        org=first.journal.lease.org_id,
        workspace=first.journal.lease.workspace_id,
        cluster=first.target["cluster_id"],
    )
    await second.establish(REMOTE)
    assert aws.peerings
    async with pool.acquire() as c:
        assert (
            await c.fetchval("SELECT max(generation) FROM controller_network_resources")
            == 2
        )
    await second.cleanup()


@pytest.mark.parametrize("changed", [None, "blackhole", "compute_region"])
async def test_recovery_requires_fresh_routes_for_original_compute_region(
    network, changed
):
    from types import SimpleNamespace

    from superplane_executor.recovery_observation import observe_request

    runtime, aws = network
    await runtime.establish(REMOTE)
    runtime.plan.data["node_count"] = 1

    async def status(_):
        return "SUCCEEDED"

    async def session_for(*_):
        return aws, "approved"

    async def instances(*_):
        return [
            {
                "InstanceId": "i-0123456789abcdef0",
                "SuperplaneRegion": HOME if changed == "compute_region" else REMOTE,
            }
        ]

    runtime.provider.sky = SimpleNamespace(status=status)
    runtime.provider.session_for = session_for
    runtime.provider.instances = instances
    if changed == "blackhole":
        next(iter(aws.routes.values()))["State"] = "blackhole"
    outcome, reference = await observe_request(
        runtime.provider,
        runtime.operation,
        runtime.plan,
        operation_kind="launch",
        request_id="original",
    )
    assert outcome == ("succeeded" if changed is None else "unknown")
    assert bool(reference) == (changed is None)
