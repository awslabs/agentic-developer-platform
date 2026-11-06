"""Host restore pins remain distinct from legacy pod restore pins."""

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT


def test_host_pin_migration_preserves_legacy_restore_pod():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    inventory = scripts.get_revision("088_crew_executor_inventory").module
    checkpoints = scripts.get_revision("094_crew_checkpoints").module
    pod_pins = scripts.get_revision("095_crew_restore_pins").module
    host_pins = scripts.get_revision("100_crew_host_restore_pin").module
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            inventory.upgrade()
            connection.execute(text("INSERT INTO crew_executors (id, tenant_id, user_id, generation) VALUES ('executor', 'tenant', 'user', 1)"))
            checkpoints.upgrade()
            pod_pins.upgrade()
            connection.execute(
                text(
                    "INSERT INTO crew_checkpoints (id, executor_id, tenant_id, user_id, owner_key, target_kind, "
                    "generation, sequence, idempotency_key, restore_pins, restore_pod_uid, restore_pod_name) "
                    "VALUES ('checkpoint', 'executor', 'tenant', 'user', 'owner', 'eks-pod', 1, 1, 'key', 1, 'uid', 'name')"
                )
            )
            host_pins.upgrade()
            columns = {column["name"] for column in inspect(connection).get_columns("crew_checkpoints")}
            assert {"restore_box_id", "restore_machine_generation"} <= columns
            assert connection.execute(
                text(
                    "SELECT restore_pins, restore_pod_uid, restore_pod_name, restore_box_id, restore_machine_generation "
                    "FROM crew_checkpoints WHERE id='checkpoint'"
                )
            ).one() == (1, "uid", "name", None, None)
            host_pins.downgrade()
            assert "restore_box_id" not in {column["name"] for column in inspect(connection).get_columns("crew_checkpoints")}
    engine.dispose()
