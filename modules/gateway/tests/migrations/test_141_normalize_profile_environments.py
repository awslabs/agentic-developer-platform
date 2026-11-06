"""Environment backfill commits its revision and retains tenant-scoped source history."""

from datetime import UTC, datetime

import psycopg2
from psycopg2.extras import Json

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL


def test_environment_migration_extends_previous_head():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [GATEWAY_HEAD]
    assert scripts.get_revision("141_normalize_environments").down_revision == "140_merge_adapter_sources"


def test_postgres_environment_backfill_records_revision_and_preserves_history(pg_url):
    upgrade(pg_url, "140_merge_adapter_sources")
    observed_at = datetime(2030, 1, 1, tzinfo=UTC)
    expected = []
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        for tenant_id, kind in (("tenant-a", "legacy"), ("tenant-b", "typed"), ("tenant-c", "conflict")):
            reference = {
                "tenant_id": tenant_id,
                "project_id": "shared-project-id",
                "source_connection_id": "fixture-connection",
                "record_type": "environment",
                "record_id": "fixture-environment",
                "record_revision": "source-1",
            }
            current = {**reference, "record_revision": "source-2"}
            provenance = [{"source_refs": [reference], "observed_at": observed_at.isoformat()}]
            if kind == "legacy":
                configuration = {"basic": {"environments": [reference], "repositories": []}}
                normalized = {"basic": {"repositories": []}, "advanced": {"environments": [reference]}}
            elif kind == "typed":
                configuration = normalized = {"advanced": {"environments": [current]}}
            else:
                configuration = {"basic": {"environments": [reference]}, "advanced": {"environments": [current]}}
                normalized = {
                    "basic": {"environments": [reference]},
                    "advanced": {"environments": [current], "environment_conflicts": [{"legacy_ref": reference, "advanced_ref": current}]},
                }
            cursor.execute(
                "INSERT INTO organizations (id, name, aws_accounts, role_mappings, settings, created_at) VALUES (%s, %s, '[]', '{}', '{}', %s)",
                (tenant_id, tenant_id, observed_at),
            )
            cursor.execute(
                "INSERT INTO projects (tenant_id, id, name, owner_user_id, configuration, provenance, created_at, updated_at) "
                "VALUES (%s, 'shared-project-id', 'Fixture project', 'fixture-owner', %s, %s, %s, %s)",
                (tenant_id, Json(configuration), Json(provenance), observed_at, observed_at),
            )
            expected.append((tenant_id, "shared-project-id", normalized, provenance, observed_at))

    upgrade(pg_url, "head")
    upgrade(pg_url, "head")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(GATEWAY_HEAD,)]
        cursor.execute(
            "SELECT character_maximum_length FROM information_schema.columns WHERE table_name = 'alembic_version' AND column_name = 'version_num'"
        )
        assert cursor.fetchone() == (32,)
        cursor.execute("SELECT tenant_id, id, configuration, provenance, updated_at FROM projects ORDER BY tenant_id, id")
        assert cursor.fetchall() == expected

    refused = run_alembic(pg_url, "downgrade", "140_merge_adapter_sources")
    assert refused.returncode != 0
    assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(GATEWAY_HEAD,)]
        cursor.execute("SELECT tenant_id, id, configuration, provenance, updated_at FROM projects ORDER BY tenant_id, id")
        assert cursor.fetchall() == expected
