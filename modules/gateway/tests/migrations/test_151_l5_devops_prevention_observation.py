"""Prevention receipts upgrade without a live database service."""

from l5_services.migrations.versions import l5_devops_prevention_observation as migration
from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_prevention_observation_upgrade_preserves_existing_receipts(monkeypatch):
    engine = create_engine("sqlite://")
    previous = Table(
        "l5_devops_space_intents", MetaData(), Column("tenant_id", String, primary_key=True), Column("operation_id", String, primary_key=True)
    )
    previous.create(engine)
    with engine.begin() as connection:
        connection.execute(previous.insert().values(tenant_id="tenant-example", operation_id="operation-example"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        columns = {column["name"] for column in inspect(connection).get_columns(previous.name)}
        assert {"prevention_observation", "prevention_policy", "prevention_operation_id"} <= columns
        assert connection.execute(previous.select()).one().tenant_id == "tenant-example"
    engine.dispose()
