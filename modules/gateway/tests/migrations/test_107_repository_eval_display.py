"""SCN-8176-04: V2 indexes existing refusals without changing V1 or the ledger."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from importlib.resources import files

import psycopg2
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.orchestration.decision_projection import projection_sql
from src.orchestration.models import OrchestrationDecision
from tests.migrations.conftest_postgres import downgrade, run_alembic, to_async_url, upgrade
from tests.migrations.legacy_orchestration import seed_flow_node
from tests.orchestration.decision_projection_fixture import apply_migration


async def test_existing_refusal_index_upgrade_recovery_and_safe_downgrade(pg_url):
    upgrade(pg_url, "105_flow_cost_covering")
    engine = create_async_engine(to_async_url(pg_url))
    connection = psycopg2.connect(pg_url)
    connection.autocommit = True
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            flow, node = await seed_flow_node(session, slug="repository-refusal-migration", node_ref="eval", kind="eval")
            for number in range(2):
                session.add(
                    OrchestrationDecision(
                        id=f"{number:036d}",
                        org_id=flow.org_id,
                        flow_id=flow.id,
                        node_id=node.id,
                        actor_id="system:repository-evaluation",
                        actor_kind="service",
                        actor_role="engine",
                        kind="transition_rejected",
                        rejection_reason='{"action":"repository_evaluation_blocked"}',
                    )
                )
            await session.commit()
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, rejection_reason FROM orchestration_decisions ORDER BY id")
            before = cursor.fetchall()
            cursor.execute("SELECT adp_decision_category_v1(kind, actor_id, actor_kind, reason, rejection_reason) FROM orchestration_decisions")
            assert cursor.fetchall() == [(None,), (None,)]
        # A canceled build of this exact index leaves the artifact seen when an
        # ordinary old snapshot outlasts the previous one-second lock timeout.
        leave_failed_index(pg_url, connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT indisvalid, indisready FROM pg_index WHERE indexrelid=to_regclass('ix_decision_display_category_v2')")
            assert cursor.fetchone() == (False, True)
        upgrade(pg_url, "107_repository_eval_display")
        # The migration can safely resume after a successful concurrent build.
        async with engine.connect() as database:
            await database.run_sync(apply_migration)
        with connection.cursor() as cursor:
            cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('ix_decision_display_category_v2')")
            assert cursor.fetchone() == (True,)
            cursor.execute(
                "SELECT adp_decision_category_v1(kind, actor_id, actor_kind, reason, rejection_reason), "
                "adp_decision_category_v2(kind, actor_id, actor_kind, reason, rejection_reason) FROM orchestration_decisions"
            )
            assert cursor.fetchall() == [(None, 9), (None, 9)]
            query = projection_sql().replace(":org_id", "%(org_id)s").replace(":flow_id", "%(flow_id)s").replace(":node_ids", "%(node_ids)s")
            cursor.execute(query, {"org_id": flow.org_id, "flow_id": flow.id, "node_ids": json.dumps([node.id])})
            assert len(cursor.fetchall()) == 1
        downgrade(pg_url, "105_flow_cost_covering")
        with connection.cursor() as cursor:
            cursor.execute("SELECT id, rejection_reason FROM orchestration_decisions ORDER BY id")
            assert cursor.fetchall() == before
            cursor.execute("SELECT to_regclass('ix_decision_display_category'), to_regclass('ix_decision_display_category_v2')")
            assert cursor.fetchone() == ("ix_decision_display_category", None)
            cursor.execute("SELECT to_regprocedure('adp_decision_category_v2(text,text,text,text,text)')")
            assert cursor.fetchone() == (None,)
    finally:
        connection.close()
        await engine.dispose()


CREATE_INDEX = """CREATE INDEX CONCURRENTLY ix_decision_display_category_v2 ON orchestration_decisions
    (org_id, flow_id, node_id, (adp_decision_category_v2(kind, actor_id, actor_kind, reason, rejection_reason)),
     created_at DESC, id COLLATE "C" DESC)
    WHERE adp_decision_category_v2(kind, actor_id, actor_kind, reason, rejection_reason) IS NOT NULL"""


def install_function(connection):
    with connection.cursor() as cursor:
        cursor.execute(files("src.orchestration").joinpath("decision_repository_projection.sql").read_text())


def leave_failed_index(pg_url, connection):
    install_function(connection)
    snapshot = psycopg2.connect(pg_url)
    try:
        with snapshot.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            cursor.execute("SELECT count(*) FROM orchestration_decisions")
        with connection.cursor() as cursor:
            cursor.execute("SET lock_timeout = '100ms'")
            try:
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    cursor.execute(CREATE_INDEX)
            finally:
                cursor.execute("RESET lock_timeout")
    finally:
        snapshot.rollback()
        snapshot.close()


@pytest.mark.parametrize("transaction", ["writer", "snapshot"])
def test_concurrent_repository_index_waits_for_ordinary_transactions(pg_url, transaction):
    upgrade(pg_url, "105_flow_cost_covering")
    writer = psycopg2.connect(pg_url)
    observer = psycopg2.connect(pg_url)
    observer.autocommit = True
    try:
        with writer.cursor() as cursor:
            if transaction == "writer":
                cursor.execute("LOCK TABLE orchestration_decisions IN ROW EXCLUSIVE MODE")
            else:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("SELECT count(*) FROM orchestration_decisions")
        with ThreadPoolExecutor(max_workers=1) as pool:
            migration = pool.submit(upgrade, pg_url, "107_repository_eval_display")
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with observer.cursor() as cursor:
                        cursor.execute("SELECT count(*) FROM pg_stat_progress_create_index WHERE relid='orchestration_decisions'::regclass")
                        if cursor.fetchone()[0]:
                            break
                    if migration.done():
                        migration.result()
                        pytest.fail("Migration completed before the transaction released its snapshot/lock")
                    time.sleep(0.05)
                else:
                    pytest.fail("Concurrent repository index did not start within the fixture deadline")
                time.sleep(1.5)
                assert not migration.done(), "Normal transactions longer than one second must not abort this migration"
            finally:
                writer.rollback()
            migration.result(timeout=30)
        with observer.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("107_repository_eval_display",)
            cursor.execute("SELECT indisvalid, indisready FROM pg_index WHERE indexrelid=to_regclass('ix_decision_display_category_v2')")
            assert cursor.fetchone() == (True, True)
    finally:
        writer.close()
        observer.close()


@pytest.mark.parametrize("conflict", ["wrong-definition", "wrong-table", "wrong-invalid", "wrong-schema", "ambiguous-schema"])
def test_repository_migration_refuses_unowned_index_without_changing_it(pg_url, conflict):
    upgrade(pg_url, "105_flow_cost_covering")
    connection = psycopg2.connect(pg_url)
    connection.autocommit = True
    try:
        install_function(connection)
        with connection.cursor() as cursor:
            if conflict == "wrong-invalid":
                cursor.execute("CREATE TABLE fixture (id integer)")
                cursor.execute("INSERT INTO fixture VALUES (1), (1)")
                with pytest.raises(psycopg2.errors.UniqueViolation):
                    cursor.execute("CREATE UNIQUE INDEX CONCURRENTLY ix_decision_display_category_v2 ON fixture (id)")
            elif conflict in {"wrong-definition", "wrong-table"}:
                table = "orchestration_decisions" if conflict == "wrong-definition" else "usage_logs"
                cursor.execute(f"CREATE INDEX ix_decision_display_category_v2 ON {table} (id)")
            else:
                cursor.execute("CREATE SCHEMA unrelated")
                cursor.execute("CREATE TABLE unrelated.fixture (id integer)")
                cursor.execute("CREATE INDEX ix_decision_display_category_v2 ON unrelated.fixture (id)")
                if conflict == "ambiguous-schema":
                    cursor.execute(CREATE_INDEX)
            query = """SELECT c.oid, n.nspname, pg_get_indexdef(c.oid), i.indisvalid, i.indisready
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                JOIN pg_index i ON i.indexrelid=c.oid
                WHERE c.relname IN ('ix_decision_display_category','ix_decision_display_category_v2') ORDER BY c.oid"""
            cursor.execute(query)
            before = cursor.fetchall()
        result = run_alembic(pg_url, "upgrade", "107_repository_eval_display")
        assert result.returncode != 0
        assert "index identity or definition differs" in result.stderr
        with connection.cursor() as cursor:
            cursor.execute(query)
            assert cursor.fetchall() == before
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone() == ("105_flow_cost_covering",)
    finally:
        connection.close()


@pytest.mark.parametrize("failure", [False, True])
async def test_repository_index_restores_session_timeouts_after_success_or_refusal(pg_url, failure):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from tests.orchestration.decision_projection_fixture import migration as migration_module

    upgrade(pg_url, "105_flow_cost_covering")
    engine = create_async_engine(to_async_url(pg_url))
    try:
        async with engine.connect() as database:

            def apply(connection):
                context = MigrationContext.configure(connection)
                with Operations.context(context), context.begin_transaction():
                    module = migration_module()
                    connection.exec_driver_sql("SET lock_timeout = '17s'")
                    connection.exec_driver_sql("SET statement_timeout = '7min'")
                    if failure:
                        # An independently owned definition must refuse inside the
                        # autocommit block too, exercising its timeout restoration.
                        connection.exec_driver_sql("CREATE INDEX ix_decision_display_category_v2 ON orchestration_decisions (id)")
                        with pytest.raises(RuntimeError, match="identity or definition differs"):
                            module._index()
                    else:
                        module.upgrade()
                    assert connection.exec_driver_sql("SHOW lock_timeout").scalar_one() == "17s"
                    assert connection.exec_driver_sql("SHOW statement_timeout").scalar_one() == "7min"

            await database.run_sync(apply)
    finally:
        await engine.dispose()


async def test_repository_index_timeout_is_bounded_restores_settings_and_can_recover(pg_url):
    from sqlalchemy.exc import DBAPIError

    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from tests.orchestration.decision_projection_fixture import migration as migration_module

    upgrade(pg_url, "105_flow_cost_covering")
    writer = psycopg2.connect(pg_url)
    engine = create_async_engine(to_async_url(pg_url))
    try:
        with writer.cursor() as cursor:
            cursor.execute("LOCK TABLE orchestration_decisions IN ROW EXCLUSIVE MODE")
        async with engine.connect() as database:

            def apply(connection):
                context = MigrationContext.configure(connection)
                with Operations.context(context), context.begin_transaction():
                    module = migration_module()
                    connection.exec_driver_sql("SET lock_timeout = '17s'")
                    connection.exec_driver_sql("SET statement_timeout = '7min'")
                    started = time.monotonic()
                    with pytest.raises(DBAPIError) as failure:
                        module.upgrade()
                    assert failure.value.orig.sqlstate == "55P03"
                    assert 25 <= time.monotonic() - started < 45, "The production lock wait must remain bounded at 30 seconds"
                    assert connection.exec_driver_sql("SHOW lock_timeout").scalar_one() == "17s"
                    assert connection.exec_driver_sql("SHOW statement_timeout").scalar_one() == "7min"
                    assert module._existing_index()["valid"] is False
                    writer.rollback()
                    module.upgrade()
                    assert module._existing_index()["valid"] is True
                    assert connection.exec_driver_sql("SHOW lock_timeout").scalar_one() == "17s"
                    assert connection.exec_driver_sql("SHOW statement_timeout").scalar_one() == "7min"

            await database.run_sync(apply)
    finally:
        writer.rollback()
        writer.close()
        await engine.dispose()
