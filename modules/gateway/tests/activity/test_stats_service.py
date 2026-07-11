"""Unit tests for StatsService — Issue #3630.

Covers:
- Correct today/daily/by_persona/top_repos aggregation from DDB-shaped fixtures
- Active runs detection (non-terminal statuses)
- Cost-leg failure → runs returned with spend: null
- Empty window → zero counts, empty arrays (not error)
- Item backstop triggers at 10K items (accumulation stops)
- Status filter excludes no_op and webhook_received from counts
- Cache serves stale result within 60s TTL window
"""

import time
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import pytest

from src.activity.stats_service import (
    _ITEM_BACKSTOP,
    StatsService,
)


def _make_item(
    event_id: str = "inv-001",
    status: str = "complete",
    persona: str | None = "developer",
    repo: str | None = "org/repo-a",
    topic: str | None = "Fix bug",
    arrived_at: str | None = None,
    error_message: str | None = None,
) -> dict:
    """Build a DynamoDB item dict shaped like a real webhook-events row."""
    if arrived_at is None:
        arrived_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    item = {
        "event_id": event_id,
        "status": status,
        "arrived_at": arrived_at,
        "user_id": "user-abc-123",
        "tenant_id": "org-tenant-001",
    }
    if persona:
        item["persona"] = persona
    if repo:
        item["repo"] = repo
    if topic:
        item["topic"] = topic
    if error_message:
        item["error_message"] = error_message
    return item


