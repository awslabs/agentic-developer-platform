"""Versioned dependency metadata extends scoped context history additively."""

from sqlalchemy import create_engine, inspect, text

from alembic.script import ScriptDirectory
from src.products.models import ConnectorEvidenceDependency
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_dependency_history_extends_contexts():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("158_connector_policy_deps").down_revision == "157_connector_operation_contexts"


def test_dependency_upgrade_adds_scoped_versions_and_context_digest(pg_url):
    upgrade(pg_url, "157_connector_operation_contexts")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert not inspect(connection).has_table("connector_evidence_dependencies")
        upgrade(pg_url, "158_connector_policy_deps")
        with engine.connect() as connection:
            inspector = inspect(connection)
            assert {column["name"] for column in inspector.get_columns("connector_evidence_dependencies")} == set(
                ConnectorEvidenceDependency.__table__.columns.keys()
            )
            assert "policy_digest" in {column["name"] for column in inspector.get_columns("connector_operation_contexts")}
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "158_connector_policy_deps"
    finally:
        engine.dispose()
