"""The gateway-held report spool column extends the current single-head chain (#7392)."""

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, upgrade


def test_spool_migration_precedes_the_single_head():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert "087_run_report_spool_document" in {revision.revision for revision in script.walk_revisions("base", heads[0])}
    assert script.get_revision("087_run_report_spool_document").down_revision == "086_orchestration_node_metadata"


def test_spool_column_upgrades_and_downgrades_without_touching_receipts(pg_url):
    upgrade(pg_url, "086_orchestration_node_metadata")
    engine = create_engine(pg_url)
    try:
        upgrade(pg_url, "087_run_report_spool_document")
        with engine.connect() as connection:
            columns = {column["name"]: column for column in inspect(connection).get_columns("orchestration_run_reports")}
            assert columns["spool_document"]["nullable"] is True
            assert {"worker_receipt", "terminal_receipt", "operations_report_receipt"} <= set(columns)
            assert connection.scalar(text("SELECT COUNT(*) FROM orchestration_run_reports WHERE spool_document IS NOT NULL")) == 0
        downgrade(pg_url, "086_orchestration_node_metadata")
        with engine.connect() as connection:
            columns = {column["name"] for column in inspect(connection).get_columns("orchestration_run_reports")}
            assert "spool_document" not in columns and "operations_report_receipt" in columns
    finally:
        engine.dispose()
