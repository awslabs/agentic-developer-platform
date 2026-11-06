"""PostgreSQL gate transitions cannot overtake legacy writers or discard holds."""

import asyncio
import importlib
from threading import Event

import psycopg2
import pytest
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.budget.metering_activation import require_non_draining_admission, transition_metering_gate
from src.budget.settlement import lock_legacy_metering_gates, settle_usage
from tests.budget import test_metering_scope_admission as scope_fixtures
from tests.budget.test_metering_scope_admission import admission_for, context
from tests.budget.test_metering_settlement_receipts import complete
from tests.budget.test_metering_writer_compatibility import deliver, ledger_rows
from tests.migrations.conftest_postgres import to_async_url, upgrade

pytest_plugins = ("tests.budget.test_metering_scope_admission",)


@pytest.fixture
def metering_db(pg_head_url):
    return scope_fixtures.metering_db.__wrapped__(pg_head_url)


@pytest.fixture
def journal():
    yield from scope_fixtures.journal.__wrapped__()


@pytest.fixture(autouse=True)
def enable_drain(monkeypatch):
    monkeypatch.setenv("BG_METERING_GATE_DRAIN_ENABLED", "true")


support = importlib.import_module("tests.lambda.test_budget_usage_tracker")


async def transition(factory, key, activation, *, tenant="tenant-1"):
    async with factory() as session:
        await transition_metering_gate(session, scope_key=key, scope_kind="org", tenant_id=tenant, activation=activation)
        await session.commit()


def gate_evidence(pg_url, key):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT activation, revision FROM metering_scope_gates WHERE scope_key=%s", (key,))
        gate = cursor.fetchone()
        cursor.execute(
            "SELECT a.state, s.bound_usd FROM metering_attempt_scopes s JOIN metering_attempts a USING (tenant_id, request_id) "
            "WHERE s.scope_key=%s ORDER BY a.request_id",
            (key,),
        )
        holds = cursor.fetchall()
    return gate, holds


async def wait_for_gate_lock(pg_url):
    for _ in range(100):
        with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE wait_event_type='Lock' "
                "AND datname=current_database() AND query LIKE '%pg_advisory_xact_lock%' AND pid <> pg_backend_pid()"
            )
            if cursor.fetchone()[0]:
                return
        await asyncio.sleep(0.02)
    pytest.fail("gate transition never waited for the in-flight legacy writer")


async def test_tracker_in_flight_before_gate_creation_drains_before_new_legacy_writes(pg_url):
    upgrade(pg_url)
    key = "org:tenant-1"
    entered, release = Event(), Event()
    handler = support.load_handler("budget-usage-tracker")

    def legacy_writer():
        with psycopg2.connect(pg_url) as connection:
            assert handler.legacy_metering_gate_state(connection, "tenant-1", "owner-1", "", "service", "first") == (False, False)
            entered.set()
            if not release.wait(15):
                raise TimeoutError("gate transition did not release legacy writer")
            handler.process_chat_log(connection, support._chat_log_4300(request_id="first"), support._bundled_rate_source())

    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    writer = asyncio.create_task(asyncio.to_thread(legacy_writer))
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        with pytest.raises(DBAPIError) as blocked:
            await transition(factory, key, "draining")
        assert blocked.value.orig.sqlstate == "55P03"
        assert "pg_advisory_xact_lock" in blocked.value.statement
        assert gate_evidence(pg_url, key) == (None, [])
        assert not writer.done()
        release.set()
        await writer
        await transition(factory, key, "draining")
        receipts, debits, usages = ledger_rows(pg_url)
        assert len(receipts) == 1 and receipts[0][0] == "first"
        assert len(debits) == 6 and all(row[-1] == 1 for row in debits)
        assert len(usages) == 0
        assert gate_evidence(pg_url, key) == (("draining", 1), [])
        async with factory() as session:
            with pytest.raises(ValueError, match="new provider admissions"):
                await require_non_draining_admission(session, tenant_id="tenant-1")
            await session.rollback()
        assert ledger_rows(pg_url) == (receipts, debits, usages)
        await transition(factory, key, "legacy")
        assert gate_evidence(pg_url, key) == (("legacy", 2), [])
        with psycopg2.connect(pg_url) as connection:
            handler.process_chat_log(connection, support._chat_log_4300(request_id="second"), support._bundled_rate_source())
        assert len(ledger_rows(pg_url)[0]) == 2
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
        await engine.dispose()


