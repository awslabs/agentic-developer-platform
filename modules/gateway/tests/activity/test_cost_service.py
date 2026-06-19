"""Unit tests for ActivityCostService — Postgres cost aggregation.

Issue #1616: Tests for batched per-run cost queries.
"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.activity.cost_service import get_cost_by_run_ids


class TestGetCostByRunIds:
    """Test batched cost aggregation queries."""

    @pytest.mark.asyncio
    async def test_empty_run_ids_returns_empty(self):
        """Empty input list returns empty dict without querying."""
        db = AsyncMock()
        result = await get_cost_by_run_ids(db, [])
        assert result == {}
        db.execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_cost_for_known_run_ids(self):
        """Returns aggregated cost data keyed by agent_run_id."""
        # Mock the DB result
        mock_row1 = MagicMock()
        mock_row1.agent_run_id = "inv-001"
        mock_row1.total_cost_usd = Decimal("0.0523")
        mock_row1.total_tokens = 15000
        mock_row1.call_count = 3

        mock_row2 = MagicMock()
        mock_row2.agent_run_id = "inv-002"
        mock_row2.total_cost_usd = Decimal("0.1200")
        mock_row2.total_tokens = 28000
        mock_row2.call_count = 5

        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row1, mock_row2]

        db = AsyncMock()
        db.execute.return_value = mock_result

        result = await get_cost_by_run_ids(db, ["inv-001", "inv-002", "inv-003"])

        assert "inv-001" in result
        assert result["inv-001"]["total_cost_usd"] == pytest.approx(0.0523)
        assert result["inv-001"]["total_tokens"] == 15000
        assert result["inv-001"]["call_count"] == 3

        assert "inv-002" in result
        assert result["inv-002"]["total_cost_usd"] == pytest.approx(0.12)
        assert result["inv-002"]["total_tokens"] == 28000
        assert result["inv-002"]["call_count"] == 5

        # inv-003 not in result (no usage_logs rows)
        assert "inv-003" not in result

    @pytest.mark.asyncio
    async def test_missing_run_id_not_in_result(self):
        """Run IDs with no matching usage_logs rows are absent from result."""
        mock_result = MagicMock()
        mock_result.all.return_value = []

        db = AsyncMock()
        db.execute.return_value = mock_result

        result = await get_cost_by_run_ids(db, ["inv-unknown"])
        assert result == {}

    @pytest.mark.asyncio
    async def test_handles_null_cost(self):
        """Rows with null/zero cost return 0.0."""
        mock_row = MagicMock()
        mock_row.agent_run_id = "inv-pending"
        mock_row.total_cost_usd = None
        mock_row.total_tokens = None
        mock_row.call_count = 1

        mock_result = MagicMock()
        mock_result.all.return_value = [mock_row]

        db = AsyncMock()
        db.execute.return_value = mock_result

        result = await get_cost_by_run_ids(db, ["inv-pending"])
        assert result["inv-pending"]["total_cost_usd"] == 0.0
        assert result["inv-pending"]["total_tokens"] == 0
        assert result["inv-pending"]["call_count"] == 1

    @pytest.mark.asyncio
    async def test_batched_query_uses_in_clause(self):
        """Verifies a single query is issued for multiple run_ids (no N+1)."""
        mock_result = MagicMock()
        mock_result.all.return_value = []

        db = AsyncMock()
        db.execute.return_value = mock_result

        run_ids = [f"inv-{i}" for i in range(20)]
        await get_cost_by_run_ids(db, run_ids)

        # Single execute call (batched, not N separate queries)
        assert db.execute.call_count == 1
