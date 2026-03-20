"""
Rate limiting configuration.

This module provides configuration settings for the rate limiting system.
"""

from pydantic import Field
from pydantic_settings import BaseSettings


class RateLimitConfig(BaseSettings):
    """Configuration settings for rate limiting."""

    # Default rate limits (applied when no specific limit is set)
    default_rpm: int = Field(default=60, ge=1, description="Default requests per minute")
    default_tpm: int = Field(default=100000, ge=1, description="Default tokens per minute")
    default_concurrent: int = Field(default=10, ge=1, description="Default concurrent requests")

    # Service account default limits (typically higher)
    service_account_default_rpm: int = Field(default=120, ge=1, description="Default RPM for service accounts")
    service_account_default_tpm: int = Field(default=200000, ge=1, description="Default TPM for service accounts")
    service_account_default_concurrent: int = Field(default=20, ge=1, description="Default concurrent for service accounts")

    # Token bucket configuration
    burst_multiplier: float = Field(default=1.5, ge=1.0, description="Burst capacity multiplier")
    refill_buffer_seconds: int = Field(default=10, ge=1, description="Seconds of tokens to allow as burst")

    # Backend configuration
    backend_type: str = Field(default="memory", description="Backend type: 'memory' or 'redis'")
    redis_url: str | None = Field(default=None, description="Redis URL for distributed rate limiting")
    redis_key_prefix: str = Field(default="bedrockgw:ratelimit", description="Redis key prefix")
    redis_key_ttl: int = Field(default=3600, ge=60, description="Redis key TTL in seconds")

    # Cleanup configuration (for in-memory backend)
    cleanup_interval_seconds: int = Field(default=60, ge=10, description="Cleanup interval for expired entries")
    entry_ttl_seconds: int = Field(default=3600, ge=60, description="TTL for inactive entries")

    # Hierarchy configuration
    enforce_hierarchy: bool = Field(default=True, description="Enforce hierarchical rate limits")

    model_config = {
        "env_prefix": "RATELIMIT_",
        "env_file": ".env",
        "extra": "ignore",
    }


# Global configuration instance
_config: RateLimitConfig | None = None


def get_ratelimit_config() -> RateLimitConfig:
    """Get the global rate limit configuration."""
    global _config
    if _config is None:
        _config = RateLimitConfig()
    return _config


def set_ratelimit_config(config: RateLimitConfig) -> None:
    """Set the global rate limit configuration (for testing)."""
    global _config
    _config = config
