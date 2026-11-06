"""Crew socket leases enforce the per-owner cap across Redis clients."""

import fakeredis.aioredis
import pytest

from src.crew import connections
from src.crew.connections import CrewConnectionLimitError, CrewConnectionRegistry


async def test_per_user_connection_cap_is_atomic_across_gateway_clients():
    server = fakeredis.FakeServer()

    def registry():
        return CrewConnectionRegistry(fakeredis.aioredis.FakeRedis(server=server), "crew-test", limit=2)

    first = await registry().acquire("tenant-a", "user-a")
    second = await registry().acquire("tenant-a", "user-a")
    other_user = await registry().acquire("tenant-a", "user-b")
    try:
        with pytest.raises(CrewConnectionLimitError):
            await registry().acquire("tenant-a", "user-a")
        assert await first.renew()
        await first.release()
        replacement = await registry().acquire("tenant-a", "user-a")
        await replacement.release()
    finally:
        await first.release()
        await second.release()
        await other_user.release()


async def test_configured_connection_cap_never_exceeds_sixty_four(monkeypatch):
    class Settings:
        redis_url = "redis://localhost/0"
        redis_cache_name = "crew-test"

    monkeypatch.setattr(connections, "get_settings", Settings)
    monkeypatch.setattr(connections, "create_redis_client", lambda *args, **kwargs: fakeredis.aioredis.FakeRedis())
    monkeypatch.setenv("CREW_PROXY_MAX_CONN_PER_USER", "200")
    registry = connections.crew_connection_registry()
    assert registry.limit == 64
    await registry.client.aclose()
