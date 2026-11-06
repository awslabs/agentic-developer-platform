"""Responses admission and terminal ordering against migrated PostgreSQL and a versioned journal."""

import asyncio
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from unittest.mock import AsyncMock
from uuid import uuid4

import boto3
import httpx
import pytest
import rfc8785
from fastapi import HTTPException
from fastapi.testclient import TestClient
from moto import mock_aws
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from pricing_policy import load_snapshot
from src.budget.metering_admission import MeteringAdmission, MeteringAdmissionError
from src.budget.pricing_v2_reader import cached_rate_state
from src.orchestration.provider_quotes import ProviderQuote, TrustedUsage, _pricing_revision, request_digest
from src.proxy.mantle_service import MantlePassthroughService
from src.proxy.model_resolver import ModelResolver
from src.proxy.routes import get_token_context
from src.shared.schemas.auth import TokenContext
from src.usage.metering_journal import ImmutableMeteringJournal
from tests.migrations.conftest_postgres import to_async_url
from tests.proxy.test_mantle_route import StubAuth, build_app

pytest_plugins = ("tests.migrations.conftest_postgres",)

pytestmark = pytest.mark.usefixtures("unmapped_mantle_routing")

MODEL = "openai.gpt-5.5"
USAGE = {"input_tokens": 1, "output_tokens": 2}


@pytest.fixture
def metering_db(pg_head_url):
    import psycopg2

    with psycopg2.connect(pg_head_url) as connection, connection.cursor() as cursor:
        for kind, identity in (("org", "tenant-1"), ("user", "owner-1"), ("agent", "owner-1")):
            scope_key = f"v1:tenant:{request_digest(rfc8785.dumps(['tenant-1', kind, identity]))}"
            cursor.execute(
                "INSERT INTO metering_scope_gates (scope_key, scope_kind, tenant_id, activation, readiness) VALUES (%s, %s, %s, 'active', 'ready')",
                (scope_key, kind, "tenant-1"),
            )
    return pg_head_url


@pytest.fixture
def context():
    return TokenContext(
        user_id="owner-1",
        org_id="tenant-1",
        team_id="",
        department_id="",
        attributed_user_id="owner-1",
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )


@pytest.fixture
def journal():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        client.create_bucket(Bucket="metering-test")
        client.put_bucket_versioning(Bucket="metering-test", VersioningConfiguration={"Status": "Enabled"})
        yield client, ImmutableMeteringJournal("metering-test", region="us-east-1", client=client)


@pytest.fixture
def metering_clock(metering_db, monkeypatch):
    """Control permit expiry without changing event-loop or settlement deadlines."""
    from src.budget import metering_admission

    clock = {"wall": datetime.now(UTC), "monotonic": monotonic()}

    class MeteringDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["wall"].astimezone(tz)

    monkeypatch.setattr(metering_admission, "datetime", MeteringDateTime)
    monkeypatch.setattr(metering_admission, "monotonic", lambda: clock["monotonic"])
    return clock


def quote(body):
    now = datetime.now(UTC)
    return ProviderQuote(
        provider="bedrock",
        endpoint="/openai/v1/responses",
        capability="responses",
        billing_model_id=MODEL,
        request_sha256=request_digest(body),
        pricing_revision=_pricing_revision(
            load_snapshot().snapshot_version,
            cached_rate_state().source,
            cached_rate_state().generation_id,
            cached_rate_state().pointer_revision,
        ),
        currency="USD",
        issued_at=now,
        expires_at=now + timedelta(minutes=1),
        max_input_tokens=20,
        max_output_tokens=20,
        max_tool_tokens=0,
        max_cache_write_tokens=0,
        total_usd=Decimal("0.001"),
        evidence_source="test-snapshot",
    )


def rows(pg_url):
    import psycopg2

    with psycopg2.connect(pg_url) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT request_id, state, admission_key, admission_version, final_key, final_version FROM metering_attempts ORDER BY created_at"
        )
        return cursor.fetchall()


def object_at(client, key, version):
    return json.loads(client.get_object(Bucket="metering-test", Key=key, VersionId=version)["Body"].read())


class MockMeteredTransport:
    """A no-network provider fixture, not the physical-send adapter owned by #7033."""

    async def dispatch_once(self, client, request, permit, *, model, stream):
        permit.consume(
            submitted=request.content,
            model=model,
            destination=str(request.url),
            pricing_revision=permit.quote.pricing_revision,
        )
        return await client.send(request, stream=stream)


