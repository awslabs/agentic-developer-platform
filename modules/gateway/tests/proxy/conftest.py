"""Pytest fixtures for proxy component tests."""

import json
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from src.proxy.format_translator import FormatTranslator
from src.proxy.model_resolver import ModelResolver
from src.proxy.routes import router, set_model_resolver, set_proxy_service
from src.proxy.schemas import (
    AnthropicMessage,
    AnthropicMessagesRequest,
    AnthropicRole,
    AnthropicTextContent,
    BedrockInvokeRequest,
    BedrockInvokeResponse,
    BedrockMessage,
    OpenAIChatCompletionRequest,
    OpenAIMessage,
    OpenAIRole,
)
from src.proxy.service import ProxyService
from src.proxy.stream_handler import StreamHandler
from src.shared.interfaces.pool import IPoolService
from src.shared.schemas.auth import TokenContext

# ============================================================================
# Token Context Fixtures
# ============================================================================


@pytest.fixture
def token_context() -> TokenContext:
    """Create a standard token context for testing."""
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
def admin_token_context() -> TokenContext:
    """Create an admin token context for testing."""
    return TokenContext(
        user_id="admin-user-123",
        org_id="test-org-456",
        team_id="admin-team",
        department_id="admin-dept",
        account_type="human",
        is_admin=True,
        expires_at=datetime.now() + timedelta(hours=12),
    )


@pytest.fixture
def service_account_context() -> TokenContext:
    """Create a service account token context for testing."""
    return TokenContext(
        user_id="service-account-123",
        org_id="test-org-456",
        team_id="test-team-789",
        department_id="test-dept-012",
        account_type="service",
        is_admin=False,
        expires_at=datetime.now() + timedelta(hours=1),
    )


# ============================================================================
# Mock Pool Service
# ============================================================================


