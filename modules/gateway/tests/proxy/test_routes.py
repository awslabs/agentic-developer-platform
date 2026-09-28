"""Tests for Proxy routes."""

from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from src.proxy.model_resolver import ModelResolver
from src.proxy.routes import get_token_context, router, set_model_resolver, set_proxy_service
from src.proxy.service import ProxyService
from src.shared.schemas.auth import TokenContext
from tests.proxy.conftest import MockPoolService


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


class TestOpenAIRoutes:
    """Test OpenAI-compatible routes (US-4.1)."""

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


class TestAnthropicRoutes:
    """Test Anthropic Messages routes (US-4.2)."""

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
