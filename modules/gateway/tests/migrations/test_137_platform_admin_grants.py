"""Rollback/re-upgrade must not erase a committed administrator revocation."""

from datetime import UTC, datetime
from pathlib import Path
from runpy import run_path

from sqlalchemy import create_engine, insert, select

from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from src.shared.models.platform_admin import PlatformAdminGrant


def assert_retained_revocation(engine):
    path = Path(__file__).resolve().parents[2] / "alembic/versions/137_platform_admin_grants.py"
    migration = run_path(str(path))
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        migration["upgrade"]()
        connection.execute(insert(PlatformAdminGrant).values(cognito_sub="revoked-login", enabled=False, updated_at=datetime.now(UTC)))
        migration["downgrade"]()
        migration["upgrade"]()
        assert connection.scalar(select(PlatformAdminGrant.enabled).where(PlatformAdminGrant.cognito_sub == "revoked-login")) is False
    engine.dispose()


def test_revocation_survives_migration_rollback_and_reupgrade():
    assert_retained_revocation(create_engine("sqlite:///:memory:"))


def test_postgres_revocation_survives_migration_rollback_and_reupgrade(pg_url):
    assert_retained_revocation(create_engine(pg_url))
