"""Accounting fault probes through the bearer-authenticated Responses route."""

import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import psycopg2
import pytest
import rfc8785

from src.proxy.mantle_service import drain_usage_finalizers
from src.proxy.model_resolver import ModelResolver
from src.proxy.routes import set_mantle_service, set_model_resolver
from src.usage.metering_journal import ImmutableMeteringJournal, MeteringObjectReceipt
from tests.budget.test_metering_person_scopes import activate_person_scopes, rooted_context, seed_person
from tests.budget.test_metering_reconciliation_routes import admin_app, body_for, grant
from tests.budget.test_metering_scope_admission import insert_budget, insert_spend, scope_key
from tests.budget.test_metering_scope_admission import live_redis as base_live_redis
from tests.proxy.test_mantle_metering_admission import (
    MODEL,
    USAGE,
    authenticate_route,
    object_at,
    quote,
    rows,
    service_for,
)
from tests.proxy.test_mantle_metering_admission import (
    context as base_context,
)
from tests.proxy.test_mantle_metering_admission import (
    journal as base_journal,
)
from tests.proxy.test_mantle_metering_admission import (
    metering_clock as base_metering_clock,
)
from tests.proxy.test_mantle_metering_admission import (
    metering_db as base_metering_db,
)
from tests.proxy.test_mantle_receipt_process_kill import BUCKET, FileBackedS3, kill_worker, object_path, read_attempt

pytest_plugins = ("tests.migrations.conftest_postgres",)
pytestmark = pytest.mark.usefixtures("unmapped_mantle_routing")

BODY = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20}).encode()
HEADERS = {"Content-Type": "application/json", "Authorization": "Bearer scenario-tenant-1"}


@pytest.fixture
def metering_db(pg_head_url):
    return base_metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def metering_clock(metering_db, monkeypatch):
    return base_metering_clock.__wrapped__(metering_db, monkeypatch)


@pytest.fixture
def context():
    return base_context.__wrapped__()


@pytest.fixture
def journal():
    yield from base_journal.__wrapped__()


@pytest.fixture
def live_redis():
    yield from base_live_redis.__wrapped__()


@pytest.fixture(autouse=True)
def reset_route():
    yield
    set_mantle_service(None)


def billed_reply():
    return httpx.Response(200, json={"usage": USAGE})


def finance(pg_url):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT tenant_id, request_id, state, bound_usd FROM metering_attempts ORDER BY request_id")
        attempts = cursor.fetchall()
        cursor.execute("SELECT tenant_id, request_id, scope_key, bound_usd FROM metering_attempt_scopes ORDER BY tenant_id, request_id, scope_key")
        scopes = cursor.fetchall()
        cursor.execute("SELECT org_id, request_id, cost_usd FROM budget_settlement_receipts ORDER BY org_id, request_id")
        receipts = cursor.fetchall()
        cursor.execute("SELECT org_id, request_id, cost_usd FROM usage_logs ORDER BY org_id, request_id")
        usages = cursor.fetchall()
    return attempts, scopes, receipts, usages


async def send(client, *, headers=None):
    return await client.post("/openai/v1/responses", content=BODY, headers=headers or HEADERS)


async def test_f01_concurrent_admission_and_duplicate_server_key(metering_db, journal, context, monkeypatch):
    context._policy_quote = replace(quote(BODY), total_usd=Decimal("0.006"))
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
        cursor.execute("SELECT budget_amount_usd FROM budget_configs WHERE org_id='tenant-1'")
        assert cursor.fetchone()[0] == Decimal("0.01")
    client, store = journal
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def provider(request):
        calls.append(request)
        entered.set()
        await release.wait()
        return billed_reply()

    service, _, engine, route = service_for(metering_db, store, context, provider)
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            assert (await send(caller, headers={"Content-Type": "application/json"})).status_code == 401
            first = asyncio.create_task(send(caller, headers=HEADERS | {"x-test-server-request-id": "first"}))
            await asyncio.wait_for(entered.wait(), 5)
            attempts, scopes, receipts, usages = finance(metering_db)
            assert attempts == [("tenant-1", "first", "dispatch_armed", Decimal("0.006000"))]
            assert len(scopes) == 3 and {row[1] for row in scopes} == {"first"}
            assert receipts == usages == []
            admission_row = rows(metering_db)[0]
            assert object_at(client, admission_row[2], admission_row[3])["request_id"] == "first"
            denied = await send(caller, headers=HEADERS | {"x-test-server-request-id": "second"})
            assert denied.status_code == 402
            assert len(calls) == 1
            release.set()
            accepted = await asyncio.wait_for(first, 5)
            assert accepted.status_code == 200 and accepted.headers["x-request-id"] == "first"
            assert await drain_usage_finalizers(timeout_seconds=2) == 0
            replay = await send(caller, headers=HEADERS | {"x-test-server-request-id": "first"})
            assert replay.status_code != 200 and len(calls) == 1
        attempts, scopes, receipts, usages = finance(metering_db)
        assert len(attempts) == 1 and attempts[0][:3] == ("tenant-1", "first", "settled")
        assert len(scopes) == 3
        assert len(receipts) == len(usages) == 1
        assert receipts[0][:2] == usages[0][:2] == ("tenant-1", "first")
    finally:
        release.set()
        await engine.dispose()


