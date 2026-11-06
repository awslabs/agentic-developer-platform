"""Real migrated PostgreSQL checks for the #4720 ingestion capability boundary."""

import hashlib
import os
import subprocess
import sys
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.internal import ingestion_control_routes as control
from src.internal import ingestion_refresh_routes as refresh
from src.knowledge.ingestion_callback_grant import mint_ingestion_grant, verify_ingestion_grant
from src.shared.database_agent_context import get_agent_context_db
from tests.migrations.conftest_postgres import pg_server, pg_url, to_async_url  # noqa: F401

KEY = "synthetic-ingestion-authority-test-key"


@pytest.fixture
async def assets(pg_url, monkeypatch):  # noqa: F811
    monkeypatch.setenv("AGENT_RUN_CREDENTIAL_KEY", KEY)
    root = Path(__file__).resolve().parents[3] / "agent-context" / "alembic"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=root,
        env={**os.environ, "AC_DATABASE_URL": to_async_url(pg_url), "AC_RDS_IAM_AUTH": "false"},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    engine = create_async_engine(to_async_url(pg_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(control.router)
    app.include_router(refresh.router)

    async def session():
        async with factory() as db:
            yield db

    app.dependency_overrides[get_agent_context_db] = session
    # GitHub and source resolution are external; authorization and catalog SQL
    # below use the real implementation against the migrated schema.
    monkeypatch.setattr(control, "admit_source", AsyncMock())
    http = MagicMock()
    response = MagicMock()
    response.json.return_value = {"private": False, "visibility": "public"}
    http.get = AsyncMock(return_value=response)
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=http)
    manager.__aexit__ = AsyncMock(return_value=False)
    # Patch only prepare_source's GitHub client, not the ASGI test transport.
    original_client = httpx.AsyncClient
    monkeypatch.setattr(control.httpx, "AsyncClient", MagicMock(return_value=manager))
    async with original_client(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
        yield factory, client, app
    await engine.dispose()


async def seed(factory, *, tenant="tenant-a", source="https://github.com/fixture/service", asset_type="repo", status="queued"):
    asset = str(uuid.uuid4())
    token = mint_ingestion_grant(asset_id=asset, tenant_id=tenant)
    grant = verify_ingestion_grant(token)
    async with factory() as db:
        await db.execute(
            text("""INSERT INTO knowledge_assets
            (id,asset_type,source_ref,tenant_id,status,ingestion_attempt_id,callback_grant_sha256)
            VALUES(CAST(:id AS uuid),:type,:source,:tenant,:status,CAST(:attempt AS uuid),:digest)"""),
            {
                "id": asset,
                "type": asset_type,
                "source": source,
                "tenant": tenant,
                "visibility": "tenant" if tenant else "shared",
                "status": status,
                "attempt": grant.attempt_id,
                "digest": hashlib.sha256(token.encode()).hexdigest(),
            },
        )
        await db.commit()
    return asset, token


async def post(client, token, operation, arguments=None):
    return await client.post(
        "/internal/v1/knowledge-assets/ingestion-control", json={"callback_grant": token, "operation": operation, "arguments": arguments or {}}
    )


async def bootstrap(client, token):
    r = await post(client, token, "bootstrap", {"receipt_handle": "synthetic-receipt"})
    assert r.status_code == 200, r.text
    return r.json()["result"]


@pytest.mark.asyncio
async def test_bootstrap_and_full_catalog_lifecycle(assets):
    factory, client, _ = assets
    asset, token = await seed(factory)
    envelope = await bootstrap(client, token)
    assert envelope["registry_asset_id"] == asset
    assert envelope["scope"]["tenant_id"] == "tenant-a"
    assert envelope["source"] == "fixture/service"

    async def command(name, **args):
        response = await post(client, token, "catalog." + name, args)
        assert response.status_code == 200, (name, response.text)
        return response.json()["result"]

    repo = await command(
        "ensure_repo_exists",
        org_repo="fixture/service",
        git_url="https://github.com/fixture/service",
        tenant_id="tenant-a",
        owner_sub=None,
        allowed_principals=["*"],
    )
    run = await command("create_index_run", repo_id=repo, repo="fixture/service", commit_sha="a" * 40)
    stage = await command("start_stage", run_id=run, repo="fixture/service", stage="clone")
    await command("verify_stage", stage_id=stage, artifact_ref="scratch", metrics={"files": 2})
    await command("skip_stage", run_id=run, repo="fixture/service", stage="scip_structural", reason="isolated backend unavailable")
    assert not await command("should_skip_stage", repo="fixture/service", stage="clone")
    assert (
        await command(
            "upsert_dependencies",
            repo_id=repo,
            records=[{"package_url": "pkg:pypi/fixture@1.0", "package_version": "1.0", "source": "code", "is_transitive": False}],
        )
        == 1
    )
    await command("update_repo_sbom_status", repo_id=repo, source_status="complete", last_source_sha="a" * 40)
    await command("complete_index_run", run_id=run)
    async with factory() as db:
        assert (await db.execute(text("SELECT status FROM index_runs WHERE id=CAST(:id AS uuid)"), {"id": run})).scalar() == "complete"
        assert (await db.execute(text("SELECT version FROM dependencies WHERE repo_id=CAST(:id AS uuid)"), {"id": repo})).scalar() == "1.0"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["forged", "replaced", "terminal", "tenant", "digest", "removed"])
async def test_invalid_authority_never_reaches_source_resolution(assets, monkeypatch, mutation):
    factory, client, _ = assets
    asset, token = await seed(factory)
    resolver = AsyncMock()
    monkeypatch.setattr(control, "prepare_source", resolver)
    if mutation == "forged":
        token = token[:-8] + "invalid!"
    else:
        clauses = {
            "replaced": "ingestion_attempt_id=gen_random_uuid()",
            "terminal": "status='complete'",
            "tenant": "tenant_id='tenant-b'",
            "digest": "callback_grant_sha256='bad'",
            "removed": "status='removed'",
        }
        async with factory() as db:
            await db.execute(text("UPDATE knowledge_assets SET " + clauses[mutation] + " WHERE id=CAST(:id AS uuid)"), {"id": asset})
            await db.commit()
    r = await post(client, token, "bootstrap", {"receipt_handle": "receipt"})
    assert r.status_code == 403
    resolver.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_cannot_select_other_repo_run_stage_or_acl(assets):
    factory, client, _ = assets
    _, token = await seed(factory)
    await bootstrap(client, token)
    for op, args in [
        ("bootstrap", {"receipt_handle": "receipt", "source": "other/repo"}),
        ("catalog.create_index_run", {"repo": "other/repo"}),
        ("catalog.complete_index_run", {"run_id": str(uuid.uuid4())}),
        ("catalog.verify_stage", {"stage_id": str(uuid.uuid4())}),
        ("catalog.upsert_dependencies", {"repo_id": str(uuid.uuid4()), "records": []}),
        (
            "catalog.ensure_repo_exists",
            {
                "org_repo": "fixture/service",
                "git_url": "https://github.com/fixture/service",
                "tenant_id": "tenant-a",
                "allowed_principals": ["attacker"],
            },
        ),
        ("sql", {"query": "DELETE FROM knowledge_assets"}),
    ]:
        assert (await post(client, token, op, args)).status_code == 403, op
    _, foreign = await seed(factory, tenant="tenant-b")
    assert (await post(client, foreign, "bootstrap", {"receipt_handle": "receipt"})).status_code == 403


@pytest.mark.asyncio
async def test_receipt_ticket_is_bound_to_asset_and_attempt_and_ack_survives_completion(assets, monkeypatch):
    factory, client, _ = assets
    asset, token = await seed(factory)
    envelope = await bootstrap(client, token)
    _, foreign = await seed(factory, source="https://github.com/fixture/other")
    await bootstrap(client, foreign)
    queue = MagicMock()
    monkeypatch.setattr(control, "client", lambda _: queue)
    monkeypatch.setenv("INGESTION_QUEUE_URL", "https://queue.invalid/fixture")
    assert (await post(client, foreign, "ack", {"receipt_ticket": envelope["receipt_ticket"]})).status_code == 403
    queue.delete_message.assert_not_called()
    async with factory() as db:
        await db.execute(text("UPDATE knowledge_assets SET status='complete' WHERE id=CAST(:id AS uuid)"), {"id": asset})
        await db.commit()
    assert (await post(client, token, "ack", {"receipt_ticket": envelope["receipt_ticket"]})).status_code == 200
    queue.delete_message.assert_called_once_with(QueueUrl="https://queue.invalid/fixture", ReceiptHandle="synthetic-receipt")
    assert (await post(client, token, "repo_acl")).status_code == 403


@pytest.mark.asyncio
async def test_refresh_uses_only_registered_canonical_assets_and_does_not_requeue_twice(assets, monkeypatch):
    factory, client, app = assets
    app.dependency_overrides[refresh.verify_refresh_identity] = lambda: None
    asset, _ = await seed(factory, status="complete")
    await seed(factory, source="https://github.com/fixture/inflight", status="queued")
    async with factory() as db:
        await db.execute(text("UPDATE knowledge_assets SET updated_at=NOW()-INTERVAL '2 days'"))
        await db.commit()
    dispatcher = AsyncMock(return_value=True)
    monkeypatch.setattr(refresh, "dispatch_ingestion", dispatcher)
    r = await client.post("/internal/v1/knowledge-assets/refresh", json={})
    assert r.status_code == 200, r.text
    assert r.json() == {"queued": 1, "blocked": 0, "after": None}
    assert dispatcher.call_args.args[:5] == (asset, "repo", "https://github.com/fixture/service", "tenant-a", None)
    assert (await client.post("/internal/v1/knowledge-assets/refresh", json={})).json()["queued"] == 0
    assert (await client.post("/internal/v1/knowledge-assets/refresh", json={"tenant_id": "other"})).status_code == 422


@pytest.mark.asyncio
async def test_document_catalog_uses_asset_id_and_nullable_repo(assets):
    factory, client, _ = assets
    asset, token = await seed(factory, asset_type="doc", source="s3://fixture/tenants/tenant-a/input.pdf")
    await bootstrap(client, token)
    r = await post(client, token, "catalog.create_index_run", {"repo": asset, "repo_id": None})
    assert r.status_code == 200, r.text
    run = r.json()["result"]
    r = await post(client, token, "catalog.start_stage", {"repo": asset, "run_id": run, "stage": "fetch"})
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_legacy_flattened_filename_collision_confers_no_object_authority(assets, monkeypatch):
    factory, client, _ = assets
    _, token = await seed(factory, source="https://github.com/a-b/c")
    await bootstrap(client, token)
    async with factory() as db:
        await db.execute(
            text("""INSERT INTO repositories(repo_name,git_url,owner,tenant_id)
            VALUES('a/b-c','https://github.com/a/b-c','a','tenant-a')""")
        )
        await db.commit()
    object_sink = MagicMock()
    monkeypatch.setattr(control, "object_command", object_sink)
    r = await post(client, token, "object.get", {"Bucket": "fixture", "Key": "tenants/tenant-a/code-indexes/a-b-c.json"})
    assert r.status_code == 403
    object_sink.assert_not_called()
