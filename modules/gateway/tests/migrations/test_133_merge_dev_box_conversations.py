"""Conversation metadata and snapshot timestamps converge from either deployed branch."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_conversation_and_snapshot_branches_have_one_merged_head():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert set(scripts.get_revision("133_merge_dev_box_conversations").down_revision) == {
        "132_crew_conversations",
        "132_dev_box_snapshot_time",
    }


@pytest.mark.parametrize("previous_head", ["132_crew_conversations", "132_dev_box_snapshot_time"])
def test_upgrade_from_either_branch_preserves_both_schemas(pg_url, previous_head):
    upgrade(pg_url, previous_head)
    upgrade(pg_url, "head")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            inspector = inspect(connection)
            assert inspector.has_table("crew_conversations")
            assert "snapshot_created_at" in {column["name"] for column in inspector.get_columns("dev_box_resources")}
    finally:
        engine.dispose()