async def test_f02_redis_expiry_and_calendar_rollover_keep_overdue_hold(metering_db, journal, context, live_redis, monkeypatch):
    context._policy_quote = quote(BODY)
    today = datetime.now(UTC).date()
    previous_day = today - timedelta(days=1)
    before_midnight = datetime.combine(previous_day, time(23, 59, 59), UTC)
    after_midnight = datetime.combine(today, time(1, 0, 1), UTC)

    class AdmissionClock(datetime):
        current = before_midnight

        @classmethod
        def now(cls, timezone=None):
            return cls.current

    monkeypatch.setattr("src.budget.metering_admission.datetime", AdmissionClock)
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
        insert_spend(cursor, "tenant-1", "org", "tenant-1", previous_day, Decimal("0.009"))
        insert_spend(cursor, "tenant-1", "org", "tenant-1", today, Decimal("0.009"))
        cursor.execute("SELECT period_start, total_cost_usd FROM budget_usage ORDER BY period_start")
        assert cursor.fetchall() == [(previous_day, Decimal("0.009000")), (today, Decimal("0.009000"))]
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(502, json={"error": "injected"})

    service, _, engine, route = service_for(metering_db, journal[1], context, provider)
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            live_redis.set("derived:scenario-tenant-1", "present", px=10)
            response = await send(caller)
            assert response.status_code == 502 and len(calls) == 1
            original, scopes, receipts, usages = finance(metering_db)
            assert len(original) == 1 and original[0][2] == "dispatch_armed" and len(scopes) == 3
            assert receipts == usages == []
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT completion_deadline FROM metering_attempts WHERE request_id=%s", (original[0][1],))
                assert cursor.fetchone()[0] < after_midnight
            for _ in range(50):
                if not live_redis.exists("derived:scenario-tenant-1"):
                    break
                await asyncio.sleep(0.01)
            assert not live_redis.exists("derived:scenario-tenant-1")
            AdmissionClock.current = after_midnight
            live_redis.flushall()
            denied = await send(caller)
            assert denied.status_code == 503 and len(calls) == 1
            assert "tenant-2" not in denied.text
            attempts, held_scopes, receipts, usages = finance(metering_db)
            assert attempts[0][2] == "dispatch_armed" and attempts[0][3] == original[0][3]
            assert held_scopes == scopes and receipts == usages == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("stage", ["before", "after"])
def test_f03_authenticated_sigkill_keeps_sql_hold_and_journal_evidence(metering_db, tmp_path, stage):
    kill_worker(metering_db, tmp_path, stage)
    attempt, scope_count, usage_count, receipt_count = read_attempt(metering_db, f"killed-{stage}")
    assert attempt[0:2] == ("dispatch_armed", Decimal("0.001000"))
    assert scope_count == 3 and usage_count == receipt_count == 0
    assert object_path(tmp_path, attempt[5]).exists() is (stage == "after")
    attempts, scopes, receipts, usages = finance(metering_db)
    assert len(attempts) == 1 and len(scopes) == 3
    assert receipts == usages == []


