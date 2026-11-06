"""Assistant activity is persisted separately from host activity."""

import importlib.util
from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_activity_migration_round_trip(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "092_dev_box_assistant_activity.py"
    spec = importlib.util.spec_from_file_location("dev_box_activity_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    Table("dev_boxes", MetaData(), Column("id", String(36), primary_key=True)).create(engine)
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        fields = {column["name"] for column in inspect(connection).get_columns("dev_boxes")}
        assert {"assistant_activity_at", "assistant_busy_until_at"} <= fields
        migration.downgrade()
        fields = {column["name"] for column in inspect(connection).get_columns("dev_boxes")}
        assert "assistant_activity_at" not in fields
        assert "assistant_busy_until_at" not in fields
    engine.dispose()
