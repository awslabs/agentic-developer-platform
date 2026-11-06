"""An additive restore-pod identity permits safe recovery without losing old pins."""

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT


def test_migration_preserves_legacy_pins_without_claiming_their_pod_is_gone():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert "095_crew_restore_pins" in {revision.revision for revision in scripts.walk_revisions()}
    inventory = scripts.get_revision("088_crew_executor_inventory").module
    checkpoint = scripts.get_revision("094_crew_checkpoints").module
    restore = scripts.get_revision("095_crew_restore_pins").module
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            inventory.upgrade()
            connection.execute(text("INSERT INTO crew_executors (id, tenant_id, user_id, generation) VALUES ('existing', 'tenant', 'user', 1)"))
            checkpoint.upgrade()
            connection.execute(
                text(
                    "INSERT INTO crew_checkpoints "
                    "(id, executor_id, tenant_id, user_id, owner_key, target_kind, "
                    "generation, sequence, idempotency_key, restore_pins, restore_token_hash) "
                    "VALUES ('legacy', 'existing', 'tenant', 'user', 'owner', 'eks-pod', "
                    "1, 1, 'retry', 1, 'tokenhash')"
                )
            )
            restore.upgrade()
            columns = {item["name"] for item in inspect(connection).get_columns("crew_checkpoints")}
            assert {"restore_pod_uid", "restore_pod_name"} <= columns
            assert connection.execute(
                text("SELECT restore_pins, restore_token_hash, restore_pod_uid, restore_pod_name FROM crew_checkpoints WHERE id='legacy'")
            ).one() == (1, "tokenhash", None, None)
            restore.downgrade()
            assert "restore_pod_uid" not in {item["name"] for item in inspect(connection).get_columns("crew_checkpoints")}
    engine.dispose()
