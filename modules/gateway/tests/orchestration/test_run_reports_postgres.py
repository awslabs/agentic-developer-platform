"""Real PostgreSQL migration chain and overlapping worker admission."""

import asyncio

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from tests.agentauth import test_run_report_routes as route_tests
from tests.migrations import conftest_postgres as postgres
from tests.migrations.conftest_postgres import downgrade, to_async_url, upgrade

pg_server = postgres.pg_server
pg_url = postgres.pg_url

reports = route_tests.reports


@pytest.fixture
async def db_session_factory(pg_url):
    upgrade(pg_url, "head")
    engine = create_async_engine(to_async_url(pg_url))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def test_two_pods_cannot_start_one_assignment(reports):
    results = await asyncio.gather(
        *[
            reports.client.post("/internal/v1/agent/report/started", headers=reports.headers, json={"ownership_nonce": nonce * 32})
            for nonce in ("a", "b")
        ]
    )
    assert sorted(result.status_code for result in results) == [200, 409]


def test_063_064_migration_upgrade_downgrade_chain(pg_url):
    import psycopg2

    upgrade(pg_url, "064_orchestration_run_reports")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone()[0] == "064_orchestration_run_reports"
        cursor.execute("SELECT is_nullable FROM information_schema.columns WHERE table_name='orchestration_pr_bindings' AND column_name='run_id'")
        assert cursor.fetchone()[0] == "YES"
        cursor.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name='orchestration_run_reports'")
        columns = dict(cursor.fetchall())
        assert columns["worker_receipt"] == columns["terminal_receipt"] == "jsonb"
        assert columns["provider_repository_id"] == "bigint"
    downgrade(pg_url, "063_pr_binding_adoption")
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT to_regclass('orchestration_run_reports')")
        assert cursor.fetchone()[0] is None
    upgrade(pg_url, "064_orchestration_run_reports")