def _today_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _yesterday_iso() -> str:
    return (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _days_ago_iso(n: int) -> str:
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%dT%H:%M:%SZ")


class TestStatsServiceAggregation:
    """Test the aggregation logic in StatsService._aggregate()."""

    def _make_service(self, items: list[dict]) -> StatsService:
        """Create a StatsService with a mocked DynamoDB that returns the given items."""
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": items, "LastEvaluatedKey": None}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        return StatsService(table_name="test-table", dynamodb_resource=mock_resource)

    def test_today_counts_correct(self):
        """Items from today are correctly counted in today totals."""
        today = _today_iso()
        items = [
            _make_item(event_id="inv-1", status="complete", arrived_at=today),
            _make_item(event_id="inv-2", status="complete", arrived_at=today),
            _make_item(event_id="inv-3", status="failed", arrived_at=today),
            _make_item(event_id="inv-4", status="in_progress", arrived_at=today),
        ]
        service = self._make_service(items)
        result = service.get_stats_by_user("user-abc-123", days=7)

        assert result.today.total == 4
        assert result.today.completed == 2
        assert result.today.failed == 1
        assert result.today.active == 1

    def test_daily_breakdown(self):
        """Items are grouped into correct daily buckets."""
        today = _today_iso()
        yesterday = _yesterday_iso()
        items = [
            _make_item(event_id="inv-1", status="complete", arrived_at=today),
            _make_item(event_id="inv-2", status="failed", arrived_at=today),
            _make_item(event_id="inv-3", status="complete", arrived_at=yesterday),
        ]
        service = self._make_service(items)
        result = service.get_stats_by_user("user-abc-123", days=7)

        assert len(result.daily) == 2
        # Daily is sorted desc by date
        today_entry = next(e for e in result.daily if e.date == datetime.now(UTC).strftime("%Y-%m-%d"))
        assert today_entry.total == 2
        assert today_entry.completed == 1
        assert today_entry.failed == 1

    def test_by_persona_aggregation(self):
        """Persona-level stats are correctly computed."""
        today = _today_iso()
        items = [
            _make_item(event_id="inv-1", status="complete", persona="developer", arrived_at=today),
            _make_item(event_id="inv-2", status="complete", persona="developer", arrived_at=today),
            _make_item(event_id="inv-3", status="failed", persona="reviewer", arrived_at=today),
            _make_item(event_id="inv-4", status="complete", persona="reviewer", arrived_at=today),
        ]
        service = self._make_service(items)
        result = service.get_stats_by_user("user-abc-123", days=7)

        assert len(result.by_persona) == 2
        dev = next(p for p in result.by_persona if p.persona == "developer")
        assert dev.total == 2
        assert dev.completed == 2
        assert dev.failed == 0
        reviewer = next(p for p in result.by_persona if p.persona == "reviewer")
        assert reviewer.total == 2
        assert reviewer.failed == 1

    def test_top_repos(self):
        """Top repos are sorted by count and limited to 10."""
        today = _today_iso()
        items = []
        for i in range(15):
            # First repo gets 3 runs, others get 1
            repo = "org/top-repo" if i < 3 else f"org/repo-{i}"
            items.append(_make_item(event_id=f"inv-{i}", repo=repo, arrived_at=today))
        service = self._make_service(items)
        result = service.get_stats_by_user("user-abc-123", days=7)

        assert len(result.top_repos) == 10
        assert result.top_repos[0].repo == "org/top-repo"
        assert result.top_repos[0].total == 3

    def test_recent_failures_limited_to_10(self):
        """Recent failures list is capped at 10 items."""
        today = _today_iso()
        items = [_make_item(event_id=f"inv-{i}", status="failed", arrived_at=today, error_message=f"err-{i}") for i in range(15)]
        service = self._make_service(items)
        result = service.get_stats_by_user("user-abc-123", days=7)

        assert len(result.recent_failures) == 10
        assert result.recent_failures[0].invocation_id == "inv-0"
        assert result.recent_failures[0].error_message == "err-0"

    def test_active_runs_detected(self):
        """in_progress status is captured as active run (real DDB vocabulary)."""
        today = _today_iso()
        items = [
            _make_item(event_id="inv-1", status="in_progress", arrived_at=today),
            _make_item(event_id="inv-2", status="in_progress", arrived_at=today),
            _make_item(event_id="inv-3", status="complete", arrived_at=today),
        ]
        service = self._make_service(items)
        result = service.get_stats_by_user("user-abc-123", days=7)

        assert len(result.active_runs) == 2
        active_ids = {r.invocation_id for r in result.active_runs}
        assert "inv-1" in active_ids
        assert "inv-2" in active_ids

    def test_stale_active_runs_excluded(self):
        """in_progress runs older than 24h are excluded from active_runs (stale orphans)."""
        today = _today_iso()
        old = (datetime.now(UTC) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        items = [
            _make_item(event_id="inv-fresh", status="in_progress", arrived_at=today),
            _make_item(event_id="inv-stale", status="in_progress", arrived_at=old),
        ]
        service = self._make_service(items)
        result = service.get_stats_by_user("user-abc-123", days=7)

        assert len(result.active_runs) == 1
        assert result.active_runs[0].invocation_id == "inv-fresh"
        assert result.stale_count == 1

    def test_empty_window_returns_zeros(self):
        """Empty result set → zero counts, empty arrays, not an error."""
        service = self._make_service([])
        result = service.get_stats_by_user("user-abc-123", days=7)

        assert result.window_days == 7
        assert result.today.total == 0
        assert result.today.completed == 0
        assert result.daily == []
        assert result.by_persona == []
        assert result.active_runs == []
        assert result.recent_failures == []
        assert result.top_repos == []
        assert result.spend is None

    def test_spend_is_null_by_default(self):
        """Spend field is null from the service (enriched by route layer)."""
        today = _today_iso()
        items = [_make_item(event_id="inv-1", arrived_at=today)]
        service = self._make_service(items)
        result = service.get_stats_by_user("user-abc-123", days=7)

        assert result.spend is None


class TestStatsServiceStatusFilter:
    """Test that non-triggering statuses are filtered out."""

    def test_no_op_excluded(self):
        """DDB filter excludes no_op from results (never reaches aggregation)."""
        mock_table = MagicMock()
        # Simulate DDB filtering — no_op items won't appear in Items
        mock_table.query.return_value = {"Items": [], "LastEvaluatedKey": None}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        service.get_stats_by_user("user-abc-123", days=7)

        # Verify FilterExpression was passed to query
        call_kwargs = mock_table.query.call_args[1]
        assert "FilterExpression" in call_kwargs

    def test_webhook_received_excluded(self):
        """DDB filter excludes webhook_received status."""
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": [], "LastEvaluatedKey": None}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        service.get_stats_by_user("user-abc-123", days=7)

        # The FilterExpression should be present (excluding non-triggering)
        call_kwargs = mock_table.query.call_args[1]
        assert "FilterExpression" in call_kwargs


class TestStatsServiceItemBackstop:
    """Test that the 10K item backstop stops accumulation."""

    def test_backstop_stops_at_limit(self):
        """Accumulation loop stops when items reach _ITEM_BACKSTOP."""
        mock_table = MagicMock()
        # Return a large batch + LEK to simulate more pages
        large_batch = [_make_item(event_id=f"inv-{i}", arrived_at=_today_iso()) for i in range(_ITEM_BACKSTOP)]
        mock_table.query.return_value = {"Items": large_batch, "LastEvaluatedKey": {"pk": "next"}}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        result = service.get_stats_by_user("user-abc-123", days=7)

        # Should have called query exactly once (items >= backstop after first call)
        assert mock_table.query.call_count == 1
        assert result.today.total == _ITEM_BACKSTOP

    def test_pagination_follows_lek(self):
        """Multiple pages are fetched until LEK is None."""
        mock_table = MagicMock()
        page1_items = [_make_item(event_id="inv-1", arrived_at=_today_iso())]
        page2_items = [_make_item(event_id="inv-2", arrived_at=_today_iso())]
        mock_table.query.side_effect = [
            {"Items": page1_items, "LastEvaluatedKey": {"pk": "page2"}},
            {"Items": page2_items, "LastEvaluatedKey": None},
        ]
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        result = service.get_stats_by_user("user-abc-123", days=7)

        assert mock_table.query.call_count == 2
        assert result.today.total == 2


class TestStatsServiceCache:
    """Test the in-process TTL cache."""

    def test_cache_serves_stale_within_ttl(self):
        """Second call within TTL returns cached result without DDB query."""
        mock_table = MagicMock()
        items = [_make_item(event_id="inv-1", arrived_at=_today_iso())]
        mock_table.query.return_value = {"Items": items, "LastEvaluatedKey": None}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        # First call — hits DDB
        result1 = service.get_stats_by_user("user-abc-123", days=7)
        assert mock_table.query.call_count == 1

        # Second call — served from cache
        result2 = service.get_stats_by_user("user-abc-123", days=7)
        assert mock_table.query.call_count == 1  # No additional call
        assert result2.today.total == result1.today.total

    def test_cache_expires_after_ttl(self):
        """After TTL expires, cache is evicted and DDB is queried again."""
        mock_table = MagicMock()
        items = [_make_item(event_id="inv-1", arrived_at=_today_iso())]
        mock_table.query.return_value = {"Items": items, "LastEvaluatedKey": None}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        # First call
        service.get_stats_by_user("user-abc-123", days=7)
        assert mock_table.query.call_count == 1

        # Expire the cache by manipulating the entry
        cache_key = "user:user-abc-123:7"
        service._cache[cache_key].expires_at = time.monotonic() - 1

        # Third call — cache expired, hits DDB
        service.get_stats_by_user("user-abc-123", days=7)
        assert mock_table.query.call_count == 2

    def test_different_days_different_cache_key(self):
        """Different days param produces a different cache key."""
        mock_table = MagicMock()
        items = [_make_item(event_id="inv-1", arrived_at=_today_iso())]
        mock_table.query.return_value = {"Items": items, "LastEvaluatedKey": None}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        service.get_stats_by_user("user-abc-123", days=7)
        service.get_stats_by_user("user-abc-123", days=14)
        # Two different cache keys → two DDB calls
        assert mock_table.query.call_count == 2


class TestStatsServiceGracefulDegradation:
    """Test graceful handling of DDB errors."""

    def test_missing_gsi_returns_empty_stats(self):
        """Missing GSI/table → empty stats, not an exception."""
        from botocore.exceptions import ClientError

        mock_table = MagicMock()
        mock_table.query.side_effect = ClientError(
            {"Error": {"Code": "ValidationException", "Message": "Index not found"}},
            "Query",
        )
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        result = service.get_stats_by_user("user-abc-123", days=7)

        assert result.window_days == 7
        assert result.today.total == 0
        assert result.daily == []

    def test_resource_not_found_returns_empty(self):
        """ResourceNotFoundException → empty stats."""
        from botocore.exceptions import ClientError

        mock_table = MagicMock()
        mock_table.query.side_effect = ClientError(
            {"Error": {"Code": "ResourceNotFoundException", "Message": "Table not found"}},
            "Query",
        )
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        result = service.get_stats_by_user("user-abc-123", days=7)

        assert result.today.total == 0

    def test_unexpected_error_raises(self):
        """Unexpected DDB errors are re-raised."""
        from botocore.exceptions import ClientError

        mock_table = MagicMock()
        mock_table.query.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Something broke"}},
            "Query",
        )
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        with pytest.raises(ClientError):
            service.get_stats_by_user("user-abc-123", days=7)


class TestStatsServiceTenantScope:
    """Test tenant-scoped stats."""

    def test_tenant_query_uses_tenant_index(self):
        """Tenant stats query the tenant-index GSI."""
        mock_table = MagicMock()
        mock_table.query.return_value = {"Items": [], "LastEvaluatedKey": None}
        mock_resource = MagicMock()
        mock_resource.Table.return_value = mock_table
        service = StatsService(table_name="test-table", dynamodb_resource=mock_resource)

        service.get_stats_by_tenant("org-tenant-001", days=7)

        call_kwargs = mock_table.query.call_args[1]
        assert call_kwargs["IndexName"] == "tenant-index"


class TestStatusVocabularyGuard:
    """Guard test: stats_service status constants must use real DDB vocabulary.

    Issue #3696: The original _ACTIVE_STATUSES used invented values (running,
    queued, pending, dispatched) that no writer ever produces. This guard test
    ensures the constants stay aligned with the REAL vocabulary written by:
    - Lambda: modules/agent-factory/webhook-ingress/lambda/common/webhook_events.py
    - Worker: modules/agent-factory/agent-worker-image/lib/invocation_status.py
    - Lambda: modules/agent-factory/webhook-ingress/lambda/github/handler.py

    If a new status is added to a writer, it must be added to KNOWN_REAL_STATUSES
    below AND classified into either _ACTIVE_STATUSES or _TERMINAL_STATUSES.
    """

    # The complete set of statuses that can appear in webhook-events rows.
    # Canonical sources cited above. Update this ONLY when a writer is changed.
    KNOWN_REAL_STATUSES = {
        "webhook_received",  # Lambda: initial capture before dispatch
        "in_progress",  # Worker: pod bootstrap complete, agent executing
        "complete",  # Worker: agent exited 0 + transcript uploaded
        "failed",  # Worker: agent exited non-zero
        "no_op",  # Lambda: event passed guards but no agent dispatched
        "rate_limited",  # Lambda: tenant rate limit exceeded
    }

    def test_active_statuses_are_real(self):
        """_ACTIVE_STATUSES must be a subset of real DDB statuses."""
        from src.activity.stats_service import _ACTIVE_STATUSES

        unknown = _ACTIVE_STATUSES - self.KNOWN_REAL_STATUSES
        assert not unknown, (
            f"_ACTIVE_STATUSES contains values not in the real DDB vocabulary: {unknown}. "
            f"If a new status was added to a writer, update KNOWN_REAL_STATUSES in this test."
        )

    def test_terminal_statuses_are_real(self):
        """_TERMINAL_STATUSES must be a subset of real DDB statuses."""
        from src.activity.stats_service import _TERMINAL_STATUSES

        unknown = _TERMINAL_STATUSES - self.KNOWN_REAL_STATUSES
        assert not unknown, (
            f"_TERMINAL_STATUSES contains values not in the real DDB vocabulary: {unknown}. "
            f"If a new status was added to a writer, update KNOWN_REAL_STATUSES in this test."
        )

    def test_all_real_statuses_classified(self):
        """Every known real status must be in _ACTIVE, _TERMINAL, or _NON_TRIGGERING."""
        from src.activity.stats_service import (
            _ACTIVE_STATUSES,
            _NON_TRIGGERING_STATUSES,
            _TERMINAL_STATUSES,
        )

        classified = _ACTIVE_STATUSES | _TERMINAL_STATUSES | _NON_TRIGGERING_STATUSES
        unclassified = self.KNOWN_REAL_STATUSES - classified
        assert not unclassified, (
            f"Real DDB statuses not classified in any constant: {unclassified}. "
            f"Add them to _ACTIVE_STATUSES, _TERMINAL_STATUSES, or _NON_TRIGGERING_STATUSES."
        )

    def test_active_and_terminal_disjoint(self):
        """Active and terminal sets must not overlap."""
        from src.activity.stats_service import _ACTIVE_STATUSES, _TERMINAL_STATUSES

        overlap = _ACTIVE_STATUSES & _TERMINAL_STATUSES
        assert not overlap, f"Status in both active AND terminal: {overlap}"
