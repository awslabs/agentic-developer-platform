"""Unit tests for admin health check module."""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.health import HealthChecker, get_health_checker
from src.admin.schemas import HealthCheckComponent, HealthCheckResponse, ReadinessCheckResponse


class TestHealthChecker:
    """Tests for HealthChecker class."""

    def test_init_without_db(self):
        """Test HealthChecker initialization without database session."""
        checker = HealthChecker()
        assert checker.db is None

    def test_init_with_db(self):
        """Test HealthChecker initialization with database session."""
        mock_db = MagicMock(spec=AsyncSession)
        checker = HealthChecker(db=mock_db)
        assert checker.db is mock_db


class TestLivenessCheck:
    """Tests for liveness check."""

    @pytest.mark.asyncio
    async def test_liveness_check_returns_healthy(self):
        """Test liveness check returns healthy status."""
        checker = HealthChecker()
        response = await checker.check_liveness()

        assert response.status == "healthy"
        assert response.timestamp is not None
        assert isinstance(response.timestamp, datetime)

    @pytest.mark.asyncio
    async def test_liveness_check_response_format(self):
        """Test liveness check returns correct response format."""
        checker = HealthChecker()
        response = await checker.check_liveness()

        assert isinstance(response, HealthCheckResponse)
        assert hasattr(response, "status")
        assert hasattr(response, "timestamp")

    @pytest.mark.asyncio
    async def test_liveness_check_timestamp_is_utc(self):
        """Test liveness check timestamp is UTC."""
        checker = HealthChecker()
        before = datetime.now(UTC)
        response = await checker.check_liveness()
        after = datetime.now(UTC)

        assert response.timestamp >= before
        assert response.timestamp <= after