def service_for(pg_url, journal, context, provider):
    engine = create_async_engine(to_async_url(pg_url), poolclass=NullPool)
    admission = MeteringAdmission(async_sessionmaker(engine, expire_on_commit=False), journal)
    service = MantlePassthroughService(
        StubAuth(),
        "https://bedrock-mantle.us-east-1.api.aws",
        metering=admission,
        metered_transport=MockMeteredTransport(),
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
    )
    service._trusted_usage = AsyncMock(return_value=TrustedUsage(1, 2, True))
    service._log_usage = AsyncMock(return_value=True)
    app = build_app(service, ModelResolver(allowed_models_config={context.org_id: ["openai.*"]}), context)

    @app.middleware("http")
    async def assign_request_id(request, call_next):
        request.state.request_id = request.headers.get("x-test-server-request-id") or str(uuid4())
        return await call_next(request)

    return service, admission, engine, TestClient(app)


def authenticate_route(route, principals, monkeypatch):
    """Exercise the real bearer dependency with an isolated token verifier."""
    route.app.dependency_overrides.pop(get_token_context, None)

    async def verify(authorization):
        principal = principals.get(authorization)
        if principal is None:
            raise HTTPException(status_code=401, detail="invalid test bearer")
        return deepcopy(principal)

    monkeypatch.setattr("src.proxy.routes.validate_cognito_jwt", verify)
    return route


async def test_same_database_split_pools_progress_through_admission_and_settlement(metering_db, context, journal):
    from src.proxy.mantle_service import drain_usage_finalizers

    request_engine = create_async_engine(to_async_url(metering_db), pool_size=1, max_overflow=0, pool_timeout=1)
    accounting_engine = create_async_engine(to_async_url(metering_db), pool_size=1, max_overflow=0, pool_timeout=1)
    request_factory = async_sessionmaker(request_engine, expire_on_commit=False)
    accounting_factory = async_sessionmaker(accounting_engine, expire_on_commit=False)
    provider_started = asyncio.Event()
    release_provider = asyncio.Event()

    async def provider(_request):
        provider_started.set()
        await release_provider.wait()
        return httpx.Response(200, json={"usage": USAGE})

    service, _, original_engine, _route = service_for(metering_db, journal[1], context, provider)
    service._metering = MeteringAdmission(request_factory, journal[1], settlement_session_factory=accounting_factory)
    body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20}).encode()
    context._policy_quote = quote(body)
    request_task = None
    try:
        async with service._http_client:
            async with accounting_factory() as accounting_session:
                accounting_pid = await accounting_session.scalar(text("SELECT pg_backend_pid()"))
                request_task = asyncio.create_task(service.create_response(body, context, stream=False, model=MODEL, request_id="split-pool"))
                await asyncio.wait_for(provider_started.wait(), 10)
                assert accounting_engine.pool.checkedout() == 1
            async with request_factory() as request_session:
                request_pid = await request_session.scalar(text("SELECT pg_backend_pid()"))
                assert request_pid != accounting_pid
                assert request_engine.pool.checkedout() == 1
                release_provider.set()
                response = await asyncio.wait_for(request_task, 10)
                assert response.status_code == 200
                assert await asyncio.wait_for(drain_usage_finalizers(timeout_seconds=5), 10) == 0
                async with accounting_factory() as session:
                    assert await session.scalar(text("SELECT state FROM metering_attempts WHERE request_id='split-pool'")) == "settled"
                    assert await session.scalar(text("SELECT count(*) FROM budget_settlement_receipts WHERE request_id='split-pool'")) == 1
    finally:
        release_provider.set()
        if request_task is not None and not request_task.done():
            request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        await original_engine.dispose()
        await request_engine.dispose()
        await accounting_engine.dispose()


