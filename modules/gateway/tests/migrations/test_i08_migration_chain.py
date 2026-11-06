"""Source verification upgrades after the merged Initiative catalog migration."""

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


def test_source_migrations_extend_single_catalog_head():
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("132_crew_conversations").down_revision == "131_legacy_binding_migration"
    assert scripts.get_revision("124_flow_product_repo").down_revision == "123_dev_box_release_pins"
    assert scripts.get_revision("123_dev_box_release_pins").down_revision == "122_product_handoff_callbacks"
    assert scripts.get_revision("122_product_handoff_callbacks").down_revision == "121_merge_metering_sources"
    assert scripts.get_revision("121_merge_metering_sources").down_revision == (
        "120_merge_metering_initiatives",
        "118_product_verification_ops",
    )
    assert scripts.get_revision("116_catalog_initiatives").down_revision == "115_product_onboarding_sessions"
    assert scripts.get_revision("117_product_source_bindings").down_revision == "116_catalog_initiatives"
    assert scripts.get_revision("118_product_verification_ops").down_revision == "117_product_source_bindings"
    assert scripts.get_revision("118_merge_metering_onboarding").down_revision == (
        "117_merge_metering_catalog",
        "115_product_onboarding_sessions",
    )


def test_postgres_upgrade_preserves_initiative_and_adds_verification(pg_url):
    upgrade(pg_url, "115_product_onboarding_sessions")
    upgrade(pg_url, "116_catalog_initiatives")
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text("""
                INSERT INTO catalog_initiative_versions
                    (tenant_id, source_connection_id, id, revision, data, recorded_at)
                VALUES ('example', 'adp.catalog', 'synthetic-initiative', 1,
                        CAST(:data AS jsonb), CURRENT_TIMESTAMP)
            """),
                {"data": '{"name": "Synthetic initiative"}'},
            )
        upgrade(pg_url, "head")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == GATEWAY_HEAD
            inspector = inspect(connection)
            assert {"catalog_initiative_versions", "product_source_bindings", "product_operations"} <= set(inspector.get_table_names())
            assert connection.scalar(text("SELECT id FROM catalog_initiative_versions WHERE tenant_id = 'example'")) == "synthetic-initiative"
            columns = {column["name"]: column for column in inspector.get_columns("product_operations")}
            assert {
                "binding_id",
                "binding_generation",
                "lease_owner",
                "lease_until",
                "attempt_count",
                "retry_at",
                "provider_operation_id",
                "callback_sequence",
            } <= columns.keys()
            assert columns["provider_operation_id"]["nullable"] is True
            assert columns["provider_operation_id"]["type"].length == 512
            assert columns["callback_sequence"]["nullable"] is False
            assert columns["callback_sequence"]["default"] == "'0'::bigint"
            constraints = {constraint["name"]: constraint["sqltext"] for constraint in inspector.get_check_constraints("product_operations")}
            assert constraints["ck_product_operations_callback_sequence"] == "callback_sequence >= 0"
            keys = inspector.get_foreign_keys("product_operations")
            assert any(key["referred_table"] == "product_source_bindings" for key in keys)
    finally:
        engine.dispose()