async def test_gateway_writer_blocks_gate_creation_and_late_debits_survive_drain(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    entered, release = asyncio.Event(), asyncio.Event()
    key = "org:tenant-1"

    async def legacy_writer():
        async with factory() as session:
            await lock_legacy_metering_gates(session, org_id="tenant-1")
            entered.set()
            await asyncio.wait_for(release.wait(), timeout=15)
            await settle_usage(
                session,
                org_id="tenant-1",
                request_id="gateway-first",
                user_id="owner-1",
                cost="0.1",
                total_tokens=3,
                entities=[("org", "tenant-1")],
            )
            await session.commit()

    writer = asyncio.create_task(legacy_writer())
    try:
        await asyncio.wait_for(entered.wait(), timeout=10)
        draining = asyncio.create_task(transition(factory, key, "draining"))
        await wait_for_gate_lock(pg_url)
        assert gate_evidence(pg_url, key) == (None, [])
        release.set()
        await writer
        await draining
        receipts, debits, usages = ledger_rows(pg_url)
        assert [row[0] for row in receipts] == ["gateway-first"]
        assert len(debits) == 3 and all(row[-1] == 1 for row in debits)
        assert usages == []
        async with factory() as session:
            with pytest.raises(ValueError, match="new provider admissions"):
                await require_non_draining_admission(session, tenant_id="tenant-1")
            await session.rollback()
        async with factory() as session:
            await lock_legacy_metering_gates(session, org_id="tenant-1")
            await settle_usage(
                session,
                org_id="tenant-1",
                request_id="late-legacy",
                user_id="owner-1",
                cost="0.1",
                total_tokens=3,
                entities=[("org", "tenant-1")],
            )
            await session.commit()
        assert gate_evidence(pg_url, key) == (("draining", 1), [])
        assert [row[0] for row in ledger_rows(pg_url)[0]] == ["gateway-first", "late-legacy"]
    finally:
        release.set()
        await asyncio.gather(writer, return_exceptions=True)
        await engine.dispose()


async def test_active_promotion_refused_and_rollback_preserves_receipt_and_hold(metering_db, journal):
    admission, engine = admission_for(metering_db, journal)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT scope_key FROM metering_scope_gates WHERE scope_kind='org' AND tenant_id='tenant-1'")
        key = cursor.fetchone()[0]
    try:
        async with factory() as session:
            with pytest.raises(ValueError, match="physical-send guard"):
                await transition_metering_gate(session, scope_key=key, scope_kind="org", tenant_id="tenant-1", activation="active")
            await session.rollback()
        attempt, receipt, decision = await complete(admission, context(), "held-rollback")
        assert decision.ledger_cost > 0
        assert gate_evidence(metering_db, key)[1] == [("dispatch_armed", attempt.quote.total_usd)]
        await transition(factory, key, "draining")
        with pytest.raises(ValueError, match="retains unresolved exposure"):
            await transition(factory, key, "legacy")
        assert gate_evidence(metering_db, key)[0][0] == "draining"
        assert ledger_rows(metering_db) == ([], [], [])
        async with factory() as session:
            with pytest.raises(ValueError, match="new provider admissions"):
                await require_non_draining_admission(session, tenant_id="tenant-1")
            await session.rollback()
        assert await admission.settle_receipt(attempt, receipt) is True
        before = ledger_rows(metering_db)
        assert len(before[0]) == 1 and before[0][0][0] == "held-rollback"
        assert len(before[1]) == 9 and len(before[2]) == 1
        async with factory() as session:
            with pytest.raises(ValueError, match="physical-send guard"):
                await transition_metering_gate(session, scope_key=key, scope_kind="org", tenant_id="tenant-1", activation="active")
            await session.rollback()
        await transition(factory, key, "legacy")
        assert gate_evidence(metering_db, key)[1] == [("settled", attempt.quote.total_usd)]
        assert ledger_rows(metering_db) == before
    finally:
        await engine.dispose()


async def test_authenticated_admission_blocks_new_provider_call_but_allows_legacy_final_debit(pg_url, monkeypatch):
    from unittest.mock import AsyncMock

    from src.budget import enforcement_middleware
    from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.shared.schemas.budget import EnforcementResult

    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = BudgetEnforcementService()
    service._get_session = lambda **kwargs: factory()
    service.prepare_enforcement_context = AsyncMock(return_value=EnforcementResult(allowed=True))
    service.reconcile_reservation = AsyncMock()
    monkeypatch.setattr(enforcement_middleware, "budget_enforcement_service", service)
    provider_calls = []

    async def provider(scope, receive, send):
        scope["state"]["token_context"]._budget_provider_started = True
        provider_calls.append(scope["state"]["request_id"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    middleware = BudgetEnforcementMiddleware(provider)

    async def request(request_id, *, caller=None):
        messages = []
        scope = {
            "type": "http",
            "path": "/openai/v1/responses",
            "method": "POST",
            "headers": [],
            "state": {"token_context": caller or context(), "request_id": request_id},
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await middleware(scope, receive, send)
        return next(message["status"] for message in messages if message["type"] == "http.response.start")

    try:
        assert await request("admitted-before-drain") == 200
        with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state FROM metering_legacy_admissions WHERE request_id='admitted-before-drain'")
            assert cursor.fetchone() == ("pending",)
        await transition(factory, "org:tenant-2", "draining", tenant="tenant-2")
        alternate = context()
        alternate.attributed_org_id = "tenant-2"
        assert await request("other-tenant-draining", caller=alternate) == 503
        await transition(factory, "org:tenant-1", "draining")
        monkeypatch.delenv("BG_METERING_GATE_DRAIN_ENABLED")
        middleware = BudgetEnforcementMiddleware(provider)
        assert await request("denied-after-drain") == 503
        alternate.org_id = "tenant-2"
        with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_scope_gates SET activation='active' WHERE scope_key='org:tenant-2'")
        assert await request("other-tenant-active", caller=alternate) == 503
        assert provider_calls == ["admitted-before-drain"]
        with pytest.raises(ValueError, match="retains unresolved exposure"):
            await transition(factory, "org:tenant-1", "legacy")
        await asyncio.to_thread(deliver, pg_url, request_id="admitted-before-drain")
        receipts, debits, usages = ledger_rows(pg_url)
        assert [row[0] for row in receipts] == ["admitted-before-drain"]
        assert len(debits) == 9 and all(row[-1] == 1 for row in debits)
        assert usages == []
        with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT state FROM metering_legacy_admissions WHERE request_id='admitted-before-drain'")
            assert cursor.fetchone() == ("pending",)
        assert gate_evidence(pg_url, "org:tenant-1") == (("draining", 1), [])
        with pytest.raises(ValueError, match="retains unresolved exposure"):
            await transition(factory, "org:tenant-1", "legacy")
        assert ledger_rows(pg_url) == (receipts, debits, usages)
    finally:
        await engine.dispose()


@pytest.mark.parametrize("body_state", ["stalled", "complete", "disconnect"])
@pytest.mark.parametrize("http_version", ["1.1", "2"])
async def test_metering_refusal_does_not_wait_for_body_eof(body_state, http_version, monkeypatch):
    from unittest.mock import AsyncMock

    from src.budget import enforcement_middleware
    from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
    from src.budget.enforcement_service import BudgetEnforcementService

    async def denied(*args, **kwargs):
        raise ValueError("gate is draining")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setattr(enforcement_middleware, "require_non_draining_admission", denied)
    service = BudgetEnforcementService()
    service._get_session = lambda **kwargs: Session()
    service.prepare_enforcement_context = AsyncMock(side_effect=AssertionError("provider admission must not continue"))
    provider_calls = []
    messages = []
    stalled = asyncio.Event()

    async def provider(scope, receive, send):
        provider_calls.append(scope)

    async def receive():
        if body_state == "stalled":
            await stalled.wait()
        if body_state == "disconnect":
            return {"type": "http.disconnect"}
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "http_version": http_version,
        "method": "POST",
        "path": "/openai/v1/responses",
        "headers": [],
        "state": {"token_context": context(), "request_id": "refused-before-provider"},
    }
    middleware = BudgetEnforcementMiddleware(provider, enforcement_service=service)
    await asyncio.wait_for(middleware(scope, receive, send), timeout=0.5)
    assert provider_calls == []
    if body_state == "disconnect":
        assert messages == []
    else:
        response = next(message for message in messages if message["type"] == "http.response.start")
        assert response["status"] == 503
        if http_version == "1.1":
            assert (b"connection", b"close") in response["headers"]
        else:
            assert (b"connection", b"close") not in response["headers"]
        assert any(message["type"] == "http.response.body" for message in messages)


def cohort_evidence(pg_url, request_id):
    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT state, disposition, disposed_at FROM metering_legacy_admissions WHERE tenant_id='tenant-1' AND request_id=%s",
            (request_id,),
        )
        return cursor.fetchone()


@pytest.mark.parametrize("outcome", ["budget_denied", "check_unavailable", "native_header_denied", "invalid_body", "cancelled"])
async def test_unsent_cohort_disposition_retains_only_ambiguous_work(pg_url, monkeypatch, outcome):
    from unittest.mock import AsyncMock

    from fastapi import FastAPI
    from pydantic import BaseModel

    from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.shared.schemas.budget import DenyReason, EnforcementResult

    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    service = BudgetEnforcementService()
    service._get_session = lambda **kwargs: factory()
    service.prepare_enforcement_context = AsyncMock(
        return_value=EnforcementResult(
            allowed=outcome not in {"budget_denied", "check_unavailable"},
            deny_reason=DenyReason.CHECK_UNAVAILABLE if outcome == "check_unavailable" else None,
        )
    )
    service.reconcile_reservation = AsyncMock()
    provider_calls = []

    class Payload(BaseModel):
        model: str

    app = FastAPI()

    @app.post("/openai/v1/responses")
    async def validate(payload: Payload):
        provider_calls.append(payload.model)
        return {"ok": True}

    if outcome == "cancelled":

        async def cancel(scope, receive, send):
            raise asyncio.CancelledError()

        downstream = cancel
    else:
        downstream = app
    middleware = BudgetEnforcementMiddleware(downstream, enforcement_service=service)
    request_id = f"cohort-{outcome}"
    scope = {
        "type": "http",
        "path": "/openai/v1/responses",
        "method": "POST",
        "headers": [(b"content-type", b"application/json")]
        + ([(b"x-adp-native-test-capability", b"invalid")] if outcome == "native_header_denied" else []),
        "query_string": b"",
        "root_path": "",
        "raw_path": b"/openai/v1/responses",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1234),
        "http_version": "1.1",
        "asgi": {"version": "3.0"},
        "state": {"token_context": context(), "request_id": request_id},
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message):
        if outcome == "invalid_body" and message["type"] == "http.response.start":
            monkeypatch.delenv("BG_METERING_GATE_DRAIN_ENABLED")
        messages.append(message)

    try:
        if outcome == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await middleware(scope, receive, send)
            assert cohort_evidence(pg_url, request_id) == ("pending", None, None)
            await transition(factory, "org:tenant-1", "draining")
            with pytest.raises(ValueError, match="retains unresolved exposure"):
                await transition(factory, "org:tenant-1", "legacy")
        else:
            await middleware(scope, receive, send)
            statuses = {"budget_denied": 402, "check_unavailable": 503, "native_header_denied": 403, "invalid_body": 422}
            assert [m["status"] for m in messages if m["type"] == "http.response.start"] == [statuses[outcome]]
            state, reason, disposed_at = cohort_evidence(pg_url, request_id)
            reasons = {"budget_denied": "budget_denied", "check_unavailable": "budget_denied", "native_header_denied": "native_header_denied"}
            assert (state, reason) == ("not_dispatched", reasons.get(outcome, "pre_provider_return"))
            monkeypatch.setenv("BG_METERING_GATE_DRAIN_ENABLED", "true")
            assert disposed_at is not None
            await transition(factory, "org:tenant-1", "draining")
            await transition(factory, "org:tenant-1", "legacy")
        assert provider_calls == []
        assert ledger_rows(pg_url) == ([], [], [])
    finally:
        await engine.dispose()


async def test_verified_tracker_decision_closes_cohort_in_same_debit_transaction(pg_url):
    from dataclasses import replace

    from pricing_policy import load_snapshot
    from src.budget.metering_activation import record_legacy_cohort_admission, require_non_draining_admission

    decision_log = importlib.import_module("tests.lambda.test_pricing_settlement_contract").decision_log
    verified_price = importlib.import_module("tests.pricing_policy.test_cost_oracles")._price

    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    request_id = "trusted-cohort"
    try:
        async with factory() as session:
            await require_non_draining_admission(session, tenant_id="tenant-1")
            await record_legacy_cohort_admission(
                session, tenant_id="tenant-1", authenticated_tenant_id="tenant-1", request_id=request_id, owner_id="owner-1"
            )
            await session.commit()
        log, _ = decision_log()
        decision = verified_price(
            load_snapshot(),
            "openai.gpt-5.6-sol",
            total_input=2048,
            output=256,
            cache_read=1920,
        )
        assert decision.confidence == "verified" and decision.estimate_reasons == ()
        log.update(org_id="tenant-1", user_id="owner-1", request_id=request_id, account_type="service")
        log["pricing_decision"] = replace(decision, org_id="tenant-1", request_id=request_id).to_dict()
        handler = support.load_handler("budget-usage-tracker")

        def deliver_verified():
            with psycopg2.connect(pg_url) as connection:
                handler.process_chat_log(connection, log, support._bundled_rate_source())

        await asyncio.to_thread(deliver_verified)
        receipts, debits, usages = ledger_rows(pg_url)
        assert receipts == [(request_id, decision.ledger_cost)]
        assert len(debits) == 9 and all(row[-1] == 1 for row in debits)
        assert cohort_evidence(pg_url, request_id) == ("settled", None, None)
        await asyncio.to_thread(deliver_verified)
        assert ledger_rows(pg_url) == (receipts, debits, usages)
        await transition(factory, "org:tenant-1", "draining")
        await transition(factory, "org:tenant-1", "legacy")
    finally:
        await engine.dispose()


@pytest.mark.parametrize("evidence", ["bare", "estimated", "untrusted", "verified"])
async def test_gateway_cohort_requires_verified_measured_usage_for_release(pg_url, evidence):
    from dataclasses import replace

    from pricing_policy import load_snapshot
    from src.budget.metering_activation import record_legacy_cohort_admission, require_non_draining_admission
    from src.budget.settlement import settle_priced_usage

    decision_log = importlib.import_module("tests.lambda.test_pricing_settlement_contract").decision_log
    verified_price = importlib.import_module("tests.pricing_policy.test_cost_oracles")._price
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    request_id = f"gateway-{evidence}"
    try:
        async with factory() as session:
            await require_non_draining_admission(session, tenant_id="tenant-1")
            await record_legacy_cohort_admission(
                session, tenant_id="tenant-1", authenticated_tenant_id="tenant-1", request_id=request_id, owner_id="owner-1"
            )
            await session.commit()
        if evidence == "estimated":
            _, decision = decision_log()
            assert decision.confidence == "estimated"
        else:
            decision = verified_price(load_snapshot(), "openai.gpt-5.6-sol", total_input=2048, output=256, cache_read=1920)
            assert decision.confidence == "verified"
        decision = replace(decision, org_id="tenant-1", request_id=request_id)
        caller = context()
        async with factory() as session:
            if evidence == "bare":
                assert await settle_usage(
                    session,
                    org_id="tenant-1",
                    request_id=request_id,
                    user_id="owner-1",
                    cost=decision.ledger_cost,
                    total_tokens=2304,
                    entities=[("org", "tenant-1")],
                )
            else:
                assert await settle_priced_usage(
                    session,
                    context=caller,
                    request_id=request_id,
                    decision=decision,
                    reservation_usage_known=evidence != "untrusted",
                )
            await session.commit()
        receipts, debits, usages = ledger_rows(pg_url)
        assert receipts == [(request_id, decision.ledger_cost)]
        assert len(debits) == (3 if evidence == "bare" else 9) and all(row[-1] == 1 for row in debits)
        assert usages == []
        assert cohort_evidence(pg_url, request_id)[0] == ("settled" if evidence == "verified" else "pending")
        async with factory() as session:
            if evidence == "bare":
                assert not await settle_usage(
                    session,
                    org_id="tenant-1",
                    request_id=request_id,
                    user_id="owner-1",
                    cost=decision.ledger_cost,
                    total_tokens=2304,
                    entities=[("org", "tenant-1")],
                )
            else:
                assert not await settle_priced_usage(
                    session,
                    context=caller,
                    request_id=request_id,
                    decision=decision,
                    reservation_usage_known=evidence != "untrusted",
                )
            await session.commit()
        assert ledger_rows(pg_url) == (receipts, debits, usages)
        assert cohort_evidence(pg_url, request_id)[0] == ("settled" if evidence == "verified" else "pending")
        await transition(factory, "org:tenant-1", "draining")
        if evidence == "verified":
            await transition(factory, "org:tenant-1", "legacy")
        else:
            with pytest.raises(ValueError, match="retains unresolved exposure"):
                await transition(factory, "org:tenant-1", "legacy")
    finally:
        await engine.dispose()


def _kill_after_cohort_admission(pg_url, ready, provider_calls):
    from unittest.mock import AsyncMock

    from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
    from src.budget.enforcement_service import BudgetEnforcementService
    from src.shared.schemas.budget import EnforcementResult

    async def run():
        engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        service = BudgetEnforcementService()
        service._get_session = lambda **kwargs: factory()
        service.prepare_enforcement_context = AsyncMock(return_value=EnforcementResult(allowed=True))
        service.reconcile_reservation = AsyncMock()

        async def pending_provider(scope, receive, send):
            ready.set()
            await asyncio.Event().wait()
            provider_calls.value += 1

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            raise AssertionError("provider never started")

        await BudgetEnforcementMiddleware(pending_provider, enforcement_service=service)(
            {
                "type": "http",
                "path": "/openai/v1/responses",
                "method": "POST",
                "headers": [],
                "state": {"token_context": context(), "request_id": "killed-after-admission"},
            },
            receive,
            send,
        )

    asyncio.run(run())


async def test_process_killed_after_admission_retains_cohort_hold_without_receipt(pg_url):
    import multiprocessing
    import signal

    upgrade(pg_url)
    runner = multiprocessing.get_context("fork")
    ready = runner.Event()
    provider_calls = runner.Value("i", 0)
    process = runner.Process(target=_kill_after_cohort_admission, args=(pg_url, ready, provider_calls))
    process.start()
    try:
        assert await asyncio.to_thread(ready.wait, 15)
        assert cohort_evidence(pg_url, "killed-after-admission") == ("pending", None, None)
        process.kill()
        await asyncio.to_thread(process.join, 10)
        assert process.exitcode == -signal.SIGKILL
        assert provider_calls.value == 0
        assert ledger_rows(pg_url) == ([], [], [])
        engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await transition(factory, "org:tenant-1", "draining")
            with pytest.raises(ValueError, match="retains unresolved exposure"):
                await transition(factory, "org:tenant-1", "legacy")
            assert cohort_evidence(pg_url, "killed-after-admission") == ("pending", None, None)
        finally:
            await engine.dispose()
    finally:
        if process.is_alive():
            process.kill()
        await asyncio.to_thread(process.join, 10)


def restarted_gateway(pg_url, *, drain_enabled):
    import json
    import os
    import subprocess
    import sys

    from tests.migrations.conftest_postgres import GATEWAY_ROOT

    program = """
import asyncio
import json
import os
import psycopg2
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import budget_enforcement_service
from src.shared.schemas.budget import EnforcementResult
from src.shared.schemas.auth import TokenContext
from datetime import UTC, datetime, timedelta
from tests.migrations.conftest_postgres import to_async_url

async def run():
    uri = os.environ["BG_TEST_POSTGRES_URI"]
    with psycopg2.connect(uri) as db, db.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM budget_settlement_receipts WHERE org_id='tenant-1'")
        old_receipts = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM usage_logs WHERE org_id='tenant-1'")
        old_usage = cursor.fetchone()[0]
    engine = create_async_engine(to_async_url(uri), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    budget_enforcement_service._get_session = lambda **kwargs: factory()
    budget_enforcement_service.prepare_enforcement_context = AsyncMock(return_value=EnforcementResult(allowed=True))
    budget_enforcement_service.reconcile_reservation = AsyncMock()
    provider_calls = []
    statuses = []

    async def provider(scope, receive, send):
        provider_calls.append(scope["state"]["request_id"])
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def send(message):
        if message["type"] == "http.response.start":
            statuses.append(message["status"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    caller = TokenContext(
        user_id="owner-1", org_id="tenant-1", team_id="", department_id="",
        account_type="service", expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    scope = {
        "type": "http", "path": "/openai/v1/responses", "method": "POST",
        "headers": [], "http_version": "1.1",
        "state": {"token_context": caller, "request_id": "restarted"},
    }
    try:
        await BudgetEnforcementMiddleware(provider)(scope, receive, send)
    finally:
        await engine.dispose()
    print(json.dumps({"old_receipts": old_receipts, "old_usage": old_usage, "statuses": statuses, "provider_calls": provider_calls}))

asyncio.run(run())
"""
    environment = os.environ.copy()
    environment["BG_TEST_POSTGRES_URI"] = pg_url
    if drain_enabled:
        environment["BG_METERING_GATE_DRAIN_ENABLED"] = "true"
    else:
        environment.pop("BG_METERING_GATE_DRAIN_ENABLED", None)
    result = subprocess.run([sys.executable, "-c", program], cwd=GATEWAY_ROOT, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("pending", [False, True])
async def test_restart_with_disabled_flag_keeps_persisted_gate_and_old_reader_rows(pg_url, monkeypatch, pending):
    from src.budget.metering_activation import record_legacy_cohort_admission

    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await asyncio.to_thread(deliver, pg_url, request_id="older-settled")
        before_ledger = ledger_rows(pg_url)
        assert len(before_ledger[0]) == 1 and before_ledger[0][0][0] == "older-settled"
        assert len(before_ledger[1]) == 9 and before_ledger[2] == []
        if pending:
            async with factory() as session:
                await record_legacy_cohort_admission(
                    session, tenant_id="tenant-1", authenticated_tenant_id="tenant-1", request_id="held", owner_id="owner-1"
                )
                await session.commit()
        await transition(factory, "org:tenant-1", "draining")
        assert cohort_evidence(pg_url, "held") == (("pending", None, None) if pending else None)
        baseline = restarted_gateway(pg_url, drain_enabled=False)
        assert (baseline["old_receipts"], baseline["old_usage"]) == (1, 0)
        assert baseline["statuses"] == [503] and baseline["provider_calls"] == []
        assert gate_evidence(pg_url, "org:tenant-1") == (("draining", 1), [])
        monkeypatch.delenv("BG_METERING_GATE_DRAIN_ENABLED")
        with pytest.raises(ValueError, match="retains unresolved exposure" if pending else "compatible provider admission fence"):
            await transition(factory, "org:tenant-1", "legacy")
        assert cohort_evidence(pg_url, "held") == (("pending", None, None) if pending else None)
        monkeypatch.setenv("BG_METERING_GATE_DRAIN_ENABLED", "true")
        if pending:
            with pytest.raises(ValueError, match="retains unresolved exposure"):
                await transition(factory, "org:tenant-1", "legacy")
            assert gate_evidence(pg_url, "org:tenant-1") == (("draining", 1), [])
            assert cohort_evidence(pg_url, "held") == ("pending", None, None)
        else:
            await transition(factory, "org:tenant-1", "legacy")
            assert gate_evidence(pg_url, "org:tenant-1") == (("legacy", 2), [])
            after = restarted_gateway(pg_url, drain_enabled=False)
            assert after["statuses"] == [200] and after["provider_calls"] == ["restarted"]
            assert (after["old_receipts"], after["old_usage"]) == (1, 0)
        assert ledger_rows(pg_url) == before_ledger
    finally:
        await engine.dispose()


async def test_spoofed_ready_baseline_cannot_enable_active_gate_with_old_reader(pg_url):
    upgrade(pg_url)
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        await transition(factory, "org:tenant-1", "draining")
        with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_scope_gates SET readiness='ready' WHERE scope_key='org:tenant-1'")
        async with factory() as session:
            with pytest.raises(ValueError, match="physical-send guard"):
                await transition_metering_gate(session, scope_key="org:tenant-1", scope_kind="org", tenant_id="tenant-1", activation="active")
            await session.rollback()
        baseline = restarted_gateway(pg_url, drain_enabled=False)
        assert baseline["statuses"] == [503] and baseline["provider_calls"] == []
        assert (baseline["old_receipts"], baseline["old_usage"]) == (0, 0)
        assert gate_evidence(pg_url, "org:tenant-1") == (("draining", 1), [])
    finally:
        await engine.dispose()
