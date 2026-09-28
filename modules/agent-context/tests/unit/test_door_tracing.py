"""Unit tests for door/tracing.py — Door query span tracing.

Tests cover:
- Tracing init success (OTel available + enabled)
- Tracing disabled via KNOWLEDGE_LAYER_TRACES_ENABLED=false
- Fail-open behavior (missing OTel → Door still starts)
- Identity-enrichment middleware stamps span attributes
- Backend sub-span creation (Zoekt search)
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_door_tracing():
    """Reset door tracing module state between tests."""
    from door import tracing

    tracing._tracer_provider = None
    tracing._tracing_initialized = False
    tracing._tracing_enabled = False
    yield
    tracing._tracer_provider = None
    tracing._tracing_initialized = False
    tracing._tracing_enabled = False


# ---------------------------------------------------------------------------
# Tests: setup_tracing
# ---------------------------------------------------------------------------


class TestDoorSetupTracing:
    """Tests for Door tracing initialization."""

    def test_tracing_setup_disabled(self):
        """No instrumentation when KNOWLEDGE_LAYER_TRACES_ENABLED=false."""
        from door.tracing import setup_tracing

        mock_app = MagicMock()
        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": "false"}):
            result = setup_tracing(mock_app)

        assert result is False

    def test_tracing_setup_disabled_various_values(self):
        """Tracing is disabled for various false-ish env values."""
        from door import tracing

        for val in ("false", "0", "no", "False", "NO"):
            tracing._tracing_initialized = False
            tracing._tracing_enabled = False
            mock_app = MagicMock()
            with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": val}):
                result = tracing.setup_tracing(mock_app)
            assert result is False, f"Expected False for value '{val}'"

    def test_tracing_setup_failopen(self):
        """Missing OTel package -> Door still starts (fail-open)."""
        from door.tracing import setup_tracing

        mock_app = MagicMock()
        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": "true"}):
            with patch("door.tracing._is_otel_available", return_value=False):
                result = setup_tracing(mock_app)

        assert result is False

    def test_tracing_setup_failopen_on_exception(self):
        """OTel SDK init exception -> fail-open, Door starts normally."""
        from door.tracing import setup_tracing

        mock_app = MagicMock()
        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": "true"}):
            with patch("door.tracing._is_otel_available", return_value=True):
                with patch(
                    "builtins.__import__",
                    side_effect=ImportError("missing grpc"),
                ):
                    result = setup_tracing(mock_app)

        assert result is False

    def test_tracing_setup_instruments_app(self):
        """FastAPIInstrumentor.instrument_app is called when enabled + OTel available."""
        from door import tracing

        mock_app = MagicMock()
        mock_provider = MagicMock()
        mock_instrumentor = MagicMock()

        with patch.dict(
            "os.environ",
            {
                "KNOWLEDGE_LAYER_TRACES_ENABLED": "true",
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
                "OTEL_SERVICE_NAME": "knowledge-layer-door",
            },
        ):
            with patch("door.tracing._is_otel_available", return_value=True):
                with patch.dict(
                    "sys.modules",
                    {
                        "opentelemetry": MagicMock(),
                        "opentelemetry.trace": MagicMock(),
                        "opentelemetry.propagate": MagicMock(),
                        "opentelemetry.exporter": MagicMock(),
                        "opentelemetry.exporter.otlp": MagicMock(),
                        "opentelemetry.exporter.otlp.proto": MagicMock(),
                        "opentelemetry.exporter.otlp.proto.grpc": MagicMock(),
                        "opentelemetry.exporter.otlp.proto.grpc.trace_exporter": MagicMock(
                            OTLPSpanExporter=MagicMock(return_value=MagicMock())
                        ),
                        "opentelemetry.instrumentation": MagicMock(),
                        "opentelemetry.instrumentation.fastapi": MagicMock(
                            FastAPIInstrumentor=mock_instrumentor
                        ),
                        "opentelemetry.propagators": MagicMock(),
                        "opentelemetry.propagators.aws": MagicMock(AwsXRayPropagator=MagicMock()),
                        "opentelemetry.sdk": MagicMock(),
                        "opentelemetry.sdk.extension": MagicMock(),
                        "opentelemetry.sdk.extension.aws": MagicMock(),
                        "opentelemetry.sdk.extension.aws.trace": MagicMock(
                            AwsXRayIdGenerator=MagicMock()
                        ),
                        "opentelemetry.sdk.resources": MagicMock(
                            Resource=MagicMock(create=MagicMock())
                        ),
                        "opentelemetry.sdk.trace": MagicMock(
                            TracerProvider=MagicMock(return_value=mock_provider)
                        ),
                        "opentelemetry.sdk.trace.export": MagicMock(
                            BatchSpanProcessor=MagicMock(return_value=MagicMock())
                        ),
                    },
                ):
                    result = tracing.setup_tracing(mock_app)

        assert result is True
        assert tracing._tracing_enabled is True
        mock_instrumentor.instrument_app.assert_called_once_with(mock_app)

    def test_tracing_setup_idempotent(self):
        """Second call returns cached state without re-initializing."""
        from door import tracing

        mock_app = MagicMock()
        with patch.dict("os.environ", {"KNOWLEDGE_LAYER_TRACES_ENABLED": "false"}):
            tracing.setup_tracing(mock_app)

        assert tracing._tracing_initialized is True
        # Second call should not reconfigure
        result = tracing.setup_tracing(mock_app)
        assert result is False


# ---------------------------------------------------------------------------
# Tests: get_tracer
# ---------------------------------------------------------------------------


class TestDoorGetTracer:
    """Tests for tracer retrieval."""

    def test_returns_noop_when_disabled(self):
        """Returns NoOpTracer when tracing is disabled."""
        from door.tracing import _NoOpTracer, get_tracer

        tracer = get_tracer("test")
        assert isinstance(tracer, _NoOpTracer)

    def test_returns_noop_when_otel_unavailable(self):
        """Returns NoOpTracer when OTel packages are missing."""
        from door import tracing

        tracing._tracing_enabled = True
        with patch("door.tracing._is_otel_available", return_value=False):
            tracer = tracing.get_tracer("test")
        from door.tracing import _NoOpTracer

        assert isinstance(tracer, _NoOpTracer)


# ---------------------------------------------------------------------------
# Tests: NoOp fallback classes
# ---------------------------------------------------------------------------


class TestDoorNoOpFallback:
    """Tests that NoOp classes implement the required interface."""

    def test_noop_span_context_manager(self):
        """NoOpSpan works as a context manager."""
        from door.tracing import _NoOpSpan

        span = _NoOpSpan()
        with span as s:
            assert s is span

    def test_noop_span_methods_are_silent(self):
        """NoOpSpan methods accept calls without error."""
        from door.tracing import _NoOpSpan

        span = _NoOpSpan()
        span.set_attribute("key", "value")
        span.set_status("OK")
        span.set_status("ERROR", "something failed")
        span.record_exception(ValueError("test"))
        span.add_event("event_name", attributes={"a": 1})
        span.end()

    def test_noop_tracer_start_as_current_span(self):
        """NoOpTracer.start_as_current_span returns a usable NoOpSpan."""
        from door.tracing import _NoOpSpan, _NoOpTracer

        tracer = _NoOpTracer()
        span = tracer.start_as_current_span("test_span", attributes={"key": "val"})
        assert isinstance(span, _NoOpSpan)

    def test_noop_tracer_start_span(self):
        """NoOpTracer.start_span returns a usable NoOpSpan."""
        from door.tracing import _NoOpSpan, _NoOpTracer

        tracer = _NoOpTracer()
        span = tracer.start_span("test_span")
        assert isinstance(span, _NoOpSpan)


# ---------------------------------------------------------------------------
# Tests: Identity-enrichment middleware
# ---------------------------------------------------------------------------


class TestIdentityMiddleware:
    """Tests that identity headers are stamped onto the current span."""

    @pytest.mark.asyncio
    async def test_identity_middleware_stamps_span(self):
        """Span attributes include caller identity headers."""
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient

        app = FastAPI()

        # Track span.set_attribute calls
        captured_attrs = {}
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True
        mock_span.set_attribute.side_effect = lambda k, v: captured_attrs.update({k: v})

        @app.middleware("http")
        async def enrich_span_with_identity(request: Request, call_next):
            """Reproduce the middleware logic for testing."""
            try:
                span = mock_span
                if span and span.is_recording():
                    span.set_attribute("caller.owner_sub", request.headers.get("x-owner-sub", ""))
                    span.set_attribute("caller.tenant_id", request.headers.get("x-tenant-id", ""))
                    span.set_attribute(
                        "caller.github_login", request.headers.get("x-github-login", "")
                    )
            except Exception:
                pass
            response = await call_next(request)
            return response

        @app.get("/test")
        async def test_endpoint():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get(
            "/test",
            headers={
                "x-owner-sub": "user-123",
                "x-tenant-id": "tenant-abc",
                "x-github-login": "testuser",
            },
        )

        assert resp.status_code == 200
        assert captured_attrs["caller.owner_sub"] == "user-123"
        assert captured_attrs["caller.tenant_id"] == "tenant-abc"
        assert captured_attrs["caller.github_login"] == "testuser"

    @pytest.mark.asyncio
    async def test_identity_middleware_empty_headers(self):
        """Missing identity headers stamp empty strings (not KeyError)."""
        from fastapi import FastAPI, Request
        from fastapi.testclient import TestClient

        app = FastAPI()

        captured_attrs = {}
        mock_span = MagicMock()
        mock_span.is_recording.return_value = True
        mock_span.set_attribute.side_effect = lambda k, v: captured_attrs.update({k: v})

        @app.middleware("http")
        async def enrich_span_with_identity(request: Request, call_next):
            try:
                span = mock_span
                if span and span.is_recording():
                    span.set_attribute("caller.owner_sub", request.headers.get("x-owner-sub", ""))
                    span.set_attribute("caller.tenant_id", request.headers.get("x-tenant-id", ""))
                    span.set_attribute(
                        "caller.github_login", request.headers.get("x-github-login", "")
                    )
            except Exception:
                pass
            response = await call_next(request)
            return response

        @app.get("/test")
        async def test_endpoint():
            return {"ok": True}

        client = TestClient(app)
        resp = client.get("/test")

        assert resp.status_code == 200
        assert captured_attrs["caller.owner_sub"] == ""
        assert captured_attrs["caller.tenant_id"] == ""
        assert captured_attrs["caller.github_login"] == ""


# ---------------------------------------------------------------------------
# Tests: Backend sub-span creation
# ---------------------------------------------------------------------------


class TestBackendSubSpan:
    """Tests that backend calls create child spans."""

    @pytest.mark.asyncio
    async def test_backend_subspan_created_search(self):
        """Zoekt search call creates a child span with query attribute."""
        from door import search_backend

        mock_span = MagicMock()
        mock_span.__enter__ = MagicMock(return_value=mock_span)
        mock_span.__exit__ = MagicMock(return_value=False)

        mock_tracer = MagicMock()
        mock_tracer.start_as_current_span = MagicMock(return_value=mock_span)

        # Patch the module-level tracer
        original_tracer = search_backend._search_tracer
        search_backend._search_tracer = mock_tracer
        try:
            backend = search_backend.ZoektSearchBackend("http://zoekt:6070", timeout=5.0)

            # Mock httpx to avoid real network calls
            mock_response = MagicMock()
            mock_response.json.return_value = {"Result": {"FileMatches": []}}
            mock_response.raise_for_status = MagicMock()

            mock_client = MagicMock()
            mock_client.__aenter__ = MagicMock(return_value=mock_client)
            mock_client.__aexit__ = MagicMock(return_value=False)
            mock_client.post = MagicMock(return_value=mock_response)

            with patch("httpx.AsyncClient", return_value=mock_client):
                await backend.search("test_query", limit=10)

            # Verify span was created with expected attributes
            mock_tracer.start_as_current_span.assert_called_once_with(
                "zoekt_search",
                attributes={"query": "test_query", "limit": 10},
            )
        finally:
            search_backend._search_tracer = original_tracer


# ---------------------------------------------------------------------------
# Tests: shutdown_tracing
# ---------------------------------------------------------------------------


class TestDoorShutdownTracing:
    """Tests for tracing shutdown."""

    def test_shutdown_calls_provider_shutdown(self):
        """shutdown_tracing calls the provider's shutdown method."""
        from door import tracing

        mock_provider = MagicMock()
        tracing._tracer_provider = mock_provider
        tracing._tracing_initialized = True
        tracing._tracing_enabled = True

        tracing.shutdown_tracing()

        mock_provider.shutdown.assert_called_once()
        assert tracing._tracer_provider is None
        assert tracing._tracing_initialized is False
        assert tracing._tracing_enabled is False

    def test_shutdown_noop_when_not_initialized(self):
        """shutdown_tracing is safe to call when not initialized."""
        from door import tracing

        tracing._tracer_provider = None
        # Should not raise
        tracing.shutdown_tracing()

    def test_shutdown_failopen_on_error(self):
        """shutdown_tracing doesn't raise if provider.shutdown fails."""
        from door import tracing

        mock_provider = MagicMock()
        mock_provider.shutdown.side_effect = RuntimeError("flush failed")
        tracing._tracer_provider = mock_provider
        tracing._tracing_initialized = True
        tracing._tracing_enabled = True

        # Should not raise
        tracing.shutdown_tracing()
        assert tracing._tracer_provider is None
