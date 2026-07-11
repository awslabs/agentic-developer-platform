"""Stats aggregation service — Issue #3630.

Composes DynamoDB time-bounded queries (user-index or tenant-index GSI) with
in-memory aggregation to produce dashboard stats. Reuses ActivityService's
DynamoDB access patterns (KeyConditionExpression, status filtering, graceful
degradation on missing GSI/table).

Design decisions:
- 10K item backstop on the accumulation loop (prevents unbounded reads).
- 60s in-process TTL cache keyed by (scope, id, days).
- Status filter: excludes no_op and webhook_received (same as Issue #1658).
- Active runs: items with non-terminal status.
- Graceful degradation: missing GSI/table → empty stats (never 500).
"""

import logging
import os
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from src.activity.stats_schemas import (
    ActiveRun,
    DailyEntry,
    PersonaStats,
    RecentFailure,
    StatsResponse,
    TodayCounts,
    TopRepo,
)

logger = logging.getLogger("bedrockgateway.activity.stats")

# Default table name; overridden via env or constructor arg for testability.
_DEFAULT_TABLE_NAME = "adp-dev-webhook-events"

# Maximum items to accumulate from DDB (prevents unbounded reads).
_ITEM_BACKSTOP = 10_000

# In-progress statuses (non-terminal).
# Canonical source: modules/agent-factory/agent-worker-image/lib/invocation_status.py
# (writes "in_progress" when pod starts; see also #3696 for the vocabulary audit).
_ACTIVE_STATUSES = {"in_progress"}

# Terminal statuses — canonical sources:
# - "complete" / "failed": agent-worker-image/lib/invocation_status.py
# - "rate_limited" / "no_op": webhook-ingress/lambda/github/handler.py
_TERMINAL_STATUSES = {"complete", "failed", "rate_limited", "no_op"}

# Staleness cutoff for active runs (hours). An in_progress run older than this
# is treated as orphaned (terminal status was never delivered). Issue #3696.
_ACTIVE_STALENESS_HOURS = 24

# Statuses to exclude from stats (same as Issue #1658)
_NON_TRIGGERING_STATUSES = {"no_op", "webhook_received"}

# Cache TTL in seconds
_CACHE_TTL_SECONDS = 60


def _get_table_name() -> str:
    return os.environ.get("WEBHOOK_EVENTS_TABLE", _DEFAULT_TABLE_NAME)


class _CacheEntry:
    """Simple TTL cache entry."""

    __slots__ = ("value", "expires_at")

    def __init__(self, value: StatsResponse, ttl: float):
        self.value = value
        self.expires_at = time.monotonic() + ttl


