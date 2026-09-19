"""Real PostgreSQL contention and retry behavior for the PMM-08 schema."""

import time
from concurrent.futures import ThreadPoolExecutor

import psycopg2

from tests.migrations.conftest_postgres import run_alembic, upgrade


def test_column_lock_wait_is_bounded_and_releases_queued_readers(pg_url):
    upgrade(pg_url, "060_orch_pending_amend")
    blocker = psycopg2.connect(pg_url)
    reader = psycopg2.connect(pg_url)
    reader.autocommit = True
    try:
        with blocker.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM usage_logs")
        with ThreadPoolExecutor(max_workers=1) as executor:
            migration = executor.submit(run_alembic, pg_url, "upgrade", "061_persona_usage_evidence")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with reader.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND wait_event_type='Lock' AND query ILIKE 'ALTER TABLE usage_logs%%'"
                    )
                    if cursor.fetchone()[0]:
                        break
                assert not migration.done(), migration.result().stderr if migration.done() else ""
                time.sleep(0.02)
            else:
                raise AssertionError("migration never reached the held table lock")
            started = time.monotonic()
            with reader.cursor() as cursor:
                cursor.execute("SET statement_timeout = '3s'")
                cursor.execute("SELECT count(*) FROM usage_logs")
            assert time.monotonic() - started < 2.5
            result = migration.result(timeout=5)
            assert result.returncode != 0 and "lock timeout" in result.stderr.lower()
            with reader.cursor() as cursor:
                cursor.execute("SELECT version_num FROM alembic_version")
                assert cursor.fetchone()[0] == "060_orch_pending_amend"
                cursor.execute("SELECT count(*) FROM information_schema.columns WHERE table_name='usage_logs' AND column_name='persona_key'")
                assert cursor.fetchone()[0] == 0
        blocker.rollback()
        upgrade(pg_url, "head")
    finally:
        blocker.close()
        reader.close()


def test_concurrent_indexes_allow_writes_and_repair_an_interrupted_build(pg_url):
    upgrade(pg_url, "061_persona_usage_evidence")
    writer = psycopg2.connect(pg_url)
    reader = psycopg2.connect(pg_url)
    reader.autocommit = True
    try:
        with reader.cursor() as cursor:
            cursor.execute(
                "INSERT INTO usage_logs (id, timestamp, org_id, department_id, team_id, user_id, account_type, "
                "model, input_tokens, output_tokens, cost_usd, latency_ms, status_code) SELECT i::text, now(), 'tenant', '', '', 'user', "
                "'human', 'model', 1, 1, 0, 1, 200 FROM generate_series(1,10000) i"
            )
            try:
                cursor.execute("CREATE UNIQUE INDEX CONCURRENTLY ix_usage_chain_id ON usage_logs(org_id)")
            except psycopg2.errors.UniqueViolation:
                pass
            else:
                raise AssertionError("duplicate tenant rows must leave an invalid concurrent index")
            cursor.execute("SELECT indisvalid FROM pg_index WHERE indexrelid=to_regclass('ix_usage_chain_id')")
            assert cursor.fetchone()[0] is False
        with writer.cursor() as cursor:
            cursor.execute("UPDATE usage_logs SET input_tokens=2 WHERE id='1'")
        with ThreadPoolExecutor(max_workers=1) as executor:
            started = time.monotonic()
            migration = executor.submit(run_alembic, pg_url, "upgrade", "062_persona_usage_indexes")
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with reader.cursor() as cursor:
                    cursor.execute(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND query ILIKE 'CREATE INDEX CONCURRENTLY%%'"
                    )
                    if cursor.fetchone()[0]:
                        break
                assert not migration.done(), migration.result().stderr if migration.done() else ""
                time.sleep(0.02)
            else:
                raise AssertionError("concurrent build never started")
            with reader.cursor() as cursor:
                cursor.execute("SET statement_timeout = '500ms'")
                cursor.execute("SELECT count(*) FROM usage_logs")
                assert cursor.fetchone()[0] == 10000
                cursor.execute("UPDATE usage_logs SET output_tokens=3 WHERE id='2'")
            writer.commit()
            result = migration.result(timeout=10)
            assert result.returncode == 0, result.stderr
            print(f"PMM usage index rollout on 10000 local rows: {time.monotonic() - started:.3f}s")
        with reader.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_index WHERE indexrelid IN (to_regclass('ix_usage_chain_id'), "
                "to_regclass('ix_usage_persona_owner')) AND indisvalid"
            )
            assert cursor.fetchone()[0] == 2
    finally:
        writer.close()
        reader.close()
