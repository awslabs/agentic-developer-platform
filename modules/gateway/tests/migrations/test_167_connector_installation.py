"""Installation authority extends, rather than fabricates, credential delegation."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT
from tests.migrations.head_contract import GATEWAY_HEAD


def test_installation_delegation_preserves_one_migration_head():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("167_connector_installation").down_revision == "166_l5_wa_generation"


def test_installation_migration_retains_credentials_and_enforces_exclusive_source(pg_url):
    engine = create_engine(pg_url)
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE channel_tenant_map (id varchar(36) PRIMARY KEY)"))
            connection.execute(text("CREATE TABLE credential_service_delegations (id varchar(36) PRIMARY KEY, credential_id varchar(36) NOT NULL)"))
            connection.execute(text("INSERT INTO credential_service_delegations VALUES ('existing', 'credential')"))
            connection.execute(text("INSERT INTO channel_tenant_map VALUES ('installation')"))
            with Operations.context(MigrationContext.configure(connection)):
                scripts.get_revision("167_connector_installation").module.upgrade()
            columns = {column["name"]: column for column in inspect(connection).get_columns("credential_service_delegations")}
            assert columns["credential_id"]["nullable"] and columns["installation_id"]["nullable"]
            assert connection.execute(
                text("SELECT credential_id, installation_id FROM credential_service_delegations WHERE id = 'existing'")
            ).one() == ("credential", None)
            connection.execute(text("INSERT INTO credential_service_delegations (id, installation_id) VALUES ('new', 'installation')"))
            for values in ("('neither', NULL, NULL)", "('both', 'credential', 'installation')", "('missing', NULL, 'unknown')"):
                with pytest.raises(IntegrityError), connection.begin_nested():
                    connection.execute(text("INSERT INTO credential_service_delegations (id, credential_id, installation_id) VALUES " + values))
    finally:
        engine.dispose()
