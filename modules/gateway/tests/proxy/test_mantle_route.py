"""Tests for the bedrock-mantle OpenAI Responses-API passthrough (Issue #2709).

Covers:
- MantleAuth: SigV4 signing over the exact body bytes (Authorization header
  shape + credential scope), missing-credentials error.
- MantlePassthroughService: byte-for-byte body passthrough (non-streaming +
  streaming), usage extraction, upstream 4xx/5xx passthrough, no auth-header
  logging, and metering wiring.
- Route: tenant-allowlist 403, disabled 503, invalid-model 400, happy path
  (non-streaming + streaming) with the gateway request-id attached.
"""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from botocore.credentials import Credentials
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.proxy.mantle_auth import MantleAuthError, SigV4MantleAuth, make_mantle_auth
from src.proxy.mantle_service import MantlePassthroughService, MantleResponse
from src.proxy.model_resolver import ModelResolver
from src.proxy.routes import get_mantle_service, get_token_context, router, set_mantle_service, set_model_resolver
from src.shared.schemas.auth import TokenContext

# A recognizable secret-key value the tests assert never leaks into logs.
SIGV4_SECRET_KEY = "wJalrXUtnFEMI-TEST-SIGV4-DO-NOT-LOG-KEY"


def _fake_session(*, with_creds: bool = True) -> MagicMock:
    """A boto3.Session double whose get_credentials returns fixed test creds."""
    session = MagicMock()
    if with_creds:
        session.get_credentials.return_value = Credentials(
            access_key="AKIAIOSFODNN7EXAMPLE",
            secret_key=SIGV4_SECRET_KEY,
            token=None,
        )
    else:
        session.get_credentials.return_value = None
    return session


# ============================================================================
# Fixtures
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
    """MantleAuth stub that returns a fixed SigV4-shaped Authorization header.

    Records the exact body bytes it was asked to sign so tests can assert the
    signed bytes match the forwarded bytes.
    """

    def __init__(self) -> None:
        self.signed_bodies: list[bytes] = []

    def sign(self, method: str, url: str, body: bytes) -> dict[str, str]:
        self.signed_bodies.append(body)
        return {
            "Authorization": (
                "AWS4-HMAC-SHA256 "
                f"Credential=AKIAIOSFODNN7EXAMPLE/20260703/us-east-1/bedrock/aws4_request, "
                f"SignedHeaders=host;x-amz-date, Signature={SIGV4_SECRET_KEY}"
            ),
            "X-Amz-Date": "20260703T000000Z",
        }


