"""Real PostgreSQL migration and signed callback generation/replay checks."""

import hashlib
import importlib.util
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.status_callback_routes import router
from src.knowledge.ingestion_callback_grant import mint_ingestion_grant, verify_ingestion_grant
from src.shared.database_agent_context import get_agent_context_db
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

_KEY = "synthetic-callback-test-key-no-provider-material"
_ENV = {"AGENT_RUN_CREDENTIAL_KEY": _KEY}


@pytest.fixture
async def assets(pg_url, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", _KEY)
    engine = create_async_engine(to_async_url(pg_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    statements = []
    path = Path(__file__).resolve().parents[3] / "agent-context/alembic/versions/013_ingestion_callback_attempt.py"
    spec = importlib.util.spec_from_file_location("callback_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    monkeypatch.setattr(migration, "op", SimpleNamespace(execute=statements.append))
    migration.upgrade()
    async with factory() as db:
        await db.execute(
            text("""CREATE TABLE knowledge_assets (
            id UUID PRIMARY KEY, tenant_id TEXT, status TEXT NOT NULL, source_ref TEXT,
            status_detail JSONB, last_error TEXT, retry_count INT NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        )
        for statement in statements:
            await db.execute(text(statement))
        await db.commit()
    app = FastAPI()
    app.include_router(router)

    async def db_session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_agent_context_db] = db_session
    app.dependency_overrides[verify_internal_or_irsa] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        yield factory, client
    await engine.dispose()


async def seed(factory, tenant="tenant-a"):
    asset = str(uuid.uuid4())
    token = mint_ingestion_grant(asset_id=asset, tenant_id=tenant, env=_ENV)
    grant = verify_ingestion_grant(token, env=_ENV)
    async with factory() as db:
        await db.execute(
            text("""INSERT INTO knowledge_assets
            (id, tenant_id, status, ingestion_attempt_id, callback_grant_sha256)
            VALUES (:id, :tenant, 'queued', CAST(:attempt AS uuid), :digest)"""),
            {"id": asset, "tenant": tenant, "attempt": grant.attempt_id, "digest": hashlib.sha256(token.encode()).hexdigest()},
        )
        await db.commit()
    return asset, token


async def read(factory, asset):
    async with factory() as db:
        return (await db.execute(text("SELECT status, retry_count, last_error FROM knowledge_assets WHERE id=:id"), {"id": asset})).one()


async def post(client, asset, token=None, status="complete", **extra):
    return await client.post(
        "/internal/v1/knowledge-assets/status-callback",
        json={"asset_id": asset, "status": status, **({"callback_grant": token} if token else {}), **extra},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("tenant", ["tenant-a", None])
async def test_current_grant_updates_exact_private_or_explicit_shared_asset(assets, tenant):
    factory, client = assets
    asset, token = await seed(factory, tenant)
    foreign, _ = await seed(factory, "tenant-b")
    assert (await post(client, asset, token, "indexing")).status_code == 200
    assert (await post(client, asset, token)).status_code == 200
    assert (await read(factory, asset)).status == "complete"
    assert (await read(factory, foreign)).status == "queued"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["missing", "asset", "tenant", "forged"])
async def test_missing_or_mismatched_authority_changes_no_rows(assets, mutation):
    factory, client = assets
    asset, token = await seed(factory)
    foreign, _ = await seed(factory, "tenant-b")
    target = foreign if mutation == "asset" else asset
    presented = None if mutation == "missing" else "adpk2.invalid.invalid" if mutation == "forged" else token
    extra = {"tenant_id": "tenant-b"} if mutation == "tenant" else {}
    assert (await post(client, target, presented, **extra)).status_code == 403
    assert (await read(factory, asset)).status == "queued"
    assert (await read(factory, foreign)).status == "queued"


@pytest.mark.asyncio
async def test_superseded_attempt_and_terminal_replay_are_denied(assets):
    factory, client = assets
    asset, old = await seed(factory)
    current = mint_ingestion_grant(asset_id=asset, tenant_id="tenant-a", env=_ENV)
    grant = verify_ingestion_grant(current, env=_ENV)
    async with factory() as db:
        await db.execute(
            text("UPDATE knowledge_assets SET ingestion_attempt_id=CAST(:attempt AS uuid),callback_grant_sha256=:digest WHERE id=:id"),
            {"id": asset, "attempt": grant.attempt_id, "digest": hashlib.sha256(current.encode()).hexdigest()},
        )
        await db.commit()
    assert (await post(client, asset, old, "failed", error="stale")).status_code == 404
    assert (await post(client, asset, current, "failed", error="current")).status_code == 200
    assert (await post(client, asset, current, "failed", error="replay")).status_code == 404
    assert (await post(client, asset, current, "indexing")).status_code == 404
    row = await read(factory, asset)
    assert row.retry_count == 1 and row.last_error == "current" and row.status == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("publish_fails", [False, True])
async def test_dispatch_reserves_once_before_publish_and_preserves_uncertain_attempt(assets, monkeypatch, publish_fails):
    from unittest.mock import AsyncMock

    from src.knowledge.dispatch import dispatch_ingestion, set_sqs_client

    factory, client = assets
    monkeypatch.setenv("INGESTION_QUEUE_URL", "https://offline.test/queue")
    monkeypatch.setattr("src.knowledge.dispatch.admit_source", AsyncMock())
    asset = str(uuid.uuid4())
    source = "https://github.com/synthetic/repo"
    async with factory() as db:
        await db.execute(
            text("INSERT INTO knowledge_assets(id,tenant_id,status,source_ref) VALUES(:id,'tenant-a','registered',:source)"),
            {"id": asset, "source": source},
        )
        await db.commit()
    messages = []

    class Queue:
        def send_message(self, **kwargs):
            import json

            messages.append(json.loads(kwargs["MessageBody"]))
            if publish_fails:
                raise TimeoutError("synthetic ambiguous send")
            return {"MessageId": "synthetic-message"}

    set_sqs_client(Queue())
    try:
        async with factory() as db:
            result = await dispatch_ingestion(asset, "repo", source, "tenant-a", None, None, db)
        assert result is not publish_fails
        async with factory() as db:
            assert not await dispatch_ingestion(asset, "repo", source, "tenant-a", None, None, db)
        assert len(messages) == 1
        # Even an uncertain send could have delivered. Its reserved grant remains
        # valid until explicit reindex, and the dispatcher cannot send it twice.
        token = messages[0]["callback_grant"]
        assert (await post(client, asset, token)).status_code == 200
        assert (await post(client, asset, token)).status_code == 404
    finally:
        set_sqs_client(None)
