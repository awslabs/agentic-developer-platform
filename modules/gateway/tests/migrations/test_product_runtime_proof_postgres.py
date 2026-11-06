"""Exercise the production proof query against PostgreSQL, not SQLite."""

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.products import runtime_proof_routes
from tests.migrations.conftest_postgres import to_async_url


@pytest.mark.parametrize("at_head", [True, False])
async def test_runtime_proof_reads_actual_postgres_head(pg_url, monkeypatch, at_head):
    gateway = Path(__file__).resolve().parents[2]
    config = Config(str(gateway / "alembic.ini"))
    config.set_main_option("script_location", str(gateway / "alembic"))
    heads = sorted(ScriptDirectory.from_config(config).get_heads())
    assert len(heads) == 1
    observed = heads[0] if at_head else "previous_head"
    manifest = gateway / "cli/command-manifest.json"
    package_identity = {"l5_package_version": "0.1.0", "l5_wheel_sha256": "a" * 64, "l5_schema_sha256": "b" * 64}
    engine = create_async_engine(to_async_url(pg_url))
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE alembic_version (version_num varchar(128) NOT NULL)"))
            await connection.execute(text("INSERT INTO alembic_version VALUES (:head)"), {"head": observed})
        monkeypatch.setattr(runtime_proof_routes, "get_engine", lambda: engine)
        monkeypatch.setattr(runtime_proof_routes, "Config", lambda _: config)
        monkeypatch.setattr(runtime_proof_routes, "Path", lambda _: manifest)
        monkeypatch.setattr(runtime_proof_routes, "release_identity", lambda: package_identity)
        result = await runtime_proof_routes.process_proof()
        assert {key: result[key] for key in package_identity} == package_identity
        assert result["database_heads"] == [observed]
        assert result["image_heads"] == heads
        assert (result["database_heads"] == result["image_heads"]) is at_head
        assert result["cli_manifest_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()
        async with engine.connect() as connection:
            assert (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars().all() == [observed]
    finally:
        await engine.dispose()
