"""The authoring outbox migration leaves old requests intact and unbound."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations

GATEWAY = Path(__file__).resolve().parents[2]


def test_authoring_assignment_migration_cycle_preserves_old_request():
    migration_path = GATEWAY / "alembic/versions/127_authoring_assignment.py"
    spec = importlib.util.spec_from_file_location("authoring_127", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "126_tracker_publication_outbox"

    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE orchestration_amendment_requests (id TEXT PRIMARY KEY, author_run_id TEXT)"))
        connection.execute(sa.text("INSERT INTO orchestration_amendment_requests VALUES ('legacy', 'replan:original')"))
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        assert connection.execute(sa.text("SELECT author_run_id, authoring_assignment FROM orchestration_amendment_requests")).one() == (
            "replan:original",
            None,
        )
        with Operations.context(MigrationContext.configure(connection)):
            module.downgrade()
        assert "authoring_assignment" not in {column["name"] for column in sa.inspect(connection).get_columns("orchestration_amendment_requests")}
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
        assert connection.execute(sa.text("SELECT authoring_assignment FROM orchestration_amendment_requests")).scalar_one() is None
    engine.dispose()
