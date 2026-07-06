"""Scenario tests for the bedrock-mantle OpenAI passthrough (issue #2711).

The unit tests in test_mantle_route.py already cover the happy paths and the
basic gates (allowlist 403, disabled 503, invalid-model 400, byte-for-byte
passthrough, request-id). This file adds the deeper *scenario* cases called out
in #2711's gateway-track group so a regression in metering, allowlist scoping,
streaming integrity, or upstream-error handling is caught in CI:

- **Allowlist scoping** — a tenant allowed a *narrower* openai pattern
  (`openai.gpt-4*`) is still 403'd for `openai.gpt-5.5` (partial-grant leak
  guard), and upstream is never called on a 403.
- **Metering accuracy** — the usage row for a known-size request carries the
  exact token counts and the openai model id; and an upstream error is still
  metered (with the error status, no usage) — no silent success.
- **Streaming integrity** — a long generation delivered across many small,
  awkwardly-split SSE chunks is reassembled byte-for-byte, and the terminal
  `response.completed` usage is captured for metering.
- **Upstream error mapping** — a 429 is passed through with the gateway
  request-id and the upstream is called exactly once (no infinite retry).

Patterns (StubAuth, make_service, httpx.MockTransport, build_app) mirror
test_mantle_route.py so the two files read consistently.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.proxy.mantle_service import MantlePassthroughService, MantleResponse
from src.proxy.model_resolver import ModelResolver
from src.proxy.routes import get_mantle_service, get_token_context, router, set_mantle_service, set_model_resolver
from src.shared.schemas.auth import TokenContext

MANTLE_URL = "https://bedrock-mantle.us-east-1.api.aws"


# ============================================================================
# Fixtures / helpers (mirror test_mantle_route.py)
# ============================================================================


@pytest.fixture
def token_context() -> TokenContext:
    return TokenContext(
        user_id="test-user-123",
        org_id="test-org-456",
        team_id="test-team-789",
        department_id="test-dept-012",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now() + timedelta(hours=12),
    )


class StubAuth:
    """MantleAuth stub that returns a fixed SigV4-shaped Authorization header."""

    def sign(self, method: str, url: str, body: bytes) -> dict[str, str]:
        return {"Authorization": "AWS4-HMAC-SHA256 Credential=test/x/us-east-1/bedrock/aws4_request", "X-Amz-Date": "20260703T000000Z"}


class ChunkedStream(httpx.AsyncByteStream):
    """Serves a preset list of byte chunks, preserving exact chunk boundaries.

    httpx 0.28 has no ``IteratorStream``; ``ByteStream`` re-chunks on read, which
    is fine for byte-fidelity but not for exercising specific split points. This
    stream hands ``aiter_bytes`` exactly the chunks given, so a test can place a
    split at any byte offset (issue #2828 split-event cases).
    """

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def __aiter__(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        pass


def make_service(handler, *, no_log: bool = True) -> MantlePassthroughService:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc = MantlePassthroughService(StubAuth(), MANTLE_URL, http_client=client)
    if no_log:
        svc._log_usage = AsyncMock()  # type: ignore[method-assign]
    return svc


class StubMantleService:
    """Route-level stub; records calls so we can assert upstream was/wasn't hit."""

    def __init__(self, result):
        self._result = result
        self.calls: list[dict] = []

    async def create_response(self, body, context, *, stream, model, request_id=None, agent_run_id=None):
        self.calls.append({"body": body, "model": model, "stream": stream, "request_id": request_id})
        return self._result


def build_app(mantle_service, resolver, context) -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    set_mantle_service(mantle_service)
    set_model_resolver(resolver)

    async def _ctx() -> TokenContext:
        return context

    app.dependency_overrides[get_token_context] = _ctx
    if mantle_service is not None:
        app.dependency_overrides[get_mantle_service] = lambda: mantle_service
    return app


@pytest.fixture(autouse=True)
def _reset_mantle_service():
    yield
    set_mantle_service(None)


# ============================================================================
# Allowlist scoping — partial grant must not leak
# ============================================================================


class TestAllowlistScoping:
    def test_narrower_openai_grant_still_403s_gpt55(self, token_context):
        # Tenant is allowed openai.gpt-4* but NOT openai.gpt-5.5. The narrower
        # grant must not leak access to gpt-5.5.
        resolver = ModelResolver(allowed_models_config={"test-org-456": ["openai.gpt-4*", "anthropic.claude-*"]})
        stub = StubMantleService(MantleResponse(status_code=200, content=b"{}"))
        client = TestClient(build_app(stub, resolver, token_context))

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-5.5", "input": "x"})
        assert resp.status_code == 403
        # Upstream must NOT be called when access is denied.
        assert stub.calls == []

    def test_narrower_openai_grant_allows_its_own_model(self, token_context):
        # Same tenant IS allowed openai.gpt-4 — that must go through.
        resolver = ModelResolver(allowed_models_config={"test-org-456": ["openai.gpt-4*", "anthropic.claude-*"]})
        stub = StubMantleService(MantleResponse(status_code=200, content=b'{"output_text":"ok"}'))
        client = TestClient(build_app(stub, resolver, token_context))

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-4", "input": "x"})
        assert resp.status_code == 200
        assert len(stub.calls) == 1
        assert stub.calls[0]["model"] == "openai.gpt-4"

    def test_team_scoped_grant_overrides_org_default(self, token_context):
        # A team-level grant (org:team key) takes precedence over the org entry.
        resolver = ModelResolver(
            allowed_models_config={
                "test-org-456": ["anthropic.claude-*"],  # org: no openai
                "test-org-456:test-team-789": ["openai.*"],  # team: openai allowed
            }
        )
        stub = StubMantleService(MantleResponse(status_code=200, content=b"{}"))
        client = TestClient(build_app(stub, resolver, token_context))

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-5.5", "input": "x"})
        assert resp.status_code == 200
        assert len(stub.calls) == 1


# ============================================================================
# Metering accuracy — exact counts, and errors are metered too
# ============================================================================


class TestMeteringAccuracy:
    async def test_usage_row_has_exact_token_counts_and_openai_model(self, token_context):
        # A known-size response → the metered row must carry the EXACT token
        # counts and the openai model id (billing distinguishes it from Claude).
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"output_text": "ok", "usage": {"input_tokens": 137, "output_tokens": 42}})

        svc = make_service(handler, no_log=False)
        captured = {}

        async def spy(context, model, usage, latency_ms, status_code, request_id, agent_run_id):
            captured.update(usage=dict(usage), model=model, status=status_code, request_id=request_id)

        svc._log_usage = spy  # type: ignore[method-assign]
        body = b'{"model":"openai.gpt-5.5","input":"x"}'
        result = await svc.create_response(body, token_context, stream=False, model="openai.gpt-5.5", request_id="req-abc")

        assert result.usage == {"input_tokens": 137, "output_tokens": 42}
        assert captured["usage"] == {"input_tokens": 137, "output_tokens": 42}
        assert captured["model"] == "openai.gpt-5.5"
        assert captured["status"] == 200
        assert captured["request_id"] == "req-abc"

    async def test_upstream_error_is_metered_with_status_and_no_usage(self, token_context):
        # An upstream 429 must still be metered — with the error status and NO
        # usage (no tokens billed for a failed call). Guards against silent
        # success / unmetered errors.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, content=b'{"error":{"type":"rate_limit"}}')

        svc = make_service(handler, no_log=False)
        captured = {}

        async def spy(context, model, usage, latency_ms, status_code, request_id, agent_run_id):
            captured.update(usage=dict(usage), status=status_code)

        svc._log_usage = spy  # type: ignore[method-assign]
        result = await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5")

        assert result.status_code == 429
        assert captured["status"] == 429
        assert captured["usage"] == {}

    async def test_metering_records_zero_tokens_when_usage_absent(self, token_context):
        # A 2xx body without a usage block → metered with zero tokens (the
        # UsageService call defaults missing counts to 0), never crashes.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"output_text": "ok"})  # no usage

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        svc = MantlePassthroughService(StubAuth(), MANTLE_URL, http_client=client)

        mock_usage = AsyncMock()
        fake_session = AsyncMock()
        fake_session.__aenter__.return_value = fake_session
        fake_session.__aexit__.return_value = False
        with (
            patch("src.proxy.mantle_service.get_session_factory", return_value=lambda: fake_session),
            patch("src.proxy.mantle_service.UsageService", return_value=mock_usage),
        ):
            await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5")

        mock_usage.log_request.assert_awaited_once()
        kwargs = mock_usage.log_request.await_args.kwargs
        assert kwargs["input_tokens"] == 0
        assert kwargs["output_tokens"] == 0
        assert kwargs["model"] == "openai.gpt-5.5"


