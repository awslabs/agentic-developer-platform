"""Nullable Product and story repository projections keep earlier rows intact."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import run_alembic, upgrade

GATEWAY = Path(__file__).resolve().parents[2]
MIGRATION = GATEWAY / "alembic/versions/124_flow_product_repo.py"
LEGACY_DOCUMENT = '{"nodes":[{"address":"legacy/epic/wave/story","issue_ref":"42"}],"org_id":"tenant-1"}'
LEGACY_HASH = "3" * 64
LEGACY_DECISION = "decision-1"


def load_migration():
    spec = importlib.util.spec_from_file_location("migration_124", MIGRATION)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    return migration


def test_revision_fits_postgres_version_column_and_remains_in_chain():
    migration = load_migration()
    config = Config(str(GATEWAY / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY / "alembic"))
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_revision(migration.revision) is not None
    assert migration.down_revision == "123_dev_box_release_pins"
    assert len(migration.revision) <= 32


def test_postgres_offline_sql_adds_only_nullable_columns_and_nonunique_indexes():
    upgrade = run_alembic(
        "postgresql://example:example@localhost/example",
        "upgrade",
        "123_dev_box_release_pins:124_flow_product_repo",
        "--sql",
    )
    assert upgrade.returncode == 0, upgrade.stderr
    assert "ALTER TABLE orchestration_flows ADD COLUMN product_id VARCHAR(36);" in upgrade.stdout
    assert "ALTER TABLE orchestration_nodes ADD COLUMN repository_identity VARCHAR(255);" in upgrade.stdout
    assert "ALTER TABLE orchestration_nodes ADD COLUMN provider_repository_id BIGINT;" in upgrade.stdout
    assert "ALTER TABLE orchestration_nodes ADD COLUMN repository_connection JSONB;" in upgrade.stdout
    assert "CREATE INDEX ix_orchestration_nodes_org_repository_issue" in upgrade.stdout
    assert "CREATE UNIQUE INDEX" not in upgrade.stdout
    assert "NOT NULL" not in upgrade.stdout


def test_additive_upgrade_preserves_existing_acceptance_and_attempt_rows():
    migration = load_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE orchestration_flows (id TEXT PRIMARY KEY, org_id TEXT NOT NULL)"))
        connection.execute(
            sa.text(
                "CREATE TABLE orchestration_nodes (id TEXT PRIMARY KEY, org_id TEXT NOT NULL, "
                "flow_id TEXT NOT NULL, issue_ref TEXT, attempts INTEGER)"
            )
        )
        connection.execute(
            sa.text("CREATE TABLE orchestration_accepted_plans (flow_id TEXT, plan_document TEXT, plan_hash TEXT, accepted_by_decision_id TEXT)")
        )
        connection.execute(sa.text("INSERT INTO orchestration_flows (id, org_id) VALUES ('flow-1', 'tenant-1')"))
        connection.execute(
            sa.text("INSERT INTO orchestration_nodes (id, org_id, flow_id, issue_ref, attempts) VALUES ('node-1', 'tenant-1', 'flow-1', '42', 2)")
        )
        connection.execute(
            sa.text(
                "INSERT INTO orchestration_accepted_plans (flow_id, plan_document, plan_hash, accepted_by_decision_id) "
                "VALUES ('flow-1', :document, :hash, :decision)"
            ),
            {"document": LEGACY_DOCUMENT, "hash": LEGACY_HASH, "decision": LEGACY_DECISION},
        )

        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()

        assert connection.execute(sa.text("SELECT product_id FROM orchestration_flows WHERE id = 'flow-1'")).scalar() is None
        assert connection.execute(
            sa.text(
                "SELECT repository_identity, provider_repository_id, repository_connection, issue_ref, attempts "
                "FROM orchestration_nodes WHERE id = 'node-1'"
            )
        ).one() == (None, None, None, "42", 2)
        assert connection.execute(
            sa.text("SELECT plan_document, plan_hash, accepted_by_decision_id FROM orchestration_accepted_plans WHERE flow_id = 'flow-1'")
        ).one() == (LEGACY_DOCUMENT, LEGACY_HASH, LEGACY_DECISION)
        assert {index["name"] for index in sa.inspect(connection).get_indexes("orchestration_nodes")} == {
            "ix_orchestration_nodes_org_repository_issue"
        }
    engine.dispose()


def test_existing_postgres_rows_survive_actual_upgrade(pg_url):
    import psycopg2

    upgrade(pg_url, "123_dev_box_release_pins")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO orchestration_flows (id, org_id, slug, title) VALUES (%s, %s, %s, %s)",
            ("flow-1", "tenant-1", "legacy", "Legacy flow"),
        )
        cursor.execute(
            "INSERT INTO orchestration_nodes (id, org_id, flow_id, epic_ref, wave_ref, node_ref, kind, state, title, issue_ref, attempts) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            ("node-1", "tenant-1", "flow-1", "epic", "wave", "story", "story", "ready", "Legacy", "42", 2),
        )
        cursor.execute(
            "INSERT INTO orchestration_decisions (id, org_id, flow_id, kind, actor_id, actor_role, actor_kind) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (LEGACY_DECISION, "tenant-1", "flow-1", "plan_accepted", "operator", "org_admin", "human"),
        )
        cursor.execute(
            "INSERT INTO orchestration_accepted_plans (id, org_id, flow_id, version, plan_document, plan_hash, accepted_by_decision_id) "
            "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s)",
            ("plan-1", "tenant-1", "flow-1", 1, LEGACY_DOCUMENT, LEGACY_HASH, LEGACY_DECISION),
        )
        cursor.execute("SELECT plan_document::text, plan_hash, accepted_by_decision_id FROM orchestration_accepted_plans WHERE id = 'plan-1'")
        accepted_before = cursor.fetchone()

    upgrade(pg_url, "124_flow_product_repo")

    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT product_id FROM orchestration_flows WHERE id = 'flow-1'")
        assert cursor.fetchone() == (None,)
        cursor.execute(
            "SELECT repository_identity, provider_repository_id, repository_connection, issue_ref, attempts "
            "FROM orchestration_nodes WHERE id = 'node-1'"
        )
        assert cursor.fetchone() == (None, None, None, "42", 2)
        cursor.execute("SELECT plan_document::text, plan_hash, accepted_by_decision_id FROM orchestration_accepted_plans WHERE id = 'plan-1'")
        assert cursor.fetchone() == accepted_before
        cursor.execute(
            "SELECT column_name, is_nullable, data_type FROM information_schema.columns "
            "WHERE table_name = 'orchestration_nodes' AND column_name IN ('repository_identity', 'provider_repository_id', 'repository_connection')"
        )
        assert set(cursor.fetchall()) == {
            ("repository_identity", "YES", "character varying"),
            ("provider_repository_id", "YES", "bigint"),
            ("repository_connection", "YES", "jsonb"),
        }
