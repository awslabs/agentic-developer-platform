"""Async S3 writer with circuit breaker for chat logs.

Issue #143: Fire-and-forget S3 writes with failure protection.

Features:
- Async S3 writes using asyncio.to_thread
- Circuit breaker pattern to prevent cascading failures
- Automatic recovery when S3 becomes available again
"""

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


class CircuitState(StrEnum):
    """Circuit breaker states."""

    CLOSED = "closed"  # Normal operation
    OPEN = "open"  # Failing, rejecting requests
    HALF_OPEN = "half_open"  # Testing if service recovered


@dataclass
class CircuitBreakerConfig:
    """Configuration for the circuit breaker."""

    failure_threshold: int = 5  # Failures before opening circuit
    recovery_timeout_seconds: float = 60.0  # Time before trying again
    success_threshold: int = 3  # Successes before closing circuit


class CircuitBreaker:
    """Circuit breaker implementation.

    Prevents cascading failures by stopping requests when a threshold
    of failures is reached.
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        """Initialize the circuit breaker.

        Args:
            config: Circuit breaker configuration
        """
        self._config = config or CircuitBreakerConfig()
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: datetime | None = None

    @property
    def state(self) -> CircuitState:
        """Get current circuit state."""
        return self._state

    @property
    def is_closed(self) -> bool:
        """Check if circuit is closed (normal operation)."""
        self._check_recovery()
        return self._state == CircuitState.CLOSED

    @property
    def is_open(self) -> bool:
        """Check if circuit is open (blocking requests)."""
        self._check_recovery()
        return self._state == CircuitState.OPEN

    def record_success(self) -> None:
        """Record a successful operation."""
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self._config.success_threshold:
                self._close_circuit()
        elif self._state == CircuitState.CLOSED:
            self._failure_count = 0  # Reset on success

    def record_failure(self) -> None:
        """Record a failed operation."""
        self._failure_count += 1
        self._last_failure_time = datetime.now(UTC)

        if self._state == CircuitState.HALF_OPEN:
            self._open_circuit()
        elif self._failure_count >= self._config.failure_threshold:
            self._open_circuit()

    def _check_recovery(self) -> None:
        """Check if it's time to try recovery."""
        if self._state != CircuitState.OPEN:
            return

        if self._last_failure_time is None:
            return

        elapsed = (datetime.now(UTC) - self._last_failure_time).total_seconds()
        if elapsed >= self._config.recovery_timeout_seconds:
            self._state = CircuitState.HALF_OPEN
            self._success_count = 0
            logger.info("Circuit breaker entering half-open state, testing recovery")

    def _open_circuit(self) -> None:
        """Open the circuit (block requests)."""
        self._state = CircuitState.OPEN
        logger.warning(f"Circuit breaker OPENED after {self._failure_count} failures. S3 writes temporarily disabled.")

    def _close_circuit(self) -> None:
        """Close the circuit (resume normal operation)."""
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        logger.info("Circuit breaker CLOSED. S3 writes resumed.")


class ChatLogS3Writer:
    """Async S3 writer for chat logs with circuit breaker.

    Writes are fire-and-forget to avoid impacting response latency.
    """

    def __init__(
        self,
        bucket_name: str,
        region_name: str = "us-east-1",
        circuit_breaker: CircuitBreaker | None = None,
    ) -> None:
        """Initialize the S3 writer.

        Args:
            bucket_name: S3 bucket name for chat logs
            region_name: AWS region
            circuit_breaker: Custom circuit breaker instance
        """
        self._bucket_name = bucket_name
        self._region_name = region_name
        self._circuit_breaker = circuit_breaker or CircuitBreaker()
        self._client: Any = None

    def _get_client(self) -> Any:
        """Get or create the S3 client.

        Returns:
            boto3 S3 client
        """
        if self._client is None:
            self._client = boto3.client("s3", region_name=self._region_name)
        return self._client

    def generate_s3_key(
        self,
        org_id: str,
        user_id: str | None,
        request_id: str,
        timestamp: datetime,
    ) -> str:
        """Generate S3 key for a chat log.

        Format: {org_id}/{user_id}/{YYYY}/{MM}/{DD}/{request_id}.json

        Args:
            org_id: Organization ID
            user_id: User ID (optional)
            request_id: Request UUID
            timestamp: Request timestamp

        Returns:
            S3 object key
        """
        user_part = user_id or "anonymous"
        return f"{org_id}/{user_part}/{timestamp.strftime('%Y/%m/%d')}/{request_id}.json"

    async def write_log(
        self,
        log_data: dict[str, Any],
        org_id: str,
        user_id: str | None,
        request_id: str,
        timestamp: datetime,
    ) -> bool:
        """Write a chat log to S3 asynchronously.

        This is a fire-and-forget operation - errors are logged but not raised.

        Args:
            log_data: Chat log data to write
            org_id: Organization ID for path
            user_id: User ID for path
            request_id: Request ID for path
            timestamp: Timestamp for path

        Returns:
            True if successful, False otherwise
        """
        # Check circuit breaker
        if self._circuit_breaker.is_open:
            logger.debug("Circuit breaker open, skipping S3 write")
            return False

        s3_key = self.generate_s3_key(org_id, user_id, request_id, timestamp)

        try:
            # Run synchronous S3 put in thread pool
            await asyncio.to_thread(
                self._write_sync,
                log_data,
                s3_key,
            )
            self._circuit_breaker.record_success()
            logger.debug(f"Chat log written to s3://{self._bucket_name}/{s3_key}")
            return True

        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            logger.error(
                f"S3 write failed: {error_code}",
                extra={
                    "bucket": self._bucket_name,
                    "key": s3_key,
                    "error": str(e),
                },
            )
            self._circuit_breaker.record_failure()
            return False

        except Exception as e:
            logger.error(
                f"Unexpected error writing chat log: {e}",
                extra={
                    "bucket": self._bucket_name,
                    "key": s3_key,
                },
            )
            self._circuit_breaker.record_failure()
            return False

    def _write_sync(self, log_data: dict[str, Any], s3_key: str) -> None:
        """Synchronous S3 write (runs in thread pool).

        Args:
            log_data: Data to write
            s3_key: S3 object key
        """
        client = self._get_client()

        # Convert to JSON with datetime handling
        body = json.dumps(log_data, default=self._json_serializer)

        client.put_object(
            Bucket=self._bucket_name,
            Key=s3_key,
            Body=body.encode("utf-8"),
            ContentType="application/json",
        )

    @staticmethod
    def _json_serializer(obj: Any) -> str:
        """JSON serializer for objects not serializable by default.

        Args:
            obj: Object to serialize

        Returns:
            String representation
        """
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")

    @property
    def is_healthy(self) -> bool:
        """Check if the writer is healthy (circuit not open)."""
        return not self._circuit_breaker.is_open

    @property
    def circuit_state(self) -> str:
        """Get the current circuit breaker state."""
        return self._circuit_breaker.state.value