# ============================================================================
# Streaming integrity — long generation, awkward chunk splits
# ============================================================================


class TestStreamingIntegrity:
    async def test_long_generation_reassembled_byte_for_byte(self, token_context):
        # Build a long stream: many delta events + a terminal completed event.
        deltas = [f'data: {{"type":"response.output_text.delta","delta":"tok{i} "}}\n\n'.encode() for i in range(200)]
        completed = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":50,"output_tokens":200}}}\n\n'
        done = b"data: [DONE]\n\n"
        full = b"".join(deltas) + completed + done

        def handler(request: httpx.Request) -> httpx.Response:
            # Serve the payload as ONE ByteStream; httpx will chunk it on read.
            return httpx.Response(200, stream=httpx.ByteStream(full))

        svc = make_service(handler)
        result = await svc.create_response(b"{}", token_context, stream=True, model="openai.gpt-5.5")
        received = b"".join([chunk async for chunk in result])
        # Byte-for-byte integrity across the whole long generation.
        assert received == full

    async def test_usage_captured_when_completed_event_split_across_chunks(self, token_context):
        # The terminal usage event arrives split across chunk boundaries. The
        # metered usage must still be captured (accumulator sees full data lines
        # once reassembled by the transport).
        completed = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":9,"output_tokens":13}}}\n\n'
        payload = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n' + completed

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=httpx.ByteStream(payload))

        svc = make_service(handler, no_log=False)
        captured = {}

        async def spy(context, model, usage, latency_ms, status_code, request_id, agent_run_id):
            captured.update(usage=dict(usage))

        svc._log_usage = spy  # type: ignore[method-assign]
        result = await svc.create_response(b"{}", token_context, stream=True, model="openai.gpt-5.5")
        _ = [c async for c in result]
        assert captured["usage"] == {"input_tokens": 9, "output_tokens": 13}

    async def _drive_stream(self, svc, token_context):
        """Run a streaming call, returning (received_bytes, captured_usage)."""
        captured = {}

        async def spy(context, model, usage, latency_ms, status_code, request_id, agent_run_id):
            captured.update(usage=dict(usage))

        svc._log_usage = spy  # type: ignore[method-assign]
        result = await svc.create_response(b"{}", token_context, stream=True, model="openai.gpt-5.5")
        received = b"".join([chunk async for chunk in result])
        return received, captured.get("usage", {})

    @staticmethod
    def _chunk_payload(payload: bytes, split_points: list[int]) -> list[bytes]:
        """Slice ``payload`` into chunks at the given byte offsets."""
        bounds = [0, *split_points, len(payload)]
        return [payload[bounds[i] : bounds[i + 1]] for i in range(len(bounds) - 1)]

    async def test_usage_captured_when_completed_event_split_mid_json_two_chunks(self, token_context):
        # #2828: the terminal response.completed data: line is split mid-JSON
        # across two chunks. Chunk-local parsing lost this; the stateful sniffer
        # reassembles it. Byte-fidelity must also hold.
        completed = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":33,"output_tokens":3540}}}\n\n'
        payload = b'data: {"type":"response.output_text.delta","delta":"x"}\n\n' + completed
        # Split inside the completed event's JSON object.
        split = payload.index(b'"usage"') + 4
        chunks = self._chunk_payload(payload, [split])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=ChunkedStream(chunks))

        svc = make_service(handler, no_log=False)
        received, usage = await self._drive_stream(svc, token_context)
        assert usage == {"input_tokens": 33, "output_tokens": 3540}
        assert received == payload

    @pytest.mark.parametrize(
        "split_points",
        [
            [10, 40, 90],  # arbitrary offsets across 4 chunks
            [5],  # split mid-`data:` prefix of the first event
            [1, 2, 3, 4, 5, 6],  # byte-at-a-time through the `data: ` prefix
        ],
    )
    async def test_usage_captured_across_arbitrary_split_points(self, token_context, split_points):
        # #2828: parametrized split offsets, including mid-`data:` prefix. Usage
        # must be extracted and the passthrough must stay byte-for-byte.
        completed = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":13,"output_tokens":16}}}\n\n'
        payload = b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n' + completed
        valid_points = [p for p in split_points if 0 < p < len(payload)]
        chunks = self._chunk_payload(payload, valid_points)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=ChunkedStream(chunks))

        svc = make_service(handler, no_log=False)
        received, usage = await self._drive_stream(svc, token_context)
        assert usage == {"input_tokens": 13, "output_tokens": 16}
        assert received == payload

    async def test_usage_captured_when_split_mid_utf8_sequence(self, token_context):
        # #2828: a multibyte UTF-8 char in a delta is split across a chunk
        # boundary. Byte-level buffering must reassemble it; usage still parses
        # and the yielded bytes are unchanged.
        # "café" — the é is 2 bytes (0xC3 0xA9); split between them.
        delta_event = 'data: {"type":"response.output_text.delta","delta":"café"}\n\n'.encode()
        completed = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":7,"output_tokens":21}}}\n\n'
        payload = delta_event + completed
        # Find the é bytes and split between the two continuation bytes.
        e_acute = "é".encode()  # b"\xc3\xa9"
        idx = payload.index(e_acute)
        chunks = self._chunk_payload(payload, [idx + 1])

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=ChunkedStream(chunks))

        svc = make_service(handler, no_log=False)
        received, usage = await self._drive_stream(svc, token_context)
        assert usage == {"input_tokens": 7, "output_tokens": 21}
        assert received == payload

    async def test_oversized_carried_fragment_dropped_with_warn(self, token_context, caplog):
        # #2828 safety valve: if a single line never terminates and the buffer
        # exceeds the 1 MiB cap, drop it with a WARN — the stream must be
        # unaffected (bytes still pass through verbatim) and no crash.
        import logging

        from src.proxy.mantle_service import _MAX_SNIFF_BUFFER_BYTES

        # A gigantic data: line with no trailing newline → never completes.
        oversized = b"data: " + b"a" * (_MAX_SNIFF_BUFFER_BYTES + 1024)
        chunks = [oversized[i : i + 65536] for i in range(0, len(oversized), 65536)]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=ChunkedStream(chunks))

        svc = make_service(handler, no_log=False)
        with caplog.at_level(logging.WARNING, logger="src.proxy.mantle_service"):
            received, usage = await self._drive_stream(svc, token_context)
        assert received == b"".join(chunks)  # byte-fidelity preserved
        assert usage == {}  # no usage extracted, but no crash
        assert any("buffer exceeded cap" in rec.message for rec in caplog.records)

    async def test_malformed_sse_lines_do_not_break_stream(self, token_context):
        # Garbage / non-JSON data lines must be passed through verbatim and must
        # not raise — usage extraction is best-effort.
        chunks = [
            b"data: not-json-at-all\n\n",
            b"event: ping\n\n",
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=httpx.ByteStream(b"".join(chunks)))

        svc = make_service(handler)
        result = await svc.create_response(b"{}", token_context, stream=True, model="openai.gpt-5.5")
        received = b"".join([chunk async for chunk in result])
        assert received == b"".join(chunks)


