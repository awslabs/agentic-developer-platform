"""Scoped SQL exposure is the admission authority, even after cache loss."""

import asyncio
import json
import os
import subprocess
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import boto3
import psycopg2
import pytest
import redis
import rfc8785
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.budget import metering_admission
from src.budget.metering_admission import MeteringAccountingUnresolvedError, MeteringAdmission, MeteringBudgetExceededError
from src.orchestration.provider_quotes import request_digest
from src.shared.schemas.auth import TokenContext
from src.usage.metering_journal import ImmutableMeteringJournal
from tests.migrations.conftest_postgres import to_async_url
from tests.proxy.test_mantle_metering_admission import MODEL, quote

pytest_plugins = ("tests.migrations.conftest_postgres",)

BODY = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20}).encode()


def scope_key(tenant_id, kind, identity):
    return f"v1:tenant:{request_digest(rfc8785.dumps([tenant_id, kind, identity]))}"


def add_gates(cursor, tenant_id, user_id):
    for kind, identity in (("org", tenant_id), ("user", user_id), ("agent", user_id)):
        cursor.execute(
            "INSERT INTO metering_scope_gates (scope_key, scope_kind, tenant_id, activation, readiness) VALUES (%s, %s, %s, 'active', 'ready')",
            (scope_key(tenant_id, kind, identity), kind, tenant_id),
        )


@pytest.fixture
def metering_db(pg_head_url):
    with psycopg2.connect(pg_head_url) as connection, connection.cursor() as cursor:
        add_gates(cursor, "tenant-1", "owner-1")
    return pg_head_url


@pytest.fixture
def journal():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        client.create_bucket(Bucket="metering-test")
        client.put_bucket_versioning(Bucket="metering-test", VersioningConfiguration={"Status": "Enabled"})
        yield ImmutableMeteringJournal("metering-test", region="us-east-1", client=client)


