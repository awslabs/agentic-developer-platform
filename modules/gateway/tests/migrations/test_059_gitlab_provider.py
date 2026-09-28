"""Exercise the provider constraint and rollback against real PostgreSQL."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_gitlab_provider_upgrade_and_lossless_rollback_refusal(pg_url):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/059_gitlab_identity_provider.py"
    spec = importlib.util.spec_from_file_location("gitlab_provider_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "CREATE TABLE user_identities (id text primary key, provider text NOT NULL, "
                "CONSTRAINT ck_user_identities_provider CHECK (provider IN ('github', 'directory')))"
            )
            connection.exec_driver_sql("INSERT INTO user_identities VALUES ('existing', 'directory')")
            with Operations.context(MigrationContext.configure(connection)):
                migration.upgrade()
            connection.exec_driver_sql("INSERT INTO user_identities VALUES ('gitlab-human', 'gitlab')")
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                with Operations.context(MigrationContext.configure(connection)):
                    migration.downgrade()
        with engine.begin() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM user_identities").scalar() == 2
            connection.exec_driver_sql("INSERT INTO user_identities VALUES ('another', 'gitlab')")
        with pytest.raises(IntegrityError):
            with engine.begin() as connection:
                connection.exec_driver_sql("INSERT INTO user_identities VALUES ('invalid', 'unknown')")
    finally:
        engine.dispose()
