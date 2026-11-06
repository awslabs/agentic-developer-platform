"""Metering schema persists scoped exposure and rejects unsafe state."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from alembic.config import Config
from alembic.script import ScriptDirectory
from src.shared.models.metering import MeteringAttempt, MeteringAttemptScope, MeteringReconciliationEvent, MeteringScopeAlias, MeteringScopeGate
from tests.migrations.conftest_postgres import GATEWAY_ROOT, upgrade
from tests.migrations.head_contract import GATEWAY_HEAD


@pytest.fixture
def migration_head():
    config = Config(str(GATEWAY_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(GATEWAY_ROOT / "alembic"))
    return ScriptDirectory.from_config(config)


def test_metering_and_product_scope_migrations_converge(migration_head):
    assert migration_head.get_current_head() == GATEWAY_HEAD
    assert migration_head.get_revision("120_merge_metering_initiatives").down_revision == (
        "119_metering_binding_fence",
        "116_catalog_initiatives",
    )
    assert migration_head.get_revision("119_metering_binding_fence").down_revision == "118_merge_metering_onboarding"
    assert migration_head.get_revision("114_merge_metering_product_scope").down_revision == ("113_metering_authority", "113_product_scope_guard")
    assert migration_head.get_revision("113_metering_authority").down_revision == "112_product_profile_foundation"


def test_postgres_upgrade_installs_all_metering_tables(pg_url):
    import psycopg2

    upgrade(pg_url, "head")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'metering_%%'")
        assert {row[0] for row in cursor.fetchall()} == {
            "metering_legacy_admissions",
            "metering_attempts",
            "metering_attempt_scopes",
            "metering_scope_gates",
            "metering_scope_aliases",
            "metering_reconciliation_events",
        }
        cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename = 'metering_attempts'")
        assert "ix_metering_attempt_due" in {row[0] for row in cursor.fetchall()}
        cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname = 'product_scope_product_guard'")
        assert cursor.fetchall() == [("product_scope_product_guard",)]


def test_postgres_constraints_keep_receipts_and_tenant_links_intact(pg_url):
    import psycopg2

    upgrade(pg_url, "head")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("INSERT INTO metering_scope_gates (scope_key, scope_kind, tenant_id) VALUES ('org:tenant-1', 'org', 'tenant-1')")
        cursor.execute(
            "INSERT INTO metering_attempts "
            "(tenant_id,request_id,owner_id,model_id,request_sha256,submitted_sha256,pricing_revision,quote_snapshot,attribution_snapshot,"
            "bound_usd,state,prepare_deadline,completion_deadline,admission_key,final_key) "
            "VALUES ('tenant-1','request-1','owner-1','model-1',%s,%s,'rev-1','{}','{}',0.002,'prepared',NOW(),NOW(),"
            "'admission','final')",
            ("a" * 64, "b" * 64),
        )
        statement = (
            "INSERT INTO metering_attempts "
            "(tenant_id,request_id,owner_id,model_id,request_sha256,submitted_sha256,pricing_revision,quote_snapshot,attribution_snapshot,"
            "bound_usd,state,prepare_deadline,completion_deadline,admission_key,final_key) "
            "VALUES ('tenant-1',%s,'owner-1','model-1',%s,%s,'rev-1','{}','{}',%s,%s,NOW(),NOW(),'admission','final')"
        )
        for request_id, bound, state in (
            ("negative", Decimal("-0.001"), "prepared"),
            ("unacknowledged", Decimal("0.002"), "dispatch_armed"),
            ("unreceipted", Decimal("0.002"), "settled"),
        ):
            cursor.execute("SAVEPOINT invalid_attempt")
            with pytest.raises(psycopg2.errors.CheckViolation):
                cursor.execute(statement, (request_id, "a" * 64, "b" * 64, bound, state))
            cursor.execute("ROLLBACK TO SAVEPOINT invalid_attempt")
        cursor.execute("SAVEPOINT missing_tenant")
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            cursor.execute(
                "INSERT INTO metering_attempt_scopes "
                "(tenant_id,request_id,scope_key,allocation_period,bound_usd,captured_allocation) "
                "VALUES ('other-tenant','request-1','org:tenant-1','2026-10',0.002,'{}')"
            )
        cursor.execute("ROLLBACK TO SAVEPOINT missing_tenant")
        cursor.execute("SELECT state FROM metering_attempts WHERE tenant_id = 'tenant-1'")
        assert cursor.fetchall() == [("prepared",)]


async def test_metering_attempt_constraints_and_tenant_keys():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    tables = [
        MeteringScopeGate.__table__,
        MeteringAttempt.__table__,
        MeteringAttemptScope.__table__,
        MeteringScopeAlias.__table__,
        MeteringReconciliationEvent.__table__,
    ]
    now = datetime.now(UTC)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(lambda sync_connection: MeteringScopeGate.metadata.create_all(sync_connection, tables=tables))
            await connection.execute(insert(MeteringScopeGate).values(scope_key="org:tenant-1", scope_kind="org", tenant_id="tenant-1"))
            await connection.execute(insert(MeteringScopeGate).values(scope_key="person:anchor", scope_kind="person", tenant_id=None))
            await connection.execute(
                insert(MeteringAttempt).values(
                    tenant_id="tenant-1",
                    request_id="req-1",
                    owner_id="owner-1",
                    model_id="model-1",
                    request_sha256="a" * 64,
                    submitted_sha256="b" * 64,
                    pricing_revision="revision-1",
                    quote_snapshot={"bound": "0.002"},
                    attribution_snapshot={"org": "tenant-1"},
                    bound_usd=Decimal("0.002"),
                    state="prepared",
                    prepare_deadline=now + timedelta(seconds=5),
                    completion_deadline=now + timedelta(minutes=5),
                    admission_key="admission",
                    final_key="final",
                )
            )
            await connection.execute(
                insert(MeteringAttemptScope).values(
                    tenant_id="tenant-1",
                    request_id="req-1",
                    scope_key="org:tenant-1",
                    allocation_period="2026-10",
                    bound_usd=Decimal("0.002"),
                    captured_allocation={"kind": "org"},
                )
            )
        async with engine.connect() as connection:
            assert (await connection.execute(select(MeteringAttempt.bound_usd))).scalar_one() == Decimal("0.002000")
            assert (await connection.execute(select(MeteringAttemptScope.scope_key))).scalar_one() == "org:tenant-1"
        for changes in ({"bound_usd": Decimal("-0.001")}, {"state": "dispatch_armed"}, {"state": "settled"}):
            async with engine.connect() as connection:
                with pytest.raises(IntegrityError):
                    await connection.execute(
                        insert(MeteringAttempt).values(
                            dict(
                                tenant_id="tenant-1",
                                request_id="invalid-" + next(iter(changes)),
                                owner_id="owner-1",
                                model_id="model-1",
                                request_sha256="a" * 64,
                                submitted_sha256="b" * 64,
                                pricing_revision="revision-1",
                                quote_snapshot={},
                                attribution_snapshot={},
                                bound_usd=Decimal("0.002"),
                                state="prepared",
                                prepare_deadline=now + timedelta(seconds=5),
                                completion_deadline=now + timedelta(minutes=5),
                                admission_key="admission",
                                final_key="final",
                            )
                            | changes
                        )
                    )
                await connection.rollback()
        async with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(insert(MeteringScopeGate).values(scope_key="invalid-org", scope_kind="org", tenant_id=None))
            await connection.rollback()
    finally:
        await engine.dispose()


def test_postgres_upgrade_from_product_scope_head_keeps_both_schemas(pg_url):
    import psycopg2

    upgrade(pg_url, "113_product_scope_guard")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [("113_product_scope_guard",)]
    upgrade(pg_url, "head")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(GATEWAY_HEAD,)]
        cursor.execute("SELECT to_regclass('metering_attempts'), to_regclass('metering_scope_gates')")
        assert cursor.fetchone() == ("metering_attempts", "metering_scope_gates")
        cursor.execute("SELECT tgname FROM pg_trigger WHERE tgname = 'product_scope_product_guard'")
        assert cursor.fetchall() == [("product_scope_product_guard",)]


def test_legacy_cohort_downgrade_refuses_and_preserves_pending_evidence(pg_url):
    import psycopg2

    from tests.migrations.conftest_postgres import run_alembic

    upgrade(pg_url, "115_legacy_metering_cohort")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "INSERT INTO metering_legacy_admissions (tenant_id, authenticated_tenant_id, request_id, owner_id) "
            "VALUES ('tenant-1', 'tenant-2', 'pending-inference', 'owner-1')"
        )
    downgrade = run_alembic(pg_url, "downgrade", "114_merge_metering_product_scope")
    assert downgrade.returncode != 0
    assert "must survive rollback" in downgrade.stderr
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT tenant_id, authenticated_tenant_id, owner_id, state FROM metering_legacy_admissions WHERE request_id='pending-inference'"
        )
        assert cursor.fetchone() == ("tenant-1", "tenant-2", "owner-1", "pending")
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [("115_legacy_metering_cohort",)]


@pytest.mark.parametrize("starting_revision", ["114_catalog_portfolios", "115_product_onboarding_sessions", "116_metering_admin_result"])
def test_catalog_and_metering_heads_join_without_losing_either_schema(pg_url, starting_revision):
    import psycopg2

    upgrade(pg_url, starting_revision)
    upgrade(pg_url, "head")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchall() == [(GATEWAY_HEAD,)]
        cursor.execute("SELECT to_regclass('metering_attempts'), to_regclass('catalog_portfolio_versions')")
        assert cursor.fetchone() == ("metering_attempts", "catalog_portfolio_versions")
        cursor.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name='metering_reconciliation_events' AND column_name='result_snapshot'"
        )
        assert cursor.fetchall() == [("result_snapshot",)]