async def test_f04_cross_org_person_barrier_does_not_block_disjoint_person(metering_db, journal, context, monkeypatch):
    await seed_person(metering_db, users=[("root-a", "tenant-1", "123"), ("root-b", "tenant-2", "125")])
    activate_person_scopes(
        metering_db,
        workers=[("tenant-1", "owner-1"), ("tenant-2", "owner-2")],
        roots=[("tenant-1", "root-a"), ("tenant-2", "root-a"), ("tenant-2", "root-b")],
        anchors=["github:123", "github:124", "github:125"],
    )
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        insert_budget(cursor, "tenant-1", "org", "tenant-1", Decimal("0.01"))
        for kind in ("user", "agent"):
            cursor.execute(
                "INSERT INTO metering_scope_gates (scope_key, scope_kind, tenant_id, activation, readiness) "
                "VALUES (%s, %s, 'tenant-1', 'active', 'ready')",
                (scope_key("tenant-1", kind, "owner-3"), kind),
            )
    first_context = rooted_context("tenant-1", "owner-1", "root-a")
    first_context._policy_quote = quote(BODY)
    same_person = rooted_context("tenant-2", "owner-2", "root-a", enforcement_enabled=False)
    same_person._policy_quote = quote(BODY)
    different_person = rooted_context("tenant-2", "owner-2", "root-b")
    different_person._policy_quote = quote(BODY)
    same_org = rooted_context("tenant-1", "owner-3", "root-a")
    same_org._policy_quote = quote(BODY)
    tokens = {
        HEADERS["Authorization"]: first_context,
        "Bearer scenario-same-person": same_person,
        "Bearer scenario-different-person": different_person,
        "Bearer scenario-same-org": same_org,
    }
    calls = []

    async def provider(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(502, json={"error": "injected"})
        return billed_reply()

    service, _, engine, route = service_for(metering_db, journal[1], context, provider)
    authenticate_route(route, tokens, monkeypatch)
    set_model_resolver(ModelResolver(allowed_models_config={"tenant-1": ["openai.*"], "tenant-2": ["openai.*"]}))
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            first = await send(caller)
            assert first.status_code == 502 and len(calls) == 1
            initial, initial_scopes, initial_receipts, initial_usages = finance(metering_db)
            assert len(initial) == 1 and initial[0][2] == "dispatch_armed" and len(initial_scopes) > 3
            assert initial_receipts == initial_usages == []
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE metering_attempts SET completion_deadline=NOW()-interval '1 second' WHERE tenant_id='tenant-1'")
                cursor.execute("UPDATE user_identities SET provider_user_id='124' WHERE user_id='root-a'")
            denied_person = await send(caller, headers=HEADERS | {"Authorization": "Bearer scenario-same-person"})
            denied_org = await send(caller, headers=HEADERS | {"Authorization": "Bearer scenario-same-org"})
            assert denied_person.status_code == denied_org.status_code == 503
            assert "tenant-1" not in denied_person.text and "root-a" not in denied_person.text
            independent = await send(caller, headers=HEADERS | {"Authorization": "Bearer scenario-different-person"})
            assert independent.status_code == 200
            assert len(calls) == 2
            assert await drain_usage_finalizers(timeout_seconds=2) == 0
        attempts, scopes, receipts, usages = finance(metering_db)
        assert len(attempts) == 2 and len(receipts) == len(usages) == 1
        assert {row[0] for row in attempts} == {"tenant-1", "tenant-2"}
        assert {row[0] for row in receipts} == {"tenant-2"}
        assert len(scopes) >= 6
    finally:
        await engine.dispose()


@pytest.mark.parametrize("mark_unavailable", [False, True])
@pytest.mark.parametrize("admission_delay_seconds", [0, 5.1])
@pytest.mark.parametrize("metering_clock_lag_seconds", [0, 15])
async def test_f05_known_final_failure_contains_unknown_or_retains_bounded_armed_hold(
    metering_db, journal, context, monkeypatch, mark_unavailable, metering_clock, admission_delay_seconds, metering_clock_lag_seconds
):
    """Allow 30 seconds for test admission; keep receipt containment and settlement bounds real."""
    metering_clock["wall"] -= timedelta(seconds=metering_clock_lag_seconds)
    context._policy_quote = quote(BODY)
    client, store = journal
    store.write_final = AsyncMock(side_effect=TimeoutError("journal unavailable"))
    calls = []

    def provider(request):
        calls.append(request)
        return billed_reply()

    service, admission, engine, route = service_for(metering_db, store, context, provider)
    prepare = admission.prepare

    async def prepare_for_receipt_fault(**kwargs):
        kwargs["deadline"] = asyncio.get_running_loop().time() + 30
        await asyncio.sleep(admission_delay_seconds)
        return await prepare(**kwargs)

    monkeypatch.setattr(admission, "prepare", prepare_for_receipt_fault)
    if mark_unavailable:
        admission.mark_unknown = AsyncMock(side_effect=TimeoutError("sql unavailable"))
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            first = await send(caller)
            assert first.status_code == 502 and len(calls) == 1
            store.write_final.assert_awaited_once()
            attempts, scopes, receipts, usages = finance(metering_db)
            assert len(attempts) == 1 and len(scopes) == 3 and receipts == usages == []
            assert attempts[0][2] == ("dispatch_armed" if mark_unavailable else "unknown")
            admission_row = rows(metering_db)[0]
            assert object_at(client, admission_row[2], admission_row[3])["request_id"] == attempts[0][1]
            assert admission_row[5] is None
            if mark_unavailable:
                with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT completion_deadline FROM metering_attempts WHERE request_id=%s", (attempts[0][1],))
                    completion = cursor.fetchone()[0]
                    assert completion == metering_clock["wall"] + timedelta(seconds=728)
                    cursor.execute(
                        "UPDATE metering_attempts SET completion_deadline=%s WHERE request_id=%s",
                        (metering_clock["wall"] - timedelta(seconds=1), attempts[0][1]),
                    )
            context._budget_enforcement_enabled = False
            denied = await send(caller)
            assert denied.status_code == 503 and denied.json()["error"] == "accounting_unresolved"
            assert len(calls) == 1
            assert await drain_usage_finalizers(timeout_seconds=2) == 0
        after, held_scopes, after_receipts, after_usages = finance(metering_db)
        assert after == attempts and held_scopes == scopes
        assert after_receipts == after_usages == []
    finally:
        await engine.dispose()


@pytest.mark.parametrize("admission_delay_seconds", [0, 5.1])
async def test_f05_receipt_wins_unknown_containment_race(metering_db, journal, context, monkeypatch, admission_delay_seconds):
    """Allow 30 seconds for test admission; keep receipt-race and settlement checks real."""
    context._policy_quote = quote(BODY)
    service, admission, engine, route = service_for(metering_db, journal[1], context, lambda request: billed_reply())
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    write_admission = journal[1].write_admission

    async def delayed_write_admission(**kwargs):
        await asyncio.sleep(admission_delay_seconds)
        return await write_admission(**kwargs)

    monkeypatch.setattr(journal[1], "write_admission", delayed_write_admission)
    scheduled = []
    service._schedule_receipted_settlement = lambda attempt, receipt, request_id: scheduled.append((attempt, receipt))
    prepared = []
    original_prepare = admission.prepare

    async def record_prepare(**kwargs):
        kwargs["deadline"] = asyncio.get_running_loop().time() + 30
        attempt = await original_prepare(**kwargs)
        prepared.append(attempt)
        return attempt

    admission.prepare = record_prepare
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            assert (await send(caller)).status_code == 200
            assert await drain_usage_finalizers(timeout_seconds=2) == 0
        attempts, scopes, receipts, usages = finance(metering_db)
        assert len(prepared) == len(attempts) == len(scheduled) == 1
        assert attempts[0][2] == "dispatch_armed" and len(scopes) == 3
        assert receipts == usages == [] and rows(metering_db)[0][5] is not None
        assert await admission.mark_unknown(prepared[0]) is False
        assert finance(metering_db) == (attempts, scopes, receipts, usages)
        assert await admission.settle_receipt(*scheduled[0]) is True
        after, held_scopes, after_receipts, after_usages = finance(metering_db)
        assert after[0][2] == "settled" and held_scopes == scopes
        assert len(after_receipts) == len(after_usages) == 1
    finally:
        await engine.dispose()


async def recover_file_attempt(pg_url, root, *, scan_barrier=None):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    from src.budget.metering_recovery import scan_metering_attempts
    from src.usage.metering_journal import ImmutableMeteringJournal
    from tests.migrations.conftest_postgres import to_async_url
    from tests.proxy.test_mantle_receipt_process_kill import BUCKET, FileBackedS3

    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    try:
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        scan_pending = True

        @asynccontextmanager
        async def recovery_session():
            nonlocal scan_pending
            is_scan = scan_pending
            scan_pending = False
            async with sessions() as session:
                yield session
            if is_scan and scan_barrier is not None:
                await asyncio.wait_for(scan_barrier.wait(), 10)

        return await scan_metering_attempts(
            recovery_session,
            ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(root)),
            now=datetime.now(UTC) + timedelta(hours=2),
        )
    finally:
        await engine.dispose()


