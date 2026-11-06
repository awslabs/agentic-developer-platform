"""Request-path checks for optional, sanitized performance diagnostics."""

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock

import asyncpg
import httpx
import pytest
import yaml
from fastapi import FastAPI, Request

from src.shared import performance_diagnostics as diagnostics
from src.shared.middleware.logging_middleware import LoggingMiddleware


async def test_slow_request_samples_blocker_while_request_is_active(monkeypatch, caplog):
    observed = asyncio.Event()

    class DiagnosticConnection:
        terminated = False

        async def fetch(self, statement):
            self.query = statement
            observed.set()
            return [
                {
                    "pid": 20,
                    "blocker_pids": [31],
                    "query_age_ms": 250,
                    "waiter_xact_age_ms": 300,
                    "blocker_xact_age_ms": 500,
                    "waiter_query_id": None,
                    "blocker_query_ids": [None],
                }
            ]

        def terminate(self):
            self.terminated = True

    connection = DiagnosticConnection()
    connect = AsyncMock(return_value=connection)
    monkeypatch.setattr(diagnostics.asyncpg, "connect", connect)
    monkeypatch.setattr(diagnostics, "DIAGNOSTIC_TRIGGER_SECONDS", 0.01)
    monkeypatch.setenv("BG_DIAGNOSTIC_DATABASE_URL", "postgresql://private-role:private-password@db.invalid/fixture")
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/slow")
    async def slow():
        await asyncio.wait_for(observed.wait(), timeout=1)
        await asyncio.sleep(0.01)
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
        response = await client.get("/slow")

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert connection.terminated
    assert connect.await_count == 1
    assert "LIMIT 100" in connection.query
    assert connect.await_args.kwargs["server_settings"]["statement_timeout"] == "500"
    diagnostic_records = [record for record in caplog.records if record.name == diagnostics.__name__]
    assert len(diagnostic_records) == 1
    assert diagnostic_records[0].request_id == response.headers["x-request-id"]
    assert diagnostic_records[0].blocker_graph == [
        {
            "waiter_pid": 20,
            "blocker_pids": (31,),
            "query_age_ms": 250,
            "waiter_xact_age_ms": 300,
            "blocker_xact_age_ms": 500,
            "waiter_query_id": None,
            "blocker_query_ids": (None,),
        }
    ]
    assert "private-password" not in str(diagnostic_records[0].__dict__)


async def test_missing_diagnostic_role_never_opens_connection(monkeypatch):
    monkeypatch.delenv("BG_DIAGNOSTIC_DATABASE_URL", raising=False)
    connect = AsyncMock()
    monkeypatch.setattr(diagnostics.asyncpg, "connect", connect)
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
        assert (await client.get("/ok")).status_code == 200
    connect.assert_not_awaited()


@pytest.mark.parametrize("connection_failed", [False, True])
async def test_diagnostics_exclude_seeded_sql_bind_prompt_tenant_and_credentials(monkeypatch, caplog, connection_failed):
    markers = ("test-db-password", "test-sql-bind", "test-prompt", "test-tenant", "test-api-token")
    observed = asyncio.Event()

    class DiagnosticConnection:
        async def fetch(self, statement):
            observed.set()
            return [
                {
                    "pid": 20,
                    "blocker_pids": [31],
                    "query_age_ms": 250,
                    "query": "SELECT 'test-sql-bind' FROM private_table",
                    "tenant_id": "test-tenant",
                    "params": {"credential": "test-api-token"},
                }
            ]

        def terminate(self):
            return None

    if connection_failed:

        async def failing_connect(**kwargs):
            observed.set()
            raise OSError("test-db-password test-sql-bind test-prompt test-tenant test-api-token")

        connect = AsyncMock(side_effect=failing_connect)
    else:
        connect = AsyncMock(return_value=DiagnosticConnection())
    monkeypatch.setattr(diagnostics.asyncpg, "connect", connect)
    monkeypatch.setattr(diagnostics, "DIAGNOSTIC_TRIGGER_SECONDS", 0.01)
    monkeypatch.setenv("BG_DIAGNOSTIC_DATABASE_URL", "postgresql://private-role:test-db-password@db.invalid/fixture")
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.post("/model")
    async def model(request: Request):
        assert (await request.json()) == {"prompt": "test-prompt", "tenant": "test-tenant", "token": "test-api-token"}
        await asyncio.wait_for(observed.wait(), 1)
        async with asyncio.timeout(1):
            while not any(record.name == diagnostics.__name__ for record in caplog.records):
                await asyncio.sleep(0.001)
        return {"ok": True}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
        response = await client.post("/model", json={"prompt": "test-prompt", "tenant": "test-tenant", "token": "test-api-token"})

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    diagnostic_records = [record for record in caplog.records if record.name == diagnostics.__name__]
    assert len(diagnostic_records) == 1
    assert diagnostic_records[0].diagnostic_status == ("failed" if connection_failed else "captured")
    assert diagnostic_records[0].request_id == response.headers["x-request-id"]
    assert all(
        set(row)
        == {
            "waiter_pid",
            "blocker_pids",
            "query_age_ms",
            "waiter_xact_age_ms",
            "blocker_xact_age_ms",
            "waiter_query_id",
            "blocker_query_ids",
        }
        for row in diagnostic_records[0].blocker_graph
    )
    for marker in markers:
        assert marker not in str(diagnostic_records[0].__dict__)
        assert marker not in response.text


