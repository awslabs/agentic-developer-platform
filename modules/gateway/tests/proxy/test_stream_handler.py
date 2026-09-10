"""Tests for StreamHandler component."""

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest

from src.proxy.stream_handler import StreamHandler, StreamingError, merge_with_keepalive


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


class TestMergeWithKeepalive:
    """Tests for merge_with_keepalive (CloudFront idle-timeout fix)."""

    @pytest.mark.asyncio
    async def test_fast_stream_gets_no_keepalive(self) -> None:
        """A stream that never stalls passes through untouched (no keepalives)."""

        async def source() -> AsyncIterator[bytes]:
            for i in range(3):
                yield f"data: {i}\n\n".encode()

        chunks = [c async for c in merge_with_keepalive(source(), interval_seconds=5.0)]

        assert chunks == [b"data: 0\n\n", b"data: 1\n\n", b"data: 2\n\n"]

    @pytest.mark.asyncio
    async def test_silent_gap_injects_keepalive(self) -> None:
        """A gap longer than the interval yields a keepalive before the real chunk.

        Proves the connection is kept warm during a model "thinking" pause, which
        is exactly what stops CloudFront's ~60s idle timeout from cutting the stream.
        """

        async def source() -> AsyncIterator[bytes]:
            yield b"data: first\n\n"
            await asyncio.sleep(0.25)  # gap > interval below
            yield b"data: second\n\n"

        chunks = [c async for c in merge_with_keepalive(source(), interval_seconds=0.05)]

        assert chunks[0] == b"data: first\n\n"
        assert chunks[-1] == b"data: second\n\n"
        # At least one keepalive was injected during the 0.25s gap.
        assert b": keep-alive\n\n" in chunks
        # Keepalives are only ever injected BETWEEN real chunks, never spliced
        # into one — so the real payload chunks arrive intact and in order.
        assert [c for c in chunks if c != b": keep-alive\n\n"] == [
            b"data: first\n\n",
            b"data: second\n\n",
        ]

    @pytest.mark.asyncio
    async def test_keepalive_format(self) -> None:
        """The keepalive is a spec-compliant SSE comment line (ignored by parsers)."""

        async def source() -> AsyncIterator[bytes]:
            await asyncio.sleep(0.1)
            yield b"data: x\n\n"

        chunks = [c async for c in merge_with_keepalive(source(), interval_seconds=0.02)]

        assert b": keep-alive\n\n" in chunks
        assert all(c.startswith(b":") for c in chunks if c != b"data: x\n\n")

    @pytest.mark.asyncio
    async def test_custom_keepalive_used(self) -> None:
        """A caller-supplied keepalive (e.g. a binary ping frame) is what gets injected.

        The Bedrock-native binary path passes a ``ping`` chunk frame instead of an
        SSE comment, because the eventstream codec drops SSE comments. This proves
        the injected bytes are exactly what the caller asked for.
        """
        ping = b"\x00\x00PING-FRAME"

        async def source() -> AsyncIterator[bytes]:
            await asyncio.sleep(0.1)
            yield b"real-frame"

        chunks = [c async for c in merge_with_keepalive(source(), interval_seconds=0.02, keepalive=ping)]

        assert ping in chunks
        assert b": keep-alive\n\n" not in chunks
        assert chunks[-1] == b"real-frame"

    @pytest.mark.asyncio
    async def test_upstream_exception_propagates(self) -> None:
        """An error from the source is not swallowed — it surfaces to the client."""

        async def source() -> AsyncIterator[bytes]:
            yield b"data: ok\n\n"
            raise ValueError("boom")

        seen: list[bytes] = []
        with pytest.raises(ValueError, match="boom"):
            async for chunk in merge_with_keepalive(source(), interval_seconds=5.0):
                seen.append(chunk)

        assert seen == [b"data: ok\n\n"]

    @pytest.mark.asyncio
    async def test_close_releases_source(self) -> None:
        """When the consumer stops early, the upstream stream is closed, not leaked.

        Mirrors a client (CloudFront) disconnect: closing the wrapper must tear
        down the underlying Bedrock stream so it does not hang around consuming a
        pool client / socket.
        """
        closed = asyncio.Event()

        async def source() -> AsyncIterator[bytes]:
            try:
                yield b"data: one\n\n"
                await asyncio.sleep(10)  # would block; consumer bails before this
                yield b"data: never\n\n"
            finally:
                closed.set()

        agen = merge_with_keepalive(source(), interval_seconds=5.0)
        first = await agen.__anext__()
        assert first == b"data: one\n\n"
        # Consumer disconnects: closing the wrapper must close the source too.
        await agen.aclose()
        assert closed.is_set()


class TestEventstreamKeepalive:
    """The binary-path keepalive must be a valid, decodable Bedrock ping chunk."""

    def test_eventstream_keepalive_decodes_to_ping(self) -> None:
        """EVENTSTREAM_KEEPALIVE is a well-formed chunk frame carrying a ping event.

        If this frame were malformed, a Bedrock-native client (claude-cli) would
        fail to decode the stream and silently retry non-streaming — the exact
        double-billing failure the eventstream codec exists to prevent.
        """
        import base64 as _b64
        import json as _json

        from src.proxy.eventstream_codec import EVENTSTREAM_KEEPALIVE

        # Frame layout: [4B total_len][4B headers_len][4B prelude_crc][headers][payload][4B msg_crc]
        total_len = int.from_bytes(EVENTSTREAM_KEEPALIVE[0:4], "big")
        headers_len = int.from_bytes(EVENTSTREAM_KEEPALIVE[4:8], "big")
        assert total_len == len(EVENTSTREAM_KEEPALIVE)

        payload_start = 12 + headers_len
        payload = EVENTSTREAM_KEEPALIVE[payload_start : total_len - 4]
        envelope = _json.loads(payload)
        event = _json.loads(_b64.b64decode(envelope["bytes"]))
        assert event == {"type": "ping"}