def test_admission_and_final_receipt_precede_success_for_both_responses_modes(metering_db, context, journal):
    client, store = journal
    calls = []

    def provider(request):
        pending = rows(metering_db)
        assert pending[-1][1] == "dispatch_armed"
        assert object_at(client, pending[-1][2], pending[-1][3])["request_sha256"] == request_digest(original)
        calls.append(request)
        if stream:
            event = {"type": "response.completed", "response": {"usage": USAGE}}
            return httpx.Response(200, content=b"event: response.completed\ndata: " + json.dumps(event).encode() + b"\n\n")
        return httpx.Response(200, json={"usage": USAGE})

    service, _, engine, route = service_for(metering_db, store, context, provider)

    try:
        for stream in (False, True):
            original = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20, "stream": stream}).encode()
            context._policy_quote = quote(original)
            response = route.post("/openai/v1/responses", content=original, headers={"Content-Type": "application/json"})
            assert response.status_code == 200
            pending = rows(metering_db)
            assert pending[-1][0] == response.headers["x-request-id"]
            assert pending[-1][5]
            final = object_at(client, pending[-1][4], pending[-1][5])
            assert final["usage"] == USAGE
            assert final["admission_version_id"] == pending[-1][3]
            assert final["request_id"] == pending[-1][0]
        assert len(calls) == 2
        assert service._log_usage.await_count == 0
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.parametrize("failure", ["gate", "admission", "arm", "final", "wall_expiry", "monotonic_expiry"])
@pytest.mark.parametrize("stream", [False, True])
def test_admission_and_receipt_fail_closed_on_real_route(metering_db, context, journal, metering_clock, monkeypatch, failure, stream):
    import psycopg2

    client, store = journal
    calls = []

    def provider(request):
        calls.append(request)
        if stream:
            event = {"type": "response.completed", "response": {"usage": USAGE}}
            return httpx.Response(200, content=b"event: response.completed\ndata: " + json.dumps(event).encode() + b"\n\n")
        return httpx.Response(200, json={"usage": USAGE})

    if failure == "gate":
        with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
            cursor.execute("UPDATE metering_scope_gates SET readiness='unverified' WHERE scope_kind='org'")
    if failure == "admission":
        store.write_admission = AsyncMock(side_effect=TimeoutError("journal unavailable"))
    if failure == "final":
        store.write_final = AsyncMock(side_effect=TimeoutError("journal unavailable"))
    service, admission, engine, route = service_for(metering_db, store, context, provider)
    if failure == "arm":
        admission.arm = AsyncMock(side_effect=MeteringAdmissionError("stale revision"))
    if failure in {"wall_expiry", "monotonic_expiry"}:
        dispatch_once = service._metered_transport.dispatch_once

        async def expire_before_handoff(*args, **kwargs):
            if failure == "wall_expiry":
                metering_clock["wall"] += timedelta(seconds=2)
            else:
                metering_clock["monotonic"] += 2
            return await dispatch_once(*args, **kwargs)

        monkeypatch.setattr(service._metered_transport, "dispatch_once", expire_before_handoff)
    try:
        original = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20, "stream": stream}).encode()
        context._policy_quote = quote(original)
        response = route.post("/openai/v1/responses", content=original, headers={"Content-Type": "application/json"})
        if failure != "final":
            assert response.status_code == 503, response.text
            assert not calls
        else:
            assert response.status_code == (200 if stream else 502), response.text
            assert len(calls) == 1
            store.write_final.assert_awaited_once()
            if stream:
                assert b"event: error" in response.content
                assert b"usage_settlement_unconfirmed" in response.content
                assert b"response.completed" not in response.content
            else:
                assert b"usage_settlement_unconfirmed" in response.content
        if failure == "gate":
            assert rows(metering_db) == []
        else:
            assert rows(metering_db)[-1][5] is None
            if failure in {"wall_expiry", "monotonic_expiry"}:
                assert response.json()["error"] == "metering_dispatch_unavailable"
                assert rows(metering_db)[-1][1] == "dispatch_armed"
                assert service._log_usage.await_count == 0
            if failure == "final":
                assert rows(metering_db)[-1][1] == "unknown"
                denied = route.post("/openai/v1/responses", content=original, headers={"Content-Type": "application/json"})
                assert denied.status_code == 503 and len(calls) == 1
                assert service._log_usage.await_count == 0
    finally:
        asyncio.run(engine.dispose())


async def test_delayed_receipt_keeps_terminal_withheld_and_pending_attempt_discoverable(metering_db, context, journal):
    from src.proxy.mantle_service import drain_usage_finalizers

    client, store = journal
    started = asyncio.Event()
    release = asyncio.Event()
    original_write = store.write_final

    async def delayed_write(**kwargs):
        started.set()
        await release.wait()
        return await original_write(**kwargs)

    store.write_final = delayed_write

    def provider(_request):
        event = {"type": "response.completed", "response": {"usage": USAGE}}
        return httpx.Response(200, content=b"event: response.completed\ndata: " + json.dumps(event).encode() + b"\n\n")

    service, _, engine, _route = service_for(metering_db, store, context, provider)
    service._stream_finalization_timeout = 0.02
    body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20, "stream": True}).encode()
    context._policy_quote = quote(body)
    try:
        stream = await service.create_response(body, context, stream=True, model=MODEL, request_id="delayed-receipt")
        payload = b"".join([chunk async for chunk in stream])
        assert started.is_set()
        assert b"usage_settlement_unconfirmed" in payload
        assert b"response.completed" not in payload
        pending = rows(metering_db)
        assert pending[0][1] == "dispatch_armed"
        assert pending[0][5] is None
        release.set()
        assert await drain_usage_finalizers(timeout_seconds=3) == 0
        pending = rows(metering_db)
        assert object_at(client, pending[0][4], pending[0][5])["usage"] == USAGE
    finally:
        release.set()
        await engine.dispose()


