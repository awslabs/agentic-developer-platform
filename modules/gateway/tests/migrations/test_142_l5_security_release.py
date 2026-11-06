"""Gateway migration installs the module-owned L5 schema at one host head."""

from l5_services.models import DevopsSpaceIntent, EventReceipt, Publication, PublicationRetryReceipt, Run, Work, WorkerAttempt
from l5_services.models.base import Base as L5Base
from l5_services.models.generation_collection import GenerationCollection
from sqlalchemy import create_engine, inspect

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT
from tests.migrations.head_contract import GATEWAY_HEAD
from tests.migrations.l5_schema import upgrade_l5_gateway_schema


def test_l5_bridge_installs_module_schema_without_split_gateway_heads():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("164_l5_work_delegation_parent").down_revision == "163_merge_connector_cost"
    migration = scripts.get_revision("142_l5_security_release")
    assert migration.down_revision == "141_normalize_environments"
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as connection:
            upgrade_l5_gateway_schema(connection)
            tables = set(inspect(connection).get_table_names())
            assert set(L5Base.metadata.tables) <= tables
            assert Run.__tablename__ in tables and Work.__tablename__ in tables
            assert "l5_service_worker_attempts" in tables
            for model in (Run, Work, WorkerAttempt, GenerationCollection, DevopsSpaceIntent, EventReceipt, Publication, PublicationRetryReceipt):
                assert {column["name"] for column in inspect(connection).get_columns(model.__tablename__)} == set(model.__table__.columns.keys())
            try:
                migration.module.downgrade()
            except RuntimeError as error:
                assert "retains work history" in str(error)
            else:
                raise AssertionError("releasing L5 work history cannot be silently downgraded")
            assert set(L5Base.metadata.tables) <= set(inspect(connection).get_table_names())
    finally:
        engine.dispose()
