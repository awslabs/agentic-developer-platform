"""Tests for ProxyService component."""

import pytest

from src.proxy.model_resolver import ModelResolver
from src.proxy.schemas import (
    AnthropicMessage,
    AnthropicMessagesRequest,
    AnthropicRole,
    OpenAIChatCompletionRequest,
    OpenAIMessage,
    OpenAIRole,
)
from src.proxy.service import ProxyService
from src.shared.exceptions import ModelNotAllowedError
from src.shared.schemas.auth import TokenContext
from tests.proxy.conftest import MockPoolService


class TestProxyServiceInit:
    """Test ProxyService initialization."""

    def test_init_with_pool_service(self, mock_pool_service: MockPoolService) -> None:
        """Test initialization with pool service."""
        service = ProxyService(pool_service=mock_pool_service)

        assert service._pool_service is mock_pool_service
        assert service._model_resolver is not None
        assert service._translator is not None
        assert service._stream_handler is not None

    def test_init_with_custom_components(self, mock_pool_service: MockPoolService, model_resolver: ModelResolver) -> None:
        """Test initialization with custom components."""
        service = ProxyService(
            pool_service=mock_pool_service,
            model_resolver=model_resolver,
        )

        assert service._model_resolver is model_resolver

    def test_component_accessors(self, proxy_service: ProxyService) -> None:
        """Test component accessor properties."""
        assert proxy_service.model_resolver is not None
        assert proxy_service.format_translator is not None
        assert proxy_service.stream_handler is not None


class TestProxyServiceInvoke:
    """Test ProxyService invoke method."""

    @pytest.mark.asyncio
    async def test_invoke_openai_format(self, proxy_service: ProxyService, token_context: TokenContext) -> None:
        """Test invoke with OpenAI format request."""
        request = {
            "api_format": "openai",
            "model": "claude-3.5-sonnet",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1024,
        }

        response = await proxy_service.invoke(request, token_context)

        assert response is not None
        # Should have OpenAI response format
        assert "choices" in response
        assert "usage" in response

    @pytest.mark.asyncio
    async def test_invoke_anthropic_format(self, proxy_service: ProxyService, token_context: TokenContext) -> None:
        """Test invoke with Anthropic format request."""
        request = {
            "api_format": "anthropic",
            "model": "claude-3-5-sonnet",  # Use valid alias
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1024,
        }

        response = await proxy_service.invoke(request, token_context)

        assert response is not None
        # Should have Anthropic response format
        assert "content" in response
        assert "usage" in response

    @pytest.mark.asyncio
    async def test_invoke_bedrock_format(self, proxy_service: ProxyService, token_context: TokenContext) -> None:
        """Test invoke with Bedrock format request."""
        request = {
            "api_format": "bedrock",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        }

        response = await proxy_service.invoke(request, token_context)

        assert response is not None

    @pytest.mark.asyncio
    async def test_invoke_model_not_allowed(self, token_context: TokenContext) -> None:
        """Test invoke with disallowed model (US-9.6)."""
        # Create resolver with restricted models
        resolver = ModelResolver(allowed_models_config={token_context.org_id: ["amazon.titan-*"]})
        pool_service = MockPoolService()
        service = ProxyService(pool_service=pool_service, model_resolver=resolver)

        request = {
            "api_format": "openai",
            "model": "claude-3.5-sonnet",  # Claude not allowed
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 1024,
        }

        with pytest.raises(ModelNotAllowedError):
            await service.invoke(request, token_context)


class TestProxyServiceChatCompletions:
    """Test ProxyService chat_completions method (US-4.1)."""

    @pytest.mark.asyncio
    async def test_chat_completions_non_streaming(
        self,
        proxy_service: ProxyService,
        sample_openai_request: OpenAIChatCompletionRequest,
        token_context: TokenContext,
    ) -> None:
        """Test non-streaming chat completions."""
        response = await proxy_service.chat_completions(sample_openai_request, token_context)

        assert response is not None
        assert response.id.startswith("chatcmpl-")
        assert response.object == "chat.completion"
        assert len(response.choices) > 0
        assert response.usage is not None

    @pytest.mark.asyncio
    async def test_chat_completions_with_model_alias(
        self,
        proxy_service: ProxyService,
        token_context: TokenContext,
    ) -> None:
        """Test chat completions with model alias resolution."""
        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",  # Alias
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
            max_tokens=1024,
        )

        response = await proxy_service.chat_completions(request, token_context)

        # Should successfully resolve alias and return response
        assert response is not None

    @pytest.mark.asyncio
    async def test_chat_completions_model_not_allowed(self, token_context: TokenContext) -> None:
        """Test chat completions with disallowed model."""
        resolver = ModelResolver(allowed_models_config={token_context.org_id: ["amazon.titan-*"]})
        pool_service = MockPoolService()
        service = ProxyService(pool_service=pool_service, model_resolver=resolver)

        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
            max_tokens=1024,
        )

        with pytest.raises(ModelNotAllowedError):
            await service.chat_completions(request, token_context)


