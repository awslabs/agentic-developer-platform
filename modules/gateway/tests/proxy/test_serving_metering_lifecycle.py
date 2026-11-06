"""Serving startup and admission lifecycle without a billed provider or test service."""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.app import lifespan
from src.budget.enforcement_middleware import BudgetEnforcementMiddleware
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.enforcement_settings import EnforcementPosture
from src.budget.run_binding import RunBinding, RunBindingError
from src.proxy.bedrock_enforcement import RoutingDecision
from src.proxy.mantle_service import MantleUpstreamError
from src.proxy.metered_transport import MeteredHTTPTransport
from src.proxy.routes import get_mantle_service
from src.shared.config import Settings
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.budget import EnforcementResult


@pytest.fixture
def serving_setup(monkeypatch):
    async def idle():
        await asyncio.Event().wait()

    pool = SimpleNamespace(aclose=AsyncMock())
    ratelimit = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
    storage = SimpleNamespace(close=MagicMock())
    gateway = FastAPI()
    gateway.state.ratelimit_service = ratelimit
    monkeypatch.setattr("src.app.get_settings", lambda: Settings(mantle_enabled=True, db_auto_create=False, mantle_operation_timeout_seconds=720))
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: object())
    monkeypatch.setattr("src.pool.simple_pool.SimplePoolService", lambda: pool)
    monkeypatch.setattr("src.proxy.model_resolver.production_model_resolver", lambda settings: object())
    monkeypatch.setattr("src.proxy.mantle_auth.make_mantle_auth", lambda region: SimpleNamespace(sign=lambda *args: {}))
    monkeypatch.setattr("src.shared.database.get_accounting_session_factory", lambda: object())
    monkeypatch.setattr("src.budget.pricing_decisions.refresh_pricing_cache", AsyncMock())
    monkeypatch.setattr("src.budget.pricing_decisions.maintain_pricing_cache", idle)
    monkeypatch.setattr("src.orchestration.work_admission.maintain_work_claims", idle)
    monkeypatch.setattr("src.agentauth.chat_notifications.maintain_chat_notifications", idle)
    monkeypatch.setattr("src.budget.metering_recovery.maintain_metering_recovery", lambda *args: idle())
    monkeypatch.setattr("src.proxy.mantle_service.drain_usage_finalizers", AsyncMock())
    monkeypatch.setattr("src.app.shutdown_tracing", MagicMock())
    monkeypatch.setattr("boto3.client", lambda *args, **kwargs: storage)
    yield gateway, pool, storage, ratelimit


@pytest.mark.parametrize("enabled", [False, True])
async def test_serving_app_is_default_off_and_closes_owned_resources(serving_setup, monkeypatch, enabled):
    gateway, pool, storage, ratelimit = serving_setup
    monkeypatch.delenv("BG_METERED_RESPONSES_ENABLED", raising=False)
    if enabled:
        monkeypatch.setenv("BG_METERING_JOURNAL_BUCKET", "fictional-test-journal")
        monkeypatch.setenv("BG_METERED_RESPONSES_ENABLED", "true")
    else:
        monkeypatch.delenv("BG_METERING_JOURNAL_BUCKET", raising=False)
    async with lifespan(gateway):
        service = get_mantle_service()
        assert gateway.state.metered_responses_ready is enabled
        if enabled:
            assert isinstance(service._metered_transport, MeteredHTTPTransport)
            assert service._http_client._transport is service._metered_transport
            assert service._metering._journal._client is storage
            owned_client = service._http_client
        else:
            assert service._metered_transport is service._metering is service._http_client is None
    assert gateway.state.shutting_down and not gateway.state.metered_responses_ready
    with pytest.raises(HTTPException) as disabled:
        get_mantle_service()
    assert disabled.value.status_code == 503
    pool.aclose.assert_awaited_once()
    ratelimit.close.assert_awaited_once()
    if enabled:
        assert owned_client.is_closed
        storage.close.assert_called_once()
    else:
        storage.close.assert_not_called()


