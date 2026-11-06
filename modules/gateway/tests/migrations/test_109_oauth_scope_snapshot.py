"""Scope snapshots must not be invented for consent started before the upgrade."""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import downgrade, upgrade


def test_scope_snapshot_is_on_single_migration_lineage():
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert "109_oauth_scope_snapshot" in {revision.revision for revision in scripts.walk_revisions("base", heads[0])}


def test_existing_attempt_remains_unverified_and_downgrade_preserves_row(pg_url):
    upgrade(pg_url, "108_native_credentials")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""INSERT INTO oauth_connection_attempts
                    (state_digest, org_id, user_id, provider, session_uri, expires_at)
                    VALUES ('legacy', 'tenant-one', 'owner-one', 'slack', 'old-session', CURRENT_TIMESTAMP + INTERVAL '10 minutes')""")
            )
        upgrade(pg_url, "109_oauth_scope_snapshot")
        with engine.connect() as connection:
            column = next(item for item in inspect(connection).get_columns("oauth_connection_attempts") if item["name"] == "requested_scopes")
            assert column["nullable"] is True and column["default"] is None
            assert connection.scalar(text("SELECT requested_scopes FROM oauth_connection_attempts WHERE state_digest = 'legacy'")) is None
        downgrade(pg_url, "108_native_credentials")
        with engine.connect() as connection:
            assert "requested_scopes" not in {item["name"] for item in inspect(connection).get_columns("oauth_connection_attempts")}
            assert connection.scalar(text("SELECT count(*) FROM oauth_connection_attempts")) == 1
    finally:
        engine.dispose()
