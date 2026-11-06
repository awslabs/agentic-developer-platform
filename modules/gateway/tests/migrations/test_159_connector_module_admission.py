"""Module generation survives disable/re-enable across gateway restarts."""

from sqlalchemy import create_engine, inspect, text

from alembic.script import ScriptDirectory
from src.products.models import ConnectorModuleAdmission
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_module_admission_follows_dependency_metadata():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("159_connector_module_admission").down_revision == "158_connector_policy_deps"


def test_module_upgrade_preserves_contexts_and_adds_admission(pg_url):
    upgrade(pg_url, "158_connector_policy_deps")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert not inspect(connection).has_table("connector_module_admissions")
        upgrade(pg_url, "159_connector_module_admission")
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert {column["name"] for column in inspector.get_columns("connector_module_admissions")} == set(
                ConnectorModuleAdmission.__table__.columns.keys()
            )
            assert "module_generation" in {column["name"] for column in inspector.get_columns("connector_operation_contexts")}
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "159_connector_module_admission"
    finally:
        engine.dispose()
