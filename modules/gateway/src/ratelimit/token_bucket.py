"""
Token bucket algorithm implementation.

This module provides a thread-safe token bucket implementation
for rate limiting. The token bucket algorithm allows for bursting
while maintaining an average rate limit.
"""

import threading
import time
from dataclasses import dataclass


@dataclass
class BucketState:
    """State of a token bucket."""

    tokens: float
    last_refill: float  # timestamp


class TokenBucket:
    """
    Token bucket implementation for rate limiting.

    The token bucket algorithm works by:
    1. Starting with a full bucket of tokens
    2. Each request consumes tokens from the bucket
    3. Tokens are refilled at a constant rate over time
    4. If not enough tokens are available, the request is denied

    This allows for short bursts while maintaining a long-term average rate.
    """

    def __init__(self, capacity: float, refill_rate: float) -> None:
        """
        Initialize a token bucket.

        Args:
            capacity: Maximum number of tokens the bucket can hold
            refill_rate: Number of tokens added per second
        """
        if capacity <= 0:
            raise ValueError("Capacity must be positive")
        if refill_rate < 0:
            raise ValueError("Refill rate cannot be negative")

        self._capacity = capacity
        self._refill_rate = refill_rate
        self._tokens = capacity
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    @property
    def capacity(self) -> float:
        """Get the maximum capacity of the bucket."""
        return self._capacity

    @property
    def refill_rate(self) -> float:
        """Get the refill rate (tokens per second)."""
        return self._refill_rate

    @property
    def tokens(self) -> float:
        """Get the current number of tokens (may be stale)."""
        return self._tokens

    def _refill(self) -> None:
        """Refill tokens based on elapsed time. Must be called with lock held."""
        now = time.monotonic()
        elapsed = now - self._last_refill

        if elapsed > 0 and self._refill_rate > 0:
            tokens_to_add = elapsed * self._refill_rate
            self._tokens = min(self._capacity, self._tokens + tokens_to_add)
            self._last_refill = now

    def consume(self, tokens: float = 1.0) -> bool:
        """
        Attempt to consume tokens from the bucket.

        Args:
            tokens: Number of tokens to consume (default: 1)

        Returns:
            True if tokens were consumed, False if not enough tokens
        """
        if tokens <= 0:
            raise ValueError("Tokens to consume must be positive")

        with self._lock:
            self._refill()

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True
            return False

    def try_consume(self, tokens: float = 1.0) -> tuple[bool, float, float]:
        """
        Try to consume tokens and return detailed state.

        Args:
            tokens: Number of tokens to consume (default: 1)

        Returns:
            Tuple of (success, remaining_tokens, seconds_until_tokens_available)
        """
        if tokens <= 0:
            raise ValueError("Tokens to consume must be positive")

        with self._lock:
            self._refill()

            if self._tokens >= tokens:
                self._tokens -= tokens
                return True, self._tokens, 0.0

            # Calculate time until enough tokens are available
            tokens_needed = tokens - self._tokens
            if self._refill_rate > 0:
                wait_time = tokens_needed / self._refill_rate
            else:
                wait_time = float("inf")

            return False, self._tokens, wait_time

    def check(self, tokens: float = 1.0) -> tuple[bool, float, float]:
        """
        Check if tokens are available without consuming.

        Args:
            tokens: Number of tokens to check for (default: 1)

        Returns:
            Tuple of (would_succeed, current_tokens, seconds_until_tokens_available)
        """
        if tokens <= 0:
            raise ValueError("Tokens to check must be positive")

        with self._lock:
            self._refill()

            if self._tokens >= tokens:
                return True, self._tokens, 0.0

            tokens_needed = tokens - self._tokens
            if self._refill_rate > 0:
                wait_time = tokens_needed / self._refill_rate
            else:
                wait_time = float("inf")

            return False, self._tokens, wait_time

    def get_remaining(self) -> float:
        """
        Get the current number of available tokens.

        Returns:
            Number of tokens currently available
        """
        with self._lock:
            self._refill()
            return self._tokens

    def reset(self) -> None:
        """Reset the bucket to full capacity."""
        with self._lock:
            self._tokens = self._capacity
            self._last_refill = time.monotonic()

    def get_state(self) -> BucketState:
        """
        Get the current state of the bucket.

        Returns:
            BucketState with current tokens and last refill time
        """
        with self._lock:
            self._refill()
            return BucketState(tokens=self._tokens, last_refill=self._last_refill)


class TokenBucketFactory:
    """Factory for creating token buckets with common configurations."""

    @staticmethod
    def create_rpm_bucket(rpm: int, burst_multiplier: float = 1.0) -> TokenBucket:
        """
        Create a token bucket for requests per minute.

        Args:
            rpm: Maximum requests per minute
            burst_multiplier: Multiplier for burst capacity (default: 1.0)

        Returns:
            Configured TokenBucket
        """
        # RPM to requests per second
        refill_rate = rpm / 60.0
        # Burst capacity (allow some bursting)
        capacity = rpm * burst_multiplier / 60.0 * 10  # Allow 10 second burst
        return TokenBucket(capacity=max(1, capacity), refill_rate=refill_rate)

    @staticmethod
    def create_tpm_bucket(tpm: int, burst_multiplier: float = 1.0) -> TokenBucket:
        """
        Create a token bucket for tokens per minute.

        Args:
            tpm: Maximum tokens per minute
            burst_multiplier: Multiplier for burst capacity (default: 1.0)

        Returns:
            Configured TokenBucket
        """
        # TPM to tokens per second
        refill_rate = tpm / 60.0
        # Burst capacity
        capacity = tpm * burst_multiplier / 60.0 * 10  # Allow 10 second burst
        return TokenBucket(capacity=max(1, capacity), refill_rate=refill_rate)
