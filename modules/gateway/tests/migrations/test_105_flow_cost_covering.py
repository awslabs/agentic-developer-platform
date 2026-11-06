"""Covering cost index is usable, recoverable and leaves ledger values intact."""

from decimal import Decimal

import psycopg2
import pytest

from tests.migrations.conftest_postgres import downgrade, upgrade

INDEX = "ix_usage_flow_cost_covering"
QUERY = """SELECT graph_address, sum(cost_usd), sum(input_tokens + output_tokens), count(id)
    FROM usage_logs WHERE org_id = %s AND graph_address IS NOT NULL
    AND (graph_address = %s OR graph_address LIKE %s ESCAPE '\\') GROUP BY graph_address"""
PARAMS = ("fixture-a", "fixture_flow", "fixture\\_flow/%")


def test_cost_index_upgrade_recovery_and_rollback(pg_url):
    upgrade(pg_url, "105_flow_cost_covering")
    connection = psycopg2.connect(pg_url)
    connection.autocommit = True
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)", (INDEX,))
            assert cursor.fetchone() == (True,)
        downgrade(pg_url, "104_decision_display_projection")
        with connection.cursor() as cursor:
            cursor.execute("""INSERT INTO usage_logs
                (id, timestamp, department_id, team_id, user_id, account_type, model, input_tokens, output_tokens,
                 cost_usd, latency_ms, status_code, org_id, graph_address)
                SELECT 'owned-' || n, now(), 'fixture', 'fixture', 'fixture', 'human', 'fixture', 2, 3,
                       0.01, 1, 200, CASE WHEN n <= 100 THEN 'fixture-a' ELSE 'fixture-b' END,
                       CASE WHEN n <= 10 THEN 'fixture_flow/epic/wave/story'
                            WHEN n <= 20 THEN 'fixtureXflow/epic/wave/story'
                            WHEN n <= 100 THEN NULL ELSE 'fixture_flow/epic/wave/story' END
                FROM generate_series(1, 20000) AS n""")
            cursor.execute(QUERY, PARAMS)
            expected = cursor.fetchall()
            assert expected == [("fixture_flow/epic/wave/story", Decimal("0.100000"), 50, 10)]
            cursor.execute("SELECT count(*), sum(cost_usd), sum(input_tokens), sum(output_tokens) FROM usage_logs")
            ledger = cursor.fetchone()
            with pytest.raises(psycopg2.errors.UniqueViolation):
                cursor.execute(f"CREATE UNIQUE INDEX CONCURRENTLY {INDEX} ON usage_logs (org_id)")
            cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)", (INDEX,))
            assert cursor.fetchone() == (False,)
        upgrade(pg_url, "105_flow_cost_covering")
        with connection.cursor() as cursor:
            cursor.execute("VACUUM ANALYZE usage_logs")
            cursor.execute(QUERY, PARAMS)
            assert cursor.fetchall() == expected
            cursor.execute("EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + QUERY, PARAMS)
            plan = cursor.fetchone()[0][0]["Plan"]

            def nodes(node):
                yield node
                for child in node.get("Plans", []):
                    yield from nodes(child)

            scans = [node for node in nodes(plan) if node.get("Index Name") == INDEX]
            assert scans and all(node["Node Type"] == "Index Only Scan" and node["Heap Fetches"] == 0 for node in scans), plan
        downgrade(pg_url, "104_decision_display_projection")
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*), sum(cost_usd), sum(input_tokens), sum(output_tokens) FROM usage_logs")
            assert cursor.fetchone() == ledger
            cursor.execute(QUERY, PARAMS)
            assert cursor.fetchall() == expected
            cursor.execute("SELECT to_regclass(%s)", (INDEX,))
            assert cursor.fetchone() == (None,)
        upgrade(pg_url, "105_flow_cost_covering")
        with connection.cursor() as cursor:
            cursor.execute(QUERY, PARAMS)
            assert cursor.fetchall() == expected
    finally:
        connection.close()


@pytest.mark.parametrize("transaction", ["writer", "snapshot"])
def test_concurrent_index_waits_for_short_lived_transaction(pg_url, transaction):
    import time
    from concurrent.futures import ThreadPoolExecutor

    upgrade(pg_url, "104_decision_display_projection")
    writer = psycopg2.connect(pg_url)
    observer = psycopg2.connect(pg_url)
    observer.autocommit = True
    try:
        with writer.cursor() as cursor:
            if transaction == "writer":
                cursor.execute("LOCK TABLE usage_logs IN ROW EXCLUSIVE MODE")
            else:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cursor.execute("SELECT count(*) FROM usage_logs")
        with ThreadPoolExecutor(max_workers=1) as pool:
            migration = pool.submit(upgrade, pg_url, "105_flow_cost_covering")
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with observer.cursor() as cursor:
                        cursor.execute("SELECT count(*) FROM pg_stat_progress_create_index WHERE relid='usage_logs'::regclass")
                        if cursor.fetchone()[0]:
                            break
                    if migration.done():
                        migration.result()
                        pytest.fail("Migration completed before transaction released its snapshot/lock")
                    time.sleep(0.05)
                else:
                    pytest.fail("Concurrent index did not start within the fixture deadline")
                time.sleep(1.5)
                assert not migration.done(), "A normal short-lived transaction must not abort the index migration"
            finally:
                writer.rollback()
            migration.result(timeout=30)
        with observer.cursor() as cursor:
            cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass(%s)", (INDEX,))
            assert cursor.fetchone() == (True,)
    finally:
        writer.close()
        observer.close()
