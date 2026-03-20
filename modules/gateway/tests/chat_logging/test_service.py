"""Unit tests for chat logging service (Issue #143)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.chat_logging.config import ScrubLevel
from src.chat_logging.service import ChatLoggingService, StreamingResponseBuffer


class TestChatLoggingService:
    """Tests for the main chat logging service."""

    @pytest.fixture
    def mock_s3_writer(self):
        """Create mock S3 writer."""
        writer = MagicMock()
        writer.write_log = AsyncMock(return_value=True)
        writer.is_healthy = True
        return writer

    @pytest.fixture
    def mock_scrub_pipeline(self):
        """Create mock scrub pipeline."""
        pipeline = MagicMock()
        pipeline.scrub_request.return_value = (
            {"messages": []},
            MagicMock(redactions_count=0, patterns_matched=[], headers_scrubbed=[]),
        )
        pipeline.scrub_response.return_value = (
            {"content": []},
            MagicMock(redactions_count=0, patterns_matched=[]),
        )
        return pipeline

    @pytest.fixture
    def mock_comprehend_detector(self):
        """Create mock Comprehend detector."""
        detector = MagicMock()
        detector.detect_and_redact_dict = AsyncMock(return_value=({}, MagicMock(redactions_count=0, pii_types_found=[])))
        return detector

    @pytest.fixture
    def service(self, mock_s3_writer, mock_scrub_pipeline, mock_comprehend_detector):
        """Create chat logging service with mocked components."""
        service = ChatLoggingService(
            s3_writer=mock_s3_writer,
            scrub_pipeline=mock_scrub_pipeline,
            comprehend_detector=mock_comprehend_detector,
            scrub_level=ScrubLevel.STANDARD,
            exclude_models=[],
            enabled=True,
        )
        # Mock bucket name
        service._bucket_name = "test-bucket"
        return service

    def test_enabled_property(self, service):
        """Test enabled property."""
        assert service.enabled is True

    def test_enabled_false_when_no_bucket(self, mock_s3_writer, mock_scrub_pipeline):
        """Test that service is disabled when no bucket configured."""
        service = ChatLoggingService(
            s3_writer=mock_s3_writer,
            scrub_pipeline=mock_scrub_pipeline,
            enabled=True,
        )
        service._bucket_name = ""
        assert service.enabled is False

    def test_should_log_returns_true(self, service):
        """Test should_log returns True for enabled service."""
        assert service.should_log("claude-3-sonnet") is True

    def test_should_log_returns_false_for_excluded_model(self, mock_s3_writer, mock_scrub_pipeline):
        """Test should_log returns False for excluded models."""
        service = ChatLoggingService(
            s3_writer=mock_s3_writer,
            scrub_pipeline=mock_scrub_pipeline,
            exclude_models=["excluded-model"],
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        assert service.should_log("excluded-model") is False
        assert service.should_log("other-model") is True

    def test_should_log_returns_false_when_disabled(self, mock_s3_writer, mock_scrub_pipeline):
        """Test should_log returns False when service is disabled."""
        service = ChatLoggingService(
            s3_writer=mock_s3_writer,
            scrub_pipeline=mock_scrub_pipeline,
            enabled=False,
        )

        assert service.should_log("any-model") is False

    @pytest.mark.asyncio
    async def test_log_chat_async_creates_task(self, service, sample_request_body, sample_response_body, sample_timestamp):
        """Test that log_chat_async creates a fire-and-forget task."""
        service.log_chat_async(
            request_id="req-123",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=150.5,
            request_body=sample_request_body,
            response_body=sample_response_body,
            headers={"Authorization": "Bearer token"},
        )

        # Allow task to complete
        await asyncio.sleep(0.1)

        # Verify S3 write was called
        service._s3_writer.write_log.assert_called_once()

    @pytest.mark.asyncio
    async def test_log_chat_skips_excluded_model(
        self, mock_s3_writer, mock_scrub_pipeline, sample_request_body, sample_response_body, sample_timestamp
    ):
        """Test that logging is skipped for excluded models."""
        service = ChatLoggingService(
            s3_writer=mock_s3_writer,
            scrub_pipeline=mock_scrub_pipeline,
            exclude_models=["excluded-model"],
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        service.log_chat_async(
            request_id="req-123",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="excluded-model",
            api_format="anthropic",
            latency_ms=150.5,
            request_body=sample_request_body,
            response_body=sample_response_body,
        )

        await asyncio.sleep(0.1)

        # S3 write should not be called
        mock_s3_writer.write_log.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_chat_basic_scrub_level(self, mock_s3_writer, mock_scrub_pipeline, sample_request_body, sample_response_body, sample_timestamp):
        """Test that basic scrub level skips Comprehend."""
        mock_comprehend = MagicMock()
        mock_comprehend.detect_and_redact_dict = AsyncMock()

        service = ChatLoggingService(
            s3_writer=mock_s3_writer,
            scrub_pipeline=mock_scrub_pipeline,
            comprehend_detector=mock_comprehend,
            scrub_level=ScrubLevel.BASIC,
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        service.log_chat_async(
            request_id="req-123",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3",
            api_format="anthropic",
            latency_ms=100,
            request_body=sample_request_body,
            response_body=sample_response_body,
        )

        await asyncio.sleep(0.1)

        # Comprehend should not be called for basic level
        mock_comprehend.detect_and_redact_dict.assert_not_called()

    @pytest.mark.asyncio
    async def test_log_chat_standard_scrub_level(
        self, service, mock_comprehend_detector, sample_request_body, sample_response_body, sample_timestamp
    ):
        """Test that standard scrub level uses Comprehend."""
        service.log_chat_async(
            request_id="req-123",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3",
            api_format="anthropic",
            latency_ms=100,
            request_body=sample_request_body,
            response_body=sample_response_body,
        )

        await asyncio.sleep(0.1)

        # Comprehend should be called for standard level
        assert mock_comprehend_detector.detect_and_redact_dict.called

    @pytest.mark.asyncio
    async def test_log_chat_handles_errors_gracefully(self, service, sample_request_body, sample_response_body, sample_timestamp):
        """Test that errors in logging don't propagate."""
        service._s3_writer.write_log.side_effect = Exception("S3 error")

        # This should not raise
        service.log_chat_async(
            request_id="req-123",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3",
            api_format="anthropic",
            latency_ms=100,
            request_body=sample_request_body,
            response_body=sample_response_body,
        )

        await asyncio.sleep(0.1)
        # No exception should be raised

    def test_is_healthy(self, service):
        """Test health check reflects S3 writer status."""
        service._s3_writer.is_healthy = True
        assert service.is_healthy is True

        service._s3_writer.is_healthy = False
        assert service.is_healthy is False