class MockBedrockClient:
    """Mock Bedrock client for testing."""

    def __init__(
        self,
        response: dict[str, Any] | None = None,
        stream_chunks: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ):
        self.response = response
        self.stream_chunks = stream_chunks or []
        self.error = error
        self.invoke_calls: list[dict[str, Any]] = []

    async def invoke_model(
        self,
        *,  # Force keyword-only arguments
        modelId: str,  # noqa: N803 - matches AWS SDK parameter name
        body: str,
        contentType: str,  # noqa: N803 - matches AWS SDK parameter name
        accept: str,
    ) -> dict[str, Any]:
        """Mock invoke_model call."""
        self.invoke_calls.append(
            {
                "modelId": modelId,
                "body": json.loads(body),
                "contentType": contentType,
                "accept": accept,
            }
        )

        if self.error:
            raise self.error

        response_body = self.response or {
            "id": "msg_mock123",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello! How can I help you?"}],
            "model": modelId,
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }

        # Create a mock response with a readable body
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps(response_body).encode()

        return {"body": mock_body}

    async def invoke_model_with_response_stream(
        self,
        *,  # Force keyword-only arguments
        modelId: str,  # noqa: N803 - matches AWS SDK parameter name
        body: str,
        contentType: str,  # noqa: N803 - matches AWS SDK parameter name
        accept: str,
    ) -> dict[str, Any]:
        """Mock invoke_model_with_response_stream call."""
        self.invoke_calls.append(
            {
                "modelId": modelId,
                "body": json.loads(body),
                "contentType": contentType,
                "accept": accept,
                "streaming": True,
            }
        )

        if self.error:
            raise self.error

        # Create async iterator for stream chunks
        async def chunk_iterator() -> AsyncIterator[dict[str, Any]]:
            chunks = self.stream_chunks or [
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_mock123",
                        "role": "assistant",
                        "content": [],
                        "model": modelId,
                        "usage": {"input_tokens": 10},
                    },
                },
                {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello!"}},
                {"type": "content_block_stop", "index": 0},
                {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
                {"type": "message_stop"},
            ]
            for chunk in chunks:
                yield {"chunk": {"bytes": json.dumps(chunk).encode()}}

        return {"body": chunk_iterator()}


class MockPoolService(IPoolService):
    """Mock implementation of IPoolService for testing."""

    def __init__(
        self,
        client: MockBedrockClient | None = None,
        error: Exception | None = None,
    ):
        self._client = client or MockBedrockClient()
        self._error = error
        self.get_client_calls = 0
        self.report_error_calls: list[str] = []

    async def get_client(self) -> Any:
        """Return mock Bedrock client."""
        self.get_client_calls += 1
        if self._error:
            raise self._error
        return self._client

    async def report_error(self, account_id: str) -> None:
        """Record error report."""
        self.report_error_calls.append(account_id)

    async def get_pool_status(self) -> list[dict[str, Any]]:
        """Return mock pool status."""
        return [
            {
                "account_id": "123456789012",
                "is_healthy": True,
                "last_check": datetime.now().isoformat(),
                "request_count": 100,
                "error_count": 0,
            }
        ]


@pytest.fixture
def mock_bedrock_client() -> MockBedrockClient:
    """Create a mock Bedrock client."""
    return MockBedrockClient()


@pytest.fixture
def mock_pool_service(mock_bedrock_client: MockBedrockClient) -> MockPoolService:
    """Create a mock pool service."""
    return MockPoolService(client=mock_bedrock_client)


# ============================================================================
# Component Fixtures
# ============================================================================


@pytest.fixture
def model_resolver() -> ModelResolver:
    """Create a model resolver for testing."""
    return ModelResolver()


@pytest.fixture
def model_resolver_with_restrictions() -> ModelResolver:
    """Create a model resolver with restricted model access."""
    return ModelResolver(
        allowed_models_config={
            "test-org-456": ["anthropic.claude-3-5-sonnet-*"],
            "restricted-org": ["amazon.titan-*"],
        }
    )


@pytest.fixture
def format_translator() -> FormatTranslator:
    """Create a format translator for testing."""
    return FormatTranslator()


@pytest.fixture
def stream_handler() -> StreamHandler:
    """Create a stream handler for testing."""
    return StreamHandler()


@pytest.fixture
def proxy_service(mock_pool_service: MockPoolService) -> ProxyService:
    """Create a proxy service with mocked dependencies."""
    return ProxyService(pool_service=mock_pool_service)


# ============================================================================
# Sample Request Fixtures
# ============================================================================


@pytest.fixture
def sample_openai_request() -> OpenAIChatCompletionRequest:
    """Create a sample OpenAI chat completion request."""
    return OpenAIChatCompletionRequest(
        model="claude-3.5-sonnet",
        messages=[
            OpenAIMessage(role=OpenAIRole.SYSTEM, content="You are a helpful assistant."),
            OpenAIMessage(role=OpenAIRole.USER, content="Hello, how are you?"),
        ],
        max_tokens=1024,
        temperature=0.7,
    )


@pytest.fixture
def sample_openai_request_stream() -> OpenAIChatCompletionRequest:
    """Create a sample OpenAI streaming request."""
    return OpenAIChatCompletionRequest(
        model="claude-3.5-sonnet",
        messages=[
            OpenAIMessage(role=OpenAIRole.USER, content="Tell me a short joke."),
        ],
        max_tokens=256,
        stream=True,
    )


@pytest.fixture
def sample_anthropic_request() -> AnthropicMessagesRequest:
    """Create a sample Anthropic messages request."""
    return AnthropicMessagesRequest(
        model="claude-3-5-sonnet",  # Use alias that maps to full Bedrock ID
        messages=[
            AnthropicMessage(role=AnthropicRole.USER, content="Hello, how are you?"),
        ],
        max_tokens=1024,
        system="You are a helpful assistant.",
    )


@pytest.fixture
def sample_anthropic_request_stream() -> AnthropicMessagesRequest:
    """Create a sample Anthropic streaming request."""
    return AnthropicMessagesRequest(
        model="claude-3-5-sonnet",  # Use alias that maps to full Bedrock ID
        messages=[
            AnthropicMessage(role=AnthropicRole.USER, content="Tell me a short joke."),
        ],
        max_tokens=256,
        stream=True,
    )


@pytest.fixture
def sample_anthropic_request_with_content_blocks() -> AnthropicMessagesRequest:
    """Create an Anthropic request with content blocks."""
    return AnthropicMessagesRequest(
        model="claude-3-5-sonnet-20241022",
        messages=[
            AnthropicMessage(
                role=AnthropicRole.USER,
                content=[
                    AnthropicTextContent(type="text", text="What's in this image?"),
                ],
            ),
        ],
        max_tokens=1024,
    )


@pytest.fixture
def sample_bedrock_request() -> BedrockInvokeRequest:
    """Create a sample Bedrock invoke request."""
    return BedrockInvokeRequest(
        anthropic_version="bedrock-2023-05-31",
        max_tokens=1024,
        messages=[
            BedrockMessage(role="user", content=[{"type": "text", "text": "Hello!"}]),
        ],
        system="You are a helpful assistant.",
    )


# ============================================================================
# Sample Response Fixtures
# ============================================================================


@pytest.fixture
def sample_bedrock_response() -> BedrockInvokeResponse:
    """Create a sample Bedrock response."""
    return BedrockInvokeResponse(
        id="msg_abc123",
        type="message",
        role="assistant",
        content=[{"type": "text", "text": "Hello! I'm doing well, thank you for asking."}],
        model="anthropic.claude-3-5-sonnet-20241022-v2:0",
        stop_reason="end_turn",
        usage={"input_tokens": 15, "output_tokens": 12},
    )


@pytest.fixture
def sample_stream_chunks() -> list[dict[str, Any]]:
    """Create sample streaming chunks."""
    return [
        {
            "type": "message_start",
            "message": {
                "id": "msg_stream123",
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
                "usage": {"input_tokens": 10},
            },
        },
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Hello"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "! "},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "How can I help?"},
        },
        {
            "type": "content_block_stop",
            "index": 0,
        },
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn"},
            "usage": {"output_tokens": 5},
        },
        {
            "type": "message_stop",
        },
    ]


# ============================================================================
# FastAPI Test Client Fixtures
# ============================================================================


@pytest.fixture
def app(proxy_service: ProxyService, model_resolver: ModelResolver) -> FastAPI:
    """Create a FastAPI app with proxy routes for testing."""
    app = FastAPI()
    app.include_router(router)

    # Inject dependencies
    set_proxy_service(proxy_service)
    set_model_resolver(model_resolver)

    return app


@pytest.fixture
def test_client(app: FastAPI) -> TestClient:
    """Create a test client for the app."""
    return TestClient(app)


@pytest.fixture
async def async_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Create an async test client for the app."""
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
