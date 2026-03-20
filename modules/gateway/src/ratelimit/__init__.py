"""
Rate limiting module for BedrockGateway.

This module provides rate limiting functionality including:
- Token bucket algorithm for RPM/TPM limits
- Concurrent request tracking
- Hierarchical enforcement (org > department > team > user/service account)
- In-memory and Redis backends
- FastAPI middleware and routes
"""

from .backend import RateLimitBackend
from .backends.in_memory import InMemoryBackend
from .backends.redis import RedisBackend
from .config import RateLimitConfig, get_ratelimit_config, set_ratelimit_config
from .exceptions import BackendConnectionError, RateLimitConfigError, RateLimitError, RateLimitExceededError, TooManyConcurrentRequestsError
from .middleware import RateLimitMiddleware, create_rate_limit_middleware
from .models import (
    ConcurrentRequestInfo,
    EntityType,
    LimitType,
    RateLimitConfigRequest,
    RateLimitConfigResponse,
    RateLimitResult,
    RateLimitState,
    RateLimitStatusResponse,
)
from .models import (
    RateLimitConfig as RateLimitConfigModel,
)
from .routes import router
from .service import RateLimitService
from .token_bucket import TokenBucket, TokenBucketFactory

__all__ = [
    # Backend interface and implementations
    "RateLimitBackend",
    "InMemoryBackend",
    "RedisBackend",
    # Configuration
    "RateLimitConfig",
    "get_ratelimit_config",
    "set_ratelimit_config",
    # Service
    "RateLimitService",
    # Middleware
    "RateLimitMiddleware",
    "create_rate_limit_middleware",
    # Routes
    "router",
    # Models
    "EntityType",
    "LimitType",
    "RateLimitConfigModel",
    "RateLimitConfigRequest",
    "RateLimitConfigResponse",
    "RateLimitResult",
    "RateLimitState",
    "RateLimitStatusResponse",
    "ConcurrentRequestInfo",
    # Token bucket
    "TokenBucket",
    "TokenBucketFactory",
    # Exceptions
    "RateLimitError",
    "RateLimitExceededError",
    "TooManyConcurrentRequestsError",
    "RateLimitConfigError",
    "BackendConnectionError",
]
