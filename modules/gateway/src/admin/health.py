"""Health and readiness check endpoints."""

import time
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.schemas import HealthCheckComponent, HealthCheckResponse, ReadinessCheckResponse
from src.shared.database import get_db

router = APIRouter(tags=["health"])


class HealthChecker:
    """
    Health checker service for liveness and readiness probes.

    Checks:
    - Basic liveness (app is running)
    - Database connectivity
    - Redis connectivity (if configured)
    - External service dependencies
    """

    def __init__(self, db: AsyncSession | None = None):
        """
        Initialize health checker.

        Args:
            db: Optional database session for connectivity checks
        """
        self.db = db

    async def check_liveness(self) -> HealthCheckResponse:
        """
        Basic liveness check.

        Returns:
            Health check response indicating app is alive
        """
        return HealthCheckResponse(
            status="healthy",
            timestamp=datetime.now(UTC),
        )

    async def check_readiness(self) -> ReadinessCheckResponse:
        """
        Full readiness check including all dependencies.

        Returns:
            Readiness check response with component statuses
        """
        components: list[HealthCheckComponent] = []
        all_healthy = True

        # Check database
        db_status = await self._check_database()
        components.append(db_status)
        if db_status.status != "healthy":
            all_healthy = False

        # Check Redis (if available)
        redis_status = await self._check_redis()
        if redis_status:
            components.append(redis_status)
            if redis_status.status != "healthy":
                all_healthy = False

        return ReadinessCheckResponse(
            status="ready" if all_healthy else "not_ready",
            timestamp=datetime.now(UTC),
            components=components,
            all_healthy=all_healthy,
        )

    async def _check_database(self) -> HealthCheckComponent:
        """
        Check database connectivity.

        Returns:
            Health check component for database
        """
        if not self.db:
            return HealthCheckComponent(
                name="database",
                status="unhealthy",
                error="Database session not available",
            )

        start_time = time.time()
        try:
            # Execute a simple query to test connectivity
            await self.db.execute(text("SELECT 1"))
            latency_ms = int((time.time() - start_time) * 1000)
            return HealthCheckComponent(
                name="database",
                status="healthy",
                latency_ms=latency_ms,
            )
        except Exception as e:
            return HealthCheckComponent(
                name="database",
                status="unhealthy",
                error=str(e),
            )

    async def _check_redis(self) -> HealthCheckComponent | None:
        """
        Check Redis connectivity.

        Returns:
            Health check component for Redis, or None if not configured
        """
        from src.shared.config import get_settings

        settings = get_settings()
        if not settings.redis_url:
            return None

        start_time = time.time()
        try:
            import redis.asyncio as redis

            client = redis.from_url(settings.redis_url)
            await client.ping()
            await client.close()
            latency_ms = int((time.time() - start_time) * 1000)
            return HealthCheckComponent(
                name="redis",
                status="healthy",
                latency_ms=latency_ms,
            )
        except Exception as e:
            return HealthCheckComponent(
                name="redis",
                status="unhealthy",
                error=str(e),
            )


async def get_health_checker(db: AsyncSession = Depends(get_db)) -> HealthChecker:
    """Get health checker instance."""
    return HealthChecker(db)


@router.get("/health", response_model=HealthCheckResponse)
async def health_check() -> HealthCheckResponse:
    """
    Basic health/liveness check.

    Returns 200 if the service is alive.
    Does not check external dependencies.
    """
    checker = HealthChecker()
    return await checker.check_liveness()


@router.get("/ready", response_model=ReadinessCheckResponse)
async def readiness_check(
    checker: HealthChecker = Depends(get_health_checker),
) -> ReadinessCheckResponse:
    """
    Full readiness check.

    Checks all dependencies (database, Redis, etc.).
    Returns 200 only if all dependencies are healthy.
    """
    result = await checker.check_readiness()
    return result


@router.get("/health/detailed", response_model=ReadinessCheckResponse)
async def detailed_health_check(
    checker: HealthChecker = Depends(get_health_checker),
) -> ReadinessCheckResponse:
    """
    Detailed health check with component breakdown.

    Same as readiness check but exposed at /health/detailed
    for more detailed monitoring.
    """
    return await checker.check_readiness()
