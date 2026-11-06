"""Either published parent upgrades without losing catalog or accounting evidence."""

import psycopg2
import pytest
from psycopg2 import sql

from tests.migrations.conftest_postgres import run_alembic, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD, HEAD_DOWNGRADE_REFUSAL

METERING_TABLES = ("metering_scope_gates", "metering_attempts", "metering_attempt_scopes", "budget_settlement_receipts")
CATALOG_TABLES = (
    "catalog_initiative_versions",
    "catalog_initiative_heads",
    "catalog_contribution_versions",
    "catalog_contribution_heads",
    "catalog_mutation_receipts",
)


def seed_metering(cursor):
    cursor.execute("INSERT INTO metering_scope_gates (scope_key,scope_kind,tenant_id) VALUES ('org:tenant','org','tenant')")
    cursor.execute(
        "INSERT INTO metering_attempts "
        "(tenant_id,request_id,owner_id,model_id,request_sha256,submitted_sha256,pricing_revision,quote_snapshot,attribution_snapshot,"
        "bound_usd,state,prepare_deadline,completion_deadline,admission_key,final_key) "
        "VALUES ('tenant','unknown-request','owner','model',%s,%s,'price-1','{}','{}',0.002,'unknown',NOW(),NOW(),'admission','final')",
        ("a" * 64, "b" * 64),
    )
    cursor.execute(
        "INSERT INTO metering_attempt_scopes (tenant_id,request_id,scope_key,allocation_period,bound_usd,captured_allocation) "
        "VALUES ('tenant','unknown-request','org:tenant','2026-10',0.002,'{}')"
    )
    cursor.execute(
        "INSERT INTO budget_settlement_receipts (org_id,request_id,user_id,cost_usd,total_tokens,allocation_key) "
        "VALUES ('tenant','settled-request','owner',0.01,6,%s)",
        ("c" * 64,),
    )


def seed_catalog(cursor):
    cursor.execute(
        "INSERT INTO catalog_initiative_versions (tenant_id,source_connection_id,id,revision,data,recorded_at) "
        "VALUES ('tenant','adp.catalog','initiative',1,'{}',NOW())"
    )
    cursor.execute("INSERT INTO catalog_initiative_heads (tenant_id,source_connection_id,id,revision) VALUES ('tenant','adp.catalog','initiative',1)")
    cursor.execute(
        "INSERT INTO catalog_contribution_versions "
        "(tenant_id,source_connection_id,id,revision,project_id,initiative_source,initiative_id,initiative_revision,"
        "product_source,product_id,data,work_links,valid_from,recorded_at) "
        "VALUES ('tenant','adp.catalog','contribution',1,'project','adp.catalog','initiative',1,'adp.profile','product','{}','[]',NOW(),NOW())"
    )
    cursor.execute(
        "INSERT INTO catalog_contribution_heads "
        "(tenant_id,source_connection_id,id,revision,project_id,initiative_source,initiative_id,product_source,product_id) "
        "VALUES ('tenant','adp.catalog','contribution',1,'project','adp.catalog','initiative','adp.profile','product')"
    )
    cursor.execute(
        "INSERT INTO catalog_mutation_receipts (tenant_id,actor_id,operation,idempotency_key,payload_digest,outcome,recorded_at) "
        "VALUES ('tenant','editor','create_initiative','retained-receipt',%s,'{}',NOW())",
        ("d" * 64,),
    )


@pytest.mark.parametrize(
    "starting_revision",
    [
        "119_metering_binding_fence",
        "116_catalog_initiatives",
        "118_product_verification_ops",
        "120_merge_metering_initiatives",
    ],
)
def test_parent_upgrade_and_refused_rollback_preserve_both_histories(pg_url, starting_revision):
    upgrade(pg_url, starting_revision)
    starts_with_metering = starting_revision in ("119_metering_binding_fence", "120_merge_metering_initiatives")
    existing_tables = METERING_TABLES if starts_with_metering else CATALOG_TABLES
    seed_existing = seed_metering if starts_with_metering else seed_catalog
    seed_incoming = seed_catalog if starts_with_metering else seed_metering
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(starting_revision,)]
        seed_existing(cursor)

    def snapshot(tables):
        with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
            rows = {}
            for table in tables:
                cursor.execute(sql.SQL("SELECT to_jsonb(record) FROM {} AS record ORDER BY to_jsonb(record)::text").format(sql.Identifier(table)))
                rows[table] = cursor.fetchall()
                assert len(rows[table]) == 1
            return rows

    before_upgrade = snapshot(existing_tables)
    upgrade(pg_url, "head")
    assert snapshot(existing_tables) == before_upgrade
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(GATEWAY_HEAD,)]
        cursor.execute("SELECT to_regclass('product_onboarding_sessions'), to_regclass('metering_reconciliation_events')")
        assert cursor.fetchone() == ("product_onboarding_sessions", "metering_reconciliation_events")
        cursor.execute("SELECT to_regclass('product_source_bindings')")
        assert cursor.fetchone() == ("product_source_bindings",)
        cursor.execute("SELECT column_name FROM information_schema.columns WHERE table_name='product_operations' AND column_name='lease_owner'")
        assert cursor.fetchall() == [("lease_owner",)]
        cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname = 'trg_metering_user_deletion_guard'")
        assert cursor.fetchall() == [("trg_metering_user_deletion_guard",)]
        seed_incoming(cursor)

    before_rollback = snapshot(METERING_TABLES + CATALOG_TABLES)
    refused = run_alembic(pg_url, "downgrade", "112_product_profile_foundation")
    assert refused.returncode != 0
    assert HEAD_DOWNGRADE_REFUSAL in refused.stderr
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(GATEWAY_HEAD,)]
    assert snapshot(METERING_TABLES + CATALOG_TABLES) == before_rollback
    upgrade(pg_url, "head")
    assert snapshot(METERING_TABLES + CATALOG_TABLES) == before_rollback