class TestStreamingResponseBuffer:
    """Tests for streaming response buffer."""

    def test_add_content_block_delta(self):
        """Test buffering content_block_delta chunks."""
        buffer = StreamingResponseBuffer()

        buffer.add_chunk(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            }
        )
        buffer.add_chunk(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": " world"},
            }
        )

        assert buffer.content == "Hello world"
        assert buffer.chunk_count == 2

    def test_add_message_start(self):
        """Test buffering message_start chunks."""
        buffer = StreamingResponseBuffer()

        buffer.add_chunk(
            {
                "type": "message_start",
                "message": {
                    "model": "claude-3-sonnet",
                    "usage": {"input_tokens": 10},
                },
            }
        )

        assert buffer._model == "claude-3-sonnet"
        assert buffer.usage["input_tokens"] == 10

    def test_add_message_delta(self):
        """Test buffering message_delta chunks."""
        buffer = StreamingResponseBuffer()

        buffer.add_chunk(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 50},
            }
        )

        assert buffer._stop_reason == "end_turn"
        assert buffer.usage["output_tokens"] == 50

    def test_reconstruct_response(self):
        """Test reconstructing full response from chunks."""
        buffer = StreamingResponseBuffer()

        # Simulate full stream
        buffer.add_chunk(
            {
                "type": "message_start",
                "message": {
                    "model": "claude-3-sonnet",
                    "usage": {"input_tokens": 10},
                },
            }
        )
        buffer.add_chunk(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            }
        )
        buffer.add_chunk(
            {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": " world!"},
            }
        )
        buffer.add_chunk(
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 5},
            }
        )

        response = buffer.reconstruct_response()

        assert response["content"][0]["text"] == "Hello world!"
        assert response["stop_reason"] == "end_turn"
        assert response["usage"]["input_tokens"] == 10
        assert response["usage"]["output_tokens"] == 5
        assert response["model"] == "claude-3-sonnet"

    def test_reconstruct_empty_response(self):
        """Test reconstructing empty response."""
        buffer = StreamingResponseBuffer()
        response = buffer.reconstruct_response()

        assert response["content"] == []
        assert response["stop_reason"] is None
        assert response["usage"]["input_tokens"] == 0
        assert response["usage"]["output_tokens"] == 0

    def test_chunk_count(self):
        """Test chunk counting."""
        buffer = StreamingResponseBuffer()

        assert buffer.chunk_count == 0

        buffer.add_chunk({"type": "message_start"})
        buffer.add_chunk({"type": "content_block_delta"})

        assert buffer.chunk_count == 2

    def test_usage_property_copy(self):
        """Test that usage property returns a copy."""
        buffer = StreamingResponseBuffer()
        buffer.add_chunk(
            {
                "type": "message_start",
                "message": {"usage": {"input_tokens": 10}},
            }
        )

        usage1 = buffer.usage
        usage1["input_tokens"] = 999

        # Original should be unchanged
        assert buffer.usage["input_tokens"] == 10