@pytest.mark.parametrize("stage", ["after", "after_pointer"])
async def test_f06_killed_receipt_replays_exactly_once_without_second_provider_call(metering_db, journal, context, monkeypatch, tmp_path, stage):
    kill_worker(metering_db, tmp_path, stage)
    request_id = f"killed-{stage}"
    initial, scope_count, usage_count, receipt_count = read_attempt(metering_db, request_id)
    assert initial[0] == "dispatch_armed" and scope_count == 3
    assert usage_count == receipt_count == 0 and object_path(tmp_path, initial[5]).exists()
    assert (initial[6] is not None) is (stage == "after_pointer")
    outcomes = await asyncio.gather(recover_file_attempt(metering_db, tmp_path), recover_file_attempt(metering_db, tmp_path))
    assert any("settled" in statuses for statuses, _ in outcomes)
    assert (await recover_file_attempt(metering_db, tmp_path))[0] == []
    attempts, scopes, receipts, usages = finance(metering_db)
    assert attempts == [("tenant-1", request_id, "settled", Decimal("0.001000"))]
    assert len(scopes) == 3 and len(receipts) == len(usages) == 1
    assert receipts[0][:2] == usages[0][:2] == ("tenant-1", request_id)
    assert receipts[0][2] == usages[0][2] > 0
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT total_cost_usd, request_count FROM budget_usage WHERE org_id='tenant-1'")
        assert cursor.fetchall() == [(receipts[0][2], 1)] * 9
    context._policy_quote = quote(BODY)
    calls = []

    def provider(request):
        calls.append(request)
        return billed_reply()

    service, _, engine, route = service_for(metering_db, journal[1], context, provider)
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            duplicate = await send(caller, headers=HEADERS | {"x-test-server-request-id": request_id})
            assert duplicate.status_code == 503 and calls == []
        assert finance(metering_db) == (attempts, scopes, receipts, usages)
    finally:
        await engine.dispose()


