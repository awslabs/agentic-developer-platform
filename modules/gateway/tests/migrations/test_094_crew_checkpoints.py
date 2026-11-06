"""One additive checkpoint migration preserves existing owner rows and fences duplicates."""

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, downgrade, upgrade


def revisions():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert "095_crew_restore_pins" in {revision.revision for revision in scripts.walk_revisions()}
    assert scripts.get_revision("094_crew_checkpoints").down_revision == "094_credential_agent_consent"
    return scripts.get_revision("088_crew_executor_inventory").module, scripts.get_revision("094_crew_checkpoints").module


def test_upgrade_preserves_owner_and_rejects_duplicate_sequences_and_retries():
    inventory, checkpoint = revisions()
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            inventory.upgrade()
            connection.execute(text("INSERT INTO crew_executors (id, tenant_id, user_id, generation) VALUES ('existing', 'tenant', 'user', 1)"))
            checkpoint.upgrade()
            rows = connection.execute(text("SELECT owner_key, latest_verified_checkpoint_id FROM crew_executors WHERE id='existing'")).one()
            assert rows == (None, None)
            columns = {column["name"] for column in inspect(connection).get_columns("crew_checkpoints")}
            assert {
                "owner_key",
                "archive_version_id",
                "manifest_version_id",
                "archive_sha256",
                "manifest_sha256",
                "captured_at",
                "verified_at",
                "retention_until",
                "restore_pins",
                "restore_token_hash",
            } <= columns
            insert = text(
                "INSERT INTO crew_checkpoints "
                "(id, executor_id, tenant_id, user_id, owner_key, target_kind, generation, sequence, idempotency_key) "
                "VALUES (:id, 'existing', 'tenant', 'user', :owner_key, 'eks-pod', 1, :sequence, :retry)"
            )
            connection.execute(insert, {"id": "candidate", "owner_key": "a" * 52, "sequence": 1, "retry": "same"})
            with pytest.raises(IntegrityError):
                connection.execute(insert, {"id": "duplicate-key", "owner_key": "a" * 52, "sequence": 2, "retry": "same"})
            with pytest.raises(IntegrityError):
                connection.execute(insert, {"id": "duplicate-sequence", "owner_key": "a" * 52, "sequence": 1, "retry": "other"})
            with pytest.raises(IntegrityError):
                connection.execute(text("UPDATE crew_checkpoints SET status='synced' WHERE id='candidate'"))
            checkpoint.downgrade()
            assert "crew_checkpoints" not in inspect(connection).get_table_names()
            assert "latest_verified_checkpoint_id" not in {column["name"] for column in inspect(connection).get_columns("crew_executors")}
    engine.dispose()


@pytest.mark.parametrize("base_revision", ["093_dev_box_cost_policy", "094_credential_agent_consent"])
def test_real_postgres_upgrade_restore_pins_and_rollback_preserve_base_schema(pg_url, base_revision):
    upgrade(pg_url, base_revision)
    engine = create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO crew_executors (id, tenant_id, user_id, generation) VALUES ('existing', 'tenant', 'user', 1)"))
        upgrade(pg_url, "095_crew_restore_pins")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "095_crew_restore_pins"
            consent = next(column for column in inspect(connection).get_columns("user_credentials") if column["name"] == "agent_consent")
            assert consent["nullable"] is False and consent["default"] == "false"
            columns = {column["name"] for column in inspect(connection).get_columns("crew_checkpoints")}
            assert {"archive_version_id", "manifest_version_id", "restore_pod_uid", "restore_pod_name"} <= columns
            assert connection.execute(
                text("SELECT generation, owner_key, latest_verified_checkpoint_id FROM crew_executors WHERE id='existing'")
            ).one() == (1, None, None)
        downgrade(pg_url, "094_credential_agent_consent")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "094_credential_agent_consent"
            assert "crew_checkpoints" not in inspect(connection).get_table_names()
            assert connection.scalar(text("SELECT generation FROM crew_executors WHERE id='existing'")) == 1
            consent = next(column for column in inspect(connection).get_columns("user_credentials") if column["name"] == "agent_consent")
            assert consent["nullable"] is False and consent["default"] == "false"
    finally:
        engine.dispose()
