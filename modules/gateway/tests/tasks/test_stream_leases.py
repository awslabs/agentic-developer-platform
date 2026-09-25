"""Shared-server contention and fencing, using production Lua across clients."""

import asyncio
import time

import fakeredis.aioredis
import pytest

from src.tasks.errors import TaskApiError
from src.tasks.stream_leases import RedisStreamRegistry


@pytest.fixture
def registries():
    server = fakeredis.FakeServer()
    return [RedisStreamRegistry(fakeredis.aioredis.FakeRedis(server=server), "test-environment") for _ in range(4)]


@pytest.mark.asyncio
@pytest.mark.parametrize(("scope", "cap"), [("task", 2), ("principal", 10), ("environment", 32)])
async def test_global_contention(registries, scope, cap):
    async def attempt(i):
        return await registries[i % 4].acquire_lease(
            tenant_id="tenant",
            principal_id="principal" if scope != "environment" else f"principal-{i}",
            task_id="task" if scope == "task" else f"task-{i}",
        )

    results = await asyncio.gather(*(attempt(i) for i in range(cap + 12)), return_exceptions=True)
    admitted = [r for r in results if not isinstance(r, Exception)]
    refused = [r for r in results if isinstance(r, Exception)]
    assert len(admitted) == cap
    assert all(isinstance(r, TaskApiError) and r.status == 429 for r in refused)
    assert await registries[0].client.zcard(admitted[0].keys[0]) == cap
    await admitted[0].release()
    replacement = await attempt(100)
    assert replacement.remaining() > 40
    await asyncio.gather(*(lease.release() for lease in admitted), replacement.release())
    assert await registries[0].client.zcard(replacement.keys[0]) == 0


@pytest.mark.asyncio
async def test_tenant_scopes_canonical_principal(registries):
    leases = []
    for tenant in ("one", "two"):
        for i in range(10):
            leases.append(await registries[i % 4].acquire_lease(tenant_id=tenant, principal_id="same-id", task_id=f"task-{i}"))
    assert len(leases) == 20
    assert leases[0].keys[1] != leases[10].keys[1]
    await asyncio.gather(*(lease.release() for lease in leases))


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [False, True])
async def test_no_renewal_if_any_scope_expired_or_missing(registries, missing):
    lease = await registries[0].acquire_lease(tenant_id="t", principal_id="p", task_id="task")
    if missing:
        await registries[1].client.zrem(lease.keys[1], lease.token)
    else:
        await registries[1].client.zadd(lease.keys[1], {lease.token: 1})
    original = await registries[0].client.zscore(lease.keys[0], lease.token)
    assert not await lease.renew()
    assert lease.remaining() == 0
    assert await registries[0].client.zscore(lease.keys[0], lease.token) == original
    await lease.release()


@pytest.mark.asyncio
async def test_expiry_reclaims_capacity_and_stale_release_preserves_new_lease(registries):
    stale = await registries[0].acquire_lease(tenant_id="t", principal_id="p", task_id="task")
    other = await registries[1].acquire_lease(tenant_id="t", principal_id="p", task_id="task")
    for key in stale.keys:
        await registries[0].client.zadd(key, {stale.token: 1})
    new = await registries[2].acquire_lease(tenant_id="t", principal_id="p", task_id="task")
    assert not await stale.renew()
    await stale.release()
    await stale.release()
    assert await new.renew()
    assert await registries[0].client.zcard(new.keys[2]) == 2
    await other.release()
    await new.release()


@pytest.mark.asyncio
async def test_local_expiry_never_renews(registries):
    lease = await registries[0].acquire_lease(tenant_id="t", principal_id="p", task_id="task")
    lease.deadline = time.monotonic() - 1
    assert not await lease.renew()
    await lease.release()


@pytest.mark.asyncio
async def test_backend_error_denies_admission_and_fences_existing_lease(registries, monkeypatch):
    registry = registries[0]
    lease = await registry.acquire_lease(tenant_id="t", principal_id="p", task_id="task")

    async def fail(*args):
        raise ConnectionError("unavailable")

    monkeypatch.setattr(registry.client, "eval", fail)
    with pytest.raises(TaskApiError) as raised:
        await registry.acquire_lease(tenant_id="t", principal_id="p", task_id="task")
    assert raised.value.status == 503
    assert not await lease.renew()
    assert lease.remaining() == 0
    await lease.release()