async def test_f06_conflicting_final_evidence_quarantines_without_charging_or_releasing(metering_db, journal, context, monkeypatch, tmp_path):
    kill_worker(metering_db, tmp_path, "after")
    initial, scope_count, usage_count, receipt_count = read_attempt(metering_db, "killed-after")
    assert initial[0] == "dispatch_armed" and scope_count == 3
    assert usage_count == receipt_count == 0 and initial[6] is None
    final_path = object_path(tmp_path, initial[5])
    original = final_path.read_bytes()
    altered = json.loads(original)
    altered["admission_sha256"] = "0" * 64
    final_path.write_bytes(rfc8785.dumps(altered))
    try:
        assert (await recover_file_attempt(metering_db, tmp_path))[0] == ["quarantined"]
        quarantined, scopes, receipts, usages = finance(metering_db)
        assert quarantined == [("tenant-1", "killed-after", "quarantined", Decimal("0.001000"))]
        assert len(scopes) == 3 and receipts == usages == []
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT action, disposition FROM metering_reconciliation_events WHERE request_id='killed-after'")
            assert cursor.fetchall() == [("quarantine", "quarantined")]
            cursor.execute("SELECT count(*) FROM budget_usage WHERE org_id='tenant-1'")
            assert cursor.fetchone() == (0,)
        context._policy_quote = quote(BODY)
        context._budget_enforcement_enabled = False
        calls = []

        def provider(request):
            calls.append(request)
            return billed_reply()

        service, _, engine, route = service_for(metering_db, journal[1], context, provider)
        authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
        try:
            async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
                denied = await send(caller)
                assert denied.status_code == 503 and denied.json()["error"] == "accounting_unresolved"
                assert calls == [] and finance(metering_db) == (quarantined, scopes, receipts, usages)
        finally:
            await engine.dispose()
    finally:
        final_path.write_bytes(original)
    assert (await recover_file_attempt(metering_db, tmp_path))[0] == ["quarantined"]
    assert finance(metering_db) == (quarantined, scopes, receipts, usages)


@pytest.mark.parametrize("second_scan_delay", [0, 6])
async def test_f07_late_receipt_settles_unknown_before_new_spend(metering_db, journal, context, monkeypatch, tmp_path, second_scan_delay):
    kill_worker(metering_db, tmp_path, "after")
    initial, scope_count, usage_count, receipt_count = read_attempt(metering_db, "killed-after")
    assert initial[0] == "dispatch_armed" and initial[6] is None
    assert scope_count == 3 and usage_count == receipt_count == 0
    final_object = object_path(tmp_path, initial[5])
    hidden_object = tmp_path / "withheld-final-evidence"
    final_object.rename(hidden_object)
    try:
        assert (await recover_file_attempt(metering_db, tmp_path))[0] == ["unknown"]
        unknown, held_scopes, receipts, usages = finance(metering_db)
        assert unknown == [("tenant-1", "killed-after", "unknown", Decimal("0.001000"))]
        assert len(held_scopes) == 3 and receipts == usages == []
        context._policy_quote = quote(BODY)
        context._budget_enforcement_enabled = False
        calls = []

        def provider(request):
            calls.append(request)
            return billed_reply()

        service, _, engine, route = service_for(metering_db, journal[1], context, provider)
        authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
        try:
            async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
                denied = await send(caller)
                assert denied.status_code == 503 and denied.json()["error"] == "accounting_unresolved"
                assert calls == [] and finance(metering_db) == (unknown, held_scopes, receipts, usages)
                hidden_object.rename(final_object)
                scan_barrier = asyncio.Barrier(2)

                async def delayed_recovery():
                    await asyncio.sleep(second_scan_delay)
                    return await recover_file_attempt(metering_db, tmp_path, scan_barrier=scan_barrier)

                recoveries = await asyncio.gather(recover_file_attempt(metering_db, tmp_path, scan_barrier=scan_barrier), delayed_recovery())
                assert sorted(result for result, _ in recoveries) == [["settled"], ["stale"]]
                assert (await recover_file_attempt(metering_db, tmp_path))[0] == []
                settled, retained_scopes, recorded, usage_rows = finance(metering_db)
                assert settled[0][2] == "settled" and retained_scopes == held_scopes
                assert len(recorded) == len(usage_rows) == 1
                assert recorded[0][:2] == usage_rows[0][:2] == ("tenant-1", "killed-after")
                assert recorded[0][2] == usage_rows[0][2] > 0
                with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT action, prior_revision, new_revision FROM metering_reconciliation_events ORDER BY prior_revision")
                    events = cursor.fetchall()
                    assert [event[0] for event in events] == ["unknown", "attach"]
                    assert all(new == prior + 1 for _, prior, new in events)
                    cursor.execute("SELECT total_cost_usd, request_count FROM budget_usage WHERE org_id='tenant-1'")
                    assert cursor.fetchall() == [(recorded[0][2], 1)] * 9
                duplicate = await send(caller, headers=HEADERS | {"x-test-server-request-id": "killed-after"})
                assert duplicate.status_code == 503 and calls == []
                allowed = await send(caller)
                assert allowed.status_code == 200 and len(calls) == 1
                assert await drain_usage_finalizers(timeout_seconds=2) == 0
            attempts, scopes, after_receipts, after_usage = finance(metering_db)
            assert len(attempts) == 2 and len(scopes) == 6
            assert len(after_receipts) == len(after_usage) == 2
            assert [row for row in after_receipts if row[1] == "killed-after"] == recorded
        finally:
            await engine.dispose()
    finally:
        if hidden_object.exists():
            hidden_object.rename(final_object)


