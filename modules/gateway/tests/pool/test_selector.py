"""Unit tests for the round-robin selector."""

import asyncio

from src.pool.models import BedrockAccount
from src.pool.selector import RoundRobinSelector
from tests.pool.conftest import make_healthy_account, make_unhealthy_account


class TestRoundRobinSelector:
    """Tests for RoundRobinSelector class (US-5.1: Round-Robin Request Distribution)."""

    async def test_get_next_account_round_robin(self, sample_accounts: list[BedrockAccount]):
        """Test that accounts are selected in round-robin order."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        # Get accounts in order
        selected_ids = []
        for _ in range(len(sample_accounts)):
            account = await selector.get_next_account(skip_unhealthy=False)
            selected_ids.append(account.account_id)

        # Should get all accounts in order
        expected_ids = [acc.account_id for acc in sample_accounts]
        assert selected_ids == expected_ids

    async def test_round_robin_wraps_around(self, sample_accounts: list[BedrockAccount]):
        """Test that selection wraps around to beginning."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        # Select all accounts plus one more
        for _ in range(len(sample_accounts) + 1):
            await selector.get_next_account(skip_unhealthy=False)

        # Should be back to second account (wrapped around)
        next_account = await selector.get_next_account(skip_unhealthy=False)
        assert next_account.account_id == sample_accounts[1].account_id

    async def test_skip_unhealthy_accounts(self):
        """Test that unhealthy accounts are skipped."""
        accounts = [
            make_healthy_account("111111111111"),
            make_unhealthy_account("222222222222"),  # Unhealthy
            make_healthy_account("333333333333"),
        ]
        selector = RoundRobinSelector(accounts=accounts)

        # First should be 111111111111
        account1 = await selector.get_next_account()
        assert account1.account_id == "111111111111"

        # Second should skip 222222222222 and return 333333333333
        account2 = await selector.get_next_account()
        assert account2.account_id == "333333333333"

        # Third should wrap and return 111111111111
        account3 = await selector.get_next_account()
        assert account3.account_id == "111111111111"

    async def test_returns_none_when_all_unhealthy(self):
        """Test that None is returned when all accounts are unhealthy (US-9.4)."""
        accounts = [
            make_unhealthy_account("111111111111"),
            make_unhealthy_account("222222222222"),
            make_unhealthy_account("333333333333"),
        ]
        selector = RoundRobinSelector(accounts=accounts)

        account = await selector.get_next_account()

        assert account is None

    async def test_returns_none_for_empty_pool(self):
        """Test that None is returned for empty pool."""
        selector = RoundRobinSelector(accounts=[])

        account = await selector.get_next_account()

        assert account is None

    async def test_get_next_healthy_account(self, sample_accounts: list[BedrockAccount]):
        """Test convenience method for getting healthy accounts."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        account = await selector.get_next_healthy_account()

        assert account is not None
        assert account.is_healthy()

    async def test_get_all_healthy_accounts(self):
        """Test getting all healthy accounts."""
        accounts = [
            make_healthy_account("111111111111"),
            make_unhealthy_account("222222222222"),
            make_healthy_account("333333333333"),
        ]
        selector = RoundRobinSelector(accounts=accounts)

        healthy = await selector.get_all_healthy_accounts()

        assert len(healthy) == 2
        assert all(acc.is_healthy() for acc in healthy)

    async def test_skip_account(self):
        """Test skipping a specific account."""
        accounts = [
            make_healthy_account("111111111111"),
            make_healthy_account("222222222222"),
        ]
        selector = RoundRobinSelector(accounts=accounts)

        # Skip the first account
        await selector.skip_account("111111111111")

        # Next should be the second account
        account = await selector.get_next_account(skip_unhealthy=False)
        assert account.account_id == "222222222222"

    async def test_reset_position(self, sample_accounts: list[BedrockAccount]):
        """Test resetting selector position."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        # Advance a few positions
        await selector.get_next_account(skip_unhealthy=False)
        await selector.get_next_account(skip_unhealthy=False)

        # Reset
        await selector.reset_position()

        # Should be back at first account
        account = await selector.get_next_account(skip_unhealthy=False)
        assert account.account_id == sample_accounts[0].account_id

    async def test_set_position(self, sample_accounts: list[BedrockAccount]):
        """Test setting selector to specific position."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        # Set to index 2
        await selector.set_position(2)

        # Should get third account
        account = await selector.get_next_account(skip_unhealthy=False)
        assert account.account_id == sample_accounts[2].account_id

    async def test_set_position_wraps(self, sample_accounts: list[BedrockAccount]):
        """Test that set_position wraps for out-of-bounds index."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        # Set to index larger than list
        await selector.set_position(len(sample_accounts) + 1)

        # Should wrap around
        position = await selector.get_current_position()
        assert position < len(sample_accounts)

    async def test_get_current_position(self, sample_accounts: list[BedrockAccount]):
        """Test getting current position."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        # Initial position should be 0
        assert await selector.get_current_position() == 0

        # After one selection, should be 1
        await selector.get_next_account(skip_unhealthy=False)
        assert await selector.get_current_position() == 1

    async def test_add_account(self):
        """Test adding an account to the pool."""
        accounts = [
            make_healthy_account("111111111111"),
            make_healthy_account("222222222222"),
            make_healthy_account("333333333333"),
        ]
        selector = RoundRobinSelector(accounts=accounts)
        new_account = make_healthy_account("444444444444")
        initial_count = await selector.get_account_count()

        await selector.add_account(new_account)

        assert await selector.get_account_count() == initial_count + 1

    async def test_remove_account(self, sample_accounts: list[BedrockAccount]):
        """Test removing an account from the pool."""
        selector = RoundRobinSelector(accounts=sample_accounts)
        initial_count = await selector.get_account_count()

        removed = await selector.remove_account(sample_accounts[1].account_id)

        assert removed is True
        assert await selector.get_account_count() == initial_count - 1

    async def test_remove_nonexistent_account(self, sample_accounts: list[BedrockAccount]):
        """Test removing a nonexistent account."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        removed = await selector.remove_account("nonexistent")

        assert removed is False

    async def test_remove_account_adjusts_index(self):
        """Test that removing account before current index adjusts index."""
        accounts = [
            make_healthy_account("111111111111"),
            make_healthy_account("222222222222"),
            make_healthy_account("333333333333"),
        ]
        selector = RoundRobinSelector(accounts=accounts)

        # Move to second position
        await selector.get_next_account(skip_unhealthy=False)
        await selector.get_next_account(skip_unhealthy=False)
        assert await selector.get_current_position() == 2

        # Remove first account
        await selector.remove_account("111111111111")

        # Index should be adjusted
        assert await selector.get_current_position() == 1

    async def test_get_account_count(self, sample_accounts: list[BedrockAccount]):
        """Test getting account count."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        count = await selector.get_account_count()

        assert count == len(sample_accounts)

    async def test_get_healthy_count(self):
        """Test getting healthy account count."""
        accounts = [
            make_healthy_account("111111111111"),
            make_unhealthy_account("222222222222"),
            make_healthy_account("333333333333"),
        ]
        selector = RoundRobinSelector(accounts=accounts)

        count = await selector.get_healthy_count()

        assert count == 2

    async def test_get_accounts_in_order(self, sample_accounts: list[BedrockAccount]):
        """Test getting accounts in order from current position."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        # Move to position 1
        await selector.get_next_account(skip_unhealthy=False)

        # Get accounts in order (starting from position 1)
        ordered = await selector.get_accounts_in_order()

        assert len(ordered) == len(sample_accounts)
        assert ordered[0].account_id == sample_accounts[1].account_id

    async def test_concurrent_access_safety(self, sample_accounts: list[BedrockAccount]):
        """Test thread safety with concurrent access."""
        selector = RoundRobinSelector(accounts=sample_accounts)

        # Run multiple concurrent selections
        results = await asyncio.gather(*[selector.get_next_account(skip_unhealthy=False) for _ in range(100)])

        # All results should be valid accounts
        assert all(acc is not None for acc in results)
        assert all(acc.account_id in [a.account_id for a in sample_accounts] for acc in results)

    async def test_custom_health_check_function(self):
        """Test using custom health check function."""
        accounts = [
            make_healthy_account("111111111111"),
            make_healthy_account("222222222222"),
        ]
        # Custom health check that only considers first account healthy
        selector = RoundRobinSelector(
            accounts=accounts,
            health_check=lambda acc: acc.account_id == "111111111111",
        )

        # Should only return first account
        for _ in range(5):
            account = await selector.get_next_account()
            assert account.account_id == "111111111111"
