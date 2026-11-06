"""Additive metadata migration neither invents consent nor qualifies legacy connections."""

import importlib.util
from pathlib import Path

from sqlalchemy import JSON, Column, MetaData, String, Table, create_engine, select

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_existing_attempts_and_bindings_keep_their_authority(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/111_oauth_reconnect_resources.py"
    spec = importlib.util.spec_from_file_location("oauth_reconnect_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = create_engine("sqlite://")
    metadata = MetaData()
    attempts = Table("oauth_connection_attempts", metadata, Column("id", String, primary_key=True), Column("requested_scopes", JSON))
    bindings = Table("oauth_connection_bindings", metadata, Column("id", String, primary_key=True), Column("provider_account_id", String))
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(attempts.insert().values(id="attempt", requested_scopes=["read:jira-work"]))
        connection.execute(bindings.insert().values(id="binding", provider_account_id="existing-account"))
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        current_attempts = Table(attempts.name, MetaData(), autoload_with=connection)
        current_bindings = Table(bindings.name, MetaData(), autoload_with=connection)
        assert connection.execute(select(current_attempts)).one() == ("attempt", ["read:jira-work"], None)
        assert connection.execute(select(current_bindings)).one() == ("binding", "existing-account", None)
        migration.downgrade()
        assert connection.execute(select(attempts)).one() == ("attempt", ["read:jira-work"])
        assert connection.execute(select(bindings)).one() == ("binding", "existing-account")
    engine.dispose()
