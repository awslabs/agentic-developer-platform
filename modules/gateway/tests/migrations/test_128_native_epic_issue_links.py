"""Native parent progress and numeric publication identity stay additive."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic.migration import MigrationContext
from alembic.operations import Operations
from tests.migrations.conftest_postgres import downgrade, upgrade

GATEWAY = Path(__file__).resolve().parents[2]
MIGRATION = GATEWAY / "alembic/versions/128_native_epic_issue_links.py"
TABLE = "orchestration_amendment_issue_bindings"


def load_migration():
    spec = importlib.util.spec_from_file_location("native_epic_128", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sqlite_upgrade_refuses_duplicate_numeric_identity_and_preserves_legacy_rows():
    migration = load_migration()
    assert migration.down_revision == "127_authoring_assignment"
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                f"CREATE TABLE {TABLE} (id TEXT PRIMARY KEY, org_id TEXT NOT NULL, repository TEXT NOT NULL, "
                "proposal_identity TEXT NOT NULL, provider_repository_id BIGINT, issue_number INTEGER)"
            )
        )
        connection.execute(
            sa.text(
                f"INSERT INTO {TABLE} VALUES ('old', 'tenant', 'acme/old', 'story', 101, 42), ('renamed', 'tenant', 'acme/new', 'story', 101, 43)"
            )
        )
        with Operations.context(MigrationContext.configure(connection)):
            with pytest.raises(RuntimeError, match="Conflicting provider repository"):
                migration.upgrade()
        connection.execute(sa.text(f"DELETE FROM {TABLE} WHERE id='renamed'"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        indexes = {item["name"] for item in sa.inspect(connection).get_indexes(TABLE)}
        assert migration.INDEX in indexes
        assert connection.execute(sa.text(f"SELECT issue_number, link_confirmed_at FROM {TABLE}")).one() == (42, None)
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
        assert {"provider_repository_id", "issue_number"} <= {column["name"] for column in sa.inspect(connection).get_columns(TABLE)}
        assert connection.scalar(sa.text(f"SELECT issue_number FROM {TABLE}")) == 42
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
        assert migration.INDEX in {item["name"] for item in sa.inspect(connection).get_indexes(TABLE)}
    engine.dispose()


def test_postgres_upgrade_downgrade_reupgrade_preserves_legacy_binding(pg_url):
    upgrade(pg_url, "127_authoring_assignment")
    engine = sa.create_engine(pg_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"INSERT INTO {TABLE} "
                    "(id,org_id,repository,proposal_identity,provider_repository_id,state,claim_token,"
                    "claimed_at,content_hash,issue_number,created_at) "
                    "VALUES ('legacy','example','acme/old','story',101,'created','claim',now(),'hash',42,now())"
                )
            )
        for revision in ("128_native_epic_issue_links", "127_authoring_assignment", "128_native_epic_issue_links"):
            if revision == "127_authoring_assignment":
                downgrade(pg_url, revision)
            else:
                upgrade(pg_url, revision)
            with engine.connect() as connection:
                assert connection.scalar(sa.text(f"SELECT issue_number FROM {TABLE} WHERE id='legacy'")) == 42
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == revision
        with engine.connect() as connection:
            assert connection.scalar(sa.text(f"SELECT link_confirmed_at FROM {TABLE} WHERE id='legacy'")) is None
            assert "parent_issue_id" in {column["name"] for column in sa.inspect(connection).get_columns(TABLE)}
    finally:
        engine.dispose()
