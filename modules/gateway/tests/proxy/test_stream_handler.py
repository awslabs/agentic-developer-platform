"""Tests for StreamHandler component."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from src.proxy.stream_handler import StreamHandler, StreamingError


class TestStreamHandler:
    """Test cases for StreamHandler."""

    @pytest.fixture
    def stream_handler(self) -> StreamHandler:
        """Create stream handler instance."""
        return StreamHandler()

    async def _create_mock_stream(self, chunks: list[dict[str, Any]]) -> AsyncIterator[bytes]:
        """Create a mock stream from chunks."""
        for chunk in chunks:
            yield json.dumps(chunk).encode("utf-8")

    @pytest.mark.asyncio
    async def test_create_sse_response_openai(
        self,
        stream_handler: StreamHandler,
        sample_stream_chunks: list[dict[str, Any]],
    ) -> None:
        """Test SSE response creation for OpenAI format."""
        stream = self._create_mock_stream(sample_stream_chunks)

        result_chunks = []
        async for chunk in stream_handler.create_sse_response(stream, "openai", "claude-3.5-sonnet", "test-response-id"):
            result_chunks.append(chunk)

        # Should have SSE formatted chunks
        assert len(result_chunks) > 0

        # Each chunk should be SSE formatted
        for chunk in result_chunks:
            chunk_str = chunk.decode("utf-8")
            assert chunk_str.startswith("data: ") or chunk_str == ""

    @pytest.mark.asyncio
    async def test_create_sse_response_anthropic(
        self,
        stream_handler: StreamHandler,
        sample_stream_chunks: list[dict[str, Any]],
    ) -> None:
        """Test SSE response creation for Anthropic format."""
        stream = self._create_mock_stream(sample_stream_chunks)

        result_chunks = []
        async for chunk in stream_handler.create_sse_response(stream, "anthropic", "claude-3-5-sonnet-20241022", "test-response-id"):
            result_chunks.append(chunk)

        assert len(result_chunks) > 0

        # Anthropic format uses event: type\ndata: json
        has_event_lines = False
        for chunk in result_chunks:
            chunk_str = chunk.decode("utf-8")
            if chunk_str.startswith("event:"):
                has_event_lines = True
                break

        assert has_event_lines

    @pytest.mark.asyncio
    async def test_create_sse_response_bedrock(
        self,
        stream_handler: StreamHandler,
        sample_stream_chunks: list[dict[str, Any]],
    ) -> None:
        """Test SSE response creation for Bedrock format."""
        stream = self._create_mock_stream(sample_stream_chunks)

        result_chunks = []
        async for chunk in stream_handler.create_sse_response(stream, "bedrock", "anthropic.claude-3-5-sonnet-20241022-v2:0", "test-response-id"):
            result_chunks.append(chunk)

        assert len(result_chunks) > 0

    @pytest.mark.asyncio
    async def test_stream_bedrock_response(
        self,
        stream_handler: StreamHandler,
        sample_stream_chunks: list[dict[str, Any]],
    ) -> None:
        """Test parsing Bedrock streaming response."""
        stream = self._create_mock_stream(sample_stream_chunks)

        parsed_chunks = []
        async for chunk in stream_handler.stream_bedrock_response(stream):
            parsed_chunks.append(chunk)

        assert len(parsed_chunks) == len(sample_stream_chunks)

        # Verify chunk types
        chunk_types = [c.get("type") for c in parsed_chunks]
        assert "message_start" in chunk_types
        assert "content_block_delta" in chunk_types
        assert "message_stop" in chunk_types

    def test_parse_bedrock_chunk_json(self, stream_handler: StreamHandler) -> None:
        """Test parsing JSON formatted chunk."""
        chunk = json.dumps({"type": "content_block_delta", "text": "Hello"}).encode()
        result = stream_handler._parse_bedrock_chunk(chunk)

        assert result is not None
        assert result["type"] == "content_block_delta"

    def test_parse_bedrock_chunk_with_headers(self, stream_handler: StreamHandler) -> None:
        """Test parsing chunk with binary headers."""
        # Simulate EventStream format with some binary prefix
        json_data = json.dumps({"type": "test"})
        chunk = b"\x00\x00\x00\x10" + json_data.encode()

        result = stream_handler._parse_bedrock_chunk(chunk)
        assert result is not None
        assert result["type"] == "test"

    def test_parse_bedrock_chunk_invalid(self, stream_handler: StreamHandler) -> None:
        """Test parsing invalid chunk returns None."""
        chunk = b"not valid json or binary"
        result = stream_handler._parse_bedrock_chunk(chunk)
        assert result is None

    @pytest.mark.asyncio
    async def test_openai_final_sse(self, stream_handler: StreamHandler) -> None:
        """Test OpenAI final SSE is [DONE]."""
        final = await stream_handler._create_final_sse("openai", "model", "id")
        assert final == b"data: [DONE]\n\n"

    @pytest.mark.asyncio
    async def test_anthropic_final_sse(self, stream_handler: StreamHandler) -> None:
        """Test Anthropic final SSE is message_stop event."""
        final = await stream_handler._create_final_sse("anthropic", "model", "id")
        assert b"event: message_stop" in final
        assert b"message_stop" in final

    @pytest.mark.asyncio
    async def test_bedrock_final_sse(self, stream_handler: StreamHandler) -> None:
        """Test Bedrock final SSE is None."""
        final = await stream_handler._create_final_sse("bedrock", "model", "id")
        assert final is None

    def test_create_openai_stream_chunk(self, stream_handler: StreamHandler) -> None:
        """Test creating OpenAI stream chunk."""
        chunk = stream_handler.create_openai_stream_chunk(
            content="Hello",
            model="claude-3.5-sonnet",
            response_id="test-id",
        )

        assert chunk["id"] == "chatcmpl-test-id"
        assert chunk["object"] == "chat.completion.chunk"
        assert chunk["model"] == "claude-3.5-sonnet"
        assert chunk["choices"][0]["delta"]["content"] == "Hello"

    def test_create_openai_stream_chunk_with_role(self, stream_handler: StreamHandler) -> None:
        """Test creating OpenAI stream chunk with role."""
        chunk = stream_handler.create_openai_stream_chunk(
            content=None,
            model="claude-3.5-sonnet",
            response_id="test-id",
            role="assistant",
        )

        assert chunk["choices"][0]["delta"]["role"] == "assistant"
        assert "content" not in chunk["choices"][0]["delta"]

    def test_create_openai_stream_chunk_with_finish_reason(self, stream_handler: StreamHandler) -> None:
        """Test creating OpenAI stream chunk with finish reason."""
        chunk = stream_handler.create_openai_stream_chunk(
            content=None,
            model="claude-3.5-sonnet",
            response_id="test-id",
            finish_reason="stop",
        )

        assert chunk["choices"][0]["finish_reason"] == "stop"

    def test_create_anthropic_stream_event(self, stream_handler: StreamHandler) -> None:
        """Test creating Anthropic stream event."""
        event = stream_handler.create_anthropic_stream_event(
            event_type="content_block_delta",
            data={"index": 0, "delta": {"type": "text_delta", "text": "Hi"}},
        )

        assert b"event: content_block_delta\n" in event
        assert b"data: " in event
        assert b"\n\n" in event

    @pytest.mark.asyncio
    async def test_collect_stream(
        self,
        stream_handler: StreamHandler,
        sample_stream_chunks: list[dict[str, Any]],
    ) -> None:
        """Test collecting stream into single response."""
        stream = self._create_mock_stream(sample_stream_chunks)

        content, usage = await stream_handler.collect_stream(stream)

        # Content should be concatenated text
        assert isinstance(content, str)
        assert len(content) > 0

        # Usage should have token counts
        assert "input_tokens" in usage
        assert "output_tokens" in usage

    @pytest.mark.asyncio
    async def test_collect_stream_empty(self, stream_handler: StreamHandler) -> None:
        """Test collecting empty stream."""

        async def empty_stream() -> AsyncIterator[bytes]:
            return
            yield  # type: ignore

        content, usage = await stream_handler.collect_stream(empty_stream())

        assert content == ""
        assert usage["input_tokens"] == 0
        assert usage["output_tokens"] == 0


class TestStreamHandlerErrorHandling:
    """Test error handling in StreamHandler."""

    @pytest.fixture
    def stream_handler(self) -> StreamHandler:
        """Create stream handler instance."""
        return StreamHandler()

    @pytest.mark.asyncio
    async def test_streaming_error_handling(self, stream_handler: StreamHandler) -> None:
        """Test handling of errors during streaming."""

        async def error_stream() -> AsyncIterator[bytes]:
            yield json.dumps({"type": "message_start"}).encode()
            raise Exception("Stream error")

        with pytest.raises(StreamingError):
            async for _ in stream_handler.create_sse_response(error_stream(), "openai", "model", "id"):
                pass

    @pytest.mark.asyncio
    async def test_streaming_cancellation(self, stream_handler: StreamHandler) -> None:
        """Test handling of stream cancellation."""

        async def slow_stream() -> AsyncIterator[bytes]:
            yield json.dumps({"type": "message_start"}).encode()
            await asyncio.sleep(10)  # Would block forever
            yield json.dumps({"type": "message_stop"}).encode()

        # Create a task that we'll cancel
        async def consume_stream() -> None:
            async for _ in stream_handler.create_sse_response(slow_stream(), "openai", "model", "id"):
                pass

        task = asyncio.create_task(consume_stream())
        await asyncio.sleep(0.1)  # Let it start
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_malformed_chunk_handling(self, stream_handler: StreamHandler) -> None:
        """Test handling of malformed chunks in stream."""

        async def mixed_stream() -> AsyncIterator[bytes]:
            yield json.dumps({"type": "message_start"}).encode()
            yield b"invalid json chunk"
            yield json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}).encode()
            yield json.dumps({"type": "message_stop"}).encode()

        chunks = []
        async for chunk in stream_handler.create_sse_response(mixed_stream(), "openai", "claude-3.5-sonnet", "id"):
            chunks.append(chunk)

        # Should still produce output despite malformed chunk
        assert len(chunks) > 0


class TestKeepAliveGenerator:
    """Test keep-alive generator functionality."""

    @pytest.fixture
    def stream_handler(self) -> StreamHandler:
        """Create stream handler instance."""
        return StreamHandler()

    @pytest.mark.asyncio
    async def test_keep_alive_format(self, stream_handler: StreamHandler) -> None:
        """Test keep-alive message format."""
        gen = stream_handler.keep_alive_generator(interval_seconds=0.1)

        # Get first keep-alive message
        message = await asyncio.wait_for(gen.__anext__(), timeout=0.5)

        assert message == b": keep-alive\n\n"
