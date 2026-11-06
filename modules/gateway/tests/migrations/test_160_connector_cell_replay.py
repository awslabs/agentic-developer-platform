"""Each cell owns its operation attempt keys without exposing sibling handles."""

from sqlalchemy import create_engine, inspect, text

from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_cell_replay_extends_module_admission():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("160_connector_cell_replay").down_revision == "159_connector_module_admission"


def test_replay_upgrade_replaces_global_attempt_uniqueness(pg_url):
    upgrade(pg_url, "159_connector_module_admission")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            before = inspect(connection).get_unique_constraints("connector_operation_contexts")
            assert any(row["column_names"] == ["tenant_id", "consumer", "operation_id", "attempt_id"] for row in before)
        upgrade(pg_url)
        with engine.connect() as connection:
            after = inspect(connection).get_unique_constraints("connector_operation_contexts")
            assert any(row["column_names"] == ["tenant_id", "consumer", "cell_id", "operation_id", "attempt_id"] for row in after)
            assert not any(row["column_names"] == ["tenant_id", "consumer", "operation_id", "attempt_id"] for row in after)
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
    finally:
        engine.dispose()
