"""Node metadata migration preserves legacy rows and keeps one Alembic head."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from src.orchestration.models import JSON_DOC


def test_node_metadata_upgrade_and_downgrade_without_a_service(monkeypatch):
    gateway = Path(__file__).resolve().parents[2]
    revision_path = gateway / "alembic/versions/086_orchestration_node_metadata.py"
    spec = importlib.util.spec_from_file_location("node_metadata_migration", revision_path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    config = Config(str(gateway / "alembic.ini"))
    config.set_main_option("script_location", str(gateway / "alembic"))
    # 086 is no longer the head (087 adds the gateway-held report spool); it must
    # still sit on the single chain with its original parent.
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert script.get_revision(migration.revision).down_revision == "086_operations_report_receipt"
    assert isinstance(JSON_DOC.dialect_impl(postgresql.dialect()), postgresql.JSONB)

    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE orchestration_nodes (id TEXT PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO orchestration_nodes (id) VALUES ('legacy')"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        columns = {column["name"]: column for column in sa.inspect(connection).get_columns("orchestration_nodes")}
        assert {"lineage", "labels"} <= columns.keys()
        assert columns["lineage"]["nullable"] and columns["labels"]["nullable"]
        assert connection.execute(sa.text("SELECT lineage, labels FROM orchestration_nodes WHERE id='legacy'")).one() == (None, None)
        migration.downgrade()
        assert {column["name"] for column in sa.inspect(connection).get_columns("orchestration_nodes")} == {"id"}
        assert connection.scalar(sa.text("SELECT id FROM orchestration_nodes")) == "legacy"
    engine.dispose()


def test_node_metadata_follows_operations_receipts_and_preserves_them_on_rollback():
    gateway = Path(__file__).resolve().parents[2]
    config = Config(str(gateway / "alembic.ini"))
    config.set_main_option("script_location", str(gateway / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    # Walk from this migration (not "head": 087 adds the gateway-held spool above it).
    migrations = list(scripts.iterate_revisions("086_orchestration_node_metadata", "085_amendment_issue_bindings"))
    assert [migration.revision for migration in migrations] == [
        "086_orchestration_node_metadata",
        "086_operations_report_receipt",
    ]

    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE orchestration_nodes (id TEXT PRIMARY KEY)"))
        connection.execute(sa.text("CREATE TABLE orchestration_run_reports (id TEXT PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO orchestration_nodes (id) VALUES ('legacy')"))
        connection.execute(sa.text("INSERT INTO orchestration_run_reports (id) VALUES ('report')"))
        with Operations.context(MigrationContext.configure(connection)):
            for migration in reversed(migrations):
                migration.module.upgrade()
            receipt = '{"status":"observed"}'
            connection.execute(
                sa.text("UPDATE orchestration_run_reports SET operations_report_receipt = :receipt WHERE id = 'report'"),
                {"receipt": receipt},
            )
            assert connection.execute(sa.text("SELECT lineage, labels FROM orchestration_nodes WHERE id = 'legacy'")).one() == (None, None)

            migrations[0].module.downgrade()
            assert {column["name"] for column in sa.inspect(connection).get_columns("orchestration_nodes")} == {"id"}
            assert connection.scalar(sa.text("SELECT operations_report_receipt FROM orchestration_run_reports WHERE id = 'report'")) == receipt

            migrations[0].module.upgrade()
            assert connection.execute(sa.text("SELECT lineage, labels FROM orchestration_nodes WHERE id = 'legacy'")).one() == (None, None)
            assert connection.scalar(sa.text("SELECT operations_report_receipt FROM orchestration_run_reports WHERE id = 'report'")) == receipt
    engine.dispose()
