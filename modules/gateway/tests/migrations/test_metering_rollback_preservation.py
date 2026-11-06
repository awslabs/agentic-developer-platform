"""Current-head rollback cannot remove unresolved authority or its evidence."""

import psycopg2
import pytest
from psycopg2 import sql

from alembic.config import Config
from alembic.script import ScriptDirectory
from tests.migrations.conftest_postgres import GATEWAY_ROOT, run_alembic, upgrade
from tests.migrations.head_contract import HEAD_DOWNGRADE_REFUSAL


@pytest.mark.parametrize(
    "starting_revision,barrier",
    [
        ("119_metering_binding_fence", "Metering identity and membership fences must survive rollback"),
        ("head", HEAD_DOWNGRADE_REFUSAL),
    ],
)
def test_current_head_refusal_and_reupgrade_preserve_metering_rows(pg_url, starting_revision, barrier):
    upgrade(pg_url, starting_revision)
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    expected_head = ScriptDirectory.from_config(config).get_current_head()
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO metering_scope_gates (scope_key,scope_kind,tenant_id,activation,readiness) VALUES "
            "('org:tenant','org','tenant','active','ready'), ('person:anchor','person',NULL,'active','ready')"
        )
        cursor.execute(
            "INSERT INTO metering_attempts "
            "(tenant_id,request_id,owner_id,model_id,request_sha256,submitted_sha256,pricing_revision,quote_snapshot,attribution_snapshot,"
            "bound_usd,state,revision,producer_epoch,prepare_deadline,completion_deadline,admission_key,final_key) "
            "VALUES ('tenant','unresolved','owner','test-model',%s,%s,'historical-price','{}','{}',0.002,'unknown',2,3,NOW(),NOW(),"
            "'owned/admission','owned/final')",
            ("a" * 64, "b" * 64),
        )
        cursor.execute(
            "INSERT INTO metering_attempt_scopes (tenant_id,request_id,scope_key,allocation_period,bound_usd,captured_allocation) VALUES "
            "('tenant','unresolved','org:tenant','2026-10',0.002,'{}'), "
            "('tenant','unresolved','person:anchor','2026-10',0.002,'{}')"
        )
        cursor.execute(
            "INSERT INTO metering_scope_aliases (alias_kind,stable_alias,scope_key,identity_revision) "
            "VALUES ('user','owner','person:anchor','identity-1')"
        )
        cursor.execute(
            "INSERT INTO metering_reconciliation_events "
            "(id,tenant_id,request_id,idempotency_key,payload_sha256,actor_id,authority,prior_revision,new_revision,"
            "action,reason,evidence_ref,evidence_sha256,disposition) "
            "VALUES ('event-1','tenant','unresolved','recovery-1',%s,'recovery','scoped-recovery',1,2,"
            "'mark_unknown','receipt unavailable','owned/evidence',%s,'unknown')",
            ("c" * 64, "d" * 64),
        )
        cursor.execute(
            "INSERT INTO metering_legacy_admissions (tenant_id,authenticated_tenant_id,request_id,owner_id) "
            "VALUES ('tenant','source-tenant','pending-legacy','owner')"
        )

    expected_counts = {
        "metering_scope_gates": 2,
        "metering_attempts": 1,
        "metering_legacy_admissions": 1,
        "metering_attempt_scopes": 2,
        "metering_scope_aliases": 1,
        "metering_reconciliation_events": 1,
    }

    def snapshot(expected_revision):
        with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
            rows = {}
            for table, count in expected_counts.items():
                cursor.execute(sql.SQL("SELECT to_jsonb(t) FROM {} AS t ORDER BY to_jsonb(t)::text").format(sql.Identifier(table)))
                rows[table] = cursor.fetchall()
                assert len(rows[table]) == count
            cursor.execute("SELECT version_num FROM alembic_version ORDER BY version_num")
            assert cursor.fetchall() == [(expected_revision,)]
            return rows

    expected_revision = expected_head if starting_revision == "head" else starting_revision
    before = snapshot(expected_revision)
    result = run_alembic(pg_url, "downgrade", "112_product_profile_foundation")
    assert result.returncode != 0
    assert barrier in result.stdout + result.stderr
    assert snapshot(expected_revision) == before
    upgrade(pg_url, "head")
    assert snapshot(expected_head) == before
