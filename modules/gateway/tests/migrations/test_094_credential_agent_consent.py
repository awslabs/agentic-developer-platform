"""Existing credentials do not gain agent access when consent is added."""

import importlib.util
from pathlib import Path

from sqlalchemy import Column, MetaData, String, Table, create_engine, inspect, select

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_credential_consent_migration_defaults_existing_rows_off(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic" / "versions" / "094_credential_agent_consent.py"
    spec = importlib.util.spec_from_file_location("credential_agent_consent_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    credentials = Table("user_credentials", metadata, Column("id", String(36), primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(credentials.insert().values(id="old-credential"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        updated = Table("user_credentials", MetaData(), autoload_with=connection)
        assert connection.execute(select(updated.c.agent_consent)).scalar_one() is False
        migration.downgrade()
        assert "agent_consent" not in {column["name"] for column in inspect(connection).get_columns("user_credentials")}
    engine.dispose()
