"""Unit tests for chat logging service (Issue #143)."""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from src.chat_logging.comprehend_client import ComprehendPiiDetector, PiiDetectionResult
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
        detector.detect_and_redact_dict = AsyncMock(return_value=({}, PiiDetectionResult(content="")))
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
        assert response["usage"] == {}

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


class TestComprehendFailureIsLoudAndStillRedacts:
    """Issue #5672: a failed PII pass must be visible, and must not store clear text.

    Comprehend can be unavailable in an account — service not enabled, workload role
    missing comprehend:DetectPiiEntities, or quota exhausted. That used to produce a
    WARNING and a silent downgrade to regex-only redaction, so an environment could
    run for months believing it had person-name and postal-address coverage it never
    had. These tests use the REAL scrub pipeline, so they also prove the stored
    record is still redacted when the external service is gone.
    """

    @pytest.fixture
    def failing_comprehend(self):
        detector = MagicMock()
        detector.detect_and_redact_dict = AsyncMock(side_effect=RuntimeError("AccessDeniedException: comprehend:DetectPiiEntities"))
        return detector

    @pytest.fixture
    def service_with_real_pipeline(self, failing_comprehend):
        from src.chat_logging.scrubber import ScrubPipeline

        writer = MagicMock()
        writer.write_log = AsyncMock(return_value=True)
        writer.is_healthy = True

        service = ChatLoggingService(
            s3_writer=writer,
            scrub_pipeline=ScrubPipeline(),
            comprehend_detector=failing_comprehend,
            scrub_level=ScrubLevel.STANDARD,
            exclude_models=[],
            enabled=True,
        )
        service._bucket_name = "test-bucket"
        return service

    @pytest.mark.asyncio
    async def test_failure_is_logged_at_error_not_warning(self, service_with_real_pipeline, sample_timestamp, caplog):
        with caplog.at_level("ERROR"):
            service_with_real_pipeline.log_chat_async(
                request_id="req-pii-fail",
                timestamp=sample_timestamp,
                org_id="org-1",
                user_id="user-1",
                team_id="team-1",
                account_type="human",
                model="claude-3-sonnet",
                api_format="anthropic",
                latency_ms=10.0,
                request_body={"messages": [{"role": "user", "content": "hello"}]},
                response_body={"content": []},
            )
            await asyncio.sleep(0.1)

        errors = [r for r in caplog.records if r.levelname == "ERROR"]
        assert errors, "a failed PII pass must reach alerting, not sit at WARNING"
        assert "Comprehend PII detection failed" in caplog.text

    @pytest.mark.asyncio
    async def test_failed_detection_sanitizes_invalid_transcript(self, service_with_real_pipeline, sample_timestamp, caplog):
        private_content = "PRIVATE-CONTENT-SENTINEL"

        with caplog.at_level("ERROR"):
            await service_with_real_pipeline._log_chat_impl(
                request_id="req-invalid-transcript",
                timestamp=sample_timestamp,
                org_id="org-1",
                user_id="user-1",
                team_id="team-1",
                account_type="human",
                model="claude-3-sonnet",
                api_format="anthropic",
                latency_ms=10.0,
                request_body={"messages": private_content},
                response_body={"content": []},
            )

        assert private_content not in caplog.text
        service_with_real_pipeline._s3_writer.write_log.assert_called_once()
        log_data = service_with_real_pipeline._s3_writer.write_log.call_args.kwargs["log_data"]
        assert private_content not in str(log_data)
        assert log_data["request"]["messages"] == [{"content": "[PII:DETECTION_FAILED]"}]

    @pytest.mark.asyncio
    async def test_record_is_still_written_and_marks_the_degradation(self, service_with_real_pipeline, sample_timestamp):
        """The audit record remains available without retaining unverified content."""
        service_with_real_pipeline.log_chat_async(
            request_id="req-pii-fail",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=10.0,
            request_body={"messages": [{"role": "user", "content": "Alice Example"}]},
            response_body={"content": [{"type": "text", "text": "123 Example Street"}]},
        )
        await asyncio.sleep(0.1)

        service_with_real_pipeline._s3_writer.write_log.assert_called_once()
        log_data = service_with_real_pipeline._s3_writer.write_log.call_args.kwargs["log_data"]
        stored = str(log_data)
        assert "Alice Example" not in stored
        assert "123 Example Street" not in stored
        assert stored.count("[PII:DETECTION_FAILED]") >= 2
        assert log_data["scrubbing"]["pii_detection_failed"] is True

    @pytest.mark.asyncio
    async def test_regex_layer_still_redacts_when_comprehend_is_gone(self, service_with_real_pipeline, sample_timestamp):
        """The acceptance case: degraded environment must not store clear personal data."""
        service_with_real_pipeline.log_chat_async(
            request_id="req-pii-fail",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=10.0,
            request_body={
                "messages": [
                    {
                        "role": "user",
                        "content": "email jane.doe@example.com, phone (555) 123-4567, SSN 123-45-6789, card 4111111111111111",
                    }
                ]
            },
            response_body={"content": [{"type": "text", "text": "Noted for jane.doe@example.com"}]},
        )
        await asyncio.sleep(0.1)

        log_data = service_with_real_pipeline._s3_writer.write_log.call_args.kwargs["log_data"]
        stored = str(log_data)
        for secret in ["jane.doe@example.com", "(555) 123-4567", "123-45-6789", "4111111111111111"]:
            assert secret not in stored, f"{secret!r} was stored in the clear"

    @pytest.mark.asyncio
    async def test_numeric_tool_data_is_redacted_before_persistence(self, sample_timestamp):
        from src.chat_logging.scrubber import ScrubPipeline

        writer = MagicMock()
        writer.write_log = AsyncMock(return_value=True)
        comprehend_client = MagicMock()
        comprehend_client.detect_pii_entities.return_value = {"Entities": []}
        detector = ComprehendPiiDetector()
        detector._client = comprehend_client

        service = ChatLoggingService(
            s3_writer=writer,
            scrub_pipeline=ScrubPipeline(),
            comprehend_detector=detector,
            scrub_level=ScrubLevel.STANDARD,
            exclude_models=[],
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        service.log_chat_async(
            request_id="req-numeric-pii",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=10.0,
            request_body={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "tool-1",
                                "content": {"ssn": 123456789, "attempt": 2},
                            }
                        ],
                    }
                ],
                "max_tokens": 128,
            },
            response_body={
                "content": [
                    {
                        "type": "tool_use",
                        "id": "tool-2",
                        "name": "charge_card",
                        "input": {"card_number": 4111111111111111, "status_code": 200},
                    }
                ],
                "usage": {"input_tokens": 123456789, "output_tokens": 16},
            },
        )
        await asyncio.sleep(0.1)

        log_data = writer.write_log.call_args.kwargs["log_data"]
        stored = str(log_data)
        assert "4111111111111111" not in stored
        assert log_data["request"]["messages"][0]["content"][0]["content"]["ssn"] == "[REDACTED:NATIONAL_ID]"
        assert log_data["request"]["messages"][0]["content"][0]["content"]["attempt"] == 2
        assert log_data["response"]["content"][0]["input"]["card_number"] == "[REDACTED:PAYMENT_CARD]"
        assert log_data["response"]["content"][0]["input"]["status_code"] == 200
        assert log_data["response"]["usage"]["input_tokens"] == 123456789

    @pytest.mark.asyncio
    async def test_healthy_comprehend_does_not_mark_degradation(self, sample_timestamp):
        """The flag must mean something: it stays false on the success path."""
        from src.chat_logging.scrubber import ScrubPipeline

        writer = MagicMock()
        writer.write_log = AsyncMock(return_value=True)
        detector = MagicMock()
        detector.detect_and_redact_dict = AsyncMock(side_effect=lambda data: (data, PiiDetectionResult(content="")))

        service = ChatLoggingService(
            s3_writer=writer,
            scrub_pipeline=ScrubPipeline(),
            comprehend_detector=detector,
            scrub_level=ScrubLevel.STANDARD,
            exclude_models=[],
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        service.log_chat_async(
            request_id="req-ok",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=10.0,
            request_body={"messages": [{"role": "user", "content": "hello"}]},
            response_body={"content": []},
        )
        await asyncio.sleep(0.1)

        log_data = writer.write_log.call_args.kwargs["log_data"]
        assert log_data["scrubbing"]["pii_detection_failed"] is False

    @pytest.mark.asyncio
    async def test_comprehend_runs_on_both_request_and_response(self, sample_timestamp):
        """Acceptance: the PII pass covers both sides of the transcript, not just the prompt."""
        from src.chat_logging.scrubber import ScrubPipeline

        writer = MagicMock()
        writer.write_log = AsyncMock(return_value=True)
        detector = MagicMock()
        detector.detect_and_redact_dict = AsyncMock(side_effect=lambda data: (data, PiiDetectionResult(content="")))

        service = ChatLoggingService(
            s3_writer=writer,
            scrub_pipeline=ScrubPipeline(),
            comprehend_detector=detector,
            scrub_level=ScrubLevel.STANDARD,
            exclude_models=[],
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        service.log_chat_async(
            request_id="req-both",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=10.0,
            request_body={"messages": [{"role": "user", "content": "hello"}]},
            response_body={"content": [{"type": "text", "text": "hi"}]},
        )
        await asyncio.sleep(0.1)

        assert detector.detect_and_redact_dict.await_count == 2

    @pytest.mark.asyncio
    async def test_request_failure_does_not_skip_response_redaction(self, sample_timestamp):
        from src.chat_logging.scrubber import ScrubPipeline

        writer = MagicMock()
        writer.write_log = AsyncMock(return_value=True)
        detector = MagicMock()
        detector.detect_and_redact_dict = AsyncMock(
            side_effect=[
                RuntimeError("request detection unavailable"),
                (
                    {"content": [{"type": "text", "text": "[PII:NAME] lives at [PII:ADDRESS]"}]},
                    PiiDetectionResult(content="", redactions_count=2, pii_types_found=["NAME", "ADDRESS"]),
                ),
            ]
        )

        service = ChatLoggingService(
            s3_writer=writer,
            scrub_pipeline=ScrubPipeline(),
            comprehend_detector=detector,
            scrub_level=ScrubLevel.STANDARD,
            exclude_models=[],
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        service.log_chat_async(
            request_id="req-request-fails",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=10.0,
            request_body={"messages": [{"role": "user", "content": "hello"}]},
            response_body={"content": [{"type": "text", "text": "Alice Example lives at 123 Example Street"}]},
        )
        await asyncio.sleep(0.1)

        assert detector.detect_and_redact_dict.await_count == 2
        log_data = writer.write_log.call_args.kwargs["log_data"]
        assert "Alice Example" not in str(log_data["response"])
        assert "123 Example Street" not in str(log_data["response"])
        assert log_data["scrubbing"]["pii_detection_failed"] is True

    @pytest.mark.asyncio
    async def test_production_error_result_marks_degradation(self, sample_timestamp):
        from src.chat_logging.scrubber import ScrubPipeline

        writer = MagicMock()
        writer.write_log = AsyncMock(return_value=True)
        comprehend_client = MagicMock()
        comprehend_client.detect_pii_entities.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "DetectPiiEntities",
        )
        detector = ComprehendPiiDetector()
        detector._client = comprehend_client

        service = ChatLoggingService(
            s3_writer=writer,
            scrub_pipeline=ScrubPipeline(),
            comprehend_detector=detector,
            scrub_level=ScrubLevel.STANDARD,
            exclude_models=[],
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        service.log_chat_async(
            request_id="req-returned-error",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=10.0,
            request_body={"messages": [{"role": "user", "content": "Alice Example"}]},
            response_body={"content": [{"type": "text", "text": "123 Example Street"}]},
        )
        await asyncio.sleep(0.1)

        assert comprehend_client.detect_pii_entities.call_count >= 2
        log_data = writer.write_log.call_args.kwargs["log_data"]
        stored = str(log_data)
        assert "Alice Example" not in stored
        assert "123 Example Street" not in stored
        assert stored.count("[PII:DETECTION_FAILED]") >= 2
        assert log_data["scrubbing"]["pii_detection_failed"] is True

    @pytest.mark.asyncio
    async def test_short_request_and_response_pii_are_detected(self, sample_timestamp):
        from src.chat_logging.scrubber import ScrubPipeline

        writer = MagicMock()
        writer.write_log = AsyncMock(return_value=True)
        comprehend_client = MagicMock()

        def detect_pii_entities(**kwargs):
            text = kwargs["Text"]
            entity_types = {"John Doe": "NAME", "1 Main St": "ADDRESS"}
            entity_type = entity_types.get(text)
            if entity_type is None:
                return {"Entities": []}
            return {"Entities": [{"Type": entity_type, "Score": 0.99, "BeginOffset": 0, "EndOffset": len(text)}]}

        comprehend_client.detect_pii_entities.side_effect = detect_pii_entities
        detector = ComprehendPiiDetector()
        detector._client = comprehend_client

        service = ChatLoggingService(
            s3_writer=writer,
            scrub_pipeline=ScrubPipeline(),
            comprehend_detector=detector,
            scrub_level=ScrubLevel.STANDARD,
            exclude_models=[],
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        service.log_chat_async(
            request_id="req-short-pii",
            timestamp=sample_timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=10.0,
            request_body={"messages": [{"role": "user", "content": "John Doe"}]},
            response_body={"content": [{"type": "text", "text": "1 Main St"}]},
        )
        await asyncio.sleep(0.1)

        log_data = writer.write_log.call_args.kwargs["log_data"]
        stored = str(log_data)
        assert "John Doe" not in stored
        assert "1 Main St" not in stored
        assert "[PII:NAME]" in stored
        assert "[PII:ADDRESS]" in stored
        assert log_data["scrubbing"]["pii_detection_failed"] is False