def test_delegated_attribution_does_not_authorize_another_tenant(metering_db, context, journal):
    _client, store = journal
    calls = []
    service, _, engine, route = service_for(metering_db, store, context, lambda request: calls.append(request))
    context.attributed_org_id = "another-tenant"
    body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20}).encode()
    context._policy_quote = quote(body)
    try:
        response = route.post("/openai/v1/responses", content=body, headers={"Content-Type": "application/json"})
        assert response.status_code == 503
        assert rows(metering_db) == []
        assert calls == []
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.parametrize("settlement_succeeds", [True, False])
@pytest.mark.parametrize("admission_delay_seconds", [0, 1.1])
async def test_receipted_settlement_runs_after_http_and_shutdown_drain_is_bounded(
    metering_db, context, journal, metering_clock, settlement_succeeds, admission_delay_seconds
):
    from src.proxy.mantle_service import drain_usage_finalizers

    client, store = journal

    def provider(request):
        if json.loads(request.content).get("stream"):
            event = {"type": "response.completed", "response": {"usage": USAGE}}
            return httpx.Response(200, content=b"event: response.completed\ndata: " + json.dumps(event).encode() + b"\n\n")
        return httpx.Response(200, json={"usage": USAGE})

    for streaming in (False, True):
        started = asyncio.Event()
        release = asyncio.Event()
        service, _, engine, _route = service_for(metering_db, store, context, provider)
        prepare = service._metering.prepare

        async def delayed_prepare(**kwargs):
            await asyncio.sleep(admission_delay_seconds)
            return await prepare(**kwargs)

        service._metering.prepare = delayed_prepare

        async def delayed_sql(*_args, **_kwargs):
            started.set()
            await release.wait()
            return settlement_succeeds

        service._metering.settle_receipt = AsyncMock(side_effect=delayed_sql)
        body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20, "stream": streaming}).encode()
        context._policy_quote = quote(body)
        request_id = f"settlement-{settlement_succeeds}-{streaming}"
        try:
            async with service._http_client:
                response = await asyncio.wait_for(service.create_response(body, context, stream=streaming, model=MODEL, request_id=request_id), 10)
                if streaming:
                    response_body = await asyncio.wait_for(consume(response), 10)
                    assert b"response.completed" in response_body
                else:
                    assert response.status_code == 200
                await asyncio.wait_for(started.wait(), 10)
                pending = [row for row in rows(metering_db) if row[0] == request_id][0]
                assert pending[1] == "dispatch_armed"
                assert object_at(client, pending[4], pending[5])["usage"] == USAGE
                assert await drain_usage_finalizers(timeout_seconds=0.02) == 1
                assert service._metering.settle_receipt.await_count == 1
                release.set()
                assert await drain_usage_finalizers(timeout_seconds=2) == 0
                assert service._metering.settle_receipt.await_count == 1
        finally:
            release.set()
            await engine.dispose()


async def consume(stream):
    return b"".join([chunk async for chunk in stream])


