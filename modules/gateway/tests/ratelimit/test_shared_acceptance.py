"""A14 durable administration, explicit failure policy and cross-process ceilings."""

import asyncio
import multiprocessing
import os
from unittest.mock import AsyncMock

import pytest
import redis.exceptions

from src.admin.schemas import RateLimitConfigUpdateRequest
from src.admin.service import AdminService
from src.ratelimit.backends.in_memory import InMemoryBackend
from src.ratelimit.backends.redis import RedisBackend
from src.ratelimit.config import RateLimitConfig
from src.ratelimit.models import EntityType, LimitType, RateLimitConfigRequest
from src.ratelimit.service import RateLimitService


async def test_admin_and_limiter_share_durable_rows(durable_rate_limit_store, human_user_context):
    one = RateLimitService(backend=InMemoryBackend())
    two = RateLimitService(backend=InMemoryBackend())
    context = human_user_context
    await one.configure_limits(EntityType.USER, context.user_id, context.org_id, RateLimitConfigRequest(rpm=1))
    assert (await two.get_limits(EntityType.USER, context.user_id, context.org_id)).rpm == 1
    async with durable_rate_limit_store() as session:
        await AdminService(session).update_ratelimit_config(context.org_id, "user", context.user_id, RateLimitConfigUpdateRequest(rpm=2))
    assert (await one.get_limits(EntityType.USER, context.user_id, context.org_id)).rpm == 2
    assert await two.get_limits(EntityType.USER, context.user_id, "another-org") is None
    assert await one.delete_limits(EntityType.USER, context.user_id, context.org_id)
    assert await two.get_limits(EntityType.USER, context.user_id, context.org_id) is None


def test_no_silent_memory_fallback(monkeypatch):
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("BG_REDIS_URL", raising=False)
    monkeypatch.delenv("RATELIMIT_REDIS_URL", raising=False)
    assert RateLimitConfig(_env_file=None).backend_type == "redis"
    with pytest.raises(RuntimeError, match="Shared rate limiting"):
        RateLimitService(config=RateLimitConfig(_env_file=None))
    with pytest.raises(RuntimeError, match="Shared rate limiting"):
        RateLimitService(config=RateLimitConfig(backend_type="memory", _env_file=None))
    local = RateLimitService(config=RateLimitConfig(backend_type="memory", allow_memory_backend=True, security_profile="development", _env_file=None))
    assert isinstance(local._backend, InMemoryBackend)
    monkeypatch.setenv("BG_REDIS_URL", "rediss://shared-cache:6379/0")
    assert isinstance(RateLimitService(config=RateLimitConfig(_env_file=None))._backend, RedisBackend)


async def test_redis_outage_never_grants_capacity():
    backend = RedisBackend()
    backend._get_client = AsyncMock(side_effect=redis.exceptions.ConnectionError("offline"))
    assert (await backend.check_limit("scope", LimitType.RPM, 10, 1))[0] is False
    assert (await backend.consume("scope", LimitType.RPM, 10, 1))[0] is False
    assert (await backend.increment_concurrent("scope"))[0] is False
    with pytest.raises(redis.exceptions.ConnectionError):
        await backend.set_concurrent_limit("scope", 2)
    with pytest.raises(redis.exceptions.ConnectionError):
        await backend.get_concurrent_count("scope")


async def test_partial_hierarchy_denial_releases_only_acquired_slots(mock_backend, human_user_context):
    service = RateLimitService(backend=mock_backend)
    mock_backend.increment_concurrent.side_effect = [(True, 1, 10), (False, 1, 1)]
    result = await service.consume_rate_limit(human_user_context)
    assert not result.allowed
    mock_backend.decrement_concurrent.assert_awaited_once_with("user:user-123:org-456")


def _consume_in_process(url, gate, output):
    async def run():
        backend = RedisBackend(redis_url=url, key_prefix="a14-cross-process")
        gate.wait()
        results = [await backend.consume("shared", LimitType.RPM, 7, 0) for _ in range(8)]
        await backend.set_concurrent_limit("shared", 3)
        concurrent = [await backend.increment_concurrent("shared") for _ in range(4)]
        output.put((sum(r[0] for r in results), sum(r[0] for r in concurrent)))
        await backend.close()

    asyncio.run(run())


@pytest.mark.skipif(not os.environ.get("RATELIMIT_TEST_REDIS_URL"), reason="requires disposable real Redis")
def test_four_processes_share_one_ceiling():
    """Independent clients/processes cannot each obtain a separate allowance."""
    ctx = multiprocessing.get_context("spawn")
    gate, output = ctx.Event(), ctx.Queue()
    workers = [ctx.Process(target=_consume_in_process, args=(os.environ["RATELIMIT_TEST_REDIS_URL"], gate, output)) for _ in range(4)]
    for worker in workers:
        worker.start()
    gate.set()
    try:
        results = [output.get(timeout=30) for _ in workers]
        assert sum(r[0] for r in results) == 7
        assert sum(r[1] for r in results) == 3
    finally:
        for worker in workers:
            worker.join(timeout=5)
            if worker.is_alive():
                worker.terminate()
            assert worker.exitcode == 0


async def test_rate_limit_route_has_durable_receipt(test_client, durable_rate_limit_store):
    from sqlalchemy import select

    from src.shared.models.audit import AuditLog

    response = test_client.put("/ratelimits/user/alice", json={"rpm": 3})
    assert response.status_code == 200
    operation_id = response.headers["X-Admin-Operation-Id"]
    async with durable_rate_limit_store() as session:
        rows = (await session.execute(select(AuditLog))).scalars().all()
    assert {row.details["outcome"] for row in rows} == {"pending", "success"}
    assert all(row.details["operation_id"] == operation_id for row in rows)
    assert all(row.actor_id == "admin-123" and row.org_id == "org-456" for row in rows)


async def test_audit_outage_blocks_configuration_mutation(test_client, durable_rate_limit_store, monkeypatch):
    from sqlalchemy import select

    from src.admin import audit_operation
    from src.shared.models.usage import RateLimitConfig as StoredConfig

    monkeypatch.setattr(audit_operation, "persist", AsyncMock(side_effect=RuntimeError("offline")))
    response = test_client.put("/ratelimits/user/alice", json={"rpm": 3})
    assert response.status_code == 503
    async with durable_rate_limit_store() as session:
        assert (await session.execute(select(StoredConfig))).scalars().all() == []