async def test_f08_missing_evidence_and_disabled_enforcement_never_release_unknown(metering_db, journal, context, monkeypatch, tmp_path):
    kill_worker(metering_db, tmp_path, "before")
    initial, _, _, _ = read_attempt(metering_db, "killed-before")
    assert not object_path(tmp_path, initial[5]).exists()
    assert (await recover_file_attempt(metering_db, tmp_path))[0] == ["unknown"]
    original = finance(metering_db)
    assert original[0][0][2] == "unknown" and original[2] == original[3] == []
    context._policy_quote = quote(BODY)
    context._budget_enforcement_enabled = False
    calls = []

    def provider(request):
        calls.append(request)
        return billed_reply()

    service, _, engine, route = service_for(metering_db, journal[1], context, provider)
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            for authorization in (None, "Bearer invalid-credential", "Bearer another-invalid-credential"):
                headers = {"Content-Type": "application/json"}
                if authorization:
                    headers["Authorization"] = authorization
                assert (await send(caller, headers=headers)).status_code == 401
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE metering_attempts SET created_at=NOW()-interval '1 day' WHERE request_id='killed-before'")
            denied = await send(caller)
            assert denied.status_code == 503 and denied.json()["error"] == "accounting_unresolved"
            assert calls == [] and finance(metering_db) == original
            assert (await recover_file_attempt(metering_db, tmp_path))[0] == ["held"]
            assert finance(metering_db) == original
    finally:
        await engine.dispose()


async def test_f07_killed_responses_late_receipt_admin_replay_stays_one_charge(metering_db, journal, context, monkeypatch, tmp_path):
    kill_worker(metering_db, tmp_path, "after")
    initial, scope_count, usage_count, receipt_count = read_attempt(metering_db, "killed-after")
    assert initial[0] == "dispatch_armed" and scope_count == 3 and usage_count == receipt_count == 0
    final_object = object_path(tmp_path, initial[5])
    withheld = tmp_path / "withheld-admin-final"
    final_object.rename(withheld)
    try:
        assert (await recover_file_attempt(metering_db, tmp_path))[0] == ["unknown"]
        unknown, scopes, receipts, usages = finance(metering_db)
        assert unknown[0][2] == "unknown" and receipts == usages == []
        withheld.rename(final_object)
        receipt = MeteringObjectReceipt(
            "tenant-1", "killed-after", "owner-1", initial[5], "fixture-v1", hashlib.sha256(final_object.read_bytes()).hexdigest()
        )
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT revision FROM metering_attempts WHERE request_id='killed-after'")
            revision = cursor.fetchone()[0]
        body = body_for(receipt, revision=revision)
        file_journal = ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path))
        with monkeypatch.context() as admin_patch:
            grant(admin_patch, receipt, body)
            app, admin_engine = await admin_app(metering_db, file_journal, admin_patch)
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app), base_url="http://test", headers={"Authorization": "Bearer admin-fixture"}
                ) as client:
                    path = "/admin/metering/attempts/killed-after/reconcile"
                    accepted = await client.post(path, json=body)
                    assert accepted.status_code == 200, accepted.text
                    assert accepted.json() == {"disposition": "settled", "revision": revision + 1, "spend_blocked": False, "exact_receipt": True}
                    replay = await client.post(path, json=body)
                    assert replay.status_code == 200 and replay.json() == accepted.json()
                    stale = await client.post(path, json=body | {"idempotency_key": "admin-key-stale"})
                    assert stale.status_code == 409
            finally:
                await admin_engine.dispose()
        assert (await recover_file_attempt(metering_db, tmp_path))[0] == []
        settled, retained_scopes, recorded, usage_rows = finance(metering_db)
        assert settled[0][2] == "settled" and retained_scopes == scopes
        assert len(recorded) == len(usage_rows) == 1
        assert recorded[0][:2] == usage_rows[0][:2] == ("tenant-1", "killed-after")
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT total_cost_usd, request_count FROM budget_usage WHERE org_id='tenant-1'")
            assert cursor.fetchall() == [(recorded[0][2], 1)] * 9
            cursor.execute("SELECT action FROM metering_reconciliation_events WHERE request_id='killed-after' ORDER BY prior_revision")
            assert [row[0] for row in cursor.fetchall()] == ["unknown", "apply_trusted_receipt"]
        context._policy_quote = quote(BODY)
        calls = []

        def provider(request):
            calls.append(request)
            return billed_reply()

        service, _, engine, route = service_for(metering_db, journal[1], context, provider)
        authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
        try:
            async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
                assert (await send(caller)).status_code == 200 and len(calls) == 1
                assert await drain_usage_finalizers(timeout_seconds=2) == 0
            after, _, after_receipts, after_usage = finance(metering_db)
            assert len(after) == len(after_receipts) == len(after_usage) == 2
            assert [row for row in after_receipts if row[1] == "killed-after"] == recorded
        finally:
            await engine.dispose()
    finally:
        if withheld.exists():
            withheld.rename(final_object)