def make_service(handler, *, no_log: bool = True) -> MantlePassthroughService:
    """Build a service backed by an httpx MockTransport handler."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    svc = MantlePassthroughService(StubAuth(), "https://bedrock-mantle.us-east-1.api.aws", http_client=client)
    if no_log:
        # Metering hits the DB; silence it for pure-passthrough tests.
        svc._log_usage = AsyncMock()  # type: ignore[method-assign]
    return svc


# ============================================================================
# MantleAuth
# ============================================================================


class TestMantleAuth:
    def test_sigv4_signs_over_exact_body(self):
        body = b'{"model":"openai.gpt-5.5","input":"say ok"}'
        auth = SigV4MantleAuth("us-east-1", session=_fake_session())
        headers = auth.sign("POST", "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses", body)
        # SigV4 header shape: algorithm + credential scope naming the `bedrock`
        # signing service and the region.
        assert headers["Authorization"].startswith("AWS4-HMAC-SHA256 ")
        assert "/us-east-1/bedrock/aws4_request" in headers["Authorization"]
        assert "X-Amz-Date" in headers

    def test_sigv4_signature_changes_with_body(self):
        auth = SigV4MantleAuth("us-east-1", session=_fake_session())
        url = "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses"
        sig_a = auth.sign("POST", url, b'{"input":"a"}')["Authorization"]
        sig_b = auth.sign("POST", url, b'{"input":"bb"}')["Authorization"]
        # Different bodies -> different signatures (proves the body is signed).
        assert sig_a != sig_b

    def test_sigv4_no_temp_token_when_creds_have_none(self):
        auth = SigV4MantleAuth("us-east-1", session=_fake_session())
        headers = auth.sign("POST", "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses", b"{}")
        # Long-term creds (no session token) -> no X-Amz-Security-Token header.
        assert "X-Amz-Security-Token" not in headers

    def test_missing_credentials_raises(self):
        auth = SigV4MantleAuth("us-east-1", session=_fake_session(with_creds=False))
        with pytest.raises(MantleAuthError, match="credentials"):
            auth.sign("POST", "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses", b"{}")

    def test_missing_region_raises(self):
        with pytest.raises(MantleAuthError):
            SigV4MantleAuth("")

    def test_make_mantle_auth_builds_sigv4(self):
        auth = make_mantle_auth("us-east-1", session=_fake_session())
        assert isinstance(auth, SigV4MantleAuth)


# ============================================================================
# MantlePassthroughService — passthrough fidelity
# ============================================================================


class TestMantlePassthrough:
    async def test_targets_gpt55_path_quirk(self, token_context):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 2}})

        svc = make_service(handler)
        await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5")
        assert captured["url"] == "https://bedrock-mantle.us-east-1.api.aws/openai/v1/responses"

    async def test_body_forwarded_byte_for_byte(self, token_context):
        body = b'{"model":"openai.gpt-5.5","input":"say ok","max_output_tokens":16}'
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = request.content
            return httpx.Response(200, json={"output_text": "ok"})

        svc = make_service(handler)
        await svc.create_response(body, token_context, stream=False, model="openai.gpt-5.5")
        assert captured["body"] == body

    async def test_upstream_request_carries_sigv4_header_over_forwarded_body(self, token_context):
        # The upstream request must carry a SigV4 Authorization header, and the
        # bytes signed must be the exact bytes forwarded (sign-then-send).
        body = b'{"model":"openai.gpt-5.5","input":"say ok"}'
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["auth"] = request.headers.get("authorization")
            captured["body"] = request.content
            return httpx.Response(200, json={"output_text": "ok"})

        auth = SigV4MantleAuth("us-east-1", session=_fake_session())
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        svc = MantlePassthroughService(auth, "https://bedrock-mantle.us-east-1.api.aws", http_client=client)
        svc._log_usage = AsyncMock()  # type: ignore[method-assign]
        await svc.create_response(body, token_context, stream=False, model="openai.gpt-5.5")

        assert captured["auth"].startswith("AWS4-HMAC-SHA256 ")
        assert "/us-east-1/bedrock/aws4_request" in captured["auth"]
        # Signed body == forwarded body (byte-for-byte).
        assert captured["body"] == body

    async def test_response_returned_verbatim(self, token_context):
        upstream_body = b'{"id":"resp_1","output_text":"ok","usage":{"input_tokens":3,"output_tokens":4}}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=upstream_body, headers={"content-type": "application/json"})

        svc = make_service(handler)
        result = await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5")
        assert isinstance(result, MantleResponse)
        assert result.content == upstream_body
        assert result.status_code == 200

    async def test_usage_extraction_nonstreaming(self, token_context):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"usage": {"input_tokens": 11, "output_tokens": 22}})

        svc = make_service(handler)
        result = await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5")
        assert result.usage == {"input_tokens": 11, "output_tokens": 22}

    async def test_upstream_error_passthrough(self, token_context):
        err_body = b'{"error":{"type":"rate_limit","message":"slow down"}}'

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, content=err_body)

        svc = make_service(handler)
        result = await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5")
        # Status + body preserved; no usage extracted from an error body.
        assert result.status_code == 429
        assert result.content == err_body
        assert result.usage == {}

    async def test_streaming_bytes_verbatim(self, token_context):
        chunks = [
            b'data: {"type":"response.output_text.delta","delta":"he"}\n\n',
            b'data: {"type":"response.output_text.delta","delta":"llo"}\n\n',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":5,"output_tokens":7}}}\n\n',
            b"data: [DONE]\n\n",
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=httpx.ByteStream(b"".join(chunks)))

        svc = make_service(handler)
        result = await svc.create_response(b"{}", token_context, stream=True, model="openai.gpt-5.5")
        assert isinstance(result, AsyncIterator)
        received = b"".join([chunk async for chunk in result])
        assert received == b"".join(chunks)

    async def test_streaming_usage_captured(self, token_context):
        chunks = [
            b'data: {"type":"response.output_text.delta","delta":"x"}\n\n',
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":9,"output_tokens":13}}}\n\n',
        ]
        captured = {}

        async def spy(context, model, usage, latency_ms, status_code, request_id, agent_run_id):
            captured["usage"] = dict(usage)
            captured["model"] = model
            captured["status"] = status_code

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=httpx.ByteStream(b"".join(chunks)))

        svc = make_service(handler, no_log=False)
        svc._log_usage = spy  # type: ignore[method-assign]
        result = await svc.create_response(b"{}", token_context, stream=True, model="openai.gpt-5.5")
        _ = [c async for c in result]
        assert captured["usage"] == {"input_tokens": 9, "output_tokens": 13}
        assert captured["model"] == "openai.gpt-5.5"

    async def test_auth_header_never_logged(self, token_context, caplog):
        # Force the metering warning path (which logs) to run, and assert the
        # SigV4 signing secret never appears in any emitted log record.
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"usage": {"input_tokens": 1, "output_tokens": 1}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        svc = MantlePassthroughService(StubAuth(), "https://bedrock-mantle.us-east-1.api.aws", http_client=client)
        with caplog.at_level("DEBUG"):
            # Make get_session_factory blow up so the warning branch logs.
            with patch("src.proxy.mantle_service.get_session_factory", side_effect=RuntimeError("no db")):
                await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5")
        assert SIGV4_SECRET_KEY not in caplog.text

    async def test_metering_records_openai_model(self, token_context):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"usage": {"input_tokens": 2, "output_tokens": 3}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        svc = MantlePassthroughService(StubAuth(), "https://bedrock-mantle.us-east-1.api.aws", http_client=client)

        mock_usage = AsyncMock()
        fake_session = AsyncMock()
        fake_session.__aenter__.return_value = fake_session
        fake_session.__aexit__.return_value = False

        with (
            patch("src.proxy.mantle_service.get_session_factory", return_value=lambda: fake_session),
            patch("src.proxy.mantle_service.UsageService", return_value=mock_usage),
        ):
            await svc.create_response(b"{}", token_context, stream=False, model="openai.gpt-5.5", request_id="req-1")

        mock_usage.log_request.assert_awaited_once()
        kwargs = mock_usage.log_request.await_args.kwargs
        assert kwargs["model"] == "openai.gpt-5.5"
        assert kwargs["input_tokens"] == 2
        assert kwargs["output_tokens"] == 3
        assert kwargs["request_id"] == "req-1"


# ============================================================================
# Route
# ============================================================================


class StubMantleService:
    """Route-level stub avoiding real HTTP."""

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


class TestMantleRoute:
    def _resolver_allowing_openai(self) -> ModelResolver:
        return ModelResolver(allowed_models_config={"test-org-456": ["openai.*", "anthropic.claude-*"]})

    def _resolver_no_openai(self) -> ModelResolver:
        return ModelResolver(allowed_models_config={"test-org-456": ["anthropic.claude-*"]})

    def test_happy_path_nonstreaming(self, token_context):
        result = MantleResponse(status_code=200, content=b'{"output_text":"ok"}', usage={"input_tokens": 1, "output_tokens": 1})
        stub = StubMantleService(result)
        app = build_app(stub, self._resolver_allowing_openai(), token_context)
        client = TestClient(app)

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-5.5", "input": "say ok"})
        assert resp.status_code == 200
        assert resp.content == b'{"output_text":"ok"}'
        assert resp.headers.get("x-request-id")
        # Body forwarded to service unchanged.
        assert b'"openai.gpt-5.5"' in stub.calls[0]["body"]

    def test_403_without_openai_allowlist(self, token_context):
        stub = StubMantleService(MantleResponse(status_code=200, content=b"{}"))
        app = build_app(stub, self._resolver_no_openai(), token_context)
        client = TestClient(app)

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-5.5", "input": "x"})
        assert resp.status_code == 403
        # Upstream must NOT have been called.
        assert stub.calls == []

    def test_upstream_error_passthrough(self, token_context):
        result = MantleResponse(status_code=429, content=b'{"error":"rate_limit"}')
        stub = StubMantleService(result)
        app = build_app(stub, self._resolver_allowing_openai(), token_context)
        client = TestClient(app)

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-5.5", "input": "x"})
        assert resp.status_code == 429
        assert resp.content == b'{"error":"rate_limit"}'
        assert resp.headers.get("x-request-id")

    def test_missing_model_400(self, token_context):
        stub = StubMantleService(MantleResponse(status_code=200, content=b"{}"))
        app = build_app(stub, self._resolver_allowing_openai(), token_context)
        client = TestClient(app)

        resp = client.post("/openai/v1/responses", json={"input": "x"})
        assert resp.status_code == 400
        assert stub.calls == []

    def test_unconfigured_model_400(self, token_context):
        # Model not matching the route-level mantle_allowed_models patterns.
        stub = StubMantleService(MantleResponse(status_code=200, content=b"{}"))
        app = build_app(stub, self._resolver_allowing_openai(), token_context)
        client = TestClient(app)

        resp = client.post("/openai/v1/responses", json={"model": "anthropic.claude-3", "input": "x"})
        assert resp.status_code == 400
        assert stub.calls == []

    def test_disabled_returns_503(self, token_context):
        # No mantle service configured → 503.
        app = FastAPI()
        app.include_router(router)
        set_mantle_service(None)
        set_model_resolver(self._resolver_allowing_openai())

        async def _ctx() -> TokenContext:
            return token_context

        app.dependency_overrides[get_token_context] = _ctx
        client = TestClient(app)

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-5.5", "input": "x"})
        assert resp.status_code == 503

    def test_streaming_happy_path(self, token_context):
        chunks = [b'data: {"delta":"he"}\n\n', b'data: {"delta":"llo"}\n\n']

        async def _gen() -> AsyncIterator[bytes]:
            for c in chunks:
                yield c

        stub = StubMantleService(_gen())
        app = build_app(stub, self._resolver_allowing_openai(), token_context)
        client = TestClient(app)

        resp = client.post("/openai/v1/responses", json={"model": "openai.gpt-5.5", "input": "x", "stream": True})
        assert resp.status_code == 200
        assert resp.content == b"".join(chunks)
