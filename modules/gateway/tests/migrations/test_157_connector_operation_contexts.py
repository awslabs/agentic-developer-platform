"""The new handle registry extends the connector grant migration."""

from sqlalchemy import create_engine, inspect, text

from alembic.script import ScriptDirectory
from src.products.models import ConnectorOperationContext
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_context_history_follows_cell_registry():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("157_connector_operation_contexts").down_revision == "156_connector_cell_authority"


def test_context_migration_adds_handle_table(pg_url):
    upgrade(pg_url, "156_connector_cell_authority")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert not inspect(connection).has_table("connector_operation_contexts")
        upgrade(pg_url, "157_connector_operation_contexts")
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert {column["name"] for column in inspector.get_columns("connector_operation_contexts")} == set(
                ConnectorOperationContext.__table__.columns.keys()
            ) - {"policy_digest", "module_generation"}
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "157_connector_operation_contexts"
    finally:
        engine.dispose()