@pytest.mark.parametrize("hold_first_scan", [False, True])
async def test_f08_killed_responses_unknown_cannot_be_released_by_admin_or_service(metering_db, journal, monkeypatch, tmp_path, hold_first_scan):
    kill_worker(metering_db, tmp_path, "before")
    initial, scope_count, usage_count, receipt_count = read_attempt(metering_db, "killed-before")
    assert scope_count == 3 and usage_count == receipt_count == 0
    before_recovery = finance(metering_db)
    with psycopg2.connect(metering_db) as blocker, blocker.cursor() as cursor:
        if hold_first_scan:
            cursor.execute("SELECT scope_key FROM metering_scope_gates ORDER BY scope_key FOR UPDATE")
        for scan_index in range(3):
            outcomes, _ = await asyncio.wait_for(recover_file_attempt(metering_db, tmp_path), 15)
            assert outcomes in (["unknown"], ["held"])
            if hold_first_scan and scan_index == 0:
                assert outcomes == ["held"]
                assert finance(metering_db) == before_recovery
            blocker.rollback()
            if finance(metering_db)[0][0][2] == "unknown":
                break
    unknown = finance(metering_db)
    assert unknown[0][0][2] == "unknown" and unknown[2] == unknown[3] == []
    assert unknown[1] == before_recovery[1]
    admission = MeteringObjectReceipt("tenant-1", "killed-before", "owner-1", initial[2], initial[3], initial[4])
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT revision FROM metering_attempts WHERE request_id='killed-before'")
        revision = cursor.fetchone()[0]
    body = body_for(admission, revision=revision, action="confirm_not_dispatched")
    file_journal = ImmutableMeteringJournal(BUCKET, region="us-east-1", client=FileBackedS3(tmp_path))
    with monkeypatch.context() as admin_patch:
        grant(admin_patch, admission, body)
        app, admin_engine = await admin_app(metering_db, file_journal, admin_patch)
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                path = "/admin/metering/attempts/killed-before/reconcile"
                for headers, expected in (
                    (None, 401),
                    ({"Authorization": "Bearer org-admin-fixture"}, 403),
                    ({"Authorization": "Bearer service-fixture"}, 403),
                    ({"Authorization": "Bearer admin-fixture"}, 422),
                ):
                    response = await client.post(path, json=body, headers=headers)
                    assert response.status_code == expected, response.text
                exception = await client.post(
                    path, json=body | {"action": "close_unknown_by_exception"}, headers={"Authorization": "Bearer admin-fixture"}
                )
                assert exception.status_code == 422
            assert finance(metering_db) == unknown
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT action, disposition FROM metering_reconciliation_events WHERE request_id='killed-before'")
                assert cursor.fetchall() == [("unknown", "unknown")]
        finally:
            await admin_engine.dispose()


@pytest.mark.parametrize("fault", ["expired_quote", "changed_policy"])
async def test_f09_f10_journal_wait_refuses_stale_dispatch_without_provider_call(metering_db, journal, context, monkeypatch, fault):
    context._policy_quote = quote(BODY)
    client, store = journal
    original_write = store.write_admission
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    written = []

    async def delayed_admission(**kwargs):
        receipt = await original_write(**kwargs)
        written.append(receipt)
        entered.set()
        await asyncio.wait_for(release.wait(), 5)
        return receipt

    store.write_admission = delayed_admission

    def provider(request):
        calls.append(request)
        return billed_reply()

    service, _, engine, route = service_for(metering_db, store, context, provider)
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            pending = asyncio.create_task(send(caller, headers=HEADERS | {"x-test-server-request-id": fault}))
            await asyncio.wait_for(entered.wait(), 5)
            before, scopes, receipts, usages = finance(metering_db)
            assert before == [("tenant-1", fault, "prepared", Decimal("0.001000"))]
            assert len(scopes) == 3 and receipts == usages == []
            assert object_at(client, written[0].key, written[0].version_id)["request_id"] == fault
            if fault == "expired_quote":
                monkeypatch.setattr("src.orchestration.provider_quotes._now", lambda: context._policy_quote.expires_at + timedelta(seconds=1))
            else:
                with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                    cursor.execute("UPDATE metering_scope_gates SET revision=revision+1 WHERE scope_kind='org' AND tenant_id='tenant-1'")
            release.set()
            denied = await asyncio.wait_for(pending, 5)
            assert denied.status_code == 503 and denied.json()["error"] == "metering_dispatch_unavailable"
            assert calls == []
            after, held_scopes, receipts, usages = finance(metering_db)
            assert len(after) == 1 and after[0][:3] == ("tenant-1", fault, "cancelled_unsubmitted")
            assert held_scopes == scopes and receipts == usages == []
    finally:
        release.set()
        await engine.dispose()


