"""Integration tests for chat logging (Issue #143)."""

import asyncio
import json
from datetime import UTC, datetime
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws

from src.chat_logging.config import ScrubLevel
from src.chat_logging.s3_writer import ChatLogS3Writer, CircuitBreaker, CircuitBreakerConfig
from src.chat_logging.scrubber import ScrubPipeline
from src.chat_logging.service import ChatLoggingService, StreamingResponseBuffer


class TestChatLoggingIntegration:
    """Integration tests for the full chat logging pipeline."""

    @pytest.fixture
    def sample_request_with_secrets(self):
        """Sample request containing embedded secrets."""
        return {
            "messages": [
                {
                    "role": "user",
                    "content": (
                        "Here is my config:\n"
                        "API key: sk-test-secret-key-1234567890123456\n"
                        "Database: postgresql://admin:secretpass@localhost/db\n"
                        "AWS key: AKIAIOSFODNN7EXAMPLE"
                    ),
                },
                {
                    "role": "assistant",
                    "content": "I see you've shared some configuration. Let me help.",
                },
            ],
            "model": "claude-3-sonnet",
            "max_tokens": 1024,
            "system": "You are a helpful assistant. JWT: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.test_jwt_token",
        }

    @pytest.fixture
    def sample_response_with_secrets(self):
        """Sample response that might contain echoed secrets."""
        return {
            "content": [
                {
                    "type": "text",
                    "text": "I noticed your API key sk-test-secret-key-1234567890123456. You should rotate it.",
                }
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }

    @pytest.fixture
    def timestamp(self):
        """Fixed timestamp for testing."""
        return datetime(2025, 2, 20, 12, 0, 0, tzinfo=UTC)

    def test_regex_scrubbing_removes_embedded_secrets(self, sample_request_with_secrets):
        """Test that embedded secrets are removed by regex scrubbing."""
        pipeline = ScrubPipeline()

        scrubbed, result = pipeline.scrub_request(sample_request_with_secrets, {})

        # Check that secrets are redacted
        content_str = json.dumps(scrubbed)

        # API key should be redacted
        assert "sk-test-secret-key" not in content_str
        assert "[REDACTED:" in content_str

        # PostgreSQL URI should be redacted
        assert "postgresql://" not in content_str

        # AWS key should be redacted
        assert "AKIAIOSFODNN7EXAMPLE" not in content_str

        # JWT should be redacted
        assert "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9" not in content_str

        # Redactions should be counted
        assert result.redactions_count >= 4

    def test_header_scrubbing_removes_auth_headers(self):
        """Test that sensitive headers are removed."""
        pipeline = ScrubPipeline()

        headers = {
            "Authorization": "Bearer super-secret-token",
            "X-Api-Key": "api-key-12345",
            "Cookie": "session=abc123",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        _, result = pipeline.scrub_request({"messages": []}, headers)

        # Sensitive headers should be scrubbed
        assert "Authorization" in result.headers_scrubbed
        assert "X-Api-Key" in result.headers_scrubbed
        assert "Cookie" in result.headers_scrubbed

        # Non-sensitive headers should not be scrubbed
        assert "Content-Type" not in result.headers_scrubbed
        assert "Accept" not in result.headers_scrubbed

    @mock_aws
    def test_s3_write_with_moto(self, timestamp):
        """Test S3 writing using moto mock."""
        # Create mock S3 bucket
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-chat-logs")

        # Create writer
        writer = ChatLogS3Writer(
            bucket_name="test-chat-logs",
            region_name="us-east-1",
        )
        writer._client = conn  # Use the moto-mocked client

        log_data = {
            "request_id": "test-req-123",
            "org_id": "org-1",
            "message": "Test log entry",
        }

        # Write log synchronously
        writer._write_sync(log_data, "org-1/user-1/2025/02/20/test-req-123.json")

        # Verify object was written
        response = conn.get_object(
            Bucket="test-chat-logs",
            Key="org-1/user-1/2025/02/20/test-req-123.json",
        )
        stored_data = json.loads(response["Body"].read().decode("utf-8"))

        assert stored_data["request_id"] == "test-req-123"
        assert stored_data["message"] == "Test log entry"

    @mock_aws
    @pytest.mark.asyncio
    async def test_full_pipeline_with_moto(self, sample_request_with_secrets, sample_response_with_secrets, timestamp):
        """Test full pipeline with mocked S3."""
        # Create mock S3 bucket
        conn = boto3.client("s3", region_name="us-east-1")
        conn.create_bucket(Bucket="test-chat-logs")

        # Create service components
        circuit_breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=5))
        s3_writer = ChatLogS3Writer(
            bucket_name="test-chat-logs",
            region_name="us-east-1",
            circuit_breaker=circuit_breaker,
        )
        s3_writer._client = conn

        scrub_pipeline = ScrubPipeline()

        # Create service with basic scrub level (no Comprehend)
        service = ChatLoggingService(
            s3_writer=s3_writer,
            scrub_pipeline=scrub_pipeline,
            scrub_level=ScrubLevel.BASIC,
            enabled=True,
        )
        service._bucket_name = "test-chat-logs"

        # Log the chat
        service.log_chat_async(
            request_id="int-test-123",
            timestamp=timestamp,
            org_id="org-integration",
            user_id="user-test",
            team_id="team-1",
            account_type="human",
            model="claude-3-sonnet",
            api_format="anthropic",
            latency_ms=250.5,
            request_body=sample_request_with_secrets,
            response_body=sample_response_with_secrets,
            headers={"Authorization": "Bearer token", "Content-Type": "application/json"},
        )

        # Wait for async task to complete
        await asyncio.sleep(0.5)

        # Verify log was written to S3
        s3_key = "org-integration/user-test/2025/02/20/int-test-123.json"
        response = conn.get_object(Bucket="test-chat-logs", Key=s3_key)
        stored_log = json.loads(response["Body"].read().decode("utf-8"))

        # Verify secrets were scrubbed
        log_str = json.dumps(stored_log)
        assert "sk-test-secret-key" not in log_str
        assert "postgresql://" not in log_str
        assert "AKIAIOSFODNN7EXAMPLE" not in log_str
        assert "[REDACTED:" in log_str

        # Verify metadata
        assert stored_log["org_id"] == "org-integration"
        assert stored_log["model"] == "claude-3-sonnet"
        assert stored_log["latency_ms"] == 250.5
        assert stored_log["scrubbing"]["level"] == "basic"

    def test_circuit_breaker_protects_against_cascading_failures(self):
        """Test that circuit breaker opens after repeated failures."""
        config = CircuitBreakerConfig(
            failure_threshold=3,
            recovery_timeout_seconds=60,
        )
        circuit_breaker = CircuitBreaker(config)
        writer = ChatLogS3Writer(
            bucket_name="test-bucket",
            region_name="us-east-1",
            circuit_breaker=circuit_breaker,
        )

        # Simulate 3 failures
        circuit_breaker.record_failure()
        circuit_breaker.record_failure()
        circuit_breaker.record_failure()

        # Circuit should be open now
        assert circuit_breaker.is_open
        assert not writer.is_healthy

    @pytest.mark.asyncio
    async def test_streaming_response_reconstruction(self):
        """Test that streaming responses are correctly reconstructed."""
        buffer = StreamingResponseBuffer()

        # Simulate streaming chunks
        chunks = [
            {"type": "message_start", "message": {"model": "claude-3", "usage": {"input_tokens": 25}}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": ", "}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "I'm "}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Claude!"}},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 15}},
        ]

        for chunk in chunks:
            buffer.add_chunk(chunk)

        response = buffer.reconstruct_response()

        assert response["content"][0]["text"] == "Hello, I'm Claude!"
        assert response["stop_reason"] == "end_turn"
        assert response["usage"]["input_tokens"] == 25
        assert response["usage"]["output_tokens"] == 15
        assert response["model"] == "claude-3"

    def test_model_exclusion_list(self):
        """Test that excluded models are not logged."""
        service = ChatLoggingService(
            exclude_models=["test-model-1", "test-model-2"],
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        assert service.should_log("test-model-1") is False
        assert service.should_log("test-model-2") is False
        assert service.should_log("other-model") is True

    def test_scrubbing_levels(self):
        """Test different scrubbing levels."""
        # Off level - should still create service but not do Comprehend
        service_off = ChatLoggingService(scrub_level=ScrubLevel.OFF, enabled=True)
        assert service_off._scrub_level == ScrubLevel.OFF

        # Basic level
        service_basic = ChatLoggingService(scrub_level=ScrubLevel.BASIC, enabled=True)
        assert service_basic._scrub_level == ScrubLevel.BASIC

        # Standard level
        service_standard = ChatLoggingService(scrub_level=ScrubLevel.STANDARD, enabled=True)
        assert service_standard._scrub_level == ScrubLevel.STANDARD

    @pytest.mark.asyncio
    async def test_zero_latency_impact_fire_and_forget(self, sample_request_with_secrets, timestamp):
        """Test that logging doesn't block the caller."""
        import time

        # Create a service that will be slow
        slow_mock_writer = MagicMock()

        async def slow_write(*args, **kwargs):
            await asyncio.sleep(0.5)  # Simulate slow S3 write
            return True

        slow_mock_writer.write_log = slow_write
        slow_mock_writer.is_healthy = True

        service = ChatLoggingService(
            s3_writer=slow_mock_writer,
            scrub_level=ScrubLevel.BASIC,
            enabled=True,
        )
        service._bucket_name = "test-bucket"

        # Time how long the call takes
        start = time.monotonic()
        service.log_chat_async(
            request_id="perf-test",
            timestamp=timestamp,
            org_id="org-1",
            user_id="user-1",
            team_id="team-1",
            account_type="human",
            model="claude-3",
            api_format="anthropic",
            latency_ms=100,
            request_body=sample_request_with_secrets,
            response_body={"content": []},
        )
        elapsed = time.monotonic() - start

        # The call should return immediately (< 100ms)
        # because it's fire-and-forget
        assert elapsed < 0.1, f"log_chat_async took {elapsed}s, should be < 0.1s"

        # Wait for async task to complete for cleanup
        await asyncio.sleep(0.6)
