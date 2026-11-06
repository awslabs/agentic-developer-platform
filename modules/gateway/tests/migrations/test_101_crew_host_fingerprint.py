"""Legacy host rows upgrade without inheriting a fingerprint."""

from sqlalchemy import create_engine, inspect, text

from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT


def test_host_fingerprint_migration_adds_nullable_generation_pin():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    migration = scripts.get_revision("101_crew_host_fingerprint").module
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE dev_box_instances (id varchar(36) primary key, machine_generation integer)"))
        connection.execute(text("INSERT INTO dev_box_instances VALUES ('host-a', 7)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(text("SELECT machine_generation, certificate_fingerprint FROM dev_box_instances")).one() == (7, None)
            connection.execute(
                text("UPDATE dev_box_instances SET certificate_fingerprint = :fingerprint WHERE id = 'host-a'"), {"fingerprint": "a" * 64}
            )
            assert "certificate_fingerprint" in {column["name"] for column in inspect(connection).get_columns("dev_box_instances")}
            migration.downgrade()
            assert "certificate_fingerprint" not in {column["name"] for column in inspect(connection).get_columns("dev_box_instances")}
    engine.dispose()
