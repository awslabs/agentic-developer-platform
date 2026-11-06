"""Scope retention never silently expands pre-existing execution authority."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT


def test_execution_scope_upgrade_preserves_legacy_grants():
    migration = ScriptDirectory(str(GATEWAY_ROOT / "alembic")).get_revision("173_l5_execution_scope")
    assert migration.down_revision == "172_story_task_board" and len(migration.revision) <= 32
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE credential_service_delegations (id VARCHAR(36) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO credential_service_delegations (id) VALUES ('existing-grant')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.module.upgrade()
                assert connection.execute(text("SELECT id, execution_scope FROM credential_service_delegations")).all() == [("existing-grant", None)]
                assert next(c for c in inspect(connection).get_columns("credential_service_delegations") if c["name"] == "execution_scope")[
                    "nullable"
                ]
                with pytest.raises(RuntimeError, match="retains execution authority"):
                    migration.module.downgrade()
    finally:
        engine.dispose()
