"""Old consent pointers have no fabricated provider account or scope observations."""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import downgrade, upgrade


def test_provider_binding_migration_remains_in_current_chain():
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert "110_oauth_provider_binding" in {revision.revision for revision in scripts.walk_revisions()}
    assert scripts.get_revision("110_oauth_provider_binding").down_revision == "109_oauth_scope_snapshot"


def test_legacy_pointers_receive_no_observed_binding(pg_url):
    upgrade(pg_url, "109_oauth_scope_snapshot")
    engine = create_engine(pg_url)
    try:
        upgrade(pg_url, "110_oauth_provider_binding")
        with engine.connect() as connection:
            assert {"credential_id", "provider_account_id", "observed_scopes", "revision"} <= {
                column["name"] for column in inspect(connection).get_columns("oauth_connection_bindings")
            }
            assert connection.scalar(text("SELECT count(*) FROM oauth_connection_bindings")) == 0
            assert connection.scalar(text("SELECT count(*) FROM oauth_tool_receipts")) == 0
        downgrade(pg_url, "109_oauth_scope_snapshot")
        with engine.connect() as connection:
            assert not inspect(connection).has_table("oauth_connection_bindings")
            assert not inspect(connection).has_table("oauth_tool_receipts")
    finally:
        engine.dispose()
