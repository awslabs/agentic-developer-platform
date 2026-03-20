"""Proxy service implementing IProxyService interface.

Handles all proxy requests across OpenAI, Anthropic, and Bedrock API formats.
"""

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

from src.proxy.exceptions import (
    BedrockInvocationError,
)
from src.proxy.format_translator import FormatTranslator
from src.proxy.model_resolver import ModelResolver
from src.proxy.schemas import (
    AnthropicMessagesRequest,
    AnthropicMessagesResponse,
    BedrockInvokeRequest,
    BedrockInvokeResponse,
    OpenAIChatCompletionRequest,
    OpenAIChatCompletionResponse,
)
from src.proxy.stream_handler import StreamHandler
from src.shared.interfaces.pool import IPoolService
from src.shared.interfaces.proxy import IProxyService
from src.shared.logging import get_logger
from src.shared.metrics import emit_error_count, emit_request_metrics
from src.shared.schemas.auth import TokenContext

logger = get_logger(__name__)


class ProxyService(IProxyService):
    """Service for proxying requests to Bedrock.

    Implements IProxyService interface and handles:
    - US-4.1: OpenAI-compatible chat completions
    - US-4.2: Anthropic Messages format
    - US-4.3: Bedrock InvokeModel pass-through
    - US-9.6: Model access control
    """

    def __init__(
        self,
        pool_service: IPoolService,
        model_resolver: ModelResolver | None = None,
        format_translator: FormatTranslator | None = None,
        stream_handler: StreamHandler | None = None,
    ) -> None:
        """Initialize the proxy service.

        Args:
            pool_service: Pool service for getting Bedrock clients
            model_resolver: Optional custom model resolver
            format_translator: Optional custom format translator
            stream_handler: Optional custom stream handler
        """
        self._pool_service = pool_service
        self._model_resolver = model_resolver or ModelResolver()
        self._translator = format_translator or FormatTranslator()
        self._stream_handler = stream_handler or StreamHandler()

    # =========================================================================
    # IProxyService Interface Implementation
    # =========================================================================

    async def invoke(
        self,
        request: dict[str, Any],
        context: TokenContext,
    ) -> dict[str, Any]:
        """Invoke Bedrock model with the given request.

        Args:
            request: The request dictionary (format depends on api_format field)
            context: Authentication context

        Returns:
            Response dictionary in the same format as the request
        """
        api_format = request.get("api_format", "bedrock")
        start_time = time.time()
        model = request.get("model", "unknown")

        try:
            # Resolve model and check access
            bedrock_model_id = self._model_resolver.resolve_model(model)
            self._model_resolver.check_model_access(bedrock_model_id, context)

            # Convert request to Bedrock format
            bedrock_request = self._prepare_bedrock_request(request, bedrock_model_id, api_format)

            # Get Bedrock client from pool
            client = await self._pool_service.get_client()

            # Invoke Bedrock
            response = await self._invoke_bedrock(client, bedrock_model_id, bedrock_request)

            # Calculate latency
            latency_ms = (time.time() - start_time) * 1000

            # Extract token usage from response for metrics
            tokens_in = getattr(response, "input_tokens", 0) or 0
            tokens_out = getattr(response, "output_tokens", 0) or 0
            cost_usd = 0.0  # Cost calculation would be done by budget service

            # Emit metrics
            emit_request_metrics(
                org_id=context.org_id,
                model=model,
                latency_ms=latency_ms,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost_usd,
                success=True,
            )

            logger.info(
                "Proxy invoke completed",
                extra={
                    "model": model,
                    "bedrock_model_id": bedrock_model_id,
                    "latency_ms": round(latency_ms, 2),
                    "tokens_in": tokens_in,
                    "tokens_out": tokens_out,
                },
            )

            # Convert response to original format
            return self._convert_response(response, model, api_format, latency_ms)

        except Exception as e:
            latency_ms = (time.time() - start_time) * 1000
            error_type = type(e).__name__

            # Emit error metrics
            emit_error_count(
                org_id=context.org_id,
                model=model,
                error_type=error_type,
            )

            logger.error(
                "Error in proxy invoke",
                extra={
                    "model": model,
                    "error": str(e),
                    "error_type": error_type,
                    "latency_ms": round(latency_ms, 2),
                },
            )
            raise

    async def invoke_stream(
        self,
        request: dict[str, Any],
        context: TokenContext,
    ) -> AsyncIterator[bytes]:
        """Invoke Bedrock model with streaming response.

        Args:
            request: The request dictionary
            context: Authentication context

        Yields:
            SSE formatted response chunks
        """
        api_format = request.get("api_format", "bedrock")

        try:
            # Resolve model and check access
            model = request.get("model", "")
            bedrock_model_id = self._model_resolver.resolve_model(model)
            self._model_resolver.check_model_access(bedrock_model_id, context)

            # Convert request to Bedrock format
            bedrock_request = self._prepare_bedrock_request(request, bedrock_model_id, api_format)

            # Get Bedrock client from pool
            client = await self._pool_service.get_client()

            # Invoke Bedrock with streaming
            response_id = str(uuid.uuid4())
            bedrock_stream = await self._invoke_bedrock_stream(client, bedrock_model_id, bedrock_request)

            # Convert stream to target format
            async for chunk in self._stream_handler.create_sse_response(bedrock_stream, api_format, model, response_id):
                yield chunk

        except Exception as e:
            logger.error(f"Error in proxy invoke_stream: {e}")
            raise

    # =========================================================================
    # API Format-Specific Methods
    # =========================================================================

    async def chat_completions(
        self,
        request: OpenAIChatCompletionRequest,
        context: TokenContext,
        anthropic_version: str | None = None,
        anthropic_beta: list[str] | None = None,
    ) -> OpenAIChatCompletionResponse | AsyncIterator[bytes]:
        """Handle OpenAI-compatible chat completions (US-4.1).

        Args:
            request: OpenAI format request
            context: Authentication context
            anthropic_version: Optional Anthropic version header
            anthropic_beta: Optional Anthropic beta features

        Returns:
            OpenAI format response or SSE stream
        """
        # Resolve model
        bedrock_model_id = self._model_resolver.resolve_model(request.model)
        self._model_resolver.check_model_access(bedrock_model_id, context)

        # Convert to Bedrock format
        bedrock_request = self._translator.openai_to_bedrock(request, bedrock_model_id)

        if request.stream:
            return self._stream_openai_response(bedrock_request, bedrock_model_id, request.model)
        else:
            return await self._invoke_openai_response(bedrock_request, bedrock_model_id, request.model)

    async def messages(
        self,
        request: AnthropicMessagesRequest,
        context: TokenContext,
        anthropic_version: str | None = None,
        anthropic_beta: list[str] | None = None,
    ) -> AnthropicMessagesResponse | AsyncIterator[bytes]:
        """Handle Anthropic Messages format (US-4.2).

        Args:
            request: Anthropic format request
            context: Authentication context
            anthropic_version: Version header value
            anthropic_beta: Beta features to enable

        Returns:
            Anthropic format response or SSE stream
        """
        # Resolve model
        bedrock_model_id = self._model_resolver.resolve_model(request.model)
        self._model_resolver.check_model_access(bedrock_model_id, context)

        # Convert to Bedrock format
        bedrock_request = self._translator.anthropic_to_bedrock(request, anthropic_version, anthropic_beta)

        if request.stream:
            return self._stream_anthropic_response(bedrock_request, bedrock_model_id, request.model)
        else:
            return await self._invoke_anthropic_response(bedrock_request, bedrock_model_id, request.model)

    async def invoke_model(
        self,
        model_id: str,
        body: dict[str, Any],
        context: TokenContext,
        stream: bool = False,
    ) -> dict[str, Any] | AsyncIterator[bytes]:
        """Handle Bedrock InvokeModel pass-through (US-4.3).

        Args:
            model_id: Bedrock model ID
            body: Request body to pass through
            context: Authentication context
            stream: Whether to use streaming

        Returns:
            Bedrock response or SSE stream
        """
        # Resolve model (in case it's an alias)
        bedrock_model_id = self._model_resolver.resolve_model(model_id)
        self._model_resolver.check_model_access(bedrock_model_id, context)

        # Create Bedrock request from body
        bedrock_request = BedrockInvokeRequest(**body)

        if stream:
            return self._stream_bedrock_response(bedrock_request, bedrock_model_id)
        else:
            return await self._invoke_bedrock_response(bedrock_request, bedrock_model_id)

    # =========================================================================
    # Internal Methods
    # =========================================================================

    def _prepare_bedrock_request(
        self,
        request: dict[str, Any],
        bedrock_model_id: str,
        api_format: str,
    ) -> BedrockInvokeRequest:
        """Prepare a Bedrock request from the input request.

        Args:
            request: Input request dictionary
            bedrock_model_id: Resolved Bedrock model ID
            api_format: Source API format

        Returns:
            Bedrock invoke request
        """
        if api_format == "openai":
            openai_request = OpenAIChatCompletionRequest(**request)
            return self._translator.openai_to_bedrock(openai_request, bedrock_model_id)
        elif api_format == "anthropic":
            anthropic_request = AnthropicMessagesRequest(**request)
            return self._translator.anthropic_to_bedrock(anthropic_request)
        else:
            # Bedrock pass-through
            return BedrockInvokeRequest(**request)

    def _convert_response(
        self,
        response: BedrockInvokeResponse,
        model: str,
        api_format: str,
        latency_ms: float,
    ) -> dict[str, Any]:
        """Convert Bedrock response to the target format.

        Args:
            response: Bedrock response
            model: Original model name
            api_format: Target API format
            latency_ms: Request latency

        Returns:
            Response dictionary in target format
        """
        if api_format == "openai":
            openai_response = self._translator.bedrock_to_openai(response, model)
            return openai_response.model_dump()
        elif api_format == "anthropic":
            anthropic_response = self._translator.bedrock_to_anthropic(response, model)
            return anthropic_response.model_dump()
        else:
            return response.model_dump()

    async def _invoke_bedrock(
        self,
        client: Any,
        model_id: str,
        request: BedrockInvokeRequest,
    ) -> BedrockInvokeResponse:
        """Invoke Bedrock model.

        Creates an OTEL span for X-Ray visibility of the Bedrock API call.

        Args:
            client: Bedrock client
            model_id: Model ID
            request: Bedrock request

        Returns:
            Bedrock response
        """
        try:
            from src.shared.tracing import get_tracer

            tracer = get_tracer(__name__)
            with tracer.start_as_current_span(
                "bedrock.invoke_model",
                attributes={"bedrock.model_id": model_id},
            ):
                response = await client.invoke_model(
                    modelId=model_id,
                    body=json.dumps(request.model_dump(exclude_none=True)),
                    contentType="application/json",
                    accept="application/json",
                )
                response_body = json.loads(response["body"].read())
                return BedrockInvokeResponse(**response_body)

        except Exception as e:
            logger.error(f"Bedrock invocation error: {e}")
            raise BedrockInvocationError(str(e))

    async def _invoke_bedrock_stream(
        self,
        client: Any,
        model_id: str,
        request: BedrockInvokeRequest,
    ) -> AsyncIterator[bytes]:
        """Invoke Bedrock model with streaming.

        Creates an OTEL span covering the full stream lifecycle (from API call
        to last chunk received) for X-Ray visibility.

        Args:
            client: Bedrock client
            model_id: Model ID
            request: Bedrock request

        Yields:
            Raw response chunks
        """
        from src.shared.tracing import get_tracer

        tracer = get_tracer(__name__)
        span_ctx = tracer.start_as_current_span(
            "bedrock.invoke_model_stream",
            attributes={"bedrock.model_id": model_id},
        )
        span = span_ctx.__enter__()

        try:
            response = await client.invoke_model_with_response_stream(
                modelId=model_id,
                body=json.dumps(request.model_dump(exclude_none=True)),
                contentType="application/json",
                accept="application/json",
            )

            event_stream = response.get("body")
            if event_stream:
                queue: asyncio.Queue[bytes | None] = asyncio.Queue()
                chunk_count = 0

                def _read_stream():
                    try:
                        for event in event_stream:
                            chunk = event.get("chunk")
                            if chunk:
                                data = chunk.get("bytes", b"")
                                if data:
                                    queue.put_nowait(data)
                    finally:
                        queue.put_nowait(None)

                read_task = asyncio.get_event_loop().run_in_executor(None, _read_stream)

                while True:
                    chunk = await queue.get()
                    if chunk is None:
                        break
                    chunk_count += 1
                    yield chunk

                await read_task
                span.set_attribute("bedrock.stream_chunks", chunk_count)

        except Exception as e:
            span.record_exception(e)
            logger.error(f"Bedrock streaming error: {e}")
            raise BedrockInvocationError(str(e))
        finally:
            span_ctx.__exit__(None, None, None)

    async def _invoke_openai_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        model: str,
    ) -> OpenAIChatCompletionResponse:
        """Invoke Bedrock and return OpenAI format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            model: Original model name

        Returns:
            OpenAI format response
        """
        client = await self._pool_service.get_client()
        bedrock_response = await self._invoke_bedrock(client, bedrock_model_id, bedrock_request)
        return self._translator.bedrock_to_openai(bedrock_response, model)

    async def _stream_openai_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        model: str,
    ) -> AsyncIterator[bytes]:
        """Stream OpenAI format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            model: Original model name

        Yields:
            SSE formatted chunks
        """
        client = await self._pool_service.get_client()
        response_id = str(uuid.uuid4())

        bedrock_stream = self._invoke_bedrock_stream(client, bedrock_model_id, bedrock_request)

        async for chunk in self._stream_handler.create_sse_response(bedrock_stream, "openai", model, response_id):
            yield chunk

    async def _invoke_anthropic_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        model: str,
    ) -> AnthropicMessagesResponse:
        """Invoke Bedrock and return Anthropic format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            model: Original model name

        Returns:
            Anthropic format response
        """
        client = await self._pool_service.get_client()
        bedrock_response = await self._invoke_bedrock(client, bedrock_model_id, bedrock_request)
        return self._translator.bedrock_to_anthropic(bedrock_response, model)

    async def _stream_anthropic_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
        model: str,
    ) -> AsyncIterator[bytes]:
        """Stream Anthropic format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID
            model: Original model name

        Yields:
            SSE formatted chunks
        """
        client = await self._pool_service.get_client()
        response_id = str(uuid.uuid4())

        bedrock_stream = self._invoke_bedrock_stream(client, bedrock_model_id, bedrock_request)

        async for chunk in self._stream_handler.create_sse_response(bedrock_stream, "anthropic", model, response_id):
            yield chunk

    async def _invoke_bedrock_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
    ) -> dict[str, Any]:
        """Invoke Bedrock and return raw response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID

        Returns:
            Bedrock response dictionary
        """
        client = await self._pool_service.get_client()
        bedrock_response = await self._invoke_bedrock(client, bedrock_model_id, bedrock_request)
        return bedrock_response.model_dump()

    async def _stream_bedrock_response(
        self,
        bedrock_request: BedrockInvokeRequest,
        bedrock_model_id: str,
    ) -> AsyncIterator[bytes]:
        """Stream Bedrock format response.

        Args:
            bedrock_request: Bedrock request
            bedrock_model_id: Bedrock model ID

        Yields:
            SSE formatted chunks
        """
        client = await self._pool_service.get_client()
        response_id = str(uuid.uuid4())

        bedrock_stream = self._invoke_bedrock_stream(client, bedrock_model_id, bedrock_request)

        async for chunk in self._stream_handler.create_sse_response(bedrock_stream, "bedrock", bedrock_model_id, response_id):
            yield chunk

    # =========================================================================
    # Utility Methods
    # =========================================================================

    def get_available_models(self, context: TokenContext) -> list[dict[str, Any]]:
        """Get list of available models for the given context.

        Args:
            context: Authentication context

        Returns:
            List of model information
        """
        return self._model_resolver.get_available_models(context)

    @property
    def model_resolver(self) -> ModelResolver:
        """Get the model resolver instance."""
        return self._model_resolver

    @property
    def format_translator(self) -> FormatTranslator:
        """Get the format translator instance."""
        return self._translator

    @property
    def stream_handler(self) -> StreamHandler:
        """Get the stream handler instance."""
        return self._stream_handler