async def test_configured_metering_failure_never_falls_back_to_legacy(serving_setup, monkeypatch):
    gateway, _, storage, _ = serving_setup
    monkeypatch.setenv("BG_METERING_JOURNAL_BUCKET", "fictional-test-journal")
    monkeypatch.setenv("BG_METERED_RESPONSES_ENABLED", "true")

    def unavailable(*args, **kwargs):
        raise RuntimeError("local S3 configuration failed")

    monkeypatch.setattr("boto3.client", unavailable)
    async with lifespan(gateway):
        assert not gateway.state.metered_responses_ready
        with pytest.raises(HTTPException):
            get_mantle_service()
    storage.close.assert_not_called()


@pytest.mark.parametrize("scope_kind", ["strict", "delegated"])
async def test_serving_admission_refuses_unsupported_scope_before_provider(serving_setup, monkeypatch, scope_kind):
    gateway, _, _, _ = serving_setup
    monkeypatch.setenv("BG_METERING_JOURNAL_BUCKET", "fictional-test-journal")
    monkeypatch.setenv("BG_METERED_RESPONSES_ENABLED", "true")
    monkeypatch.setattr("src.proxy.mantle_service.resolve_routing_decision", AsyncMock(return_value=RoutingDecision()))
    physical_calls = []
    async with lifespan(gateway):
        service = get_mantle_service()
        client = service._http_client
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: physical_calls.append(request))) as local_client:
            service._http_client = local_client
            context = TokenContext(
                user_id="local-owner",
                org_id="local-tenant",
                team_id="",
                department_id="",
                account_type="service" if scope_kind == "strict" else "user",
                expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
            if scope_kind == "strict":
                context._policy_flow_target = object()
            try:
                with pytest.raises(MantleUpstreamError) as error:
                    await service.create_response(
                        b'{"model":"openai.gpt-6-astra","input":"hello"}', context, stream=False, model="openai.gpt-6-astra"
                    )
                assert error.value.status_code == 503
            finally:
                service._http_client = client
    assert physical_calls == []


@pytest.mark.parametrize(
    "path,ready,native_header,allowed",
    [
        ("/openai/v1/responses", True, False, True),
        ("/openai/v1/responses", False, False, False),
        ("/v1/chat/completions", True, False, False),
        ("/openai/v1/responses", True, True, False),
    ],
)
async def test_serving_fence_allows_only_owned_metered_responses(monkeypatch, path, ready, native_header, allowed):
    async def denied(*args, **kwargs):
        raise ValueError("local gate active or draining")

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    monkeypatch.setenv("BG_METERING_GATE_DRAIN_ENABLED", "true")
    monkeypatch.setattr("src.budget.enforcement_middleware.require_non_draining_admission", denied)
    budget = SimpleNamespace(
        _get_session=lambda: Session(),
        prepare_enforcement_context=AsyncMock(return_value=None),
        check_budget_hierarchy=AsyncMock(return_value=EnforcementResult(allowed=True)),
        reconcile_reservation=AsyncMock(),
    )
    physical_calls = []
    responses = []

    async def next_app(scope, receive, send):
        physical_calls.append(scope)

    async def send(message):
        responses.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    token = TokenContext(
        user_id="local-owner",
        org_id="local-tenant",
        team_id="",
        department_id="",
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "path": path,
        "method": "POST",
        "headers": [(b"x-adp-native-test-capability", b"fictional")] if native_header else [],
        "state": {"token_context": token, "request_id": "local-request"},
        "app": SimpleNamespace(state=SimpleNamespace(metered_responses_ready=ready)),
    }
    await asyncio.wait_for(BudgetEnforcementMiddleware(next_app, enforcement_service=budget)(scope, receive, send), timeout=1)
    assert bool(physical_calls) is allowed
    if not allowed:
        start = next(message for message in responses if message["type"] == "http.response.start")
        assert start["status"] == (403 if native_header else 503)
    if allowed:
        budget.prepare_enforcement_context.assert_awaited_once()
        assert budget.check_budget_hierarchy.await_args.kwargs["reserve"] is False
        budget.reconcile_reservation.assert_not_awaited()
    else:
        budget.prepare_enforcement_context.assert_not_awaited()
        budget.check_budget_hierarchy.assert_not_awaited()


async def test_recovery_only_journal_does_not_enable_serving(serving_setup, monkeypatch):
    gateway, _, storage, _ = serving_setup
    monkeypatch.setenv("BG_METERING_JOURNAL_BUCKET", "fictional-test-journal")
    monkeypatch.delenv("BG_METERED_RESPONSES_ENABLED", raising=False)
    monkeypatch.setenv("BG_METERING_GATE_DRAIN_ENABLED", "true")
    gate = AsyncMock(side_effect=ValueError("draining"))
    monkeypatch.setattr("src.budget.enforcement_middleware.require_non_draining_admission", gate)
    session = AsyncMock()
    session.__aenter__.return_value = session
    budget = SimpleNamespace(_get_session=lambda: session, prepare_enforcement_context=AsyncMock())
    calls, responses = [], []

    async def next_app(scope, receive, send):
        calls.append(scope)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        responses.append(message)

    token = TokenContext(
        user_id="local-owner",
        org_id="local-tenant",
        team_id="",
        department_id="",
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    async with lifespan(gateway):
        assert gateway.state.metered_responses_ready is False
        assert get_mantle_service()._metering is None
        await asyncio.wait_for(
            BudgetEnforcementMiddleware(next_app, enforcement_service=budget)(
                {
                    "type": "http",
                    "http_version": "1.1",
                    "path": "/openai/v1/responses",
                    "method": "POST",
                    "headers": [],
                    "state": {"token_context": token, "request_id": "local-request"},
                    "app": gateway,
                },
                receive,
                send,
            ),
            timeout=1,
        )
    assert calls == []
    assert next(message["status"] for message in responses if message["type"] == "http.response.start") == 503
    gate.assert_awaited_once()
    budget.prepare_enforcement_context.assert_not_awaited()
    storage.close.assert_called_once()


async def test_enabled_serving_without_journal_refuses_instead_of_falling_back(serving_setup, monkeypatch):
    gateway, _, _, _ = serving_setup
    monkeypatch.setenv("BG_METERED_RESPONSES_ENABLED", "true")
    monkeypatch.delenv("BG_METERING_JOURNAL_BUCKET", raising=False)
    async with lifespan(gateway):
        assert gateway.state.metered_responses_ready is False
        with pytest.raises(HTTPException) as unavailable:
            get_mantle_service()
        assert unavailable.value.status_code == 503


async def test_serving_request_pool_progresses_while_accounting_pool_is_occupied(serving_setup, monkeypatch, tmp_path):
    gateway, _, _, _ = serving_setup
    monkeypatch.setenv("BG_METERING_JOURNAL_BUCKET", "fictional-test-journal")
    monkeypatch.setenv("BG_METERED_RESPONSES_ENABLED", "true")
    request_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'request.db'}", pool_size=1, max_overflow=0)
    accounting_engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'accounting.db'}", pool_size=1, max_overflow=0)
    request_factory = async_sessionmaker(request_engine)
    accounting_factory = async_sessionmaker(accounting_engine)
    monkeypatch.setattr("src.shared.database.get_session_factory", lambda: request_factory)
    monkeypatch.setattr("src.shared.database.get_accounting_session_factory", lambda: accounting_factory)
    recovery_factories = []

    async def recovery(factory, journal):
        recovery_factories.append(factory)
        await asyncio.Event().wait()

    monkeypatch.setattr("src.budget.metering_recovery.maintain_metering_recovery", recovery)
    try:
        async with lifespan(gateway):
            metering = get_mantle_service()._metering
            assert metering._session_factory is request_factory
            assert metering._settlement_session_factory is accounting_factory
            async with accounting_factory() as occupied:
                await occupied.execute(text("SELECT 1"))
                async with asyncio.timeout(1):
                    async with metering._session_factory() as request:
                        assert (await request.scalar(text("SELECT 1"))) == 1
            await asyncio.sleep(0)
            assert recovery_factories == [accounting_factory]
            settle = AsyncMock(return_value=True)
            monkeypatch.setattr("src.budget.metering_settlement.settle_metering_receipt", settle)
            attempt = SimpleNamespace(receipt=object(), scope_revisions={})
            assert await metering.settle_receipt(attempt, object()) is True
            assert settle.await_args.args[0] is accounting_factory
    finally:
        await request_engine.dispose()
        await accounting_engine.dispose()


