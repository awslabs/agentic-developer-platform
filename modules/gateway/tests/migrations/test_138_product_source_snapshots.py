"""Source receipt migration preserves pinned history across rollback attempts."""

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_source_migrations_fit_version_column_and_extend_previous_head():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    revisions = list(scripts.iterate_revisions("138_product_source_snapshots", "136_tool_adapter_activation"))
    assert len(revisions) == 2
    assert all(len(revision.revision) <= 32 for revision in revisions)
    assert revisions[0].down_revision == revisions[1].revision
    assert revisions[1].down_revision == "136_tool_adapter_activation"


def test_postgres_upgrade_and_refused_rollback_preserve_source_receipt(pg_url):
    upgrade(pg_url, "136_tool_adapter_activation")
    upgrade(pg_url, "139_merge_admin_sources")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "139_merge_admin_sources"
            assert {"credential_service_delegations", "product_source_snapshots"} <= set(inspect(connection).get_table_names())
            connection.execute(
                text("""INSERT INTO product_source_snapshots
                    (tenant_id, id, product_id, project_id, delegation_id, delegation_revision,
                     source_ref, receipt, idempotency_key, request_hash, retain_until, expires_at, created_at)
                    VALUES ('tenant-a', 'snapshot-a', 'product-a', 'project-a', 'delegation-a', 1,
                            '{}'::jsonb, '{"source_revision":"original"}'::jsonb, 'once', 'request-digest',
                            '2026-12-01T00:00:00Z', '2026-12-01T00:00:00Z', '2026-10-01T00:00:00Z')""")
            )
        refused = run_alembic(pg_url, "downgrade", "136_tool_adapter_activation")
        assert refused.returncode != 0
        assert "Retain Product source receipts through rollback and admission drain" in refused.stderr
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "139_merge_admin_sources"
            assert connection.scalar(text("SELECT receipt FROM product_source_snapshots WHERE id = 'snapshot-a'")) == {"source_revision": "original"}
    finally:
        engine.dispose()


def test_receipt_migration_creates_retained_scope_and_fences_downgrade():
    path = Path(__file__).resolve().parents[2] / "alembic/versions/138_product_source_snapshots.py"
    spec = importlib.util.spec_from_file_location("product_source_snapshot_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.down_revision == "137_service_delegations"
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            inspector = inspect(connection)
            columns = {column["name"] for column in inspector.get_columns("product_source_snapshots")}
            assert {
                "tenant_id",
                "id",
                "product_id",
                "project_id",
                "delegation_id",
                "delegation_revision",
                "source_ref",
                "receipt",
                "idempotency_key",
                "request_hash",
                "retain_until",
                "expires_at",
            } <= columns
            constraints = inspector.get_unique_constraints("product_source_snapshots")
            assert any(set(item["column_names"]) == {"tenant_id", "delegation_id", "idempotency_key"} for item in constraints)
            assert {"ix_product_source_snapshots_scope", "ix_product_source_snapshots_expiry"} <= {
                index["name"] for index in inspector.get_indexes("product_source_snapshots")
            }
            with pytest.raises(RuntimeError, match="Retain Product source receipts"):
                migration.downgrade()
            assert inspector.has_table("product_source_snapshots")
    finally:
        engine.dispose()
