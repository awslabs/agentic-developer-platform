"""Tests for Proxy routes."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.stub import Stubber
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from src.pool.simple_pool import AsyncBedrockClient
from src.proxy.format_translator import FormatTranslator
from src.proxy.model_resolver import ModelResolver
from src.proxy.pricing_capture import PricingCapture
from src.proxy.routes import get_proxy_service, get_token_context, router, set_model_resolver, set_proxy_service
from src.proxy.schemas import AnthropicMessagesRequest, OpenAIChatCompletionRequest
from src.proxy.service import ProxyService
from src.shared.schemas.auth import TokenContext
from tests.proxy.conftest import MockBedrockClient, MockPoolService


@pytest.fixture(autouse=True)
def confirmed_receipt(monkeypatch):
    async def confirm_usage(self, **kwargs):
        return True

    monkeypatch.setattr(ProxyService, "_log_usage", confirm_usage)


@pytest.fixture
def mock_token_context() -> TokenContext:
    """Create a mock token context for testing."""
    return TokenContext(
        user_id="test-user-123",
        org_id="test-org-456",
        team_id="test-team-789",
        department_id="test-dept-012",
        account_type="human",
        is_admin=False,
        expires_at=datetime.now() + timedelta(hours=12),
    )


@pytest.fixture
def test_app(
    mock_pool_service: MockPoolService,
    model_resolver: ModelResolver,
    mock_token_context: TokenContext,
    unmapped_routing,
) -> FastAPI:
    """Create a test app with routes configured."""
    app = FastAPI()
    app.include_router(router)

    proxy_service = ProxyService(
        pool_service=mock_pool_service,
        model_resolver=model_resolver,
    )
    set_proxy_service(proxy_service)
    set_model_resolver(model_resolver)

    # Override authentication dependency to bypass Cognito validation
    async def mock_get_token_context() -> TokenContext:
        return mock_token_context

    app.dependency_overrides[get_token_context] = mock_get_token_context

    return app


@pytest.fixture
def client(test_app: FastAPI) -> TestClient:
    """Create a test client."""
    return TestClient(test_app)


@pytest.fixture
async def async_client(test_app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Create an async test client."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        yield client


class ClosingBody:
    def __init__(self, events, error=None):
        self.events = events
        self.error = error
        self.closed = False

    def __iter__(self):
        for event in self.events:
            yield {"chunk": {"bytes": event if isinstance(event, bytes) else json.dumps(event).encode()}}
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


class TestOpenAIRoutes:
    """Test OpenAI-compatible routes (US-4.1)."""

    @pytest.mark.parametrize("phase", ["before", "after", "provider_error", "transport_error"])
    def test_chat_stream_interruption_is_explicit(self, client, mock_bedrock_client, monkeypatch, phase):
        events = (
            []
            if phase == "before"
            else [
                {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            ]
        )
        if phase == "provider_error":
            events.append({"type": "error", "error": {"message": "private provider detail"}})
        body = ClosingBody(events, RuntimeError("private transport detail") if phase == "transport_error" else None)
        mock_bedrock_client.invoke_model_with_response_stream = AsyncMock(return_value={"body": body})
        usage_log = AsyncMock(return_value=True)
        monkeypatch.setattr(get_proxy_service(), "_log_usage", usage_log)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3.5-sonnet", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 20, "stream": True},
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200
        assert response.text.count("event: error\n") == 1
        assert '"upstream_stream_error"' in response.text
        assert "private provider detail" not in response.text
        assert "private transport detail" not in response.text
        assert "[DONE]" not in response.text
        assert '"finish_reason": "stop"' not in response.text
        assert body.closed
        mock_bedrock_client.invoke_model_with_response_stream.assert_awaited_once()
        usage_log.assert_awaited_once()
        assert usage_log.await_args.kwargs["status_code"] == 502

    @pytest.mark.parametrize("partial", [False, True])
    @pytest.mark.parametrize("malformed", [b'{"type":"content_block_delta","delta":BROKEN}', b"not-json-content", b"\xff\xfe", b"null", b"[]"])
    def test_chat_corrupt_provider_content_is_not_success(self, client, mock_bedrock_client, monkeypatch, partial, malformed):
        events = [{"type": "message_start", "message": {"usage": {"input_tokens": 10}}}]
        if partial:
            events.append({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}})
        events += [
            malformed,
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
            {"type": "message_stop"},
        ]
        body = ClosingBody(events)
        mock_bedrock_client.invoke_model_with_response_stream = AsyncMock(return_value={"body": body})
        usage_log = AsyncMock(return_value=True)
        monkeypatch.setattr(get_proxy_service(), "_log_usage", usage_log)

        response = client.post(
            "/v1/chat/completions",
            json={"model": "claude-3.5-sonnet", "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 20, "stream": True},
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200
        assert response.text.count("event: error\n") == 1
        assert '"upstream_stream_error"' in response.text
        assert "[DONE]" not in response.text
        assert '"finish_reason": "stop"' not in response.text
        assert body.closed
        mock_bedrock_client.invoke_model_with_response_stream.assert_awaited_once()
        usage_log.assert_awaited_once()
        assert usage_log.await_args.kwargs["status_code"] == 502

    @pytest.mark.asyncio
    async def test_chat_disconnect_closes_provider_body(self, test_app, mock_bedrock_client, mock_token_context, monkeypatch):
        body = ClosingBody(
            [
                {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "partial"}},
            ]
        )
        mock_bedrock_client.invoke_model_with_response_stream = AsyncMock(return_value={"body": body})
        usage_log = AsyncMock(return_value=True)
        monkeypatch.setattr(get_proxy_service(), "_log_usage", usage_log)
        request = OpenAIChatCompletionRequest(model="claude-3.5-sonnet", messages=[{"role": "user", "content": "Hi"}], max_tokens=20, stream=True)
        stream = await get_proxy_service().chat_completions(
            request, mock_token_context, pricing_capture=PricingCapture("test-chat-disconnect", request.model)
        )
        assert b'"role": "assistant"' in await anext(stream)
        await stream.aclose()
        assert body.closed
        mock_bedrock_client.invoke_model_with_response_stream.assert_awaited_once()
        usage_log.assert_awaited_once()
        assert usage_log.await_args.kwargs["status_code"] == 502

    @pytest.mark.asyncio
    @pytest.mark.parametrize("confirmed", [True, False])
    async def test_chat_terminal_waits_for_usage_confirmation(self, test_app, mock_bedrock_client, mock_token_context, monkeypatch, confirmed):
        body = ClosingBody(
            [
                {"type": "message_start", "message": {"usage": {"input_tokens": 10}}},
                {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "answer"}},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
                {"type": "message_stop"},
            ]
        )
        mock_bedrock_client.invoke_model_with_response_stream = AsyncMock(return_value={"body": body})
        entered = asyncio.Event()
        release = asyncio.Event()

        async def confirm_usage(**kwargs):
            entered.set()
            await release.wait()
            return confirmed

        monkeypatch.setattr(get_proxy_service(), "_log_usage", AsyncMock(side_effect=confirm_usage))
        request = OpenAIChatCompletionRequest(model="claude-3.5-sonnet", messages=[{"role": "user", "content": "Hi"}], max_tokens=20, stream=True)
        capture = PricingCapture("test-chat-terminal", request.model)
        stream = await get_proxy_service().chat_completions(request, mock_token_context, pricing_capture=capture)
        frames = []

        async def collect():
            async for frame in stream:
                frames.append(frame)

        task = asyncio.create_task(collect())
        await asyncio.wait_for(entered.wait(), 2)
        assert b"answer" in b"".join(frames)
        assert b'"finish_reason": "stop"' not in b"".join(frames)
        assert b"[DONE]" not in b"".join(frames)
        release.set()
        await asyncio.wait_for(task, 2)
        assert body.closed
        mock_bedrock_client.invoke_model_with_response_stream.assert_awaited_once()
        if confirmed:
            assert b"".join(frames).count(b'"finish_reason": "stop"') == 1
            assert frames[-1] == b"data: [DONE]\n\n"
        else:
            assert b'"finish_reason": "stop"' not in b"".join(frames)
            assert b"[DONE]" not in b"".join(frames)
            assert frames[-1].startswith(b"event: error\ndata: ")

    def test_chat_completions_success(self, client: TestClient) -> None:
        """Test POST /v1/chat/completions success."""
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-3.5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "id" in data
        assert "choices" in data
        assert "usage" in data

    def test_chat_completions_with_system_message(self, client: TestClient) -> None:
        """Test chat completions with system message."""
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-3.5-sonnet",
                "messages": [
                    {"role": "system", "content": "You are a helpful assistant."},
                    {"role": "user", "content": "Hello"},
                ],
                "max_tokens": 1024,
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200

    def test_chat_completions_with_parameters(self, client: TestClient) -> None:
        """Test chat completions with optional parameters."""
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-3.5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
                "temperature": 0.7,
                "top_p": 0.9,
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200

    def test_chat_completions_invalid_request(self, client: TestClient) -> None:
        """Test chat completions with invalid request."""
        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-3.5-sonnet",
                # Missing required 'messages' field
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 422  # Validation error

    def test_list_models(self, client: TestClient) -> None:
        """Test GET /v1/models."""
        response = client.get(
            "/v1/models",
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["object"] == "list"
        assert "data" in data
        assert len(data["data"]) > 0

        # Each model should have required fields
        for model in data["data"]:
            assert "id" in model
            assert "object" in model
            assert model["object"] == "model"

    @pytest.fixture
    def legacy_models_snapshot(self, model_resolver: ModelResolver, monkeypatch: pytest.MonkeyPatch) -> bytes:
        monkeypatch.setattr(
            model_resolver,
            "_aliases",
            {
                "sonnet5": "global.anthropic.claude-sonnet-5",
                "titan-text-express": "amazon.titan-text-express-v1",
            },
        )
        monkeypatch.setattr("time.time", lambda: 1_700_000_000)
        return (
            b'{"object":"list","data":['
            b'{"id":"sonnet5","object":"model","created":1700000000,"owned_by":"unknown"},'
            b'{"id":"titan-text-express","object":"model","created":1700000000,"owned_by":"amazon"}'
            b"]}"
        )

    def test_models_default_response_bytes_match_legacy_fixture(self, client: TestClient, legacy_models_snapshot: bytes) -> None:
        response = client.get("/v1/models", headers={"Authorization": "Bearer bg-test-token"})

        assert response.status_code == 200
        assert response.content == legacy_models_snapshot

    @pytest.mark.parametrize("requested_client", ["codex", "claude-code", "Claude-Desktop", ""])
    def test_models_other_clients_keep_legacy_bytes(self, client: TestClient, legacy_models_snapshot: bytes, requested_client: str) -> None:
        response = client.get("/v1/models", params={"client": requested_client}, headers={"Authorization": "Bearer bg-test-token"})

        assert response.status_code == 200
        assert response.content == legacy_models_snapshot

    def test_models_openapi_documents_desktop_client(self, client: TestClient) -> None:
        response = client.get("/openapi.json")

        assert response.status_code == 200
        parameters = response.json()["paths"]["/v1/models"]["get"]["parameters"]
        assert any(parameter["name"] == "client" and parameter["in"] == "query" for parameter in parameters)

    def test_models_desktop_filters_mixed_team_policy(self, client: TestClient, model_resolver: ModelResolver) -> None:
        model_resolver.set_allowed_models(
            "test-org-456:test-team-789",
            [
                "global.anthropic.claude-sonnet-5",
                "global.moonshotai.kimi-k3",
            ],
        )

        response = client.get("/v1/models?client=claude-desktop", headers={"Authorization": "Bearer bg-test-token"})

        assert response.status_code == 200
        assert response.json()["data"] == [
            {
                "id": "global.anthropic.claude-sonnet-5",
                "object": "model",
                "created": response.json()["data"][0]["created"],
                "owned_by": "anthropic",
            }
        ]

    def test_models_desktop_excludes_non_claude_and_disallowed(self, client: TestClient, model_resolver: ModelResolver) -> None:
        model_resolver.set_allowed_models(
            "test-org-456:test-team-789",
            [
                "global.moonshotai.kimi-k3",
                "anthropic.claude-3-5-sonnet-20241022-v2:0",
            ],
        )

        response = client.get("/v1/models?client=claude-desktop", headers={"Authorization": "Bearer bg-test-token"})

        assert response.status_code == 200
        assert response.json() == {"object": "list", "data": []}

    @pytest.fixture
    def isolated_models(self, model_resolver: ModelResolver) -> None:
        model_resolver.set_allowed_models("test-org-456", ["global.anthropic.claude-*"])
        model_resolver.set_allowed_models("test-org-456:test-team-789", ["global.anthropic.claude-sonnet-5"])
        model_resolver.set_allowed_models("another-org", ["global.anthropic.claude-opus-5-5"])

    def test_models_desktop_hides_other_org_only_profile(self, client: TestClient, isolated_models: None) -> None:
        response = client.get("/v1/models?client=claude-desktop", headers={"Authorization": "Bearer bg-test-token"})

        assert response.status_code == 200
        assert [model["id"] for model in response.json()["data"]] == ["global.anthropic.claude-sonnet-5"]

    def test_models_desktop_other_org_profile_still_denied_on_invoke(
        self, client: TestClient, isolated_models: None, mock_pool_service: MockPoolService
    ) -> None:
        response = client.post(
            "/v1/messages",
            json={
                "model": "global.anthropic.claude-opus-5-5",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 8,
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "model_not_allowed"
        assert mock_pool_service.get_client_calls == 0

    def test_models_desktop_listed_profiles_invoke_by_id_locally(
        self, client: TestClient, model_resolver: ModelResolver, mock_bedrock_client: MockBedrockClient
    ) -> None:
        model_resolver.set_allowed_models(
            "test-org-456:test-team-789",
            [
                "global.anthropic.claude-sonnet-5",
                "global.anthropic.claude-opus-5-5",
            ],
        )
        headers = {"Authorization": "Bearer bg-test-token"}

        models = client.get("/v1/models?client=claude-desktop", headers=headers)

        assert models.status_code == 200
        listed_ids = [model["id"] for model in models.json()["data"]]
        assert listed_ids == ["global.anthropic.claude-opus-5-5", "global.anthropic.claude-sonnet-5"]
        for model_id in listed_ids:
            response = client.post(
                "/v1/messages",
                json={"model": model_id, "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 8},
                headers=headers,
            )

            assert response.status_code == 200
            assert response.json()["model"] == model_id
            assert mock_bedrock_client.invoke_calls[-1]["modelId"] == model_id

        assert len(mock_bedrock_client.invoke_calls) == len(listed_ids)


class TestAnthropicRoutes:
    """Test Anthropic Messages routes (US-4.2)."""

    def test_upstream_error_event_marks_usage_failed(
        self, client: TestClient, mock_bedrock_client: MockBedrockClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mock_bedrock_client.stream_chunks = [
            {"type": "message_start", "message": {"id": "upstream", "model": "provider", "usage": {"input_tokens": 82}}},
            {"type": "error", "error": {"type": "api_error", "message": "Upstream rejected the stream"}},
            {"type": "message_stop"},
        ]
        usage_log = AsyncMock()
        monkeypatch.setattr(get_proxy_service(), "_log_usage", usage_log)
        response = client.post(
            "/v1/messages",
            json={"model": "claude-3-5-sonnet", "max_tokens": 128, "stream": True, "messages": [{"role": "user", "content": "Hello"}]},
            headers={"Authorization": "Bearer bg-test-token"},
        )

        frames = [frame for frame in response.text.split("\n\n") if frame and frame.startswith("event:")]
        assert [frame.split("\n", 1)[0] for frame in frames] == ["event: message_start", "event: error"]
        assert json.loads(frames[-1].split("data: ", 1)[1]) == mock_bedrock_client.stream_chunks[1]
        assert usage_log.await_args.kwargs["status_code"] == 500

    def test_stream_error_after_first_chunk_logs_failure(
        self, client: TestClient, mock_bedrock_client: MockBedrockClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def broken_stream():
            yield {
                "chunk": {
                    "bytes": json.dumps(
                        {
                            "type": "message_start",
                            "message": {"id": "upstream", "model": "provider", "role": "assistant", "usage": {"input_tokens": 81}},
                        }
                    ).encode()
                }
            }
            raise RuntimeError("connection reset")

        monkeypatch.setattr(mock_bedrock_client, "invoke_model_with_response_stream", AsyncMock(return_value={"body": broken_stream()}))
        usage_log = AsyncMock()
        monkeypatch.setattr(get_proxy_service(), "_log_usage", usage_log)
        response = client.post(
            "/v1/messages",
            json={"model": "claude-3-5-sonnet", "max_tokens": 128, "stream": True, "messages": [{"role": "user", "content": "Hello"}]},
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200
        frames = [frame for frame in response.text.split("\n\n") if frame and frame.startswith("event:")]
        assert [frame.split("\n", 1)[0] for frame in frames] == ["event: message_start", "event: error"]
        assert json.loads(frames[-1].split("data: ", 1)[1]) == {
            "type": "error",
            "error": {"type": "api_error", "message": "Upstream stream interrupted"},
        }
        assert usage_log.await_args.kwargs["status_code"] == 500
        assert usage_log.await_args.kwargs["input_tokens"] == 81

    def test_tool_fidelity_messages_route_wires_bedrock(self, client: TestClient, mock_bedrock_client: MockBedrockClient) -> None:
        input_schema = {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
            "$defs": {"integer": {"type": "integer", "minimum": 0}},
            "additionalProperties": False,
        }
        mock_bedrock_client.response = {
            "id": "msg_tool",
            "model": "provider-model",
            "content": [{"type": "tool_use", "id": "tool_123", "name": "add", "input": {"a": 2, "b": 3}}],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 87, "output_tokens": 15},
        }
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "max_tokens": 128,
                "messages": [
                    {"role": "user", "content": "Add 2 and 3"},
                    {"role": "assistant", "content": [{"type": "tool_use", "id": "previous", "name": "add", "input": {"a": 1, "b": 1}}]},
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "previous", "content": [{"type": "text", "text": "2"}], "is_error": False}
                        ],
                    },
                ],
                "tools": [{"name": "add", "input_schema": input_schema, "cache_control": {"type": "ephemeral"}}],
                "tool_choice": {"type": "tool", "name": "add"},
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": "high"},
                "metadata": {"user_id": "example-user"},
                "anthropic_beta": ["body-beta"],
            },
            headers={"Authorization": "Bearer bg-test-token", "anthropic-beta": "header-beta"},
        )

        assert response.status_code == 200
        body = mock_bedrock_client.invoke_calls[-1]["body"]
        assert body["tools"] == [{"name": "add", "input_schema": input_schema, "cache_control": {"type": "ephemeral"}}]
        assert body["tool_choice"] == {"type": "tool", "name": "add"}
        assert body["messages"][2]["content"][0]["is_error"] is False
        assert body["messages"][2]["content"][0]["content"] == [{"type": "text", "text": "2"}]
        assert {key: body[key] for key in ("thinking", "output_config", "metadata")} == {
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": "high"},
            "metadata": {"user_id": "example-user"},
        }
        assert body["anthropic_beta"] == ["body-beta", "header-beta"]
        assert response.json()["content"] == mock_bedrock_client.response["content"]
        assert response.json()["usage"]["input_tokens"] >= 50

    def test_tool_fidelity_mixed_bedrock_blocks(self, client: TestClient, mock_bedrock_client: MockBedrockClient) -> None:
        blocks = [
            {"type": "text", "text": "Computing"},
            {"type": "tool_use", "id": "tool_123", "name": "add", "input": {"a": 2, "b": 3}},
            {"type": "thinking", "thinking": "Need arithmetic", "signature": "signed-fragment"},
            {"type": "redacted_thinking", "data": "opaque-data"},
        ]
        mock_bedrock_client.response = {
            "id": "msg_bedrock",
            "type": "message",
            "role": "assistant",
            "content": blocks,
            "model": "provider-model",
            "stop_reason": "tool_use",
            "stop_sequence": "END",
            "usage": {"input_tokens": 87, "output_tokens": 18, "cache_read_input_tokens": 10, "custom_tokens": 4},
            "provider_detail": {"version": 1},
        }
        response = client.post(
            "/v1/messages",
            json={"model": "claude-3-5-sonnet", "max_tokens": 128, "messages": [{"role": "user", "content": "Add 2 and 3"}]},
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "msg_bedrock"
        assert data["model"] == "claude-3-5-sonnet"
        assert data["content"] == blocks
        assert data["stop_reason"] == "tool_use"
        assert data["stop_sequence"] == "END"
        for key, value in mock_bedrock_client.response["usage"].items():
            assert data["usage"][key] == value
        assert data["provider_detail"] == {"version": 1}

    def test_unknown_field_reaches_bedrock(self, client: TestClient, mock_bedrock_client: MockBedrockClient) -> None:
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 128,
                "bogus_field": {"nested": [1, 2]},
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200
        body = mock_bedrock_client.invoke_calls[-1]["body"]
        assert body["bogus_field"] == {"nested": [1, 2]}
        assert "model" not in body
        assert "stream" not in body

    def test_bedrock_rejects_unknown_field_explicitly(self, client: TestClient, mock_bedrock_client: MockBedrockClient) -> None:
        mock_bedrock_client.error = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Extra inputs are not permitted: bogus_field"}},
            "InvokeModel",
        )
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 128,
                "bogus_field": "unrecognised",
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert mock_bedrock_client.invoke_calls[-1]["body"]["bogus_field"] == "unrecognised"
        assert response.status_code == 400
        assert response.json()["detail"]["error"] == "bedrock_invocation_error"
        assert "bogus_field" in response.json()["detail"]["message"]

    def test_create_message_success(self, client: TestClient) -> None:
        """Test POST /v1/messages success."""
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
            },
            headers={
                "Authorization": "Bearer bg-test-token",
                "anthropic-version": "2024-01-01",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["type"] == "message"
        assert data["role"] == "assistant"
        assert "content" in data
        assert "usage" in data

    def test_create_message_with_system(self, client: TestClient) -> None:
        """Test create message with system prompt."""
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
                "system": "You are a helpful assistant.",
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200

    def test_create_message_with_x_api_key(self, client: TestClient) -> None:
        """Test create message with X-Api-Key header."""
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
            },
            headers={"X-Api-Key": "bg-test-token"},
        )

        assert response.status_code == 200

    def test_create_message_with_anthropic_beta(self, client: TestClient) -> None:
        """Test create message with anthropic-beta header."""
        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
            },
            headers={
                "Authorization": "Bearer bg-test-token",
                "anthropic-beta": "computer-use-2024-10-22,prompt-caching-2024-07-31",
            },
        )

        assert response.status_code == 200

    def test_count_tokens(self, client: TestClient) -> None:
        """Test POST /v1/messages/count_tokens."""
        response = client.post(
            "/v1/messages/count_tokens",
            json={
                "model": "claude-3-5-sonnet",
                "messages": [{"role": "user", "content": "Hello, how are you today?"}],
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "input_tokens" in data
        assert data["input_tokens"] > 0

    @pytest.mark.parametrize("system", [None, "Keep replies short", [{"type": "text", "text": "Keep replies short"}]])
    def test_count_tokens_simple_request_keeps_anthropic_json_shape(self, client: TestClient, system) -> None:
        payload = {"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hello"}]}
        if system is not None:
            payload["system"] = system

        response = client.post("/v1/messages/count_tokens", json=payload)

        assert response.status_code == 200
        assert set(response.json()) == {"input_tokens"}
        assert isinstance(response.json()["input_tokens"], int)
        assert response.json()["input_tokens"] > 0
        assert response.headers["X-ADP-Token-Count-Mode"] == "estimate"

    def test_count_tokens_translates_tools_and_tool_results(self, client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
        payload = {
            "model": "claude-3-5-sonnet",
            "max_tokens": 1024,
            "system": [{"type": "text", "text": "Use the weather tool."}],
            "messages": [
                {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "weather", "input": {"city": "Paris"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "sunny"}]},
            ],
            "tools": [{"name": "weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
            "tool_choice": {"type": "tool", "name": "weather"},
            "thinking": {"type": "enabled", "budget_tokens": 512},
        }
        expected = FormatTranslator().anthropic_to_bedrock(AnthropicMessagesRequest.model_validate(payload)).model_dump(exclude_none=True)
        captured = []
        translate = FormatTranslator.anthropic_to_bedrock

        def capture(translator, request, *args, **kwargs):
            result = translate(translator, request, *args, **kwargs)
            captured.append(result.model_dump(exclude_none=True))
            return result

        monkeypatch.setattr(FormatTranslator, "anthropic_to_bedrock", capture)
        response = client.post("/v1/messages/count_tokens", json=payload)

        assert response.status_code == 200
        assert captured == [expected]
        assert captured[0]["tools"] == expected["tools"]
        assert captured[0]["messages"][1]["content"][0]["type"] == "tool_result"
        assert response.json() == {"input_tokens": max(1, len(json.dumps(expected, ensure_ascii=False)) // 4)}

    def test_count_tokens_rejects_unknown_fields(self, client: TestClient) -> None:
        response = client.post(
            "/v1/messages/count_tokens",
            json={"model": "claude-3-5-sonnet", "messages": [], "unexpected": "value"},
        )

        assert response.status_code == 400
        assert "unexpected" in response.json()["detail"]

    def test_count_tokens_access_denied_falls_back_to_estimate(
        self, client: TestClient, mock_pool_service: MockPoolService, caplog: pytest.LogCaptureFixture
    ) -> None:
        bedrock_client = mock_pool_service._client
        bedrock_client.count_tokens = AsyncMock(
            side_effect=ClientError({"Error": {"Code": "AccessDeniedException", "Message": "Denied"}}, "CountTokens")
        )
        payload = {
            "model": "claude-3-5-sonnet",
            "messages": [{"role": "user", "content": "Check Paris weather"}],
            "tools": [{"name": "weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
        }

        first = client.post("/v1/messages/count_tokens", json=payload)
        second = client.post("/v1/messages/count_tokens", json=payload)

        for response in (first, second):
            assert response.status_code == 200
            assert response.headers["X-ADP-Token-Count-Mode"] == "estimate"
            assert response.json() == first.json()
            assert set(response.json()) == {"input_tokens"}
        assert bedrock_client.count_tokens.await_count == 2
        assert mock_pool_service.get_client_calls == 2
        assert sum("CountTokens denied" in record.message for record in caplog.records) == 1

    @pytest.mark.parametrize("model", ["claude-sonnet-4-us", "claude-sonnet-4-global", "claude-sonnet-4-eu"])
    def test_count_tokens_returns_exact_for_supported_profile(self, client: TestClient, mock_pool_service: MockPoolService, model: str) -> None:
        bedrock_client = mock_pool_service._client
        bedrock_client.count_tokens = AsyncMock(return_value={"inputTokens": 123})
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "What is the weather?"}],
            "tools": [{"name": "weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}}}],
        }
        response = client.post("/v1/messages/count_tokens", json=payload)

        assert response.status_code == 200
        assert response.json() == {"input_tokens": 123}
        assert response.headers["X-ADP-Token-Count-Mode"] == "exact"
        call = bedrock_client.count_tokens.await_args.kwargs
        assert call["modelId"] == "anthropic.claude-sonnet-4-20250514-v1:0"
        translated_body = json.loads(call["input"]["invokeModel"]["body"])
        assert translated_body["tools"][0]["input_schema"] == payload["tools"][0]["input_schema"]
        assert mock_pool_service.get_client_calls == 1

    def test_count_tokens_denied_for_inference_profile(self, client: TestClient, mock_pool_service: MockPoolService) -> None:
        bedrock_client = mock_pool_service._client
        bedrock_client.count_tokens = AsyncMock(side_effect=ClientError({"Error": {"Code": "AccessDenied", "Message": "Denied"}}, "CountTokens"))
        response = client.post(
            "/v1/messages/count_tokens", json={"model": "claude-sonnet-4-global", "messages": [{"role": "user", "content": "Hello"}]}
        )

        assert response.status_code == 200
        assert response.headers["X-ADP-Token-Count-Mode"] == "estimate"
        assert response.json()["input_tokens"] > 0
        assert bedrock_client.count_tokens.await_args.kwargs["modelId"] == "anthropic.claude-sonnet-4-20250514-v1:0"

    def test_count_tokens_mode_header_contract(self, client: TestClient, mock_pool_service: MockPoolService) -> None:
        count = AsyncMock(
            side_effect=[
                {"inputTokens": 123},
                ClientError({"Error": {"Code": "AccessDeniedException", "Message": "Denied"}}, "CountTokens"),
            ]
        )
        mock_pool_service._client.count_tokens = count
        payload = {"model": "claude-sonnet-4-us", "messages": [{"role": "user", "content": "Hello"}]}

        exact = client.post("/v1/messages/count_tokens", json=payload)
        estimate = client.post("/v1/messages/count_tokens", json=payload)

        assert exact.status_code == estimate.status_code == 200
        assert exact.headers["X-ADP-Token-Count-Mode"] == "exact"
        assert exact.json() == {"input_tokens": 123}
        assert estimate.headers["X-ADP-Token-Count-Mode"] == "estimate"
        assert set(estimate.json()) == {"input_tokens"}
        assert isinstance(estimate.json()["input_tokens"], int)
        assert count.await_count == 2

    def test_count_tokens_validation_exception_returns_bedrock_message(self, client: TestClient, mock_pool_service: MockPoolService) -> None:
        mock_pool_service._client.count_tokens = AsyncMock(
            side_effect=ClientError(
                {"Error": {"Code": "ValidationException", "Message": "Model does not support counting tokens"}},
                "CountTokens",
            )
        )
        response = client.post("/v1/messages/count_tokens", json={"model": "claude-sonnet-4-us", "messages": [{"role": "user", "content": "Hello"}]})

        assert response.status_code == 400
        assert response.json()["detail"] == "Model does not support counting tokens"

    @pytest.mark.asyncio
    async def test_count_tokens_sdk_operation_uses_async_wrapper(self, monkeypatch: pytest.MonkeyPatch) -> None:
        sdk_client = boto3.client("bedrock-runtime", region_name="us-east-1", aws_access_key_id="testing", aws_secret_access_key="testing")
        assert "CountTokens" in sdk_client.meta.service_model.operation_names
        monkeypatch.setattr("src.pool.simple_pool.boto3.client", lambda *args, **kwargs: sdk_client)
        client = AsyncBedrockClient(region="us-east-1")
        request = {"modelId": "anthropic.claude-sonnet-4-20250514-v1:0", "input": {"invokeModel": {"body": "{}"}}}
        with Stubber(sdk_client) as stubber:
            stubber.add_response("count_tokens", {"inputTokens": 123}, request)
            assert await client.count_tokens(**request) == {"inputTokens": 123}


class TestBedrockRoutes:
    """Test Bedrock pass-through routes (US-4.3)."""

    def test_invoke_model_success(self, client: TestClient) -> None:
        """Test POST /bedrock/invoke success."""
        response = client.post(
            "/bedrock/invoke",
            json={
                "model": "anthropic.claude-3-5-sonnet-v2:0",
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200

    def test_invoke_model_with_model_id_field(self, client: TestClient) -> None:
        """Test invoke model with modelId field (alternative)."""
        response = client.post(
            "/bedrock/invoke",
            json={
                "modelId": "anthropic.claude-3-5-sonnet-v2:0",
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200

    def test_invoke_model_missing_model(self, client: TestClient) -> None:
        """Test invoke model without model ID."""
        response = client.post(
            "/bedrock/invoke",
            json={
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 400
        assert "model" in response.json()["detail"].lower()

    @pytest.mark.usefixtures("unmapped_routing")
    def test_invoke_model_preserves_anthropic_fields(
        self,
        mock_pool_service: MockPoolService,
        model_resolver: ModelResolver,
        mock_token_context: TokenContext,
    ) -> None:
        """Test that anthropic_version and anthropic_beta are preserved."""
        app = FastAPI()
        app.include_router(router)

        proxy_service = ProxyService(
            pool_service=mock_pool_service,
            model_resolver=model_resolver,
        )
        set_proxy_service(proxy_service)
        set_model_resolver(model_resolver)

        # Override authentication dependency to bypass Cognito validation
        async def mock_get_token_context() -> TokenContext:
            return mock_token_context

        app.dependency_overrides[get_token_context] = mock_get_token_context

        client = TestClient(app)

        response = client.post(
            "/bedrock/invoke",
            json={
                "model": "anthropic.claude-3-5-sonnet-v2:0",
                "anthropic_version": "custom-version-2024",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 200

        # Check that the body sent to Bedrock contains the anthropic_version
        bedrock_client = mock_pool_service._client
        assert len(bedrock_client.invoke_calls) > 0
        sent_body = bedrock_client.invoke_calls[0]["body"]
        assert sent_body["anthropic_version"] == "custom-version-2024"


class TestModelNotAllowed:
    """Test Model Not Allowed error (US-9.6)."""

    @pytest.fixture
    def restricted_app(self) -> FastAPI:
        """Create app with restricted model access."""
        app = FastAPI()
        app.include_router(router)

        # Create resolver that only allows Titan models
        # Note: "test-org" matches the mock token context
        resolver = ModelResolver(allowed_models_config={"test-org-456": ["amazon.titan-*"]})
        pool_service = MockPoolService()
        proxy_service = ProxyService(
            pool_service=pool_service,
            model_resolver=resolver,
        )

        set_proxy_service(proxy_service)
        set_model_resolver(resolver)

        # Override authentication dependency to bypass Cognito validation
        # Use org_id "test-org-456" to match the allowed_models_config
        mock_context = TokenContext(
            user_id="test-user-123",
            org_id="test-org-456",
            team_id="test-team-789",
            department_id="test-dept-012",
            account_type="human",
            is_admin=False,
            expires_at=datetime.now() + timedelta(hours=12),
        )

        async def mock_get_token_context() -> TokenContext:
            return mock_context

        app.dependency_overrides[get_token_context] = mock_get_token_context

        return app

    def test_model_not_allowed_openai(self, restricted_app: FastAPI) -> None:
        """Test model not allowed error for OpenAI endpoint."""
        client = TestClient(restricted_app)

        response = client.post(
            "/v1/chat/completions",
            json={
                "model": "claude-3.5-sonnet",  # Not allowed
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 403
        data = response.json()["detail"]
        assert data["error"] == "model_not_allowed"
        assert "allowed_models" in data

    def test_model_not_allowed_anthropic(self, restricted_app: FastAPI) -> None:
        """Test model not allowed error for Anthropic endpoint."""
        client = TestClient(restricted_app)

        response = client.post(
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",  # Not allowed
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 403

    def test_count_tokens_rejects_disallowed_model_before_bedrock(self, restricted_app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
        service = get_proxy_service()
        count = AsyncMock(return_value=123)
        monkeypatch.setattr(service, "count_bedrock_tokens", count)
        client = TestClient(restricted_app)

        response = client.post(
            "/v1/messages/count_tokens",
            json={"model": "claude-3-5-sonnet", "messages": [{"role": "user", "content": "Hello"}]},
        )

        assert response.status_code == 403
        assert response.json()["detail"]["error"] == "model_not_allowed"
        assert count.await_count == 0
        assert service._pool_service.get_client_calls == 0

    @pytest.mark.parametrize("model", ["claude-3-5-sonnet", "unknown.internal.model"])
    def test_count_tokens_denial_matches_inference_without_translating(
        self, restricted_app: FastAPI, monkeypatch: pytest.MonkeyPatch, model: str
    ) -> None:
        service = get_proxy_service()
        count = AsyncMock(return_value=123)
        monkeypatch.setattr(service, "count_bedrock_tokens", count)
        translate = AsyncMock(side_effect=AssertionError("Disallowed models must not be translated"))
        monkeypatch.setattr(FormatTranslator, "anthropic_to_bedrock", translate)
        client = TestClient(restricted_app)
        payload = {"model": model, "messages": [{"role": "user", "content": "Hello"}]}

        inference = client.post("/v1/messages", json={**payload, "max_tokens": 1024})
        counted = client.post("/v1/messages/count_tokens", json=payload)

        assert inference.status_code == counted.status_code == 403
        assert counted.json() == inference.json()
        assert count.await_count == 0
        assert translate.call_count == 0

    def test_model_not_allowed_bedrock(self, restricted_app: FastAPI) -> None:
        """Test model not allowed error for Bedrock endpoint."""
        client = TestClient(restricted_app)

        response = client.post(
            "/bedrock/invoke",
            json={
                "model": "anthropic.claude-3-5-sonnet-v2:0",  # Not allowed
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            },
            headers={"Authorization": "Bearer bg-test-token"},
        )

        assert response.status_code == 403


class TestHealthCheck:
    """Test health check endpoint."""

    def test_health_check(self, client: TestClient) -> None:
        """Test GET /v1/health."""
        response = client.get("/v1/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["service"] == "proxy"


class TestStreamingRoutes:
    """Test streaming endpoints."""

    @pytest.mark.asyncio
    async def test_chat_completions_streaming(self, async_client: AsyncClient) -> None:
        """Test streaming chat completions."""
        async with async_client.stream(
            "POST",
            "/v1/chat/completions",
            json={
                "model": "claude-3.5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
                "stream": True,
            },
            headers={"Authorization": "Bearer bg-test-token"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"] == "text/event-stream; charset=utf-8"

            chunks = []
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)

            assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_messages_streaming(self, async_client: AsyncClient) -> None:
        """Test streaming messages."""
        async with async_client.stream(
            "POST",
            "/v1/messages",
            json={
                "model": "claude-3-5-sonnet",
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 1024,
                "stream": True,
            },
            headers={"Authorization": "Bearer bg-test-token"},
        ) as response:
            assert response.status_code == 200

            chunks = []
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)

            assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_bedrock_invoke_streaming(self, async_client: AsyncClient) -> None:
        """Test streaming Bedrock invoke."""
        async with async_client.stream(
            "POST",
            "/bedrock/invoke-with-response-stream",
            json={
                "model": "anthropic.claude-3-5-sonnet-v2:0",
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 1024,
                "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
            },
            headers={"Authorization": "Bearer bg-test-token"},
        ) as response:
            assert response.status_code == 200

            chunks = []
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)

            assert len(chunks) > 0