# ============================================================================
# Upstream error mapping — 429 passthrough, request-id, single call
# ============================================================================


class TestUpstreamErrorMapping:
    def test_429_passthrough_carries_request_id(self, token_context):
        result = MantleResponse(status_code=429, content=b'{"error":{"type":"rate_limit"}}')
        stub = StubMantleService(result)
        resolver = ModelResolver(allowed_models_config={"test-org-456": ["openai.*"]})
        client = TestClient(build_app(stub, resolver, token_context))

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-5.5", "input": "x"})
        assert resp.status_code == 429
        assert resp.content == b'{"error":{"type":"rate_limit"}}'
        # Gateway request-id must ride along for tracing/correlation.
        assert resp.headers.get("x-request-id")

    async def test_upstream_called_exactly_once_on_429_no_retry_loop(self, token_context):
        # The passthrough must NOT retry a 429 upstream (no infinite loop); the
        # agent handles backoff. Assert the upstream handler fired exactly once.
        call_count = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            call_count["n"] += 1
            return httpx.Response(429, content=b'{"error":{"type":"rate_limit"}}', headers={"x-amzn-requestid": "up-req-1"})

        svc = make_service(handler)
        result = await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5")
        assert result.status_code == 429
        assert call_count["n"] == 1

    async def test_500_error_body_preserved_verbatim(self, token_context):
        err = b'{"error":{"type":"internal","message":"boom"}}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=err)

        svc = make_service(handler)
        result = await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5")
        assert result.status_code == 500
        assert result.content == err
        assert result.usage == {}


# ============================================================================
# Streaming happy path through the route (request-id header on stream)
# ============================================================================


class TestStreamingRoute:
    def test_streaming_response_attaches_request_id_header(self, token_context):
        chunks = [b'data: {"delta":"he"}\n\n', b'data: {"delta":"llo"}\n\n']

        async def _gen() -> AsyncIterator[bytes]:
            for c in chunks:
                yield c

        stub = StubMantleService(_gen())
        resolver = ModelResolver(allowed_models_config={"test-org-456": ["openai.*"]})
        client = TestClient(build_app(stub, resolver, token_context))

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-5.5", "input": "x", "stream": True})
        assert resp.status_code == 200
        assert resp.content == b"".join(chunks)
        assert resp.headers.get("x-request-id")
