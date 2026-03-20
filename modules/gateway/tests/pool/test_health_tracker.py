"""Unit tests for the health tracker."""

from datetime import UTC, datetime

from src.pool.config import PoolSettings
from src.pool.health_tracker import HealthTracker
from src.pool.models import BedrockAccount, HealthStatus
from tests.pool.conftest import make_expired_cooldown_account, make_healthy_account, make_unhealthy_account


class TestHealthTracker:
    """Tests for HealthTracker class."""

    async def test_mark_healthy(self, health_tracker: HealthTracker, sample_accounts: list[BedrockAccount]):
        """Test marking an account as healthy."""
        account = sample_accounts[0]
        account.health_status = HealthStatus.UNKNOWN

        await health_tracker.mark_healthy(account)

        assert account.health_status == HealthStatus.HEALTHY
        assert account.cooldown_until is None
        assert account.consecutive_failures == 0
        assert account.last_health_check is not None

    async def test_mark_unhealthy_with_cooldown(
        self,
        health_tracker: HealthTracker,
        sample_accounts: list[BedrockAccount],
    ):
        """Test marking an account as unhealthy with cooldown."""
        account = sample_accounts[0]
        account.health_status = HealthStatus.HEALTHY

        await health_tracker.mark_unhealthy(account, "Test error", apply_cooldown=True)

        assert account.health_status == HealthStatus.COOLDOWN
        assert account.cooldown_until is not None
        assert account.cooldown_until > datetime.now(UTC)
        assert account.consecutive_failures == 1
        assert account.error_count == 1

    async def test_mark_unhealthy_without_cooldown(
        self,
        health_tracker: HealthTracker,
        sample_accounts: list[BedrockAccount],
    ):
        """Test marking an account as unhealthy without cooldown."""
        account = sample_accounts[0]

        await health_tracker.mark_unhealthy(account, "Test error", apply_cooldown=False)

        assert account.health_status == HealthStatus.UNHEALTHY
        assert account.cooldown_until is None
        assert account.consecutive_failures == 1

    async def test_exponential_backoff_cooldown(self, health_tracker: HealthTracker):
        """Test that cooldown increases with consecutive failures."""
        account = make_healthy_account("111111111111")

        # First failure - base cooldown (30 seconds from fixture)
        await health_tracker.mark_unhealthy(account, "Error 1")
        first_cooldown = account.cooldown_until

        # Second failure - should have longer cooldown
        account.health_status = HealthStatus.HEALTHY  # Reset for next test
        await health_tracker.mark_unhealthy(account, "Error 2")
        second_cooldown = account.cooldown_until

        assert second_cooldown > first_cooldown

    async def test_cooldown_capped_at_max(self, pool_settings: PoolSettings):
        """Test that cooldown is capped at maximum (5 minutes)."""
        tracker = HealthTracker(settings=pool_settings)
        account = make_healthy_account("111111111111")

        # Simulate many failures
        for _ in range(10):
            await tracker.mark_unhealthy(account, "Error")

        # Cooldown should not exceed 5 minutes (300 seconds)
        cooldown_duration = (account.cooldown_until - datetime.now(UTC)).total_seconds()
        assert cooldown_duration <= 300

    async def test_is_healthy_for_healthy_account(
        self,
        health_tracker: HealthTracker,
    ):
        """Test is_healthy returns True for healthy accounts."""
        account = make_healthy_account()
        assert await health_tracker.is_healthy(account) is True

    async def test_is_healthy_for_unhealthy_account(
        self,
        health_tracker: HealthTracker,
    ):
        """Test is_healthy returns False for unhealthy accounts."""
        account = make_unhealthy_account()
        assert await health_tracker.is_healthy(account) is False

    async def test_is_healthy_for_expired_cooldown(
        self,
        health_tracker: HealthTracker,
    ):
        """Test is_healthy returns True when cooldown has expired."""
        account = make_expired_cooldown_account()
        assert await health_tracker.is_healthy(account) is True

    async def test_get_cooldown_remaining(self, health_tracker: HealthTracker):
        """Test getting remaining cooldown time."""
        account = make_unhealthy_account(cooldown_seconds=60)

        remaining = await health_tracker.get_cooldown_remaining(account)

        assert remaining > 0
        assert remaining <= 60

    async def test_get_cooldown_remaining_zero_when_expired(self, health_tracker: HealthTracker):
        """Test cooldown remaining is 0 when expired."""
        account = make_expired_cooldown_account()

        remaining = await health_tracker.get_cooldown_remaining(account)

        assert remaining == 0.0

    async def test_get_cooldown_remaining_zero_for_healthy(self, health_tracker: HealthTracker):
        """Test cooldown remaining is 0 for healthy accounts."""
        account = make_healthy_account()

        remaining = await health_tracker.get_cooldown_remaining(account)

        assert remaining == 0.0

    async def test_reset_cooldown(self, health_tracker: HealthTracker):
        """Test resetting cooldown for an account."""
        account = make_unhealthy_account(cooldown_seconds=120)
        account.consecutive_failures = 5

        await health_tracker.reset_cooldown(account)

        assert account.health_status == HealthStatus.HEALTHY
        assert account.cooldown_until is None
        assert account.consecutive_failures == 0

    async def test_record_request(self, health_tracker: HealthTracker):
        """Test recording a request updates statistics."""
        account = make_healthy_account()
        initial_count = account.request_count

        await health_tracker.record_request(account)

        assert account.request_count == initial_count + 1
        assert account.last_used is not None

    async def test_record_success_recovers_from_cooldown(self, health_tracker: HealthTracker):
        """Test that success recovers account from cooldown."""
        account = make_unhealthy_account(cooldown_seconds=60)
        account.consecutive_failures = 3

        await health_tracker.record_success(account)

        assert account.health_status == HealthStatus.HEALTHY
        assert account.cooldown_until is None
        assert account.consecutive_failures == 0
        assert account.request_count == 1

    async def test_record_error_with_throttling(self, health_tracker: HealthTracker):
        """Test recording an error with throttling applies cooldown."""
        account = make_healthy_account()

        await health_tracker.record_error(account, "Throttling", is_throttling=True)

        assert account.health_status == HealthStatus.COOLDOWN
        assert account.cooldown_until is not None

    async def test_record_error_without_throttling(self, health_tracker: HealthTracker):
        """Test recording an error without throttling marks unhealthy without cooldown."""
        account = make_healthy_account()

        await health_tracker.record_error(account, "Generic error", is_throttling=False)

        # Without throttling, should just mark unhealthy
        assert account.consecutive_failures == 1
        assert account.error_count == 1

    async def test_get_healthy_accounts(
        self,
        health_tracker: HealthTracker,
    ):
        """Test getting list of healthy accounts."""
        accounts = [
            make_healthy_account("111111111111"),
            make_unhealthy_account("222222222222"),
            make_healthy_account("333333333333"),
            make_expired_cooldown_account("444444444444"),
        ]

        healthy = await health_tracker.get_healthy_accounts(accounts)

        # Should include healthy and expired cooldown accounts
        assert len(healthy) == 3
        healthy_ids = [acc.account_id for acc in healthy]
        assert "111111111111" in healthy_ids
        assert "333333333333" in healthy_ids
        assert "444444444444" in healthy_ids
        assert "222222222222" not in healthy_ids

    async def test_get_status_summary(
        self,
        health_tracker: HealthTracker,
    ):
        """Test getting status summary."""
        accounts = [
            make_healthy_account("111111111111"),
            make_unhealthy_account("222222222222"),
            make_expired_cooldown_account("333333333333"),  # Should count as healthy
        ]
        # Add one truly unhealthy (not just cooldown)
        unhealthy = make_healthy_account("444444444444")
        unhealthy.health_status = HealthStatus.UNHEALTHY
        accounts.append(unhealthy)

        summary = await health_tracker.get_status_summary(accounts)

        assert summary["total"] == 4
        assert summary["healthy"] == 2  # 111111111111 and 333333333333 (expired cooldown)
        assert summary["unhealthy"] == 1  # 444444444444
        assert summary["cooldown"] == 1  # 222222222222

    async def test_concurrent_mark_healthy(self, health_tracker: HealthTracker):
        """Test thread safety of mark_healthy."""
        import asyncio

        account = make_unhealthy_account("111111111111")

        # Run multiple concurrent mark_healthy calls
        await asyncio.gather(*[health_tracker.mark_healthy(account) for _ in range(10)])

        assert account.health_status == HealthStatus.HEALTHY
        assert account.consecutive_failures == 0