class TestProxyServiceMessages:
    """Test ProxyService messages method (US-4.2)."""

    @pytest.mark.asyncio
    async def test_messages_non_streaming(
        self,
        proxy_service: ProxyService,
        sample_anthropic_request: AnthropicMessagesRequest,
        token_context: TokenContext,
    ) -> None:
        """Test non-streaming messages."""
        response = await proxy_service.messages(sample_anthropic_request, token_context)

        assert response is not None
        assert response.type == "message"
        assert response.role == "assistant"
        assert len(response.content) > 0
        assert response.usage is not None

    @pytest.mark.asyncio
    async def test_messages_with_anthropic_version(
        self,
        proxy_service: ProxyService,
        token_context: TokenContext,
    ) -> None:
        """Test messages with anthropic-version header."""
        request = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",  # Use valid alias
            messages=[AnthropicMessage(role=AnthropicRole.USER, content="Hello")],
            max_tokens=1024,
        )

        response = await proxy_service.messages(request, token_context, anthropic_version="2024-01-01")

        assert response is not None

    @pytest.mark.asyncio
    async def test_messages_with_anthropic_beta(
        self,
        proxy_service: ProxyService,
        token_context: TokenContext,
    ) -> None:
        """Test messages with anthropic-beta features."""
        request = AnthropicMessagesRequest(
            model="claude-3-5-sonnet",  # Use valid alias
            messages=[AnthropicMessage(role=AnthropicRole.USER, content="Hello")],
            max_tokens=1024,
        )

        response = await proxy_service.messages(
            request,
            token_context,
            anthropic_beta=["computer-use-2024-10-22", "prompt-caching-2024-07-31"],
        )

        assert response is not None


class TestProxyServiceInvokeModel:
    """Test ProxyService invoke_model method (US-4.3)."""

    @pytest.mark.asyncio
    async def test_invoke_model_pass_through(
        self,
        proxy_service: ProxyService,
        token_context: TokenContext,
    ) -> None:
        """Test Bedrock InvokeModel pass-through."""
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        }

        response = await proxy_service.invoke_model("anthropic.claude-3-5-sonnet-20241022-v2:0", body, token_context, stream=False)

        assert response is not None
        assert "content" in response

    @pytest.mark.asyncio
    async def test_invoke_model_sets_agent_run_id_contextvar(
        self,
        proxy_service: ProxyService,
        token_context: TokenContext,
    ) -> None:
        """Issue #1755: invoke_model must re-set the agent_run_id contextvar in
        the service context so _log_usage reads it. The route-dependency sets it
        in an earlier context that is lost across the service-call boundary —
        usage_logs.agent_run_id was NULL on 100% of agent rows. Threading it
        explicitly fixes it, exactly like request_id (#1074).
        """
        from src.proxy.service import _current_agent_run_id

        _current_agent_run_id.set(None)  # simulate the lost route-context value
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 16,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        }
        await proxy_service.invoke_model(
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            body,
            token_context,
            stream=False,
            agent_run_id="inv-run-abc",
        )
        assert _current_agent_run_id.get() == "inv-run-abc"
        _current_agent_run_id.set(None)

    @pytest.mark.asyncio
    async def test_invoke_model_with_alias(
        self,
        proxy_service: ProxyService,
        token_context: TokenContext,
    ) -> None:
        """Test invoke_model with model alias."""
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        }

        # Use alias instead of full model ID
        response = await proxy_service.invoke_model("claude-3.5-sonnet", body, token_context, stream=False)

        assert response is not None

    @pytest.mark.asyncio
    async def test_invoke_model_preserves_anthropic_fields(
        self,
        mock_pool_service: MockPoolService,
        token_context: TokenContext,
    ) -> None:
        """Test that anthropic_version and anthropic_beta are preserved."""
        service = ProxyService(pool_service=mock_pool_service)

        body = {
            "anthropic_version": "custom-version-2024",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        }

        await service.invoke_model("anthropic.claude-3-5-sonnet-20241022-v2:0", body, token_context, stream=False)

        # Check that the body sent to Bedrock contains the anthropic_version
        client = mock_pool_service._client
        assert len(client.invoke_calls) > 0
        sent_body = client.invoke_calls[0]["body"]
        assert sent_body["anthropic_version"] == "custom-version-2024"