@pytest.mark.parametrize("failure,expected_status", [("unbound_run", 402), ("accounting_incomplete", 503), ("posture_unavailable", 503)])
@pytest.mark.parametrize("body_mode", ["complete", "stalled", "disconnected"])
@pytest.mark.parametrize("http_version", ["1.1", "2"])
async def test_metered_responses_preserve_server_control_denials(monkeypatch, failure, expected_status, body_mode, http_version):
    budget = BudgetEnforcementService()
    if failure == "unbound_run":
        budget._resolve_run_scope = AsyncMock(side_effect=RunBindingError("unknown_run", "unbound run"))
    else:
        budget._resolve_run_scope = AsyncMock(
            return_value=RunBinding("fictional-run", "local-flow", "local-tenant", "local-owner", "local-owner", False, "local-flow")
        )

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(
            get=AsyncMock(return_value=None),
            scalar=AsyncMock(return_value=SimpleNamespace(product_id=None, project_id=None)),
            commit=AsyncMock(),
        )

    budget._get_session = session
    posture = AsyncMock(return_value=EnforcementPosture(True, None, 1, 0, accounting_incomplete=failure == "accounting_incomplete"))
    if failure == "posture_unavailable":
        posture.side_effect = TimeoutError("local posture unavailable")
    monkeypatch.setattr("src.budget.enforcement_service.read_enforcement", posture)
    calls, responses = [], []

    async def next_app(scope, receive, send):
        calls.append(scope)

    chunks = [
        {"type": "http.request", "body": b"local-payload", "more_body": True},
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    receive_cancelled = asyncio.Event()

    async def receive():
        if body_mode == "disconnected":
            return {"type": "http.disconnect"}
        if body_mode == "stalled":
            try:
                await asyncio.Event().wait()
            finally:
                receive_cancelled.set()
        return chunks.pop(0)

    async def send(message):
        responses.append(message)

    token = TokenContext(
        user_id="local-owner",
        org_id="local-tenant",
        team_id="",
        department_id="",
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    scope = {
        "type": "http",
        "http_version": http_version,
        "path": "/openai/v1/responses",
        "method": "POST",
        "headers": [(b"x-agent-runid", b"fictional-run")],
        "state": {"token_context": token, "request_id": "local-request"},
        "app": SimpleNamespace(state=SimpleNamespace(metered_responses_ready=True)),
    }
    await asyncio.wait_for(BudgetEnforcementMiddleware(next_app, enforcement_service=budget)(scope, receive, send), timeout=1)
    assert calls == []
    if body_mode == "disconnected":
        assert responses == []
    else:
        start = next(message for message in responses if message["type"] == "http.response.start")
        assert start["status"] == expected_status
        assert ((b"connection", b"close") in start["headers"]) == (body_mode == "stalled" and http_version == "1.1")
        assert responses[-1]["type"] == "http.response.body"
    if body_mode == "stalled":
        await asyncio.wait_for(receive_cancelled.wait(), timeout=1)
    budget._resolve_run_scope.assert_awaited_once_with(token, "fictional-run")
    if failure == "unbound_run":
        posture.assert_not_awaited()
    else:
        posture.assert_awaited_once()


@pytest.mark.parametrize("action", ["mark_unknown", "write_final"])
async def test_post_provider_updates_use_accounting_factory(monkeypatch, action):
    from src.budget.metering_admission import MeteringAdmission

    receipt = SimpleNamespace(
        tenant_id="local-tenant",
        request_id="local-request",
        owner_id="local-owner",
        version_id="local-version",
        sha256="a" * 64,
    )
    attempt = SimpleNamespace(receipt=receipt, scope_revisions={}, producer_epoch=1)
    journal = SimpleNamespace(write_final=AsyncMock(return_value=receipt))
    request_factory = MagicMock(side_effect=AssertionError("request pool used after provider handoff"))
    session = AsyncMock()
    session.__aenter__.return_value = session
    session.execute.return_value = SimpleNamespace(rowcount=1)
    accounting_factory = MagicMock(return_value=session)
    metering = MeteringAdmission(request_factory, journal, settlement_session_factory=accounting_factory)

    if action == "mark_unknown":
        assert await metering.mark_unknown(attempt)
    else:
        assert (
            await metering.write_final(
                attempt=attempt,
                usage={},
                trusted=object(),
                provider_request_id=None,
                decision=SimpleNamespace(to_dict=lambda: {}),
                latency_ms=1,
            )
            is receipt
        )
    request_factory.assert_not_called()
    accounting_factory.assert_called_once()
    session.execute.assert_awaited_once()
    session.commit.assert_awaited_once()


@pytest.mark.parametrize("metered", [True, False])
async def test_serving_timeout_is_explicit_and_metered_requires_it(serving_setup, monkeypatch, metered):
    gateway, _, storage, _ = serving_setup
    monkeypatch.setattr("src.app.get_settings", lambda: Settings(mantle_enabled=True, db_auto_create=False, mantle_operation_timeout_seconds=None))
    monkeypatch.setenv("BG_METERING_JOURNAL_BUCKET", "fictional-test-journal")
    if metered:
        monkeypatch.setenv("BG_METERED_RESPONSES_ENABLED", "true")
    else:
        monkeypatch.delenv("BG_METERED_RESPONSES_ENABLED", raising=False)
    async with lifespan(gateway):
        if metered:
            assert not gateway.state.metered_responses_ready
            with pytest.raises(HTTPException) as unavailable:
                get_mantle_service()
            assert unavailable.value.status_code == 503
        else:
            assert get_mantle_service()._operation_timeout is None
    storage.close.assert_called_once()


async def test_metered_denial_cancellation_releases_pending_body_receive():
    budget = BudgetEnforcementService()
    budget._resolve_run_scope = AsyncMock(side_effect=RunBindingError("unknown_run", "unbound run"))
    started = asyncio.Event()
    cancelled = asyncio.Event()
    calls = []

    async def receive():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def next_app(scope, receive, send):
        calls.append(scope)

    async def send(message):
        calls.append(message)

    token = TokenContext(
        user_id="local-owner",
        org_id="local-tenant",
        team_id="",
        department_id="",
        account_type="service",
        expires_at=datetime.now(UTC) + timedelta(hours=1),
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "path": "/openai/v1/responses",
        "method": "POST",
        "headers": [(b"x-agent-runid", b"fictional-run")],
        "state": {"token_context": token, "request_id": "local-request"},
        "app": SimpleNamespace(state=SimpleNamespace(metered_responses_ready=True)),
    }
    task = asyncio.create_task(BudgetEnforcementMiddleware(next_app, enforcement_service=budget)(scope, receive, send))
    await asyncio.wait_for(started.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    await asyncio.wait_for(cancelled.wait(), 1)
    assert calls == []
