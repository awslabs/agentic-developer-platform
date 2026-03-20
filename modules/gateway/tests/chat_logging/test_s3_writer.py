"""Unit tests for S3 writer with circuit breaker (Issue #143)."""

import time
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from src.chat_logging.s3_writer import (
    ChatLogS3Writer,
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitState,
)


class TestCircuitBreaker:
    """Tests for circuit breaker pattern."""

    def test_initial_state_closed(self, circuit_breaker):
        """Test that circuit breaker starts in closed state."""
        assert circuit_breaker.state == CircuitState.CLOSED
        assert circuit_breaker.is_closed

    def test_opens_after_failure_threshold(self, circuit_breaker):
        """Test that circuit opens after reaching failure threshold."""
        # Failure threshold is 3 in fixture
        for _ in range(3):
            circuit_breaker.record_failure()

        assert circuit_breaker.state == CircuitState.OPEN
        assert circuit_breaker.is_open

    def test_stays_closed_below_threshold(self, circuit_breaker):
        """Test that circuit stays closed below failure threshold."""
        circuit_breaker.record_failure()
        circuit_breaker.record_failure()

        assert circuit_breaker.state == CircuitState.CLOSED

    def test_success_resets_failure_count(self, circuit_breaker):
        """Test that success resets the failure count."""
        circuit_breaker.record_failure()
        circuit_breaker.record_failure()
        circuit_breaker.record_success()

        # After reset, need 3 more failures to open
        circuit_breaker.record_failure()
        circuit_breaker.record_failure()
        assert circuit_breaker.state == CircuitState.CLOSED

    def test_half_open_after_recovery_timeout(self):
        """Test transition to half-open state after recovery timeout."""
        # Create a circuit breaker with a very short recovery timeout for testing
        config = CircuitBreakerConfig(
            failure_threshold=2,
            recovery_timeout_seconds=0.05,  # 50ms
            success_threshold=1,
        )
        cb = CircuitBreaker(config)

        # Open the circuit
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open

        # Wait for recovery timeout with margin
        time.sleep(0.15)

        # Accessing is_closed triggers recovery check
        _ = cb.is_closed
        assert cb.state == CircuitState.HALF_OPEN

    def test_closes_after_success_threshold_in_half_open(self, circuit_breaker):
        """Test that circuit closes after success threshold in half-open."""
        # Open the circuit
        for _ in range(3):
            circuit_breaker.record_failure()

        # Wait for recovery
        time.sleep(0.15)
        _ = circuit_breaker.is_closed  # Triggers state check

        # Now in half-open, need 2 successes (from fixture)
        circuit_breaker.record_success()
        circuit_breaker.record_success()

        assert circuit_breaker.state == CircuitState.CLOSED

    def test_reopens_on_failure_in_half_open(self, circuit_breaker):
        """Test that circuit reopens on failure in half-open state."""
        # Open the circuit
        for _ in range(3):
            circuit_breaker.record_failure()

        # Wait for recovery
        time.sleep(0.15)
        _ = circuit_breaker.is_closed  # Triggers state check, now half-open

        # Failure in half-open should reopen
        circuit_breaker.record_failure()

        assert circuit_breaker.state == CircuitState.OPEN