def context(tenant_id="tenant-1", user_id="owner-1"):
    result = TokenContext(
        user_id=user_id,
        org_id=tenant_id,
        team_id="",
        department_id="",
        attributed_user_id=user_id,
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    result._policy_quote = replace(quote(BODY), total_usd=Decimal("0.006"))
    return result


def admission_for(pg_url, journal, *, max_open_per_scope=1000):
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    admission = MeteringAdmission(async_sessionmaker(engine, expire_on_commit=False), journal, max_open_per_scope=max_open_per_scope)
    return admission, engine


async def prepare(admission, caller, request_id):
    # Creating real SQL and journal state is setup, not a five-second latency
    # assertion. Keep it bounded while tolerating contended CI workers; expiry
    # scenarios set their own clocks/deadlines after the attempt is prepared.
    return await admission.prepare(
        context=caller,
        request_id=request_id,
        model=MODEL,
        original=BODY,
        submitted=BODY,
        deadline=asyncio.get_running_loop().time() + 30,
    )


def insert_budget(cursor, tenant_id, entity_type, entity_id, amount, period_type="daily", enforcement_mode="hard"):
    cursor.execute(
        "INSERT INTO budget_configs (id, org_id, entity_type, entity_id, period_type, budget_amount_usd, enforcement_mode, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, NOW())",
        (str(uuid4()), tenant_id, entity_type, entity_id, period_type, amount, enforcement_mode),
    )


def insert_spend(cursor, tenant_id, entity_type, entity_id, period_start, amount):
    cursor.execute(
        "INSERT INTO budget_usage (id, org_id, entity_type, entity_id, period_start, period_type, total_cost_usd, total_tokens, request_count) "
        "VALUES (%s, %s, %s, %s, %s, 'daily', %s, 0, 1)",
        (str(uuid4()), tenant_id, entity_type, entity_id, period_start, amount),
    )


def counts(pg_url, tenant_id):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM metering_attempts WHERE tenant_id=%s", (tenant_id,))
        attempts = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM metering_attempt_scopes WHERE tenant_id=%s", (tenant_id,))
        scopes = cursor.fetchone()[0]
        cursor.execute("SELECT coalesce(sum(bound_usd),0) FROM metering_attempt_scopes WHERE tenant_id=%s AND allocation_period='all'", (tenant_id,))
        exposure = cursor.fetchone()[0]
    return attempts, scopes, exposure


async def test_competing_admissions_respect_settled_spend_and_open_hold(metering_db, journal, monkeypatch):
    """Exercise budget serialization independently of runner-speed lock expiry."""
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
        insert_spend(cursor, "tenant-1", "org", "tenant-1", datetime.now(UTC).date(), Decimal("0.001"))
    admission, engine = admission_for(metering_db, journal)
    admission_barrier = asyncio.Barrier(2)
    original_lock = metering_admission.lock_admission_policy

    async def lock_policy_for_budget_assertions(session, **kwargs):
        await original_lock(session, **kwargs)
        assert await session.scalar(text("SHOW lock_timeout")) == "500ms"
        await session.execute(text("SET LOCAL lock_timeout = '30s'"))
        await asyncio.wait_for(admission_barrier.wait(), 30)

    monkeypatch.setattr(metering_admission, "lock_admission_policy", lock_policy_for_budget_assertions)
    try:
        results = await asyncio.wait_for(
            asyncio.gather(
                prepare(admission, context(), "competitor-1"),
                prepare(admission, context(), "competitor-2"),
                return_exceptions=True,
            ),
            30,
        )
        outcomes = [repr(result) if isinstance(result, Exception) else type(result).__name__ for result in results]
        assert sum(isinstance(result, MeteringBudgetExceededError) for result in results) == 1, outcomes
        assert sum(not isinstance(result, Exception) for result in results) == 1, outcomes
        assert counts(metering_db, "tenant-1") == (1, 3, Decimal("0.018000"))
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT total_cost_usd FROM budget_usage WHERE org_id='tenant-1'")
            assert cursor.fetchall() == [(Decimal("0.001000"),)]
    finally:
        await engine.dispose()


async def test_competing_admission_lock_timeout_preserves_budget_and_hold(metering_db, journal, monkeypatch):
    """A real production lock timeout cannot reserve exposure or bypass a retry's cap."""
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
        insert_spend(cursor, "tenant-1", "org", "tenant-1", datetime.now(UTC).date(), Decimal("0.001"))
    admission, engine = admission_for(metering_db, journal)
    entered, release = asyncio.Event(), asyncio.Event()
    original_check = admission._check_scope

    async def paused_check(session, **kwargs):
        if not entered.is_set():
            assert await session.scalar(text("SHOW lock_timeout")) == "500ms"
            entered.set()
            await asyncio.wait_for(release.wait(), 30)
        return await original_check(session, **kwargs)

    monkeypatch.setattr(admission, "_check_scope", paused_check)
    admitted = asyncio.create_task(prepare(admission, context(), "lock-holder"))
    try:
        await asyncio.wait_for(entered.wait(), 30)
        with pytest.raises(DBAPIError, match="lock timeout") as blocked:
            await asyncio.wait_for(prepare(admission, context(), "lock-contender"), 30)
        assert blocked.value.orig.sqlstate == "55P03"
        assert counts(metering_db, "tenant-1") == (0, 0, Decimal("0"))
        release.set()
        await asyncio.wait_for(admitted, 30)
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, context(), "lock-contender")
        assert counts(metering_db, "tenant-1") == (1, 3, Decimal("0.018000"))
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT request_id, state, bound_usd FROM metering_attempts WHERE tenant_id='tenant-1'")
            assert cursor.fetchall() == [("lock-holder", "prepared", Decimal("0.006000"))]
            cursor.execute("SELECT total_cost_usd FROM budget_usage WHERE org_id='tenant-1'")
            assert cursor.fetchall() == [(Decimal("0.001000"),)]
    finally:
        release.set()
        await asyncio.gather(admitted, return_exceptions=True)
        await engine.dispose()


