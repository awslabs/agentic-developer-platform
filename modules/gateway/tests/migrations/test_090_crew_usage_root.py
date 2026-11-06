"""Existing raw usage remains readable when Crew attribution is installed."""

import importlib.util
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_crew_usage_root_migration_preserves_existing_records():
    path = Path(__file__).parents[2] / "alembic/versions/090_crew_usage_root.py"
    spec = importlib.util.spec_from_file_location("crew_usage_root_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE usage_logs (id TEXT PRIMARY KEY, user_id TEXT NOT NULL)"))
        connection.execute(text("INSERT INTO usage_logs (id, user_id) VALUES ('old', 'worker')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(text("SELECT user_id, root_human_id FROM usage_logs WHERE id = 'old'")).one() == ("worker", None)
            assert next(column for column in inspect(connection).get_columns("usage_logs") if column["name"] == "root_human_id")["nullable"]
            migration.downgrade()
        assert connection.scalar(text("SELECT user_id FROM usage_logs WHERE id = 'old'")) == "worker"
    engine.dispose()
