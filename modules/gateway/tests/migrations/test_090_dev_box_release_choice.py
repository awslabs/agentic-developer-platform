"""Release retention is stored on the canonical Dev Box operation schema."""

from sqlalchemy import create_engine, inspect

from tests.migrations.conftest_postgres import downgrade, upgrade


def test_release_choice_migration_and_rollback(pg_url):
    upgrade(pg_url, "089_merge_dev_box_crew")
    engine = create_engine(pg_url)
    try:
        upgrade(pg_url, "090_dev_box_release_choice")
        assert "retention_choice" in {column["name"] for column in inspect(engine).get_columns("dev_box_operations")}
        downgrade(pg_url, "089_merge_dev_box_crew")
        assert "retention_choice" not in {column["name"] for column in inspect(engine).get_columns("dev_box_operations")}
    finally:
        engine.dispose()


def test_release_choice_column_offline_sqlite(monkeypatch):
    import importlib.util
    from pathlib import Path

    from sqlalchemy import Column, MetaData, String, Table

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "090_dev_box_release_choice.py"
    spec = importlib.util.spec_from_file_location("dev_box_release_choice_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    Table("dev_box_operations", metadata, Column("id", String(36), primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert "retention_choice" in {column["name"] for column in inspect(connection).get_columns("dev_box_operations")}
        migration.downgrade()
        assert "retention_choice" not in {column["name"] for column in inspect(connection).get_columns("dev_box_operations")}
    engine.dispose()