class StatsService:
    """Service for aggregating agent run statistics from DynamoDB."""

    def __init__(self, table_name: str | None = None, dynamodb_resource=None):
        """Initialize the stats service.

        Args:
            table_name: Override DynamoDB table name (for testing).
            dynamodb_resource: Override boto3 DynamoDB resource (for testing).
        """
        self._table_name = table_name or _get_table_name()
        self._dynamodb = dynamodb_resource or boto3.resource("dynamodb", region_name=os.environ.get("AWS_REGION", "us-east-1"))
        self._table = self._dynamodb.Table(self._table_name)
        self._cache: dict[str, _CacheEntry] = {}

    def get_stats_by_user(self, user_id: str, days: int = 7) -> StatsResponse:
        """Get aggregated stats for a specific user.

        Args:
            user_id: The canonical user_id (resolved from token).
            days: Number of days in the stats window (1-30).

        Returns:
            StatsResponse with aggregated dashboard data.
        """
        cache_key = f"user:{user_id}:{days}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        items = self._fetch_items(
            index_name="user-index",
            partition_key_name="user_id",
            partition_key_value=user_id,
            days=days,
        )
        result = self._aggregate(items, days)
        self._set_cached(cache_key, result)
        return result

    def get_stats_by_tenant(self, tenant_id: str, days: int = 7) -> StatsResponse:
        """Get aggregated stats for a tenant (org-wide).

        Args:
            tenant_id: The tenant/org ID.
            days: Number of days in the stats window (1-30).

        Returns:
            StatsResponse with aggregated dashboard data.
        """
        cache_key = f"tenant:{tenant_id}:{days}"
        cached = self._get_cached(cache_key)
        if cached is not None:
            return cached

        items = self._fetch_items(
            index_name="tenant-index",
            partition_key_name="tenant_id",
            partition_key_value=tenant_id,
            days=days,
        )
        result = self._aggregate(items, days)
        self._set_cached(cache_key, result)
        return result

    def _get_cached(self, key: str) -> StatsResponse | None:
        """Return cached result if within TTL, else None."""
        entry = self._cache.get(key)
        if entry and time.monotonic() < entry.expires_at:
            return entry.value
        # Expired or missing — evict
        self._cache.pop(key, None)
        return None

    def _set_cached(self, key: str, value: StatsResponse) -> None:
        """Store a result in the cache with TTL."""
        self._cache[key] = _CacheEntry(value, _CACHE_TTL_SECONDS)

    def _fetch_items(
        self,
        *,
        index_name: str,
        partition_key_name: str,
        partition_key_value: str,
        days: int,
    ) -> list[dict]:
        """Fetch all items from DDB within the time window.

        Accumulates across pages up to _ITEM_BACKSTOP items. Applies status
        filter to exclude no_op and webhook_received. Uses KeyConditionExpression
        on the date sort key for time-bounding.
        """
        # Compute time window
        now = datetime.now(UTC)
        since = (now - timedelta(days=days)).strftime("%Y-%m-%dT00:00:00Z")

        # Build KeyConditionExpression with date lower bound
        key_condition = Key(partition_key_name).eq(partition_key_value) & Key("arrived_at").gte(since)

        # Filter out non-triggering statuses
        filter_expression = ~Attr("status").is_in(list(_NON_TRIGGERING_STATUSES))

        query_kwargs: dict = {
            "IndexName": index_name,
            "KeyConditionExpression": key_condition,
            "FilterExpression": filter_expression,
            "ScanIndexForward": False,  # newest first
            "Limit": 500,  # large page size for bulk accumulation
        }

        items: list[dict] = []
        try:
            while len(items) < _ITEM_BACKSTOP:
                response = self._table.query(**query_kwargs)
                items.extend(response.get("Items", []))
                lek = response.get("LastEvaluatedKey")
                if lek is None:
                    break
                query_kwargs["ExclusiveStartKey"] = lek
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ValidationException", "ResourceNotFoundException"):
                logger.warning(
                    "DynamoDB query failed (likely missing GSI/table) — returning empty stats",
                    extra={"index_name": index_name, "error_code": error_code},
                )
                return []
            raise

        return items

    def _aggregate(self, items: list[dict], days: int) -> StatsResponse:
        """Aggregate raw DDB items into the stats response shape."""
        now = datetime.now(UTC)
        today_str = now.strftime("%Y-%m-%d")
        staleness_cutoff = (now - timedelta(hours=_ACTIVE_STALENESS_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Initialize containers
        active_runs: list[ActiveRun] = []
        stale_count = 0
        today_counts = TodayCounts()
        daily_map: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "completed": 0, "failed": 0})
        persona_map: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "completed": 0, "failed": 0})
        repo_counts: dict[str, int] = defaultdict(int)
        failures: list[RecentFailure] = []

        for item in items:
            status = item.get("status", "")
            arrived_at = item.get("arrived_at", "")
            persona = item.get("persona")
            repo = item.get("repo")
            invocation_id = item.get("event_id", "")

            # Extract date part from ISO timestamp
            date_part = arrived_at[:10] if len(arrived_at) >= 10 else ""

            # Active runs (non-terminal status) — exclude stale orphans (#3696)
            if status in _ACTIVE_STATUSES:
                if arrived_at >= staleness_cutoff:
                    active_runs.append(
                        ActiveRun(
                            invocation_id=invocation_id,
                            invoked_at=arrived_at,
                            persona=persona,
                            repo=repo,
                            topic=item.get("topic"),
                        )
                    )
                else:
                    stale_count += 1

            # Today's counts
            if date_part == today_str:
                today_counts.total += 1
                if status == "complete":
                    today_counts.completed += 1
                elif status == "failed":
                    today_counts.failed += 1
                elif status in _ACTIVE_STATUSES and arrived_at >= staleness_cutoff:
                    today_counts.active += 1

            # Daily breakdown
            if date_part:
                daily_map[date_part]["total"] += 1
                if status == "complete":
                    daily_map[date_part]["completed"] += 1
                elif status == "failed":
                    daily_map[date_part]["failed"] += 1

            # Per-persona
            if persona:
                persona_map[persona]["total"] += 1
                if status == "complete":
                    persona_map[persona]["completed"] += 1
                elif status == "failed":
                    persona_map[persona]["failed"] += 1

            # Repo counts
            if repo:
                repo_counts[repo] += 1

            # Recent failures (collect up to 10)
            if status == "failed" and len(failures) < 10:
                failures.append(
                    RecentFailure(
                        invocation_id=invocation_id,
                        invoked_at=arrived_at,
                        persona=persona,
                        repo=repo,
                        topic=item.get("topic"),
                        error_message=item.get("error_message"),
                    )
                )

        # Build daily list sorted by date descending
        daily = sorted(
            [DailyEntry(date=d, **counts) for d, counts in daily_map.items()],
            key=lambda e: e.date,
            reverse=True,
        )

        # Build persona list sorted by total descending
        by_persona = sorted(
            [PersonaStats(persona=p, **counts) for p, counts in persona_map.items()],
            key=lambda e: e.total,
            reverse=True,
        )

        # Top repos (up to 10) sorted by count descending
        top_repos = sorted(
            [TopRepo(repo=r, total=c) for r, c in repo_counts.items()],
            key=lambda e: e.total,
            reverse=True,
        )[:10]

        return StatsResponse(
            window_days=days,
            active_runs=active_runs,
            stale_count=stale_count,
            today=today_counts,
            daily=daily,
            by_persona=by_persona,
            recent_failures=failures,
            top_repos=top_repos,
            spend=None,  # Cost enriched by route layer (cross-store pattern)
        )

    def get_run_ids(self, items: list[dict]) -> list[str]:
        """Extract invocation IDs from raw DDB items (for cost enrichment)."""
        return [item.get("event_id", "") for item in items if item.get("event_id")]