@pytest.mark.parametrize("admission_delay_seconds", [0, 1.1])
async def test_nonstream_receipt_finishing_after_timeout_still_settles_from_durable_work(
    metering_db, context, journal, metering_clock, admission_delay_seconds
):
    from src.proxy.mantle_service import MantleUpstreamError, drain_usage_finalizers

    client, store = journal
    release = asyncio.Event()
    started = asyncio.Event()
    write_final = store.write_final

    async def delayed_write(**kwargs):
        started.set()
        await release.wait()
        return await write_final(**kwargs)

    store.write_final = delayed_write
    service, _, engine, _route = service_for(metering_db, store, context, lambda _: httpx.Response(200, json={"usage": USAGE}))
    prepare = service._metering.prepare

    async def delayed_prepare(**kwargs):
        await asyncio.sleep(admission_delay_seconds)
        return await prepare(**kwargs)

    service._metering.prepare = delayed_prepare
    service._stream_finalization_timeout = 0.02
    service._metering.settle_receipt = AsyncMock(return_value=True)
    body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20}).encode()
    context._policy_quote = quote(body)
    request_task = None
    try:
        async with service._http_client:
            request_task = asyncio.create_task(service.create_response(body, context, stream=False, model=MODEL, request_id="late-final"))
            await asyncio.wait_for(started.wait(), 10)
            with pytest.raises(MantleUpstreamError) as failure:
                await asyncio.wait_for(request_task, 1)
            assert failure.value.status_code == 502
            assert started.is_set()
            assert service._log_usage.await_count == 0
            assert rows(metering_db)[0][5] is None
            release.set()
            assert await drain_usage_finalizers(timeout_seconds=3) == 0
            pending = rows(metering_db)[0]
            assert object_at(client, pending[4], pending[5])["usage"] == USAGE
            service._metering.settle_receipt.assert_awaited_once()
    finally:
        release.set()
        if request_task is not None:
            if not request_task.done():
                request_task.cancel()
            await asyncio.gather(request_task, return_exceptions=True)
        await drain_usage_finalizers(timeout_seconds=3)
        await engine.dispose()


async def test_terminal_stops_reading_and_slow_close_does_not_hold_stream(metering_db, context, journal):
    from src.proxy.mantle_service import drain_usage_finalizers

    client, store = journal
    release = asyncio.Event()
    closing = asyncio.Event()

    class SlowCloseStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            event = {"type": "response.completed", "response": {"usage": USAGE}}
            yield b"event: response.completed\ndata: " + json.dumps(event).encode() + b"\n\n"
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            await release.wait()

    upstream_stream = SlowCloseStream()
    service, _, engine, _route = service_for(metering_db, store, context, lambda _: httpx.Response(200, stream=upstream_stream))
    service._stream_finalization_timeout = 3
    body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20, "stream": True}).encode()
    context._policy_quote = quote(body)
    settlement_started = asyncio.Event()
    settlement_task = None
    settle_receipt = service._metering.settle_receipt

    async def observe_settlement(*args, **kwargs):
        nonlocal settlement_task
        settlement_task = asyncio.current_task()
        settlement_started.set()
        return await settle_receipt(*args, **kwargs)

    service._metering.settle_receipt = observe_settlement
    try:
        async with service._http_client:
            response = await service.create_response(body, context, stream=True, model=MODEL, request_id="slow-close")
            started = asyncio.get_running_loop().time()
            payload = await asyncio.wait_for(consume(response), 10)
            assert asyncio.get_running_loop().time() - started < 5
            assert b"response.completed" in payload
            assert closing.is_set()
            pending = rows(metering_db)[0]
            assert object_at(client, pending[4], pending[5])["usage"] == USAGE
            await asyncio.wait_for(settlement_started.wait(), timeout=5)
            assert settlement_task is not None
            assert await asyncio.wait_for(asyncio.shield(settlement_task), timeout=5) is True
            import psycopg2

            with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                cursor.execute("SELECT state FROM metering_attempts WHERE request_id='slow-close'")
                assert cursor.fetchone() == ("settled",)
                cursor.execute("SELECT cost_usd FROM budget_settlement_receipts WHERE request_id='slow-close'")
                charged = cursor.fetchall()
                assert len(charged) == 1 and charged[0][0] > 0
                cursor.execute("SELECT cost_usd, input_tokens, output_tokens FROM usage_logs WHERE request_id='slow-close'")
                assert cursor.fetchall() == [(charged[0][0], 1, 2)]
            assert closing.is_set()
            close_tasks = [task for task in asyncio.all_tasks() if task.get_name() == "mantle_upstream_close_slow-close"]
            assert len(close_tasks) == 1 and not close_tasks[0].done()
            release.set()
            assert await asyncio.wait_for(asyncio.shield(close_tasks[0]), timeout=2) is True
            await drain_usage_finalizers(timeout_seconds=2)
    finally:
        release.set()
        await engine.dispose()


