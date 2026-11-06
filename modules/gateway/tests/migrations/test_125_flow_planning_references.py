"""The planning projection adds no authority to pre-existing rows."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

GATEWAY = Path(__file__).resolve().parents[2]
MIGRATION = GATEWAY / "alembic/versions/125_flow_planning_references.py"


def _migration():
    spec = importlib.util.spec_from_file_location("planning_125", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_planning_projection_is_additive_and_preserves_legacy_plan():
    migration = _migration()
    config = Config(str(GATEWAY / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY / "alembic"))
    assert ScriptDirectory.from_config(config).get_revision(migration.revision) is not None
    assert migration.down_revision == "124_flow_product_repo"
    assert len(migration.revision) <= 32

    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE orchestration_flows (id TEXT PRIMARY KEY, product_id TEXT)"))
        connection.execute(sa.text("CREATE TABLE orchestration_accepted_plans (flow_id TEXT, plan_document TEXT, plan_hash TEXT)"))
        connection.execute(sa.text("INSERT INTO orchestration_flows VALUES ('legacy', 'product-one')"))
        connection.execute(sa.text("INSERT INTO orchestration_accepted_plans VALUES ('legacy', '{\"nodes\":[]}', 'old-hash')"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(sa.text("SELECT product_id, origin_ref, tracker_refs FROM orchestration_flows")).one() == (
            "product-one",
            None,
            None,
        )
        assert connection.execute(sa.text("SELECT plan_document, plan_hash FROM orchestration_accepted_plans")).one() == ('{"nodes":[]}', "old-hash")
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert "origin_ref" not in {column["name"] for column in sa.inspect(connection).get_columns("orchestration_flows")}
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert connection.execute(sa.text("SELECT origin_ref, tracker_refs FROM orchestration_flows")).one() == (None, None)
    engine.dispose()
