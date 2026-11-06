"""A single gateway migration owns Dev Box identity and reservation constraints."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.shared.models import Base
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, upgrade


def test_dev_box_migration_precedes_the_single_head():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    script = ScriptDirectory.from_config(config)
    heads = script.get_heads()
    assert len(heads) == 1
    assert "088_dev_box_contract" in {revision.revision for revision in script.walk_revisions("base", heads[0])}
    assert script.get_revision("088_dev_box_contract").down_revision == "087_run_report_spool_document"


def test_dev_box_migration_constraints_and_rollback(pg_url):
    upgrade(pg_url, "087_run_report_spool_document")
    upgrade(pg_url, "088_dev_box_contract")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            inspector = inspect(connection)
            assert {
                "dev_boxes",
                "dev_box_instances",
                "dev_box_resources",
                "dev_box_operations",
                "dev_box_events",
                "dev_box_template_versions",
                "dev_box_policies",
                "dev_box_usage",
                "dev_box_devices",
                "dev_box_access_sessions",
            } <= set(inspector.get_table_names())
            later_columns = {
                # 099/101 enrollment + fingerprint; 123 the generation's release pins (#8223 cause 34)
                "dev_box_instances": {
                    "enrollment_public_key",
                    "certificate_fingerprint",
                    "profile_revision",
                    "source_revision",
                    "crew_bundle_sha256",
                    "launch_template_version",
                },
                "dev_box_devices": {"sealed_enrollment"},
                "dev_box_operations": {"retention_choice"},
                "dev_boxes": {"assistant_activity_at", "assistant_busy_until_at", "assistant_consolidated_at"},
                "dev_box_policies": {
                    "max_storage_gb",
                    "max_running_hours",
                    "instance_hour_rate_usd",
                    "disk_gb_hour_rate_usd",
                    "snapshot_gb_hour_rate_usd",
                },
                "dev_box_resources": {
                    "last_observed_at",
                    "last_observed_state",
                    "last_rate_usd",
                    "coverage_gap_at",
                    "snapshot_created_at",
                },
            }
            for table in (name for name in inspector.get_table_names() if name.startswith("dev_box")):
                expected = set(Base.metadata.tables[table].columns.keys()) - later_columns.get(table, set())
                assert {column["name"] for column in inspector.get_columns(table)} == expected
            assert any(fk["referred_table"] == "user_identities" for fk in inspector.get_foreign_keys("dev_box_instances"))
            assert any(fk["referred_table"] == "dev_box_instances" for fk in inspector.get_foreign_keys("dev_box_devices"))
            assert any(
                fk["referred_table"] == "dev_box_resources" and len(fk["constrained_columns"]) == 6
                for fk in inspector.get_foreign_keys("dev_box_usage")
            )
            assert any(
                set(fk["constrained_columns"]) == {"box_id", "user_id", "org_id", "deployment_id"}
                for fk in inspector.get_foreign_keys("dev_box_instances")
            )
            connection.execute(
                text(
                    "INSERT INTO organizations (id,name,aws_accounts,role_mappings,settings,created_at) "
                    "VALUES ('org-box','Box org','[]','{}','{}',NOW())"
                )
            )
            connection.execute(
                text("INSERT INTO users (id,org_id,team_id,email,created_at) VALUES ('owner-box','org-box','','owner@example.test',NOW())")
            )
            connection.execute(
                text(
                    "INSERT INTO dev_box_template_versions (id,name,revision,profile,instance_size,disk_gb,approved) "
                    "VALUES ('template','assistant','r1','crew','small',30,true)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO dev_boxes (id,user_id,org_id,deployment_id,template_id,template_revision,profile,status,"
                    "desired_state,observed_state,machine_generation,created_at) VALUES "
                    "('box','owner-box','org-box','dev','template','r1','crew','requested','running','unknown',1,NOW())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO dev_box_resources (id,box_id,user_id,org_id,deployment_id,machine_generation,resource_kind,"
                    "provider_resource_id,retention_state,deletion_state) VALUES "
                    "('disk','box','owner-box','org-box','dev',1,'volume','example-volume','retained','unknown')"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dev_boxes (id,user_id,org_id,deployment_id,template_id,template_revision,profile,status,"
                    "desired_state,observed_state,machine_generation,created_at) VALUES "
                    "('duplicate','owner-box','org-box','dev','template','r1','crew','requested','running','unknown',1,NOW())"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dev_box_instances (id,box_id,user_id,org_id,deployment_id,machine_generation,template_revision,"
                    "observed_state) VALUES ('invalid','box','owner-box','org-box','foreign',1,'r1','unknown')"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dev_box_usage (id,box_id,user_id,org_id,deployment_id,machine_generation,resource_id,"
                    "interval_start,interval_end,cost_usd,units,unit_name,rate_usd,currency,reconciled,source_revision) "
                    "VALUES ('bad-usage','box','owner-box','org-box','dev',2,'disk',NOW(),NOW(),0,0,'hour',0,'USD',false,'r1')"
                )
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO dev_boxes (id,user_id,org_id,deployment_id,template_id,template_revision,profile,status,"
                    "desired_state,observed_state,machine_generation,created_at) VALUES "
                    "('bad-generation','owner-box','org-box','dev','template','r1','crew','released','running','unknown',0,NOW())"
                )
            )
        downgrade(pg_url, "087_run_report_spool_document")
        with engine.connect() as connection:
            assert not any(table.startswith("dev_box") for table in inspect(connection).get_table_names())
            assert "user_identities" in inspect(connection).get_table_names()
    finally:
        engine.dispose()


def test_dev_box_head_schema_matches_current_models(pg_url):
    upgrade(pg_url, "head")
    engine = create_engine(pg_url)
    try:
        with engine.connect() as connection:
            inspector = inspect(connection)
            for table in Base.metadata.tables.values():
                if table.name.startswith("dev_box"):
                    assert {column["name"] for column in inspector.get_columns(table.name)} == set(table.columns.keys())
    finally:
        engine.dispose()