class TestProxyServiceGetAvailableModels:
    """Test ProxyService get_available_models method."""

    def test_get_available_models(
        self,
        proxy_service: ProxyService,
        token_context: TokenContext,
    ) -> None:
        """Test getting available models."""
        models = proxy_service.get_available_models(token_context)

        assert isinstance(models, list)
        assert len(models) > 0

        for model in models:
            assert "id" in model
            assert "object" in model
            assert model["object"] == "model"

    def test_get_available_models_filtered_by_org(self, token_context: TokenContext) -> None:
        """Test models are filtered by org permissions."""
        resolver = ModelResolver(allowed_models_config={token_context.org_id: ["anthropic.claude-3-5-*"]})
        pool_service = MockPoolService()
        service = ProxyService(pool_service=pool_service, model_resolver=resolver)

        models = service.get_available_models(token_context)

        # Should only include Claude 3.5 models
        for model in models:
            # The model IDs are aliases, need to check the resolved model
            resolved = resolver.resolve_model(model["id"])
            # Should match the pattern
            assert "claude-3-5" in resolved or "claude-3.5" in model["id"].lower()


class TestProxyServicePoolInteraction:
    """Test ProxyService interaction with pool service."""

    @pytest.mark.asyncio
    async def test_pool_service_get_client_called(
        self,
        mock_pool_service: MockPoolService,
        token_context: TokenContext,
    ) -> None:
        """Test that get_client is called on pool service."""
        service = ProxyService(pool_service=mock_pool_service)

        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
            max_tokens=1024,
        )

        await service.chat_completions(request, token_context)

        assert mock_pool_service.get_client_calls > 0

    @pytest.mark.asyncio
    async def test_bedrock_client_invoke_called(
        self,
        mock_pool_service: MockPoolService,
        token_context: TokenContext,
    ) -> None:
        """Test that Bedrock client invoke_model is called."""
        service = ProxyService(pool_service=mock_pool_service)

        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
            max_tokens=1024,
        )

        await service.chat_completions(request, token_context)

        client = mock_pool_service._client
        assert len(client.invoke_calls) > 0

    @pytest.mark.asyncio
    async def test_correct_model_id_sent_to_bedrock(
        self,
        mock_pool_service: MockPoolService,
        token_context: TokenContext,
    ) -> None:
        """Test that resolved model ID is sent to Bedrock."""
        service = ProxyService(pool_service=mock_pool_service)

        request = OpenAIChatCompletionRequest(
            model="claude-3.5-sonnet",  # Alias
            messages=[OpenAIMessage(role=OpenAIRole.USER, content="Hello")],
            max_tokens=1024,
        )

        await service.chat_completions(request, token_context)

        client = mock_pool_service._client
        assert len(client.invoke_calls) > 0
        # Should be resolved to full model ID
        assert client.invoke_calls[0]["modelId"] == "anthropic.claude-3-5-sonnet-20241022-v2:0"


class TestProxyServiceStreaming:
    """Test ProxyService streaming functionality."""

    @pytest.mark.asyncio
    async def test_chat_completions_streaming(
        self,
        proxy_service: ProxyService,
        sample_openai_request_stream: OpenAIChatCompletionRequest,
        token_context: TokenContext,
    ) -> None:
        """Test streaming chat completions."""
        stream = await proxy_service.chat_completions(sample_openai_request_stream, token_context)

        chunks = []
        async for chunk in stream:
            chunks.append(chunk)

        assert len(chunks) > 0
        # Chunks should be SSE formatted
        for chunk in chunks:
            chunk_str = chunk.decode("utf-8")
            assert chunk_str.startswith("data: ") or chunk_str == ""

    @pytest.mark.asyncio
    async def test_messages_streaming(
        self,
        proxy_service: ProxyService,
        sample_anthropic_request_stream: AnthropicMessagesRequest,
        token_context: TokenContext,
    ) -> None:
        """Test streaming messages."""
        stream = await proxy_service.messages(sample_anthropic_request_stream, token_context)

        chunks = []
        async for chunk in stream:
            chunks.append(chunk)

        assert len(chunks) > 0

    @pytest.mark.asyncio
    async def test_invoke_model_streaming(
        self,
        proxy_service: ProxyService,
        token_context: TokenContext,
    ) -> None:
        """Test streaming invoke_model."""
        body = {
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 1024,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "Hello"}]}],
        }

        stream = await proxy_service.invoke_model("anthropic.claude-3-5-sonnet-20241022-v2:0", body, token_context, stream=True)

        chunks = []
        async for chunk in stream:
            chunks.append(chunk)

        assert len(chunks) > 0