async def test_unknown_quarantined_and_overdue_rows_block_only_intersecting_scopes(metering_db, journal):
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        add_gates(cursor, "tenant-2", "owner-1")
    admission, engine = admission_for(metering_db, journal)
    same = context()
    same._budget_enforcement_enabled = False
    independent = context("tenant-2", "owner-1")
    try:
        await prepare(admission, same, "first")
        for state in ("unknown", "quarantined", "overdue_prepared", "overdue_armed"):
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute(
                    "UPDATE metering_attempts SET state=%s, prepare_deadline=NOW()-%s::interval, "
                    "completion_deadline=NOW()-%s::interval, dispatch_deadline=NOW() "
                    "WHERE tenant_id='tenant-1' AND request_id='first'",
                    (
                        "dispatch_armed" if state == "overdue_armed" else "prepared" if state == "overdue_prepared" else state,
                        "1 second" if state == "overdue_prepared" else "-1 hour",
                        "1 second" if state == "overdue_armed" else "-1 hour",
                    ),
                )
            with pytest.raises(MeteringAccountingUnresolvedError):
                await prepare(admission, same, f"denied-{state}")
            assert counts(metering_db, "tenant-1")[0] == 1
        await prepare(admission, independent, "other-tenant")
        assert counts(metering_db, "tenant-2") == (1, 3, Decimal("0.018000"))
    finally:
        await engine.dispose()


async def test_previous_period_open_hold_survives_rollover_and_old_settled_usage_does_not(metering_db, journal):
    now = datetime.now(UTC).date()
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
        insert_spend(cursor, "tenant-1", "org", "tenant-1", now - timedelta(days=1), Decimal("0.009"))
    admission, engine = admission_for(metering_db, journal)
    try:
        await prepare(admission, context(), "previous-period")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute(
                "UPDATE metering_attempts SET created_at=NOW()-interval '1 day', prepare_deadline=NOW()+interval '1 minute' "
                "WHERE request_id='previous-period'"
            )
            insert_spend(cursor, "tenant-1", "org", "tenant-1", now, Decimal("0.001"))
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, context(), "current-period")
        assert counts(metering_db, "tenant-1") == (1, 3, Decimal("0.018000"))
    finally:
        await engine.dispose()


async def test_open_work_backlog_denies_without_discarding_earlier_bound(metering_db, journal):
    admission, engine = admission_for(metering_db, journal, max_open_per_scope=1)
    try:
        await prepare(admission, context(), "backlog-first")
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, context(), "backlog-second")
        assert counts(metering_db, "tenant-1") == (1, 3, Decimal("0.018000"))
    finally:
        await engine.dispose()


