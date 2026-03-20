"""
Abstract backend interface for rate limiting.

This module defines the abstract base class for rate limit backends,
allowing different implementations (in-memory, Redis, etc.).
"""

from abc import ABC, abstractmethod

from .models import LimitType, RateLimitState


class RateLimitBackend(ABC):
    """Abstract base class for rate limit backends."""

    @abstractmethod
    async def check_limit(
        self,
        key: str,
        limit_type: LimitType,
        max_tokens: int,
        refill_rate: float,
    ) -> tuple[bool, int, int]:
        """
        Check if a rate limit would be exceeded without consuming tokens.

        Args:
            key: Unique identifier for the rate limit bucket
            limit_type: Type of rate limit (RPM, TPM, concurrent)
            max_tokens: Maximum tokens (capacity) for the bucket
            refill_rate: Tokens refilled per second

        Returns:
            Tuple of (allowed, remaining_tokens, retry_after_seconds)
        """
        ...

    @abstractmethod
    async def consume(
        self,
        key: str,
        limit_type: LimitType,
        max_tokens: int,
        refill_rate: float,
        tokens_to_consume: int = 1,
    ) -> tuple[bool, int, int]:
        """
        Attempt to consume tokens from a rate limit bucket.

        Args:
            key: Unique identifier for the rate limit bucket
            limit_type: Type of rate limit (RPM, TPM, concurrent)
            max_tokens: Maximum tokens (capacity) for the bucket
            refill_rate: Tokens refilled per second
            tokens_to_consume: Number of tokens to consume (default: 1)

        Returns:
            Tuple of (success, remaining_tokens, retry_after_seconds)
        """
        ...

    @abstractmethod
    async def get_remaining(
        self,
        key: str,
        limit_type: LimitType,
        max_tokens: int,
        refill_rate: float,
    ) -> int:
        """
        Get the number of remaining tokens in a bucket.

        Args:
            key: Unique identifier for the rate limit bucket
            limit_type: Type of rate limit
            max_tokens: Maximum tokens (capacity) for the bucket
            refill_rate: Tokens refilled per second

        Returns:
            Number of remaining tokens
        """
        ...

    @abstractmethod
    async def get_state(
        self,
        key: str,
        limit_type: LimitType,
    ) -> RateLimitState | None:
        """
        Get the current state of a rate limit bucket.

        Args:
            key: Unique identifier for the rate limit bucket
            limit_type: Type of rate limit

        Returns:
            Current state or None if not exists
        """
        ...

    @abstractmethod
    async def reset(self, key: str, limit_type: LimitType) -> None:
        """
        Reset a rate limit bucket to full capacity.

        Args:
            key: Unique identifier for the rate limit bucket
            limit_type: Type of rate limit
        """
        ...

    @abstractmethod
    async def increment_concurrent(self, key: str) -> tuple[bool, int, int]:
        """
        Increment concurrent request counter.

        Args:
            key: Unique identifier for the concurrent request counter

        Returns:
            Tuple of (success, current_count, limit)
        """
        ...

    @abstractmethod
    async def decrement_concurrent(self, key: str) -> int:
        """
        Decrement concurrent request counter.

        Args:
            key: Unique identifier for the concurrent request counter

        Returns:
            Current count after decrement
        """
        ...

    @abstractmethod
    async def get_concurrent_count(self, key: str) -> int:
        """
        Get current concurrent request count.

        Args:
            key: Unique identifier for the concurrent request counter

        Returns:
            Current count
        """
        ...

    @abstractmethod
    async def set_concurrent_limit(self, key: str, limit: int) -> None:
        """
        Set the concurrent request limit for a key.

        Args:
            key: Unique identifier for the concurrent request counter
            limit: Maximum concurrent requests allowed
        """
        ...

    @abstractmethod
    async def close(self) -> None:
        """Close and cleanup backend resources."""
        ...
