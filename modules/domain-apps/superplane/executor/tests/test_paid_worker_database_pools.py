"""The execution store contract must never be checked against the domain port."""

from contextlib import AsyncExitStack, asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from harness_jobs.identity import OperationRefused
from superplane_executor import task_worker


@pytest.mark.asyncio
async def test_pools_keep_domain_and_harness_connections_separate(monkeypatch):
    for key, value in {
        "SUPERPLANE_DOMAIN_SCHEMA": "superplane",
        "SUPERPLANE_OPERATION_SCHEMA": "superplane_operations",
        "SUPERPLANE_DOMAIN_DSN_FILE": "/private/domain",
        "SUPERPLANE_EXECUTION_DSN_FILE": "/private/execution",
        "SUPERPLANE_DATABASE_CA_FILE": "/private/ca.pem",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(
        task_worker.ssl, "create_default_context", lambda **kw: object()
    )
    monkeypatch.setattr(task_worker, "read_token", lambda path: str(path))
    observed = []
    checks = AsyncMock()
    monkeypatch.setattr(task_worker, "check_schema_version", checks)

    async def create(dsn, **kwargs):
        schema = kwargs["server_settings"]["search_path"].split(",")[0]
        connection = SimpleNamespace(
            fetchval=AsyncMock(return_value=schema),
            fetch=AsyncMock(return_value=[{"version_num": "domain-head"}]),
        )

        @asynccontextmanager
        async def acquire():
            yield connection

        pool = SimpleNamespace(acquire=acquire)

        @asynccontextmanager
        async def context():
            yield pool

        observed.append((dsn, schema, connection, pool))
        return context()

    monkeypatch.setattr(task_worker.asyncpg, "create_pool", create)
    async with AsyncExitStack() as stack:
        pools = await task_worker.pools(stack)
    assert [(r[0], r[1]) for r in observed] == [
        ("/private/domain", "superplane"),
        ("/private/execution", "superplane_operations"),
    ]
    assert pools == [r[3] for r in observed]
    checks.assert_awaited_once_with(observed[1][2])
    observed[0][2].fetch.assert_awaited_once_with(
        "SELECT version_num FROM alembic_version"
    )
    observed[1][2].fetch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "domain,operation",
    [("same", "same"), ("public", "operations"), ("domain; DROP SCHEMA", "operations")],
)
async def test_invalid_schema_refuses_before_connecting(monkeypatch, domain, operation):
    monkeypatch.setenv("SUPERPLANE_DOMAIN_SCHEMA", domain)
    monkeypatch.setenv("SUPERPLANE_OPERATION_SCHEMA", operation)
    connect = AsyncMock()
    monkeypatch.setattr(task_worker.asyncpg, "create_pool", connect)
    async with AsyncExitStack() as stack:
        with pytest.raises(OperationRefused):
            await task_worker.pools(stack)
    connect.assert_not_called()
