"""Cost policy and provider observation cursors survive schema upgrades."""

import importlib.util
from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_cost_policy_migration_round_trip(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "093_dev_box_cost_policy.py"
    spec = importlib.util.spec_from_file_location("dev_box_cost_policy_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table("dev_box_policies", metadata, Column("id", String(36), primary_key=True))
    Table("dev_box_resources", metadata, Column("id", String(36), primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        policies = {item["name"] for item in inspect(connection).get_columns("dev_box_policies")}
        resources = {item["name"] for item in inspect(connection).get_columns("dev_box_resources")}
        assert {"max_storage_gb", "max_running_hours", "instance_hour_rate_usd", "disk_gb_hour_rate_usd", "snapshot_gb_hour_rate_usd"} <= policies
        assert {"last_observed_at", "last_observed_state", "last_rate_usd", "coverage_gap_at"} <= resources
        migration.downgrade()
        assert "max_storage_gb" not in {item["name"] for item in inspect(connection).get_columns("dev_box_policies")}
        assert "last_observed_at" not in {item["name"] for item in inspect(connection).get_columns("dev_box_resources")}
    engine.dispose()
