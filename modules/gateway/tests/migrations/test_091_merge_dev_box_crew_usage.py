"""Both lifecycle and attribution migrations survive a shared gateway upgrade."""

import pytest
from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, upgrade


def test_merge_preserves_release_and_usage_histories():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert "091_merge_dev_box_crew_usage" in {revision.revision for revision in script.walk_revisions()}
    assert set(script.get_revision("091_merge_dev_box_crew_usage").down_revision) == {
        "090_dev_box_release_choice",
        "090_crew_usage_root",
    }
    for revision in ("090_dev_box_release_choice", "090_crew_usage_root"):
        assert script.get_revision(revision).down_revision == "089_merge_dev_box_crew"


@pytest.mark.parametrize("initial_head", ["089_merge_dev_box_crew", "090_dev_box_release_choice", "090_crew_usage_root"])
def test_upgrade_from_either_branch_preserves_rows_and_rolls_back(pg_url, initial_head):
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
            connection.execute(
                text(
                    "INSERT INTO dev_box_operations (id,box_id,user_id,org_id,deployment_id,machine_generation,action,status,"
                    "idempotency_key,request_digest,actor_user_id,provider_token,progress,retries,created_at) VALUES "
                    "('merge-release','merge-box','merge-owner','merge-org','test',1,'release','pending',"
                    "'merge-key','merge-digest','merge-owner','merge-token','accepted',0,NOW())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO usage_logs (id,org_id,timestamp,department_id,team_id,user_id,account_type,model,"
                    "input_tokens,output_tokens,cost_usd,latency_ms,status_code) VALUES "
                    "('merge-usage','merge-org',NOW(),'','','merge-worker','service','test-model',10,5,0.01,100,200)"
                )
            )
            if initial_head == "090_dev_box_release_choice":
                connection.execute(text("UPDATE dev_box_operations SET retention_choice='retain' WHERE id='merge-release'"))
            if initial_head == "090_crew_usage_root":
                connection.execute(text("UPDATE usage_logs SET root_human_id='merge-owner' WHERE id='merge-usage'"))

        upgrade(pg_url, "091_merge_dev_box_crew_usage")
        upgrade(pg_url, "091_merge_dev_box_crew_usage")
        with engine.connect() as connection:
            assert len(connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()) == 1
            assert connection.execute(text("SELECT user_id,status,retention_choice FROM dev_box_operations WHERE id='merge-release'")).one() == (
                "merge-owner",
                "pending",
                "retain" if initial_head == "090_dev_box_release_choice" else None,
            )
            assert connection.execute(text("SELECT user_id,input_tokens,root_human_id FROM usage_logs WHERE id='merge-usage'")).one() == (
                "merge-worker",
                10,
                "merge-owner" if initial_head == "090_crew_usage_root" else None,
            )

        downgrade(pg_url, "089_merge_dev_box_crew")
        with engine.connect() as connection:
            schema = inspect(connection)
            assert "retention_choice" not in {column["name"] for column in schema.get_columns("dev_box_operations")}
            assert "root_human_id" not in {column["name"] for column in schema.get_columns("usage_logs")}
            assert connection.scalar(text("SELECT status FROM dev_box_operations WHERE id='merge-release'")) == "pending"
            assert connection.scalar(text("SELECT input_tokens FROM usage_logs WHERE id='merge-usage'")) == 10
            assert connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all() == ["089_merge_dev_box_crew"]
        upgrade(pg_url, "head")
        with engine.connect() as connection:
            assert len(connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()) == 1
            assert connection.execute(text("SELECT user_id,status,retention_choice FROM dev_box_operations WHERE id='merge-release'")).one() == (
                "merge-owner",
                "pending",
                None,
            )
            assert connection.execute(text("SELECT user_id,input_tokens,root_human_id FROM usage_logs WHERE id='merge-usage'")).one() == (
                "merge-worker",
                10,
                None,
            )
    finally:
        engine.dispose()