# F11 reaches the provider before losing its acknowledgement. Permit expiry has
# separate wall/monotonic fault cases; runner scheduling must not select that fault.
@pytest.mark.usefixtures("metering_clock")
async def test_f11_lost_send_ack_keeps_armed_attempt_without_inference_replay(metering_db, journal, context, monkeypatch):
    context._policy_quote = quote(BODY)
    calls = []

    def provider(request):
        calls.append(request)
        return httpx.Response(502, json={"error": "lost acknowledgement"})

    service, _, engine, route = service_for(metering_db, journal[1], context, provider)
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            first = await send(caller, headers=HEADERS | {"x-test-server-request-id": "lost-ack"})
            assert first.status_code == 502 and len(calls) == 1, first.text
            original = finance(metering_db)
            assert original[0] == [("tenant-1", "lost-ack", "dispatch_armed", Decimal("0.001000"))]
            assert len(original[1]) == 3 and original[2] == original[3] == []
            replay = await send(caller, headers=HEADERS | {"x-test-server-request-id": "lost-ack"})
            assert replay.status_code != 200 and len(calls) == 1 and finance(metering_db) == original
    finally:
        await engine.dispose()


async def test_f12_competing_recovery_retains_unknown_and_refuses_new_spend(metering_db, journal, context, monkeypatch, tmp_path):
    kill_worker(metering_db, tmp_path, "before")
    outcomes = []
    # A bounded scan may retain exposure as held under load, including after a
    # commit whose acknowledgement times out. Retry bounded scans as the recovery
    # worker does, then verify the durable transition instead of first-scan timing.
    for _ in range(3):
        first, second = await asyncio.wait_for(
            asyncio.gather(recover_file_attempt(metering_db, tmp_path), recover_file_attempt(metering_db, tmp_path)), 15
        )
        outcomes.extend((*first[0], *second[0]))
        assert set(outcomes) <= {"unknown", "stale", "held"}
        if finance(metering_db)[0][0][2] == "unknown":
            break
    assert outcomes.count("unknown") <= 1
    original = finance(metering_db)
    assert original[0][0][2] == "unknown" and len(original[1]) == 3 and original[2] == original[3] == []
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM metering_reconciliation_events WHERE tenant_id='tenant-1' AND request_id='killed-before' AND action='unknown'"
        )
        assert cursor.fetchone() == (1,)
    context._policy_quote = quote(BODY)
    calls = []

    def provider(request):
        calls.append(request)
        return billed_reply()

    service, _, engine, route = service_for(metering_db, journal[1], context, provider)
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            denied = await send(caller)
            assert denied.status_code == 503 and denied.json()["error"] == "accounting_unresolved"
            assert calls == [] and finance(metering_db) == original
    finally:
        await engine.dispose()


async def test_f14_rollback_gate_cannot_clear_unknown_or_reenable_spend(metering_db, journal, context, monkeypatch, tmp_path):
    kill_worker(metering_db, tmp_path, "before")
    assert (await recover_file_attempt(metering_db, tmp_path))[0] == ["unknown"]
    original = finance(metering_db)
    context._policy_quote = quote(BODY)
    calls = []

    def provider(request):
        calls.append(request)
        return billed_reply()

    service, _, engine, route = service_for(metering_db, journal[1], context, provider)
    authenticate_route(route, {HEADERS["Authorization"]: context}, monkeypatch)
    try:
        async with service._http_client, httpx.AsyncClient(transport=httpx.ASGITransport(app=route.app), base_url="http://test") as caller:
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE metering_scope_gates SET activation='draining' WHERE scope_kind='org' AND tenant_id='tenant-1'")
            assert (await send(caller)).status_code == 503
            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("UPDATE metering_scope_gates SET activation='active' WHERE scope_kind='org' AND tenant_id='tenant-1'")
            denied = await send(caller)
            assert denied.status_code == 503 and denied.json()["error"] == "accounting_unresolved"
            assert calls == [] and finance(metering_db) == original
    finally:
        await engine.dispose()
