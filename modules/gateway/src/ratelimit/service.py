"""
Rate limiting service implementation.

This module implements the IRateLimitService interface and provides
hierarchical rate limit enforcement.
"""

import time
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import select

from src.shared.interfaces.ratelimit import IRateLimitService
from src.shared.logging import get_logger
from src.shared.metrics import emit_rate_limit_remaining
from src.shared.schemas.auth import TokenContext
from src.shared.schemas.common import RateLimitCheckResult

from .backend import RateLimitBackend
from .backends.in_memory import InMemoryBackend
from .backends.redis import RedisBackend
from .config import RateLimitConfig, get_ratelimit_config
from .models import EntityType, LimitType, RateLimitConfigRequest, RateLimitConfigResponse, RateLimitStatusResponse

logger = get_logger(__name__)

# How often to reload rate limit configs from the database (seconds)
_DB_RELOAD_INTERVAL = 60


class RateLimitService(IRateLimitService):
    """
    Rate limiting service implementing IRateLimitService.

    This service provides:
    - Token bucket rate limiting for RPM and TPM
    - Concurrent request tracking
    - Hierarchical enforcement (org > department > team > user/service account)
    - Support for both in-memory and Redis backends
    """

    def __init__(
        self,
        backend: RateLimitBackend | None = None,
        config: RateLimitConfig | None = None,
    ) -> None:
        """
        Initialize the rate limit service.

        Args:
            backend: Rate limit backend (defaults to config-based selection)
            config: Rate limit configuration (defaults to global config)
        """
        self._config = config or get_ratelimit_config()
        self._backend = backend or self._create_backend()
        self._rate_limits: dict[str, dict[str, Any]] = {}  # entity_key -> limits
        self._last_db_load: float = 0  # monotonic timestamp of last DB load

    def _create_backend(self) -> RateLimitBackend:
        """Create the appropriate backend based on configuration."""
        if self._config.backend_type == "redis" and self._config.redis_url:
            return RedisBackend(
                redis_url=self._config.redis_url,
                key_prefix=self._config.redis_key_prefix,
                default_ttl=self._config.redis_key_ttl,
            )
        return InMemoryBackend(
            cleanup_interval=self._config.cleanup_interval_seconds,
            entry_ttl=self._config.entry_ttl_seconds,
        )

    @asynccontextmanager
    async def _get_session(self):
        """Get a database session, handling IAM auth token refresh."""
        from src.shared.config import get_settings
        from src.shared.database import get_session_factory, reset_engine

        settings = get_settings()
        if settings.rds_iam_auth and settings.rds_host:
            reset_engine()
        factory = get_session_factory()
        async with factory() as session:
            yield session

    async def _load_from_db(self) -> None:
        """Load rate limit configs from the database into memory.

        Called lazily on first request and refreshed every _DB_RELOAD_INTERVAL seconds.
        """
        now = time.monotonic()
        if now - self._last_db_load < _DB_RELOAD_INTERVAL:
            return

        try:
            from src.shared.models.usage import RateLimitConfig as RateLimitConfigModel

            async with self._get_session() as session:
                result = await session.execute(select(RateLimitConfigModel))
                rows = result.scalars().all()

            new_limits: dict[str, dict[str, Any]] = {}
            for row in rows:
                key = f"{row.entity_type}:{row.entity_id}:{row.org_id}"
                limits: dict[str, Any] = {}
                if row.rpm is not None:
                    limits["rpm"] = row.rpm
                if row.tpm is not None:
                    limits["tpm"] = row.tpm
                if row.concurrent_requests is not None:
                    limits["concurrent_requests"] = row.concurrent_requests
                new_limits[key] = limits

            self._rate_limits = new_limits
            self._last_db_load = now
            logger.info(f"Loaded {len(new_limits)} rate limit configs from database")

        except Exception as e:
            logger.error(f"Failed to load rate limit configs from DB: {e}", exc_info=True)
            # Keep existing in-memory configs on failure
            self._last_db_load = now  # Don't retry immediately

    def _get_entity_key(self, entity_type: EntityType, entity_id: str, org_id: str) -> str:
        """Generate a unique key for an entity."""
        return f"{entity_type.value}:{entity_id}:{org_id}"

    def _get_limits_for_entity(self, entity_type: EntityType, entity_id: str, org_id: str, is_service_account: bool = False) -> dict[str, int | None]:
        """Get rate limits for an entity, with defaults."""
        key = self._get_entity_key(entity_type, entity_id, org_id)
        limits = self._rate_limits.get(key, {})

        # Apply defaults based on account type
        if is_service_account:
            return {
                "rpm": limits.get("rpm", self._config.service_account_default_rpm),
                "tpm": limits.get("tpm", self._config.service_account_default_tpm),
                "concurrent": limits.get("concurrent_requests", self._config.service_account_default_concurrent),
            }
        return {
            "rpm": limits.get("rpm", self._config.default_rpm),
            "tpm": limits.get("tpm", self._config.default_tpm),
            "concurrent": limits.get("concurrent_requests", self._config.default_concurrent),
        }

    def _get_hierarchy_entities(self, context: TokenContext) -> list[tuple[EntityType, str]]:
        """
        Get the hierarchy of entities to check for rate limits.

        Order: user/service_account -> team -> department -> org
        The most restrictive limit across all levels applies.
        """
        entities = []

        # User or service account level
        if context.account_type == "service":
            entities.append((EntityType.SERVICE_ACCOUNT, context.user_id))
        else:
            entities.append((EntityType.USER, context.user_id))

        # Team level
        if context.team_id:
            entities.append((EntityType.TEAM, context.team_id))

        # Department level
        if context.department_id:
            entities.append((EntityType.DEPARTMENT, context.department_id))

        # Organization level
        if context.org_id:
            entities.append((EntityType.ORGANIZATION, context.org_id))

        return entities

    async def check_rate_limit(self, context: TokenContext) -> RateLimitCheckResult:
        """
        Check if a request is allowed under current rate limits.

        Implements hierarchical enforcement: checks all levels and
        applies the most restrictive limit.
        """
        # Ensure configs are loaded from DB
        await self._load_from_db()
        is_service_account = context.account_type == "service"
        entities = self._get_hierarchy_entities(context)

        # Check each level if hierarchy enforcement is enabled
        if self._config.enforce_hierarchy:
            for entity_type, entity_id in entities:
                result = await self._check_entity_limits(entity_type, entity_id, context.org_id, is_service_account)
                if not result.allowed:
                    return result
        else:
            # Only check the user/service account level
            entity_type, entity_id = entities[0]
            result = await self._check_entity_limits(entity_type, entity_id, context.org_id, is_service_account)
            if not result.allowed:
                return result

        return RateLimitCheckResult(allowed=True)

    async def _check_entity_limits(
        self,
        entity_type: EntityType,
        entity_id: str,
        org_id: str,
        is_service_account: bool,
    ) -> RateLimitCheckResult:
        """Check rate limits for a specific entity."""
        limits = self._get_limits_for_entity(entity_type, entity_id, org_id, is_service_account)
        key = self._get_entity_key(entity_type, entity_id, org_id)

        # Check RPM limit
        if limits["rpm"]:
            rpm = limits["rpm"]
            refill_rate = rpm / 60.0  # tokens per second
            max_tokens = int(rpm * self._config.burst_multiplier / 60.0 * self._config.refill_buffer_seconds)
            max_tokens = max(1, max_tokens)

            allowed, remaining, retry_after = await self._backend.check_limit(key, LimitType.RPM, max_tokens, refill_rate)
            if not allowed:
                return RateLimitCheckResult(
                    allowed=False,
                    limit_type="rpm",
                    limit=rpm,
                    remaining=remaining,
                    retry_after_seconds=retry_after,
                )

        # Check concurrent request limit
        if limits["concurrent"]:
            concurrent_limit = limits["concurrent"]
            await self._backend.set_concurrent_limit(key, concurrent_limit)

            current = await self._backend.get_concurrent_count(key)
            if current >= concurrent_limit:
                return RateLimitCheckResult(
                    allowed=False,
                    limit_type="concurrent",
                    limit=concurrent_limit,
                    remaining=0,
                    retry_after_seconds=5,
                )

        return RateLimitCheckResult(allowed=True, remaining=None)

    async def consume_rate_limit(
        self,
        context: TokenContext,
        tokens: int = 1,
    ) -> RateLimitCheckResult:
        """
        Consume rate limit tokens for a request.

        This should be called when a request is being processed.
        """
        is_service_account = context.account_type == "service"
        entities = self._get_hierarchy_entities(context)

        # Consume from each level if hierarchy enforcement is enabled
        levels_to_consume = entities if self._config.enforce_hierarchy else [entities[0]]

        for entity_type, entity_id in levels_to_consume:
            limits = self._get_limits_for_entity(entity_type, entity_id, context.org_id, is_service_account)
            key = self._get_entity_key(entity_type, entity_id, context.org_id)

            # Consume RPM token
            if limits["rpm"]:
                rpm = limits["rpm"]
                refill_rate = rpm / 60.0
                max_tokens = int(rpm * self._config.burst_multiplier / 60.0 * self._config.refill_buffer_seconds)
                max_tokens = max(1, max_tokens)

                success, remaining, retry_after = await self._backend.consume(key, LimitType.RPM, max_tokens, refill_rate, 1)
                if not success:
                    return RateLimitCheckResult(
                        allowed=False,
                        limit_type="rpm",
                        limit=rpm,
                        remaining=remaining,
                        retry_after_seconds=retry_after,
                    )

            # Consume TPM tokens
            if limits["tpm"] and tokens > 0:
                tpm = limits["tpm"]
                refill_rate = tpm / 60.0
                max_tokens_bucket = int(tpm * self._config.burst_multiplier / 60.0 * self._config.refill_buffer_seconds)
                max_tokens_bucket = max(tokens, max_tokens_bucket)

                success, remaining, retry_after = await self._backend.consume(key, LimitType.TPM, max_tokens_bucket, refill_rate, tokens)
                if not success:
                    return RateLimitCheckResult(
                        allowed=False,
                        limit_type="tpm",
                        limit=tpm,
                        remaining=remaining,
                        retry_after_seconds=retry_after,
                    )

            # Increment concurrent count
            if limits["concurrent"]:
                await self._backend.set_concurrent_limit(key, limits["concurrent"])
                success, current, limit = await self._backend.increment_concurrent(key)
                if not success:
                    return RateLimitCheckResult(
                        allowed=False,
                        limit_type="concurrent",
                        limit=limit,
                        remaining=0,
                        retry_after_seconds=5,
                    )

        return RateLimitCheckResult(allowed=True)

    async def release_concurrent(self, context: TokenContext) -> None:
        """Release a concurrent request slot after request completes."""
        entities = self._get_hierarchy_entities(context)

        # Release from each level if hierarchy enforcement is enabled
        levels_to_release = entities if self._config.enforce_hierarchy else [entities[0]]

        for entity_type, entity_id in levels_to_release:
            key = self._get_entity_key(entity_type, entity_id, context.org_id)
            await self._backend.decrement_concurrent(key)

    async def configure_limits(
        self,
        entity_type: EntityType,
        entity_id: str,
        org_id: str,
        config: RateLimitConfigRequest,
    ) -> RateLimitConfigResponse:
        """Configure rate limits for an entity."""
        key = self._get_entity_key(entity_type, entity_id, org_id)

        limits = self._rate_limits.get(key, {})
        if config.rpm is not None:
            limits["rpm"] = config.rpm
        if config.tpm is not None:
            limits["tpm"] = config.tpm
        if config.concurrent_requests is not None:
            limits["concurrent_requests"] = config.concurrent_requests
        if config.burst_size is not None:
            limits["burst_size"] = config.burst_size

        self._rate_limits[key] = limits

        logger.info(f"Configured rate limits for {entity_type.value} {entity_id}: {limits}")

        return RateLimitConfigResponse(
            entity_type=entity_type.value,
            entity_id=entity_id,
            org_id=org_id,
            rpm=limits.get("rpm"),
            tpm=limits.get("tpm"),
            concurrent_requests=limits.get("concurrent_requests"),
            burst_size=limits.get("burst_size"),
        )

    async def get_limits(
        self,
        entity_type: EntityType,
        entity_id: str,
        org_id: str,
    ) -> RateLimitConfigResponse | None:
        """Get configured rate limits for an entity."""
        key = self._get_entity_key(entity_type, entity_id, org_id)
        limits = self._rate_limits.get(key)

        if not limits:
            return None

        return RateLimitConfigResponse(
            entity_type=entity_type.value,
            entity_id=entity_id,
            org_id=org_id,
            rpm=limits.get("rpm"),
            tpm=limits.get("tpm"),
            concurrent_requests=limits.get("concurrent_requests"),
            burst_size=limits.get("burst_size"),
        )

    async def get_status(
        self,
        entity_type: EntityType,
        entity_id: str,
        org_id: str,
        is_service_account: bool = False,
    ) -> RateLimitStatusResponse:
        """Get current rate limit status for an entity."""
        key = self._get_entity_key(entity_type, entity_id, org_id)
        limits = self._get_limits_for_entity(entity_type, entity_id, org_id, is_service_account)

        rpm_remaining = None
        rpm_reset = None
        tpm_remaining = None
        tpm_reset = None
        concurrent_used = None

        # Get RPM status
        if limits["rpm"]:
            rpm = limits["rpm"]
            refill_rate = rpm / 60.0
            max_tokens = int(rpm * self._config.burst_multiplier / 60.0 * self._config.refill_buffer_seconds)
            max_tokens = max(1, max_tokens)

            _, remaining, reset = await self._backend.check_limit(key, LimitType.RPM, max_tokens, refill_rate)
            rpm_remaining = remaining
            rpm_reset = reset if remaining < max_tokens else None

        # Get TPM status
        if limits["tpm"]:
            tpm = limits["tpm"]
            refill_rate = tpm / 60.0
            max_tokens = int(tpm * self._config.burst_multiplier / 60.0 * self._config.refill_buffer_seconds)
            max_tokens = max(1, max_tokens)

            _, remaining, reset = await self._backend.check_limit(key, LimitType.TPM, max_tokens, refill_rate)
            tpm_remaining = remaining
            tpm_reset = reset if remaining < max_tokens else None

        # Get concurrent status
        if limits["concurrent"]:
            concurrent_used = await self._backend.get_concurrent_count(key)

        # Emit rate limit remaining metrics
        if rpm_remaining is not None:
            emit_rate_limit_remaining(
                org_id=org_id,
                entity_type=entity_type.value,
                entity_id=entity_id,
                limit_type="rpm",
                remaining=rpm_remaining,
            )
        if tpm_remaining is not None:
            emit_rate_limit_remaining(
                org_id=org_id,
                entity_type=entity_type.value,
                entity_id=entity_id,
                limit_type="tpm",
                remaining=tpm_remaining,
            )

        logger.debug(
            "Rate limit status retrieved",
            extra={
                "entity_type": entity_type.value,
                "entity_id": entity_id,
                "rpm_remaining": rpm_remaining,
                "tpm_remaining": tpm_remaining,
            },
        )

        return RateLimitStatusResponse(
            entity_type=entity_type.value,
            entity_id=entity_id,
            rpm_limit=limits["rpm"],
            rpm_remaining=rpm_remaining,
            rpm_reset_seconds=rpm_reset,
            tpm_limit=limits["tpm"],
            tpm_remaining=tpm_remaining,
            tpm_reset_seconds=tpm_reset,
            concurrent_limit=limits["concurrent"],
            concurrent_used=concurrent_used,
        )

    async def delete_limits(
        self,
        entity_type: EntityType,
        entity_id: str,
        org_id: str,
    ) -> bool:
        """Delete configured rate limits for an entity."""
        key = self._get_entity_key(entity_type, entity_id, org_id)

        if key in self._rate_limits:
            del self._rate_limits[key]
            # Reset backend state
            await self._backend.reset(key, LimitType.RPM)
            await self._backend.reset(key, LimitType.TPM)
            return True
        return False

    async def close(self) -> None:
        """Close and cleanup resources."""
        await self._backend.close()
