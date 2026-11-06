"""Existing disk and snapshot rows survive addition of optional snapshot creation time."""

import importlib.util
from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect, select

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_snapshot_time_migration_preserves_existing_resources(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "132_dev_box_snapshot_time.py"
    spec = importlib.util.spec_from_file_location("dev_box_snapshot_time_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    resources = Table("dev_box_resources", metadata, Column("id", String(36), primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(resources.insert(), [{"id": "disk"}, {"id": "snapshot"}])
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert "snapshot_created_at" in {item["name"] for item in inspect(connection).get_columns("dev_box_resources")}
        assert connection.execute(select(resources.c.id).order_by(resources.c.id)).scalars().all() == ["disk", "snapshot"]
        migration.downgrade()
        assert "snapshot_created_at" not in {item["name"] for item in inspect(connection).get_columns("dev_box_resources")}
    engine.dispose()
