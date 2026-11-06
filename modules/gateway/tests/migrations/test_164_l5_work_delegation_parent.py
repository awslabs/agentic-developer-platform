"""A work delegation remains tied to revocable owner consent after upgrade."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT


def test_security_work_delegation_parent_upgrade_keeps_existing_grants():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    migration = scripts.get_revision("164_l5_work_delegation_parent")
    assert migration.down_revision == "163_merge_connector_cost" and len(migration.revision) <= 32
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE credential_service_delegations (id VARCHAR(36) PRIMARY KEY)"))
            connection.execute(text("INSERT INTO credential_service_delegations (id) VALUES ('owner-grant')"))
            with Operations.context(MigrationContext.configure(connection)):
                migration.module.upgrade()
                assert connection.execute(text("SELECT id, parent_delegation_id FROM credential_service_delegations")).all() == [
                    ("owner-grant", None)
                ]
                columns = {column["name"]: column for column in inspect(connection).get_columns("credential_service_delegations")}
                assert columns["parent_delegation_id"]["nullable"]
                assert any(
                    key["referred_table"] == "credential_service_delegations"
                    for key in inspect(connection).get_foreign_keys("credential_service_delegations")
                )
                assert "ix_l5_work_delegation_parent" in {
                    index["name"] for index in inspect(connection).get_indexes("credential_service_delegations")
                }
                with pytest.raises(RuntimeError, match="retains work history"):
                    migration.module.downgrade()
    finally:
        engine.dispose()