async def test_nonstream_owned_client_close_cannot_delay_receipt_or_response(metering_db, context, journal, metering_clock):
    from src.proxy.mantle_service import drain_usage_finalizers

    client, store = journal
    service, _, engine, _route = service_for(metering_db, store, context, lambda _: httpx.Response(200, json={"usage": USAGE}))
    owned_client = service._http_client
    service._http_client = None
    service._client = lambda: owned_client
    close_started = asyncio.Event()
    release = asyncio.Event()
    original_close = owned_client.aclose

    async def slow_close():
        close_started.set()
        await release.wait()
        await original_close()

    owned_client.aclose = AsyncMock(side_effect=slow_close)
    body = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20}).encode()
    context._policy_quote = quote(body)
    try:
        # Freeze only the dispatch-permit clock; client close and settlement
        # deadlines remain real. Shared-runner delays must not expire the permit
        # before the cleanup ordering under test begins.
        # PostgreSQL admission precedes the behavior under test. Allow scheduling
        # headroom on shared runners; the unreleased event, not elapsed database
        # time, proves that closing the owned client cannot hold the response.
        response = await asyncio.wait_for(service.create_response(body, context, stream=False, model=MODEL, request_id="slow-client-close"), 10)
        assert response.status_code == 200
        await asyncio.wait_for(close_started.wait(), 10)
        assert not release.is_set()
        pending = rows(metering_db)[0]
        assert object_at(client, pending[4], pending[5])["usage"] == USAGE
        assert await drain_usage_finalizers(timeout_seconds=0.02) >= 1
        release.set()
        assert await drain_usage_finalizers(timeout_seconds=2) == 0
        owned_client.aclose.assert_awaited_once()
    finally:
        release.set()
        await engine.dispose()


def test_draining_denies_new_responses_before_provider_but_preserves_in_flight_receipt(metering_db, context, journal, monkeypatch):
    monkeypatch.setenv("BG_METERING_GATE_DRAIN_ENABLED", "true")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import psycopg2

    from src.budget.metering_activation import transition_metering_gate
    from tests.budget.test_metering_writer_compatibility import ledger_rows

    entered, release = Event(), Event()
    calls = []

    async def provider(request):
        calls.append(request)
        entered.set()
        if not await asyncio.to_thread(release.wait, 15):
            raise TimeoutError("provider fixture not released")
        return httpx.Response(200, json={"usage": USAGE})

    _, store = journal
    service, _, engine, route = service_for(metering_db, store, context, provider)
    original = json.dumps({"model": MODEL, "input": "hello", "max_output_tokens": 20}).encode()
    context._policy_quote = quote(original)

    async def drain():
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            for kind in ("agent", "org", "user"):
                with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                    cursor.execute("SELECT scope_key FROM metering_scope_gates WHERE scope_kind=%s AND tenant_id='tenant-1'", (kind,))
                    key = cursor.fetchone()[0]
                await transition_metering_gate(session, scope_key=key, scope_kind=kind, tenant_id="tenant-1", activation="draining")
            with pytest.raises(ValueError, match="physical-send guard"):
                await transition_metering_gate(session, scope_key=key, scope_kind=kind, tenant_id="tenant-1", activation="active")
            await session.commit()

    try:
        from src.proxy.mantle_service import drain_usage_finalizers

        with route:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(route.post, "/openai/v1/responses", content=original, headers={"Content-Type": "application/json"})
                try:
                    assert entered.wait(10)
                    assert len(calls) == 1
                    assert rows(metering_db)[-1][1] == "dispatch_armed"
                    asyncio.run(drain())
                    with psycopg2.connect(metering_db) as connection, connection.cursor() as cursor:
                        cursor.execute("SELECT count(*) FROM metering_scope_gates WHERE activation='active'")
                        assert cursor.fetchone()[0] == 0
                        cursor.execute("SELECT count(*) FROM metering_attempts WHERE state='dispatch_armed'")
                        assert cursor.fetchone()[0] == 1
                    second = pool.submit(route.post, "/openai/v1/responses", content=original, headers={"Content-Type": "application/json"})
                    denied = second.result(timeout=10)
                    assert denied.status_code == 503
                    assert len(calls) == 1
                    assert ledger_rows(metering_db) == ([], [], [])
                finally:
                    release.set()
                accepted = first.result(timeout=15)
            # This integration assertion covers settlement, not a two-second SLA.
            # Use the production drain window when coverage slows the database.
            assert route.portal.call(drain_usage_finalizers) == 0
        assert accepted.status_code == 200
        pending = rows(metering_db)
        assert len(pending) == 1
        assert pending[0][5]
        receipts, debits, usages = ledger_rows(metering_db)
        assert len(receipts) == len(usages) == 1
        assert receipts[0][0] == accepted.headers["x-request-id"]
        assert len(debits) == 9 and all(row[-1] == 1 for row in debits)
    finally:
        release.set()
        asyncio.run(engine.dispose())