class TestChatLogS3Writer:
    """Tests for S3 writer functionality."""

    @pytest.fixture
    def s3_writer(self, circuit_breaker):
        """Create S3 writer with mocked components."""
        writer = ChatLogS3Writer(
            bucket_name="test-bucket",
            region_name="us-east-1",
            circuit_breaker=circuit_breaker,
        )
        return writer

    def test_generate_s3_key(self, s3_writer, sample_timestamp):
        """Test S3 key generation format."""
        key = s3_writer.generate_s3_key(
            org_id="org-123",
            user_id="user-456",
            request_id="req-789",
            timestamp=sample_timestamp,
        )

        expected = "org-123/user-456/2025/02/20/req-789.json"
        assert key == expected

    def test_generate_s3_key_anonymous_user(self, s3_writer, sample_timestamp):
        """Test S3 key generation with anonymous user."""
        key = s3_writer.generate_s3_key(
            org_id="org-123",
            user_id=None,
            request_id="req-789",
            timestamp=sample_timestamp,
        )

        expected = "org-123/anonymous/2025/02/20/req-789.json"
        assert key == expected

    @pytest.mark.asyncio
    async def test_write_log_success(self, s3_writer, sample_timestamp):
        """Test successful log write."""
        log_data = {"message": "test log"}

        with patch.object(s3_writer, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.put_object.return_value = {}
            mock_client_getter.return_value = mock_client

            result = await s3_writer.write_log(
                log_data=log_data,
                org_id="org-123",
                user_id="user-456",
                request_id="req-789",
                timestamp=sample_timestamp,
            )

            assert result is True
            mock_client.put_object.assert_called_once()

    @pytest.mark.asyncio
    async def test_write_log_circuit_open(self, s3_writer, sample_timestamp, circuit_breaker):
        """Test that writes are skipped when circuit is open."""
        # Open the circuit
        for _ in range(3):
            circuit_breaker.record_failure()

        log_data = {"message": "test log"}

        result = await s3_writer.write_log(
            log_data=log_data,
            org_id="org-123",
            user_id="user-456",
            request_id="req-789",
            timestamp=sample_timestamp,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_write_log_client_error(self, s3_writer, sample_timestamp, circuit_breaker):
        """Test handling of S3 client error."""
        log_data = {"message": "test log"}

        with patch.object(s3_writer, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.put_object.side_effect = ClientError(
                {"Error": {"Code": "AccessDenied", "Message": "Access Denied"}},
                "PutObject",
            )
            mock_client_getter.return_value = mock_client

            result = await s3_writer.write_log(
                log_data=log_data,
                org_id="org-123",
                user_id="user-456",
                request_id="req-789",
                timestamp=sample_timestamp,
            )

            assert result is False

    @pytest.mark.asyncio
    async def test_write_log_triggers_circuit_breaker(self, sample_timestamp):
        """Test that repeated failures trigger circuit breaker."""
        config = CircuitBreakerConfig(failure_threshold=2, recovery_timeout_seconds=60)
        cb = CircuitBreaker(config)
        writer = ChatLogS3Writer(bucket_name="test-bucket", circuit_breaker=cb)

        log_data = {"message": "test log"}

        with patch.object(writer, "_get_client") as mock_client_getter:
            mock_client = MagicMock()
            mock_client.put_object.side_effect = ClientError(
                {"Error": {"Code": "InternalError", "Message": "Internal Error"}},
                "PutObject",
            )
            mock_client_getter.return_value = mock_client

            # First failure
            await writer.write_log(
                log_data=log_data,
                org_id="org",
                user_id="user",
                request_id="req1",
                timestamp=sample_timestamp,
            )
            assert cb.state == CircuitState.CLOSED

            # Second failure should open circuit
            await writer.write_log(
                log_data=log_data,
                org_id="org",
                user_id="user",
                request_id="req2",
                timestamp=sample_timestamp,
            )
            assert cb.state == CircuitState.OPEN

    def test_is_healthy(self, s3_writer, circuit_breaker):
        """Test health check."""
        assert s3_writer.is_healthy is True

        # Open circuit
        for _ in range(3):
            circuit_breaker.record_failure()

        assert s3_writer.is_healthy is False

    def test_circuit_state_property(self, s3_writer):
        """Test circuit state property."""
        assert s3_writer.circuit_state == "closed"

    def test_json_serializer_datetime(self, s3_writer):
        """Test datetime serialization."""
        dt = datetime(2025, 2, 20, 10, 30, 0, tzinfo=UTC)
        result = s3_writer._json_serializer(dt)
        assert result == "2025-02-20T10:30:00+00:00"

    def test_json_serializer_unknown_type(self, s3_writer):
        """Test serializer raises for unknown types."""

        class CustomClass:
            pass

        with pytest.raises(TypeError):
            s3_writer._json_serializer(CustomClass())
