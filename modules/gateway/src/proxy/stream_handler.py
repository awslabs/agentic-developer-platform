"""SSE streaming response handler.

Handles Server-Sent Events (SSE) formatting and streaming for all API formats.
"""

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any, Literal

from src.proxy.format_translator import FormatTranslator

logger = logging.getLogger(__name__)


class StreamingError(Exception):
    """Error during streaming."""

    def __init__(self, message: str, chunk_index: int | None = None):
        self.message = message
        self.chunk_index = chunk_index
        if chunk_index is not None:
            err_msg = f"Streaming error at chunk {chunk_index}: {message}"
        else:
            err_msg = f"Streaming error: {message}"
        super().__init__(err_msg)


class StreamHandler:
    """Handles SSE streaming responses for all API formats.

    Supports:
    - OpenAI SSE format (data: {json}\\n\\n)
    - Anthropic SSE format (event: type\\ndata: {json}\\n\\n)
    - Bedrock streaming format (raw bytes)
    """

    def __init__(self) -> None:
        """Initialize the stream handler."""
        self._translator = FormatTranslator()

    async def create_sse_response(
        self,
        stream: AsyncIterator[bytes],
        api_format: Literal["openai", "anthropic", "bedrock"],
        model: str,
        response_id: str | None = None,
    ) -> AsyncIterator[bytes]:
        """Create an SSE response from a Bedrock stream.

        Args:
            stream: Async iterator of Bedrock response chunks
            api_format: Target API format for the SSE response
            model: Model name for the response
            response_id: Optional response ID (generated if not provided)

        Yields:
            SSE formatted bytes
        """
        response_id = response_id or str(uuid.uuid4())
        chunk_index = 0

        try:
            async for chunk in stream:
                try:
                    # Parse the Bedrock chunk
                    bedrock_chunk = self._parse_bedrock_chunk(chunk)
                    if bedrock_chunk is None:
                        continue

                    # Convert and format based on target API format
                    if api_format == "openai":
                        formatted = await self._format_openai_sse(bedrock_chunk, model, response_id)
                    elif api_format == "anthropic":
                        formatted = await self._format_anthropic_sse(bedrock_chunk, model, response_id)
                    else:  # bedrock
                        formatted = await self._format_bedrock_sse(bedrock_chunk)

                    if formatted:
                        yield formatted

                    chunk_index += 1

                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse chunk {chunk_index}: {e}")
                    continue

            # Send final SSE based on format
            final = await self._create_final_sse(api_format, model, response_id)
            if final:
                yield final

        except asyncio.CancelledError:
            logger.info("Stream cancelled by client")
            raise
        except Exception as e:
            logger.error(f"Error during streaming: {e}")
            raise StreamingError(str(e), chunk_index)

    async def stream_bedrock_response(
        self,
        bedrock_stream: AsyncIterator[bytes],
    ) -> AsyncIterator[dict[str, Any]]:
        """Parse and yield Bedrock streaming chunks.

        Args:
            bedrock_stream: Raw Bedrock streaming response

        Yields:
            Parsed chunk dictionaries
        """
        async for chunk in bedrock_stream:
            parsed = self._parse_bedrock_chunk(chunk)
            if parsed:
                yield parsed

    def _parse_bedrock_chunk(self, chunk: bytes) -> dict[str, Any] | None:
        """Parse a raw Bedrock streaming chunk.

        Bedrock returns chunks in EventStream format with headers and payload.

        Args:
            chunk: Raw chunk bytes

        Returns:
            Parsed chunk dictionary, or None if parsing fails
        """
        try:
            # Check if chunk is already JSON
            if chunk.startswith(b"{"):
                return json.loads(chunk)

            # Try to extract JSON from EventStream format
            # The chunk may have binary headers followed by JSON payload
            chunk_str = chunk.decode("utf-8", errors="ignore")

            # Look for JSON object in the chunk
            start = chunk_str.find("{")
            if start >= 0:
                # Find matching closing brace
                brace_count = 0
                for i, char in enumerate(chunk_str[start:]):
                    if char == "{":
                        brace_count += 1
                    elif char == "}":
                        brace_count -= 1
                        if brace_count == 0:
                            json_str = chunk_str[start : start + i + 1]
                            return json.loads(json_str)

            return None

        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            logger.debug(f"Failed to parse chunk: {e}")
            return None

    async def _format_openai_sse(
        self,
        bedrock_chunk: dict[str, Any],
        model: str,
        response_id: str,
    ) -> bytes | None:
        """Format a Bedrock chunk as OpenAI SSE.

        Args:
            bedrock_chunk: Parsed Bedrock chunk
            model: Model name
            response_id: Response ID

        Returns:
            SSE formatted bytes, or None to skip
        """
        converted = self._translator.convert_bedrock_stream_chunk_to_openai(bedrock_chunk, model, response_id)

        if converted is None:
            # Check if this is the message_stop event
            if bedrock_chunk.get("type") == "message_stop":
                return b"data: [DONE]\n\n"
            return None

        json_data = json.dumps(converted)
        return f"data: {json_data}\n\n".encode()

    async def _format_anthropic_sse(
        self,
        bedrock_chunk: dict[str, Any],
        model: str,
        response_id: str,
    ) -> bytes | None:
        """Format a Bedrock chunk as Anthropic SSE.

        Args:
            bedrock_chunk: Parsed Bedrock chunk
            model: Model name
            response_id: Response ID

        Returns:
            SSE formatted bytes, or None to skip
        """
        converted = self._translator.convert_bedrock_stream_chunk_to_anthropic(bedrock_chunk, model, response_id)

        if converted is None:
            return None

        event_type = converted.get("type", "message")
        json_data = json.dumps(converted)
        return f"event: {event_type}\ndata: {json_data}\n\n".encode()

    async def _format_bedrock_sse(
        self,
        bedrock_chunk: dict[str, Any],
    ) -> bytes | None:
        """Format a Bedrock chunk for pass-through SSE.

        Args:
            bedrock_chunk: Parsed Bedrock chunk

        Returns:
            SSE formatted bytes
        """
        json_data = json.dumps(bedrock_chunk)
        event_type = bedrock_chunk.get("type", "chunk")
        return f"event: {event_type}\ndata: {json_data}\n\n".encode()

    async def _create_final_sse(
        self,
        api_format: Literal["openai", "anthropic", "bedrock"],
        model: str,
        response_id: str,
    ) -> bytes | None:
        """Create the final SSE event to close the stream.

        Args:
            api_format: Target API format
            model: Model name
            response_id: Response ID

        Returns:
            Final SSE bytes, or None if no final event needed
        """
        if api_format == "openai":
            # OpenAI uses [DONE] marker
            return b"data: [DONE]\n\n"
        elif api_format == "anthropic":
            # Anthropic uses message_stop event
            return b'event: message_stop\ndata: {"type": "message_stop"}\n\n'
        else:
            # Bedrock pass-through ends naturally
            return None

    def create_openai_stream_chunk(
        self,
        content: str | None,
        model: str,
        response_id: str,
        finish_reason: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        """Create an OpenAI format streaming chunk.

        Args:
            content: Text content delta
            model: Model name
            response_id: Response ID
            finish_reason: Optional finish reason
            role: Optional role (for first chunk)

        Returns:
            OpenAI streaming chunk dictionary
        """
        delta: dict[str, Any] = {}
        if role:
            delta["role"] = role
        if content is not None:
            delta["content"] = content

        return {
            "id": f"chatcmpl-{response_id}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": delta,
                    "finish_reason": finish_reason,
                }
            ],
        }

    def create_anthropic_stream_event(
        self,
        event_type: str,
        data: dict[str, Any],
    ) -> bytes:
        """Create an Anthropic format SSE event.

        Args:
            event_type: Event type (message_start, content_block_delta, etc.)
            data: Event data

        Returns:
            SSE formatted bytes
        """
        json_data = json.dumps(data)
        return f"event: {event_type}\ndata: {json_data}\n\n".encode()

    async def collect_stream(
        self,
        stream: AsyncIterator[bytes],
    ) -> tuple[str, dict[str, int]]:
        """Collect a stream into a single response.

        Useful for non-streaming mode when we need to wait for full response.

        Args:
            stream: The stream to collect

        Returns:
            Tuple of (full content text, usage stats)
        """
        content_parts: list[str] = []
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}

        async for chunk in stream:
            parsed = self._parse_bedrock_chunk(chunk)
            if parsed is None:
                continue

            chunk_type = parsed.get("type", "")

            if chunk_type == "content_block_delta":
                delta = parsed.get("delta", {})
                if delta.get("type") == "text_delta":
                    content_parts.append(delta.get("text", ""))

            elif chunk_type == "message_delta":
                delta_usage = parsed.get("usage", {})
                usage["output_tokens"] = delta_usage.get("output_tokens", 0)

            elif chunk_type == "message_start":
                message = parsed.get("message", {})
                start_usage = message.get("usage", {})
                usage["input_tokens"] = start_usage.get("input_tokens", 0)

        return "".join(content_parts), usage

    def keep_alive_generator(
        self,
        interval_seconds: float = 15.0,
    ) -> AsyncIterator[bytes]:
        """Create a keep-alive generator for long-running streams.

        Args:
            interval_seconds: Interval between keep-alive messages

        Yields:
            Keep-alive comment bytes
        """

        async def _generate() -> AsyncIterator[bytes]:
            while True:
                await asyncio.sleep(interval_seconds)
                yield b": keep-alive\n\n"

        return _generate()
