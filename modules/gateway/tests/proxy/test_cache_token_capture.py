"""
Tests for prompt-cache token capture through the gateway proxy.

Issue #1486: Validates that cache_read_input_tokens and cache_creation_input_tokens
are propagated from Bedrock response → AnthropicUsage → chat log (S3).
"""

from src.proxy.format_translator import FormatTranslator
from src.proxy.schemas import AnthropicUsage, BedrockInvokeResponse
from src.proxy.service import ProxyService


class TestAnthropicUsageCacheFields:
    """AnthropicUsage schema must carry cache token fields."""

    def test_cache_fields_default_to_zero(self):
        """Cache fields default to 0 for non-cached requests."""
        usage = AnthropicUsage(input_tokens=100, output_tokens=50)
        assert usage.cache_read_input_tokens == 0
        assert usage.cache_creation_input_tokens == 0

    def test_cache_fields_populated(self):
        """Cache fields accept nonzero values."""
        usage = AnthropicUsage(
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
            cache_creation_input_tokens=5000,
        )
        assert usage.cache_read_input_tokens == 65000
        assert usage.cache_creation_input_tokens == 5000

    def test_extra_fields_allowed(self):
        """Extra fields pass through (future-proofing)."""
        usage = AnthropicUsage(
            input_tokens=100,
            output_tokens=50,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            some_future_field=42,
        )
        assert usage.model_dump()["some_future_field"] == 42

    def test_model_dump_includes_cache_fields(self):
        """model_dump() must include cache fields (this is what lands in S3)."""
        usage = AnthropicUsage(
            input_tokens=1,
            output_tokens=4000,
            cache_read_input_tokens=65000,
            cache_creation_input_tokens=5000,
        )
        dumped = usage.model_dump(mode="json")
        assert dumped["cache_read_input_tokens"] == 65000
        assert dumped["cache_creation_input_tokens"] == 5000
        assert dumped["input_tokens"] == 1
        assert dumped["output_tokens"] == 4000


class TestBedrockToAnthropicCacheFields:
    """bedrock_to_anthropic must thread cache fields into AnthropicUsage."""

    def test_cache_fields_propagated(self):
        """Cache tokens in Bedrock response.usage reach AnthropicUsage."""
        translator = FormatTranslator()
        bedrock_response = BedrockInvokeResponse(
            id="msg_test123",
            type="message",
            role="assistant",
            content=[{"type": "text", "text": "Hello"}],
            model="us.anthropic.claude-opus-4-6-v1",
            stop_reason="end_turn",
            usage={
                "input_tokens": 1,
                "output_tokens": 4000,
                "cache_read_input_tokens": 65000,
                "cache_creation_input_tokens": 5000,
            },
        )

        result = translator.bedrock_to_anthropic(bedrock_response, "claude-opus-4.6")
        assert result.usage.cache_read_input_tokens == 65000
        assert result.usage.cache_creation_input_tokens == 5000
        assert result.usage.input_tokens == 1
        assert result.usage.output_tokens == 4000

    def test_no_cache_fields_defaults_to_zero(self):
        """When Bedrock response has no cache fields, they default to 0."""
        translator = FormatTranslator()
        bedrock_response = BedrockInvokeResponse(
            id="msg_test123",
            type="message",
            role="assistant",
            content=[{"type": "text", "text": "Hello"}],
            model="anthropic.claude-3-5-sonnet-20241022-v2:0",
            stop_reason="end_turn",
            usage={"input_tokens": 100, "output_tokens": 50},
        )

        result = translator.bedrock_to_anthropic(bedrock_response, "claude-3.5-sonnet")
        assert result.usage.cache_read_input_tokens == 0
        assert result.usage.cache_creation_input_tokens == 0

    def test_response_model_dump_has_cache_fields(self):
        """Full response model_dump includes cache fields in usage."""
        translator = FormatTranslator()
        bedrock_response = BedrockInvokeResponse(
            id="msg_test123",
            type="message",
            role="assistant",
            content=[{"type": "text", "text": "Hello"}],
            model="us.anthropic.claude-opus-4-6-v1",
            stop_reason="end_turn",
            usage={
                "input_tokens": 1,
                "output_tokens": 4000,
                "cache_read_input_tokens": 65000,
                "cache_creation_input_tokens": 0,
            },
        )

        result = translator.bedrock_to_anthropic(bedrock_response, "claude-opus-4.6")
        dumped = result.model_dump(mode="json")
        assert dumped["usage"]["cache_read_input_tokens"] == 65000
        assert dumped["usage"]["cache_creation_input_tokens"] == 0


class TestStreamingCacheTokenExtraction:
    """_extract_usage_from_sse_chunk must capture cache tokens from message_start."""

    def _make_service(self):
        """Create a minimal ProxyService for testing the extraction method."""
        from unittest.mock import MagicMock

        service = ProxyService.__new__(ProxyService)
        service._pool_service = MagicMock()
        service._translator = MagicMock()
        service._model_resolver = MagicMock()
        service._stream_handler = MagicMock()
        return service

    def test_cache_tokens_extracted_from_message_start(self):
        """message_start event with cache tokens → usage dict updated."""
        service = self._make_service()
        usage = {"input_tokens": 0, "output_tokens": 0}

        chunk = (
            b"event: message_start\n"
            b'data: {"type": "message_start", "message": {"id": "msg_123", '
            b'"role": "assistant", "content": [], "model": "claude-opus-4.6", '
            b'"usage": {"input_tokens": 1, "cache_read_input_tokens": 65000, '
            b'"cache_creation_input_tokens": 5000}}}\n\n'
        )

        service._extract_usage_from_sse_chunk(chunk, usage)

        assert usage["input_tokens"] == 1
        assert usage["cache_read_input_tokens"] == 65000
        assert usage["cache_creation_input_tokens"] == 5000

    def test_cache_tokens_not_set_when_absent(self):
        """message_start without cache fields → usage dict unchanged for cache keys."""
        service = self._make_service()
        usage = {"input_tokens": 0, "output_tokens": 0}

        chunk = (
            b"event: message_start\n"
            b'data: {"type": "message_start", "message": {"id": "msg_123", '
            b'"role": "assistant", "content": [], "model": "claude-3.5-sonnet", '
            b'"usage": {"input_tokens": 100}}}\n\n'
        )

        service._extract_usage_from_sse_chunk(chunk, usage)

        assert usage["input_tokens"] == 100
        assert "cache_read_input_tokens" not in usage
        assert "cache_creation_input_tokens" not in usage

    def test_output_tokens_still_extracted_from_message_delta(self):
        """message_delta still captures output_tokens (regression check)."""
        service = self._make_service()
        usage = {"input_tokens": 1, "output_tokens": 0}

        chunk = b'event: message_delta\ndata: {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 4000}}\n\n'

        service._extract_usage_from_sse_chunk(chunk, usage)

        assert usage["output_tokens"] == 4000

    def test_cache_only_read_no_creation(self):
        """Only cache_read present (typical for subsequent turns)."""
        service = self._make_service()
        usage = {"input_tokens": 0, "output_tokens": 0}

        chunk = (
            b"event: message_start\n"
            b'data: {"type": "message_start", "message": {"id": "msg_123", '
            b'"role": "assistant", "content": [], "model": "claude-opus-4.6", '
            b'"usage": {"input_tokens": 1, "cache_read_input_tokens": 65000}}}\n\n'
        )

        service._extract_usage_from_sse_chunk(chunk, usage)

        assert usage["input_tokens"] == 1
        assert usage["cache_read_input_tokens"] == 65000
        assert "cache_creation_input_tokens" not in usage
