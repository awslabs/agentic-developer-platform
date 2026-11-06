"""Upgrade preserves the highest historical owner generation."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from src.shared.models import Base, CrewOwnerGeneration


def test_upgrade_seeds_owner_max_from_existing_pods_and_boxes(tmp_path, monkeypatch):
    assert Base.metadata.tables["crew_owner_generations"] is CrewOwnerGeneration.__table__
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'old.sqlite'}")
    metadata = sa.MetaData()
    sa.Table("crew_executors", metadata, sa.Column("tenant_id", sa.String), sa.Column("user_id", sa.String), sa.Column("generation", sa.Integer))
    sa.Table("dev_boxes", metadata, sa.Column("org_id", sa.String), sa.Column("user_id", sa.String), sa.Column("machine_generation", sa.Integer))
    metadata.create_all(engine)
    migration_path = Path(__file__).resolve().parents[2] / "alembic/versions/102_crew_owner_generation.py"
    spec = importlib.util.spec_from_file_location("crew_owner_generation_migration", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("INSERT INTO crew_executors VALUES ('tenant-a', 'owner-a', 7), ('tenant-b', 'owner-a', 2)"))
            connection.execute(sa.text("INSERT INTO dev_boxes VALUES ('tenant-a', 'owner-a', 5), ('tenant-b', 'owner-a', 9)"))
            monkeypatch.setattr(module, "op", Operations(MigrationContext.configure(connection)))
            module.upgrade()
            assert connection.execute(sa.text("SELECT tenant_id, user_id, generation FROM crew_owner_generations ORDER BY tenant_id")).all() == [
                ("tenant-a", "owner-a", 7),
                ("tenant-b", "owner-a", 9),
            ]
            module.downgrade()
            assert "crew_owner_generations" not in sa.inspect(connection).get_table_names()
    finally:
        engine.dispose()