def test_deployment_uses_optional_private_diagnostic_secret():
    manifest = Path(__file__).resolve().parents[2] / "k8s" / "deployment.yaml"
    deployment = next(document for document in yaml.safe_load_all(manifest.read_text()) if document.get("kind") == "Deployment")
    container = deployment["spec"]["template"]["spec"]["containers"][0]
    diagnostic = next(value for value in container["env"] if value["name"] == "BG_DIAGNOSTIC_DATABASE_URL")
    assert diagnostic["valueFrom"]["secretKeyRef"] == {
        "name": "bedrockgateway-secrets",
        "key": "diagnostic-database-url",
        "optional": True,
    }


async def test_existing_postgres_lock_is_captured_before_request_completes(monkeypatch, caplog):
    serving_uri = os.getenv("BG_TEST_POSTGRES_URI")
    diagnostic_uri = os.getenv("BG_TEST_DIAGNOSTIC_URI")
    if not serving_uri or not diagnostic_uri:
        pytest.skip("requires pre-existing test PostgreSQL and a separate diagnostic-role URI; never provisions a server")

    locker = waiter = observer = None
    lock_key = 7035
    waiting = None
    try:
        locker = await asyncpg.connect(serving_uri)
        waiter = await asyncpg.connect(serving_uri)
        observer = await asyncpg.connect(diagnostic_uri)
        locker_pid = await locker.fetchval("SELECT pg_backend_pid()")
        waiter_pid = await waiter.fetchval("SELECT pg_backend_pid()")
        await locker.execute("SELECT pg_advisory_lock($1)", lock_key)
        waiting = asyncio.create_task(waiter.execute("SELECT pg_advisory_lock($1)", lock_key))
        async with asyncio.timeout(3):
            while not await observer.fetchval("SELECT cardinality(pg_blocking_pids($1)) > 0", waiter_pid):
                await asyncio.sleep(0.01)

        monkeypatch.setenv("BG_DIAGNOSTIC_DATABASE_URL", diagnostic_uri)
        monkeypatch.setattr(diagnostics, "DIAGNOSTIC_TRIGGER_SECONDS", 0.01)
        app = FastAPI()
        app.add_middleware(LoggingMiddleware)

        @app.get("/blocked")
        async def blocked():
            await waiting
            return {"completed": True}

        async def release_after_observation():
            async with asyncio.timeout(3):
                while not any(
                    record.name == diagnostics.__name__
                    and any(row["waiter_pid"] == waiter_pid and locker_pid in row["blocker_pids"] for row in getattr(record, "blocker_graph", ()))
                    for record in caplog.records
                ):
                    await asyncio.sleep(0.01)
            await locker.execute("SELECT pg_advisory_unlock($1)", lock_key)

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
            async with asyncio.TaskGroup() as group:
                release = group.create_task(release_after_observation())
                response = group.create_task(client.get("/blocked"))
            assert release.exception() is None
            assert response.result().status_code == 200
            assert response.result().json() == {"completed": True}
    finally:
        if waiting is not None and not waiting.done():
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
        if locker is not None:
            await locker.execute("SELECT pg_advisory_unlock_all()")
        if waiter is not None:
            await waiter.execute("SELECT pg_advisory_unlock_all()")
        for connection in (observer, waiter, locker):
            if connection is not None:
                await connection.close()
