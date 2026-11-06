"""Verify additive connector authority migration against the real gateway schema."""

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.dialects.postgresql import JSONB

from alembic.script import ScriptDirectory
from src.products.models import ConnectorCell, ConnectorCollectionGrant, ConnectorPolicyRealm
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_connector_registry_extends_gateway_history():
    scripts = ScriptDirectory(str(GATEWAY_ROOT / "alembic"))
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("156_connector_cell_authority").down_revision == "155_l5_publication_retry"


def test_connector_registry_migrates_without_replacing_bindings(pg_url):
    upgrade(pg_url, "155_l5_publication_retry")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert not inspect(connection).has_table(ConnectorCell.__tablename__)
        upgrade(pg_url, "156_connector_cell_authority")
        with engine.connect() as connection:
            inspector = inspect(connection)
            for model in (ConnectorPolicyRealm, ConnectorCell, ConnectorCollectionGrant):
                assert {column["name"] for column in inspector.get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
            assert isinstance(next(col for col in inspector.get_columns("connector_policy_realms") if col["name"] == "descriptor")["type"], JSONB)
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "156_connector_cell_authority"
            assert inspector.has_table("product_source_bindings")
    finally:
        engine.dispose()
