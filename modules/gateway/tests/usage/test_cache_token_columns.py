"""usage_logs must record prompt-cache read/write tokens (issue #4180).

Before this change the platform recorded token counts but not the cache-read vs.
cache-write breakdown, so no dashboard, bill, or query could tell "caching is
working beautifully" from "caching has never worked once".

The load-bearing invariant here is NULL != 0:
  - NULL  = the provider did not report the counter (or a pre-feature row)
  - 0     = the provider reported zero cache activity
Collapsing them (e.g. via a column default, or a ``.get(key, 0)`` anywhere on the
write path) makes the hit-rate query silently wrong in exactly the direction that
hides the original bug.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.proxy.service import ProxyService
from src.shared.models.usage import UsageLog
from src.shared.schemas.auth import TokenContext
from src.usage.service import UsageService


class TestLogRequestPersistsCacheTokens:
    """UsageService.log_request must round-trip both counters, including NULL."""

    @pytest.mark.asyncio
    async def test_reported_counters_persist(
        self,
        usage_service: UsageService,
        org_user_context: TokenContext,
        db_session: AsyncSession,
    ):
        await usage_service.log_request(
            context=org_user_context,
            model="claude-opus-4.6",
            input_tokens=1,
            output_tokens=4000,
            cost_usd=0.0,
            latency_ms=900,
            status_code=200,
            request_id="req-cache-hit",
            cache_read_input_tokens=65000,
            cache_creation_input_tokens=5000,
        )

        log = (await db_session.execute(select(UsageLog).where(UsageLog.request_id == "req-cache-hit"))).scalar_one()
        assert log.cache_read_input_tokens == 65000
        assert log.cache_creation_input_tokens == 5000

    @pytest.mark.asyncio
    async def test_unreported_counters_are_null_not_zero(
        self,
        usage_service: UsageService,
        org_user_context: TokenContext,
        db_session: AsyncSession,
    ):
        """The core null-vs-zero assertion. A default of 0 here would break the metric."""
        await usage_service.log_request(
            context=org_user_context,
            model="claude-3-5-sonnet",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0,
            latency_ms=200,
            status_code=200,
            request_id="req-no-cache-info",
        )

        log = (await db_session.execute(select(UsageLog).where(UsageLog.request_id == "req-no-cache-info"))).scalar_one()
        assert log.cache_read_input_tokens is None
        assert log.cache_creation_input_tokens is None

    @pytest.mark.asyncio
    async def test_reported_zero_is_distinguishable_from_unreported(
        self,
        usage_service: UsageService,
        org_user_context: TokenContext,
        db_session: AsyncSession,
    ):
        """An explicit provider-reported 0 must store as 0, not collapse to NULL."""
        await usage_service.log_request(
            context=org_user_context,
            model="claude-opus-4.6",
            input_tokens=100,
            output_tokens=50,
            cost_usd=0.0,
            latency_ms=200,
            status_code=200,
            request_id="req-cache-miss",
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        )

        log = (await db_session.execute(select(UsageLog).where(UsageLog.request_id == "req-cache-miss"))).scalar_one()
        assert log.cache_read_input_tokens == 0
        assert log.cache_creation_input_tokens == 0


class TestCacheTokensFromUsage:
    """The raw provider usage dict is the only null-preserving source."""

    def test_absent_keys_yield_none(self):
        assert ProxyService._cache_tokens_from_usage({"input_tokens": 100, "output_tokens": 50}) == (None, None)

    def test_present_keys_yield_values(self):
        usage = {
            "input_tokens": 1,
            "output_tokens": 4000,
            "cache_read_input_tokens": 65000,
            "cache_creation_input_tokens": 5000,
        }
        assert ProxyService._cache_tokens_from_usage(usage) == (65000, 5000)

    def test_reported_zero_is_not_none(self):
        """`.get()` with no default — a reported 0 must survive as 0."""
        usage = {"cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        assert ProxyService._cache_tokens_from_usage(usage) == (0, 0)

    def test_partial_report_preserves_the_gap(self):
        """Read reported, creation absent — typical for a cache-hit turn."""
        assert ProxyService._cache_tokens_from_usage({"cache_read_input_tokens": 65000}) == (65000, None)


class TestStreamingCacheTokenNullSemantics:
    """The SSE accumulator must not turn a reported 0 into "unknown".

    The pre-#4180 extractor gated on truthiness, so a provider explicitly
    reporting 0 cache tokens was discarded and the row landed NULL — meaning
    "unknown" when the provider had in fact told us "no cache activity".
    """

    def _service(self) -> ProxyService:
        from unittest.mock import MagicMock

        service = ProxyService.__new__(ProxyService)
        service._pool_service = MagicMock()
        service._translator = MagicMock()
        service._model_resolver = MagicMock()
        service._stream_handler = MagicMock()
        return service

    def test_reported_zero_is_recorded(self):
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        chunk = (
            b'data: {"type": "message_start", "message": {"usage": '
            b'{"input_tokens": 100, "cache_read_input_tokens": 0, '
            b'"cache_creation_input_tokens": 0}}}\n\n'
        )

        self._service()._extract_usage_from_sse_chunk(chunk, usage)

        assert usage["cache_read_input_tokens"] == 0
        assert usage["cache_creation_input_tokens"] == 0

    def test_absent_stays_absent(self):
        """Absent keys must not be materialised — .get() later yields None."""
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        chunk = b'data: {"type": "message_start", "message": {"usage": {"input_tokens": 100}}}\n\n'

        self._service()._extract_usage_from_sse_chunk(chunk, usage)

        assert usage.get("cache_read_input_tokens") is None
        assert usage.get("cache_creation_input_tokens") is None


class TestCacheHitRateQuery:
    """The metric the whole issue exists to make possible.

    Note on the denominator: Anthropic reports ``input_tokens`` EXCLUDING cache
    read/creation tokens, so total prompt tokens is the sum of all three. Dividing
    cached tokens by ``input_tokens`` alone can exceed 100%.
    """

    @pytest.mark.asyncio
    async def test_hit_rate_is_queryable(self, db_session: AsyncSession):
        now = datetime.now(UTC)
        common = {
            "org_id": "org-001",
            "department_id": "dept-001",
            "team_id": "team-001",
            "user_id": "user-001",
            "account_type": "human",
            "model": "claude-opus-4.6",
            "cost_usd": Decimal("0.01"),
            "latency_ms": 100,
            "status_code": 200,
        }
        db_session.add_all(
            [
                # Turn 1: writes the cache.
                UsageLog(
                    id="cache-001",
                    input_tokens=100,
                    output_tokens=50,
                    cache_read_input_tokens=0,
                    cache_creation_input_tokens=700,
                    timestamp=now - timedelta(minutes=30),
                    **common,
                ),
                # Turn 2: reads it back.
                UsageLog(
                    id="cache-002",
                    input_tokens=100,
                    output_tokens=50,
                    cache_read_input_tokens=700,
                    cache_creation_input_tokens=0,
                    timestamp=now - timedelta(minutes=20),
                    **common,
                ),
                # A non-reporting row: must not be counted as zero cache activity.
                UsageLog(
                    id="cache-003",
                    input_tokens=100,
                    output_tokens=50,
                    timestamp=now - timedelta(minutes=10),
                    **common,
                ),
            ]
        )
        await db_session.commit()

        total_prompt = (
            UsageLog.input_tokens + func.coalesce(UsageLog.cache_read_input_tokens, 0) + func.coalesce(UsageLog.cache_creation_input_tokens, 0)
        )
        row = (
            await db_session.execute(
                select(
                    func.sum(UsageLog.cache_read_input_tokens).label("cached"),
                    func.sum(UsageLog.cache_creation_input_tokens).label("written"),
                    func.sum(total_prompt).label("total_prompt"),
                ).where(UsageLog.org_id == "org-001", UsageLog.timestamp > now - timedelta(hours=2))
            )
        ).one()

        assert row.cached == 700
        assert row.written == 700
        # 300 raw input + 700 read + 700 written; the NULL row contributes only
        # its input_tokens, which is exactly the point of COALESCE-on-read.
        assert row.total_prompt == 1700
        assert round(100.0 * row.cached / row.total_prompt, 1) == 41.2