@pytest.fixture
def live_redis():
    binary = os.environ.get("BG_TEST_REDIS_BIN")
    if not binary or not Path(binary).is_file():
        pytest.skip("Real Redis admission test needs a local BG_TEST_REDIS_BIN")
    with tempfile.TemporaryDirectory(prefix="metering-redis-") as directory:
        socket_path = str(Path(directory) / "redis.sock")
        process = subprocess.Popen(
            [binary, "--port", "0", "--unixsocket", socket_path, "--save", "", "--appendonly", "no", "--loglevel", "warning"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        client = redis.Redis(unix_socket_path=socket_path, socket_connect_timeout=1)
        try:
            for _ in range(50):
                if process.poll() is not None:
                    pytest.fail(f"local Redis exited before startup: {process.returncode}")
                try:
                    if client.ping():
                        break
                except (redis.ConnectionError, redis.TimeoutError):
                    time.sleep(0.05)
            else:
                pytest.fail("local Redis did not become ready")
            yield client
        finally:
            client.close()
            process.terminate()
            process.communicate(timeout=5)


async def test_real_redis_expiry_cannot_erase_sql_exposure_or_unknown_barrier(metering_db, journal, live_redis):
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
    admission, engine = admission_for(metering_db, journal)
    try:
        live_redis.set("derived:tenant-1", "present", px=10)
        await prepare(admission, context(), "redis-expiry-first")
        for _ in range(50):
            if not live_redis.exists("derived:tenant-1"):
                break
            await asyncio.sleep(0.01)
        assert not live_redis.exists("derived:tenant-1")
        with pytest.raises(MeteringBudgetExceededError):
            await prepare(admission, context(), "redis-expiry-denied")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='redis-expiry-first'")
        live_redis.flushall()
        with pytest.raises(MeteringAccountingUnresolvedError):
            await prepare(admission, context(), "redis-unknown-denied")
        assert counts(metering_db, "tenant-1") == (1, 3, Decimal("0.018000"))
    finally:
        await engine.dispose()


async def test_each_hierarchy_cap_is_checked_without_multiplying_one_attempt(metering_db, journal):
    caller = context()
    caller.team_id = "team-1"
    caller.department_id = "department-1"
    cases = (
        ("org", "tenant-1", "org"),
        ("user", "owner-1", "service_account"),
        ("agent", "owner-1", "agent"),
        ("team", "team-1", "team"),
        ("department", "department-1", "department"),
    )
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        for kind, identity, _ in cases[-2:]:
            cursor.execute(
                "INSERT INTO metering_scope_gates (scope_key, scope_kind, tenant_id, activation, readiness) "
                "VALUES (%s, %s, 'tenant-1', 'active', 'ready')",
                (scope_key("tenant-1", kind, identity), kind),
            )
    admission, engine = admission_for(metering_db, journal)
    try:
        for kind, identity, entity_type in cases:
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                insert_budget(cursor, "tenant-1", entity_type, identity, Decimal("0.00"))
            with pytest.raises(MeteringBudgetExceededError):
                await prepare(admission, caller, f"deny-{kind}")
            assert counts(metering_db, "tenant-1") == (0, 0, Decimal("0"))
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("DELETE FROM budget_configs WHERE org_id='tenant-1'")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.00"), enforcement_mode="soft")
        await prepare(admission, caller, "soft-cap")
        assert counts(metering_db, "tenant-1") == (1, 5, Decimal("0.030000"))
    finally:
        await engine.dispose()


def test_route_distinguishes_numeric_budget_exhaustion_from_unknown_exposure(metering_db, journal):
    from unittest.mock import AsyncMock, patch

    import httpx

    from src.proxy.bedrock_enforcement import RoutingDecision
    from src.proxy.bedrock_routing import BedrockTarget
    from tests.proxy.test_mantle_metering_admission import service_for

    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.00"))
    caller = context()
    calls = []
    service, admission, engine, route = service_for(metering_db, journal, caller, lambda request: calls.append(request) or httpx.Response(200))
    decision = RoutingDecision(target=BedrockTarget(account_id=None, rung="platform"))
    try:
        with patch("src.proxy.mantle_service.resolve_routing_decision", new=AsyncMock(return_value=decision)):
            response = route.post("/openai/v1/responses", content=BODY, headers={"Content-Type": "application/json"})
            assert response.status_code == 402
            assert response.json()["error"] == "budget_exceeded"
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("DELETE FROM budget_configs WHERE org_id='tenant-1'")
            asyncio.run(prepare(admission, caller, "unresolved-first"))
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE metering_attempts SET state='unknown' WHERE request_id='unresolved-first'")
            response = route.post("/openai/v1/responses", content=BODY, headers={"Content-Type": "application/json"})
            assert response.status_code == 503
            assert response.json()["error"] == "accounting_unresolved"
            assert calls == []
            assert counts(metering_db, "tenant-1")[0] == 1
    finally:
        asyncio.run(engine.dispose())
