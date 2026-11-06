"""Both inventory branches converge without losing existing owner records."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.shared.models import Base, CrewExecutor, DevBox
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, upgrade


def test_merge_preserves_both_inventory_histories_and_model_registration():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert "089_merge_dev_box_crew" in {revision.revision for revision in script.walk_revisions("base", heads[0])}
    assert set(script.get_revision("089_merge_dev_box_crew").down_revision) == {"088_dev_box_contract", "088_crew_executor_inventory"}
    for revision in ("088_dev_box_contract", "088_crew_executor_inventory"):
        assert script.get_revision(revision).down_revision == "087_run_report_spool_document"
    assert Base.metadata.tables["dev_boxes"] is DevBox.__table__
    assert Base.metadata.tables["crew_executors"] is CrewExecutor.__table__


@pytest.mark.parametrize("initial_head", ["087_run_report_spool_document", "088_dev_box_contract", "088_crew_executor_inventory"])
def test_upgrade_from_either_inventory_preserves_rows_and_rolls_back(pg_url, initial_head):
    upgrade(pg_url, initial_head)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO organizations (id,name,aws_accounts,role_mappings,settings,created_at) "
                    "VALUES ('merge-org','Merge org','[]','{}','{}',NOW())"
                )
            )
            connection.execute(
                text("INSERT INTO users (id,org_id,team_id,email,created_at) VALUES ('merge-owner','merge-org','','owner@example.test',NOW())")
            )
            if initial_head == "088_dev_box_contract":
                connection.execute(
                    text(
                        "INSERT INTO dev_box_template_versions (id,name,revision,profile,instance_size,disk_gb,approved) "
                        "VALUES ('merge-template','assistant','r1','crew','small',30,true)"
                    )
                )
                connection.execute(
                    text(
                        "INSERT INTO dev_boxes (id,user_id,org_id,deployment_id,template_id,template_revision,profile,status,"
                        "desired_state,observed_state,machine_generation,created_at) VALUES "
                        "('merge-box','merge-owner','merge-org','test','merge-template','r1','crew','requested','running','unknown',1,NOW())"
                    )
                )
            if initial_head == "088_crew_executor_inventory":
                connection.execute(
                    text("INSERT INTO crew_executors (id,tenant_id,user_id,generation) VALUES ('merge-executor','merge-org','merge-owner',1)")
                )
        upgrade(pg_url, "089_merge_dev_box_crew")
        with engine.connect() as connection:
            schema = inspect(connection)
            assert {"dev_boxes", "crew_executors"} <= set(schema.get_table_names())
            assert "uq_dev_boxes_live_owner" in {index["name"] for index in schema.get_indexes("dev_boxes")}
            assert "uq_crew_executors_active_owner" in {index["name"] for index in schema.get_indexes("crew_executors")}
            assert any(foreign_key["referred_table"] == "user_identities" for foreign_key in schema.get_foreign_keys("dev_box_instances"))
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == ["089_merge_dev_box_crew"]
            if initial_head == "088_dev_box_contract":
                assert connection.execute(
                    text("SELECT user_id,org_id,deployment_id,machine_generation,observed_state FROM dev_boxes WHERE id='merge-box'")
                ).one() == ("merge-owner", "merge-org", "test", 1, "unknown")
            if initial_head == "088_crew_executor_inventory":
                assert connection.execute(text("SELECT tenant_id,user_id,generation,state FROM crew_executors WHERE id='merge-executor'")).one() == (
                    "merge-org",
                    "merge-owner",
                    1,
                    "provisioning",
                )
        downgrade(pg_url, "087_run_report_spool_document")
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert "crew_executors" not in tables
            assert not any(table.startswith("dev_box") for table in tables)
            assert connection.scalar(text("SELECT org_id FROM users WHERE id='merge-owner'")) == "merge-org"
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == ["087_run_report_spool_document"]
    finally:
        engine.dispose()
