"""Chat Logging Service - main orchestration.

Issue #143: Coordinates the full async chat logging pipeline.

Pipeline:
1. Capture request/response
2. Header scrubbing
3. Regex-based secret detection
4. Comprehend PII detection (if standard level)
5. S3 write (fire-and-forget)
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Literal

from src.chat_logging.comprehend_client import ComprehendPiiDetector
from src.chat_logging.config import ScrubLevel, get_chat_logging_settings
from src.chat_logging.s3_writer import ChatLogS3Writer
from src.chat_logging.schemas import ChatLog, ChatLogRequest, ChatLogResponse, ScrubbingMetadata, UsageInfo
from src.chat_logging.scrubber import ScrubPipeline
from src.shared.config import get_settings

logger = logging.getLogger(__name__)


class ChatLoggingService:
    """Service for async chat logging with PII scrubbing.

    This service:
    - Captures full request/response bodies
    - Applies configurable scrubbing (headers, regex, Comprehend)
    - Writes to S3 asynchronously (fire-and-forget)
    - Has zero impact on response latency
    """

    def __init__(
        self,
        s3_writer: ChatLogS3Writer | None = None,
        scrub_pipeline: ScrubPipeline | None = None,
        comprehend_detector: ComprehendPiiDetector | None = None,
        scrub_level: ScrubLevel | None = None,
        exclude_models: list[str] | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Initialize the chat logging service.

        Args:
            s3_writer: Custom S3 writer instance
            scrub_pipeline: Custom scrub pipeline instance
            comprehend_detector: Custom Comprehend detector instance
            scrub_level: Scrubbing level (off|basic|standard)
            exclude_models: List of model IDs to skip logging for
            enabled: Override for enabling/disabling logging
        """
        settings = get_settings()
        chat_settings = get_chat_logging_settings()

        self._enabled = enabled if enabled is not None else chat_settings.chat_logging_enabled
        self._scrub_level = scrub_level or chat_settings.chat_logging_scrub_level
        self._exclude_models = exclude_models or chat_settings.chat_logging_exclude_models_list

        # Initialize components lazily or use provided ones
        self._s3_writer = s3_writer
        self._scrub_pipeline = scrub_pipeline or ScrubPipeline()
        self._comprehend_detector = comprehend_detector

        # Store config for lazy initialization
        self._bucket_name = chat_settings.chat_logging_bucket
        self._region = settings.aws_region

    def _get_s3_writer(self) -> ChatLogS3Writer:
        """Get or create the S3 writer."""
        if self._s3_writer is None:
            self._s3_writer = ChatLogS3Writer(
                bucket_name=self._bucket_name,
                region_name=self._region,
            )
        return self._s3_writer

    def _get_comprehend_detector(self) -> ComprehendPiiDetector:
        """Get or create the Comprehend detector."""
        if self._comprehend_detector is None:
            self._comprehend_detector = ComprehendPiiDetector(region_name=self._region)
        return self._comprehend_detector

    @property
    def enabled(self) -> bool:
        """Check if logging is enabled."""
        return self._enabled and bool(self._bucket_name)

    def should_log(self, model: str) -> bool:
        """Check if a model should be logged.

        Args:
            model: Model ID to check

        Returns:
            True if the model should be logged
        """
        if not self.enabled:
            return False

        # Check exclusion list
        if model in self._exclude_models:
            return False

        return True

    def log_chat_async(
        self,
        request_id: str,
        timestamp: datetime,
        org_id: str,
        user_id: str | None,
        team_id: str | None,
        account_type: Literal["human", "service"],
        model: str,
        api_format: Literal["bedrock", "anthropic", "openai"],
        latency_ms: float,
        request_body: dict[str, Any],
        response_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Fire-and-forget log a chat interaction.

        This method schedules the logging task and returns immediately.
        Any errors are logged but not propagated.

        Args:
            request_id: Unique request identifier
            timestamp: Request timestamp
            org_id: Organization ID
            user_id: User ID (optional)
            team_id: Team ID (optional)
            account_type: Account type (human/service)
            model: Model ID
            api_format: API format used
            latency_ms: Request latency in milliseconds
            request_body: Full request body
            response_body: Full response body
            headers: Request headers (for scrubbing)
        """
        if not self.should_log(model):
            return

        # Create fire-and-forget task
        asyncio.create_task(
            self._log_chat_impl(
                request_id=request_id,
                timestamp=timestamp,
                org_id=org_id,
                user_id=user_id,
                team_id=team_id,
                account_type=account_type,
                model=model,
                api_format=api_format,
                latency_ms=latency_ms,
                request_body=request_body,
                response_body=response_body,
                headers=headers,
            ),
            name=f"chat_log_{request_id}",
        )

    async def _log_chat_impl(
        self,
        request_id: str,
        timestamp: datetime,
        org_id: str,
        user_id: str | None,
        team_id: str | None,
        account_type: Literal["human", "service"],
        model: str,
        api_format: Literal["bedrock", "anthropic", "openai"],
        latency_ms: float,
        request_body: dict[str, Any],
        response_body: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> None:
        """Implementation of chat logging.

        Args:
            Same as log_chat_async
        """
        try:
            # Step 1: Apply basic scrubbing (headers + regex)
            scrubbed_request, request_result = self._scrub_pipeline.scrub_request(
                request_body.copy(),
                headers,
            )
            scrubbed_response, response_result = self._scrub_pipeline.scrub_response(
                response_body.copy(),
            )

            # Track scrubbing metadata
            total_redactions = request_result.redactions_count + response_result.redactions_count
            all_patterns = list(set(request_result.patterns_matched + response_result.patterns_matched))
            headers_scrubbed = request_result.headers_scrubbed
            pii_types_found: list[str] = []

            # Step 2: Apply Comprehend PII detection if standard level
            if self._scrub_level == ScrubLevel.STANDARD:
                try:
                    comprehend = self._get_comprehend_detector()

                    # Process request
                    scrubbed_request, req_pii_result = await comprehend.detect_and_redact_dict(scrubbed_request)
                    total_redactions += req_pii_result.redactions_count
                    pii_types_found.extend(req_pii_result.pii_types_found)

                    # Process response
                    scrubbed_response, resp_pii_result = await comprehend.detect_and_redact_dict(scrubbed_response)
                    total_redactions += resp_pii_result.redactions_count
                    pii_types_found.extend(resp_pii_result.pii_types_found)

                except Exception as e:
                    logger.warning(f"Comprehend PII detection failed, continuing with regex only: {e}")

            # Step 3: Build chat log record
            chat_log = self._build_chat_log(
                request_id=request_id,
                timestamp=timestamp,
                org_id=org_id,
                user_id=user_id,
                team_id=team_id,
                account_type=account_type,
                model=model,
                api_format=api_format,
                latency_ms=latency_ms,
                scrubbed_request=scrubbed_request,
                scrubbed_response=scrubbed_response,
                scrub_level=self._scrub_level.value,
                total_redactions=total_redactions,
                pii_types_found=list(set(pii_types_found)),
                patterns_matched=all_patterns,
                headers_scrubbed=headers_scrubbed,
            )

            # Step 4: Write to S3
            s3_writer = self._get_s3_writer()
            await s3_writer.write_log(
                log_data=chat_log.model_dump(mode="json"),
                org_id=org_id,
                user_id=user_id,
                request_id=request_id,
                timestamp=timestamp,
            )

            logger.debug(
                "Chat logged successfully",
                extra={
                    "request_id": request_id,
                    "model": model,
                    "redactions": total_redactions,
                },
            )

        except Exception as e:
            # Log error but don't propagate - this is fire-and-forget
            logger.error(
                f"Failed to log chat: {e}",
                extra={
                    "request_id": request_id,
                    "model": model,
                    "error_type": type(e).__name__,
                },
            )

    def _build_chat_log(
        self,
        request_id: str,
        timestamp: datetime,
        org_id: str,
        user_id: str | None,
        team_id: str | None,
        account_type: Literal["human", "service"],
        model: str,
        api_format: Literal["bedrock", "anthropic", "openai"],
        latency_ms: float,
        scrubbed_request: dict[str, Any],
        scrubbed_response: dict[str, Any],
        scrub_level: str,
        total_redactions: int,
        pii_types_found: list[str],
        patterns_matched: list[str],
        headers_scrubbed: list[str],
    ) -> ChatLog:
        """Build the ChatLog record from components.

        Args:
            All the individual components

        Returns:
            ChatLog instance
        """
        # Build request schema
        request = ChatLogRequest(
            messages=scrubbed_request.get("messages", []),
            system=scrubbed_request.get("system"),
            tools=scrubbed_request.get("tools"),
            max_tokens=scrubbed_request.get("max_tokens"),
            temperature=scrubbed_request.get("temperature"),
            top_p=scrubbed_request.get("top_p"),
            top_k=scrubbed_request.get("top_k"),
            stop_sequences=scrubbed_request.get("stop_sequences"),
        )

        # Build response schema
        # Issue #1486: Include cache token fields for correct cost calculation
        usage_data = scrubbed_response.get("usage", {})
        usage = None
        if usage_data:
            usage = UsageInfo(
                input_tokens=usage_data.get("input_tokens", 0),
                output_tokens=usage_data.get("output_tokens", 0),
                cache_read_input_tokens=usage_data.get("cache_read_input_tokens", 0),
                cache_creation_input_tokens=usage_data.get("cache_creation_input_tokens", 0),
            )

        response = ChatLogResponse(
            content=scrubbed_response.get("content"),
            stop_reason=scrubbed_response.get("stop_reason"),
            usage=usage,
            model=scrubbed_response.get("model"),
        )

        # Build scrubbing metadata
        scrubbing = ScrubbingMetadata(
            level=scrub_level,
            redactions_count=total_redactions,
            pii_types_found=pii_types_found,
            regex_patterns_matched=patterns_matched,
            headers_scrubbed=headers_scrubbed,
        )

        return ChatLog(
            request_id=request_id,
            timestamp=timestamp,
            org_id=org_id,
            user_id=user_id,
            team_id=team_id,
            account_type=account_type,
            model=model,
            api_format=api_format,
            latency_ms=latency_ms,
            request=request,
            response=response,
            scrubbing=scrubbing,
        )

    @property
    def is_healthy(self) -> bool:
        """Check if the service is healthy."""
        if not self.enabled:
            return True  # Not enabled = not unhealthy

        if self._s3_writer:
            return self._s3_writer.is_healthy
        return True


# =============================================================================
# Streaming Response Buffer
# =============================================================================


class StreamingResponseBuffer:
    """Buffers streaming response chunks for logging.

    For streaming responses, we need to reconstruct the full response
    before logging while still passing chunks through to the client.
    """

    def __init__(self) -> None:
        """Initialize the buffer."""
        self._chunks: list[dict[str, Any]] = []
        self._content_parts: list[str] = []
        self._usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        self._stop_reason: str | None = None
        self._model: str | None = None

    def add_chunk(self, chunk: dict[str, Any]) -> None:
        """Add a streaming chunk to the buffer.

        Args:
            chunk: Parsed streaming chunk
        """
        self._chunks.append(chunk)
        chunk_type = chunk.get("type", "")

        if chunk_type == "content_block_delta":
            delta = chunk.get("delta", {})
            if delta.get("type") == "text_delta":
                self._content_parts.append(delta.get("text", ""))

        elif chunk_type == "message_delta":
            delta = chunk.get("delta", {})
            self._stop_reason = delta.get("stop_reason")
            delta_usage = chunk.get("usage", {})
            self._usage["output_tokens"] = delta_usage.get("output_tokens", 0)

        elif chunk_type == "message_start":
            message = chunk.get("message", {})
            self._model = message.get("model")
            start_usage = message.get("usage", {})
            self._usage["input_tokens"] = start_usage.get("input_tokens", 0)

    def reconstruct_response(self) -> dict[str, Any]:
        """Reconstruct the full response from buffered chunks.

        Returns:
            Dictionary representing the full response
        """
        content_text = "".join(self._content_parts)

        return {
            "content": [{"type": "text", "text": content_text}] if content_text else [],
            "stop_reason": self._stop_reason,
            "usage": self._usage,
            "model": self._model,
        }

    @property
    def content(self) -> str:
        """Get the accumulated content text."""
        return "".join(self._content_parts)

    @property
    def usage(self) -> dict[str, int]:
        """Get the usage statistics."""
        return self._usage.copy()

    @property
    def chunk_count(self) -> int:
        """Get the number of chunks buffered."""
        return len(self._chunks)


# =============================================================================
# Streaming Logging Wrapper Helper
# =============================================================================


def create_streaming_logging_wrapper(
    stream: Any,
    chat_logger: "ChatLoggingService",
    request_id: str,
    timestamp: datetime,
    org_id: str,
    user_id: str | None,
    team_id: str | None,
    account_type: Literal["human", "service"],
    model: str,
    api_format: Literal["bedrock", "anthropic", "openai"],
    request_body: dict[str, Any],
    headers: dict[str, str] | None,
    start_time: float,
) -> Any:
    """Create a streaming response wrapper that buffers chunks for logging.

    This helper reduces code duplication in proxy routes by providing a
    reusable wrapper for streaming responses with chat logging.

    Args:
        stream: The async iterator stream to wrap
        chat_logger: ChatLoggingService instance
        request_id: Unique request identifier
        timestamp: Request timestamp
        org_id: Organization ID
        user_id: User ID (optional)
        team_id: Team ID (optional)
        account_type: Account type (human/service)
        model: Model ID
        api_format: API format used (bedrock/anthropic/openai)
        request_body: Request body dict for logging
        headers: Request headers (optional)
        start_time: Start time from time.monotonic() for latency calculation

    Returns:
        An async generator that yields chunks and logs after completion
    """
    import json
    import time

    async def stream_with_logging():
        """Wrap stream to buffer response for logging."""
        buffer = StreamingResponseBuffer()

        try:
            async for chunk in stream:
                # Parse and buffer chunk for logging
                if chunk:
                    try:
                        chunk_str = chunk.decode("utf-8", errors="ignore")
                        # Look for JSON data in SSE format
                        if "data:" in chunk_str:
                            for line in chunk_str.split("\n"):
                                if line.startswith("data:"):
                                    json_str = line[5:].strip()
                                    if json_str and json_str != "[DONE]":
                                        try:
                                            chunk_data = json.loads(json_str)
                                            buffer.add_chunk(chunk_data)
                                        except json.JSONDecodeError:
                                            pass
                        elif chunk_str.startswith("{"):
                            try:
                                chunk_data = json.loads(chunk_str)
                                buffer.add_chunk(chunk_data)
                            except json.JSONDecodeError:
                                pass
                    except Exception:
                        pass  # Don't fail streaming due to logging

                yield chunk

            # After stream completes, log the reconstructed response
            latency_ms = (time.monotonic() - start_time) * 1000
            reconstructed_response = buffer.reconstruct_response()

            chat_logger.log_chat_async(
                request_id=request_id,
                timestamp=timestamp,
                org_id=org_id,
                user_id=user_id,
                team_id=team_id,
                account_type=account_type,
                model=model,
                api_format=api_format,
                latency_ms=latency_ms,
                request_body=request_body,
                response_body=reconstructed_response,
                headers=headers,
            )

        except Exception as e:
            logger.warning(f"Error in stream logging wrapper: {e}")
            raise

    return stream_with_logging()
