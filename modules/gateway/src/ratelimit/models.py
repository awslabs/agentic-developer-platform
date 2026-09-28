"""
Pydantic models for rate limiting.

This module defines the data models used throughout the rate limiting system.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class LimitType(str, Enum):
    """Types of rate limits that can be enforced."""

    RPM = "rpm"  # Requests per minute
    TPM = "tpm"  # Tokens per minute
    CONCURRENT = "concurrent"  # Concurrent requests


class EntityType(str, Enum):
    """Entity types for rate limit hierarchy."""

    ORGANIZATION = "organization"
    DEPARTMENT = "department"
    TEAM = "team"
    USER = "user"
    SERVICE_ACCOUNT = "service_account"


class RateLimitConfig(BaseModel):
    """Configuration for a rate limit."""

    entity_type: EntityType
    entity_id: str
    org_id: str
    rpm: int | None = Field(default=None, ge=1, description="Requests per minute limit")
    tpm: int | None = Field(default=None, ge=1, description="Tokens per minute limit")
    concurrent_requests: int | None = Field(default=None, ge=1, description="Max concurrent requests")
    burst_size: int | None = Field(default=None, ge=1, description="Token bucket burst size")

    model_config = {"from_attributes": True}


class RateLimitState(BaseModel):
    """Current state of a rate limit bucket."""

    entity_type: EntityType
    entity_id: str
    limit_type: LimitType
    tokens: float = Field(ge=0, description="Current tokens in bucket")
    max_tokens: float = Field(ge=1, description="Maximum tokens (capacity)")
    refill_rate: float = Field(ge=0, description="Tokens added per second")
    last_refill: datetime = Field(default_factory=datetime.utcnow, description="Last refill timestamp")


class RateLimitResult(BaseModel):
    """Result of a rate limit check."""

    allowed: bool
    limit_type: LimitType | None = None
    limit: int | None = None
    remaining: int | None = None
    retry_after_seconds: int | None = None
    exceeded_entity_type: EntityType | None = None
    exceeded_entity_id: str | None = None


class RateLimitConfigRequest(BaseModel):
    """Request to configure rate limits for an entity."""

    rpm: int | None = Field(default=None, ge=1, description="Requests per minute limit")
    tpm: int | None = Field(default=None, ge=1, description="Tokens per minute limit")
    concurrent_requests: int | None = Field(default=None, ge=1, description="Max concurrent requests")
    burst_size: int | None = Field(default=None, ge=1, description="Token bucket burst size")


class RateLimitConfigResponse(BaseModel):
    """Response for rate limit configuration."""

    entity_type: str
    entity_id: str
    org_id: str
    rpm: int | None = None
    tpm: int | None = None
    concurrent_requests: int | None = None
    burst_size: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RateLimitStatusResponse(BaseModel):
    """Response for rate limit status."""

    entity_type: str
    entity_id: str
    rpm_limit: int | None = None
    rpm_remaining: int | None = None
    rpm_reset_seconds: int | None = None
    tpm_limit: int | None = None
    tpm_remaining: int | None = None
    tpm_reset_seconds: int | None = None
    concurrent_limit: int | None = None
    concurrent_used: int | None = None


class ConcurrentRequestInfo(BaseModel):
    """Information about a concurrent request."""

    request_id: str
    entity_id: str
    entity_type: EntityType
    started_at: datetime = Field(default_factory=datetime.utcnow)