class TestReadinessCheck:
    """Tests for readiness check."""

    @pytest.mark.asyncio
    async def test_readiness_check_without_db(self):
        """Test readiness check without database returns unhealthy database."""
        checker = HealthChecker()
        response = await checker.check_readiness()

        assert response.status == "not_ready"
        assert response.all_healthy is False
        assert len(response.components) >= 1

        # Find database component
        db_component = next((c for c in response.components if c.name == "database"), None)
        assert db_component is not None
        assert db_component.status == "unhealthy"
        assert db_component.error == "Database session not available"

    @pytest.mark.asyncio
    async def test_readiness_check_with_healthy_db(self):
        """Test readiness check with healthy database."""
        mock_db = MagicMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(return_value=MagicMock())

        checker = HealthChecker(db=mock_db)

        with patch("src.shared.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url=None)
            response = await checker.check_readiness()

        assert response.status == "ready"
        assert response.all_healthy is True

        db_component = next((c for c in response.components if c.name == "database"), None)
        assert db_component is not None
        assert db_component.status == "healthy"
        assert db_component.latency_ms is not None
        assert db_component.latency_ms >= 0

    @pytest.mark.asyncio
    async def test_readiness_check_with_unhealthy_db(self):
        """Test readiness check with failing database."""
        mock_db = MagicMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(side_effect=Exception("Connection refused"))

        checker = HealthChecker(db=mock_db)

        with patch("src.shared.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url=None)
            response = await checker.check_readiness()

        assert response.status == "not_ready"
        assert response.all_healthy is False

        db_component = next((c for c in response.components if c.name == "database"), None)
        assert db_component is not None
        assert db_component.status == "unhealthy"
        assert "Connection refused" in db_component.error

    @pytest.mark.asyncio
    async def test_readiness_check_response_format(self):
        """Test readiness check returns correct response format."""
        checker = HealthChecker()
        response = await checker.check_readiness()

        assert isinstance(response, ReadinessCheckResponse)
        assert hasattr(response, "status")
        assert hasattr(response, "timestamp")
        assert hasattr(response, "components")
        assert hasattr(response, "all_healthy")


class TestDatabaseCheck:
    """Tests for database connectivity check."""

    @pytest.mark.asyncio
    async def test_check_database_no_session(self):
        """Test database check without session returns unhealthy."""
        checker = HealthChecker()
        result = await checker._check_database()

        assert result.name == "database"
        assert result.status == "unhealthy"
        assert result.error == "Database session not available"

    @pytest.mark.asyncio
    async def test_check_database_success(self):
        """Test database check with successful connection."""
        mock_db = MagicMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(return_value=MagicMock())

        checker = HealthChecker(db=mock_db)
        result = await checker._check_database()

        assert result.name == "database"
        assert result.status == "healthy"
        assert result.latency_ms is not None
        assert result.latency_ms >= 0
        assert result.error is None

        # Verify execute was called with SELECT 1
        mock_db.execute.assert_called_once()
        call_args = mock_db.execute.call_args[0][0]
        assert "SELECT 1" in str(call_args)

    @pytest.mark.asyncio
    async def test_check_database_failure(self):
        """Test database check with failed connection."""
        mock_db = MagicMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(side_effect=Exception("Database timeout"))

        checker = HealthChecker(db=mock_db)
        result = await checker._check_database()

        assert result.name == "database"
        assert result.status == "unhealthy"
        assert "Database timeout" in result.error

    @pytest.mark.asyncio
    async def test_check_database_returns_component(self):
        """Test database check returns HealthCheckComponent."""
        checker = HealthChecker()
        result = await checker._check_database()

        assert isinstance(result, HealthCheckComponent)


class TestRedisCheck:
    """Tests for Redis connectivity check."""

    @pytest.mark.asyncio
    async def test_check_redis_not_configured(self):
        """Test Redis check when not configured returns None."""
        checker = HealthChecker()

        with patch("src.shared.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url=None)
            result = await checker._check_redis()

        assert result is None

    @pytest.mark.asyncio
    async def test_check_redis_healthy(self):
        """Test Redis check with healthy connection."""
        checker = HealthChecker()

        mock_redis_client = MagicMock()
        mock_redis_client.ping = AsyncMock(return_value=True)
        mock_redis_client.close = AsyncMock()

        with patch("src.shared.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url="redis://localhost:6379")
            with patch("redis.asyncio.from_url", return_value=mock_redis_client):
                result = await checker._check_redis()

        assert result is not None
        assert result.name == "redis"
        assert result.status == "healthy"
        assert result.latency_ms is not None
        assert result.latency_ms >= 0
        mock_redis_client.ping.assert_called_once()
        mock_redis_client.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_redis_unhealthy(self):
        """Test Redis check with failed connection."""
        checker = HealthChecker()

        with patch("src.shared.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url="redis://localhost:6379")
            with patch("redis.asyncio.from_url", side_effect=Exception("Connection refused")):
                result = await checker._check_redis()

        assert result is not None
        assert result.name == "redis"
        assert result.status == "unhealthy"
        assert "Connection refused" in result.error

    @pytest.mark.asyncio
    async def test_readiness_with_redis_configured_healthy(self):
        """Test readiness check includes healthy Redis when configured."""
        mock_db = MagicMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(return_value=MagicMock())

        mock_redis_client = MagicMock()
        mock_redis_client.ping = AsyncMock(return_value=True)
        mock_redis_client.close = AsyncMock()

        checker = HealthChecker(db=mock_db)

        with patch("src.shared.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url="redis://localhost:6379")
            with patch("redis.asyncio.from_url", return_value=mock_redis_client):
                response = await checker.check_readiness()

        assert response.status == "ready"
        assert response.all_healthy is True

        # Should have both database and redis components
        component_names = [c.name for c in response.components]
        assert "database" in component_names
        assert "redis" in component_names

    @pytest.mark.asyncio
    async def test_readiness_with_redis_configured_unhealthy(self):
        """Test readiness check with unhealthy Redis returns not_ready."""
        mock_db = MagicMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(return_value=MagicMock())

        checker = HealthChecker(db=mock_db)

        with patch("src.shared.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url="redis://localhost:6379")
            with patch("redis.asyncio.from_url", side_effect=Exception("Connection refused")):
                response = await checker.check_readiness()

        assert response.status == "not_ready"
        assert response.all_healthy is False

        redis_component = next((c for c in response.components if c.name == "redis"), None)
        assert redis_component is not None
        assert redis_component.status == "unhealthy"


class TestHealthCheckComponent:
    """Tests for HealthCheckComponent response format."""

    def test_component_healthy_with_latency(self):
        """Test healthy component with latency."""
        component = HealthCheckComponent(
            name="test",
            status="healthy",
            latency_ms=5,
        )

        assert component.name == "test"
        assert component.status == "healthy"
        assert component.latency_ms == 5
        assert component.error is None

    def test_component_unhealthy_with_error(self):
        """Test unhealthy component with error."""
        component = HealthCheckComponent(
            name="test",
            status="unhealthy",
            error="Connection failed",
        )

        assert component.name == "test"
        assert component.status == "unhealthy"
        assert component.error == "Connection failed"
        assert component.latency_ms is None


class TestGetHealthChecker:
    """Tests for get_health_checker dependency."""

    @pytest.mark.asyncio
    async def test_get_health_checker_with_db(self):
        """Test get_health_checker creates checker with db session."""
        mock_db = MagicMock(spec=AsyncSession)
        checker = await get_health_checker(db=mock_db)

        assert isinstance(checker, HealthChecker)
        assert checker.db is mock_db


class TestIntegrationWithRealDatabase:
    """Integration tests with real database session."""

    @pytest.mark.asyncio
    async def test_liveness_check_always_succeeds(self, db_session):
        """Test liveness check always succeeds regardless of db state."""
        checker = HealthChecker(db=db_session)
        response = await checker.check_liveness()

        assert response.status == "healthy"

    @pytest.mark.asyncio
    async def test_readiness_with_real_db(self, db_session):
        """Test readiness check with real database session."""
        checker = HealthChecker(db=db_session)

        with patch("src.shared.config.get_settings") as mock_settings:
            mock_settings.return_value = MagicMock(redis_url=None)
            response = await checker.check_readiness()

        assert response.status == "ready"
        assert response.all_healthy is True

        db_component = next((c for c in response.components if c.name == "database"), None)
        assert db_component is not None
        assert db_component.status == "healthy"


class TestLatencyMeasurement:
    """Tests for latency measurement in health checks."""

    @pytest.mark.asyncio
    async def test_database_latency_measurement(self):
        """Test that database latency is measured correctly."""
        mock_db = MagicMock(spec=AsyncSession)

        # Simulate a 10ms delay
        async def slow_execute(*args):
            await AsyncMock()()
            return MagicMock()

        mock_db.execute = slow_execute

        checker = HealthChecker(db=mock_db)
        result = await checker._check_database()

        assert result.latency_ms is not None
        assert result.latency_ms >= 0  # Should measure some latency

    @pytest.mark.asyncio
    async def test_latency_is_integer_milliseconds(self):
        """Test that latency is returned as integer milliseconds."""
        mock_db = MagicMock(spec=AsyncSession)
        mock_db.execute = AsyncMock(return_value=MagicMock())

        checker = HealthChecker(db=mock_db)
        result = await checker._check_database()

        assert isinstance(result.latency_ms, int)
