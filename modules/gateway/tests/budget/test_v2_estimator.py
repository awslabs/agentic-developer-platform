"""Budget quotes follow the process's active V2 generation without inference I/O."""

from dataclasses import replace
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from pricing_policy import load_snapshot
from pricing_policy.storage import ActiveGeneration, RateSourceState, V2DisabledError, V2RateCache
from src.budget import pricing_v2_reader
from src.budget.pricing import PricingService
from src.budget.service import BudgetService
from src.budget.utils import calculate_model_cost
from src.shared.schemas.budget import CostCalculationRequest


def generation(multiplier=1, *, revision=1):
    rows = tuple(
        replace(
            row,
            generation_id=revision,
            input_price_per_1k_tokens=row.input_price_per_1k_tokens * multiplier,
            output_price_per_1k_tokens=row.output_price_per_1k_tokens * multiplier,
            cache_read_price_per_1k_tokens=None if row.cache_read_price_per_1k_tokens is None else row.cache_read_price_per_1k_tokens * multiplier,
            cache_write_price_per_1k_tokens=None if row.cache_write_price_per_1k_tokens is None else row.cache_write_price_per_1k_tokens * multiplier,
        )
        for row in load_snapshot().rows_for_model("openai.gpt-5.6-sol")
    )
    return ActiveGeneration(revision, revision, load_snapshot().snapshot_version, 1, rows, "2026-09-12T00:00:00+00:00")


def test_sync_estimators_follow_publication_and_explicit_disable_without_io(monkeypatch):
    cache = V2RateCache()
    monkeypatch.setattr(pricing_v2_reader, "cached_rate_state", lambda: cache.state(monotonic=0, now_iso="2026-09-12T00:00:00+00:00"))
    service = PricingService()
    cache.record_success(generation(), monotonic=0)
    assert service.calculate_cost("openai.gpt-5.6-sol", 1000, 1000) == Decimal("0.026400")
    cache.record_success(generation(2, revision=2), monotonic=0)
    assert service.calculate_cost("openai.gpt-5.6-sol", 1000, 1000) == Decimal("0.052800")
    assert calculate_model_cost("openai.gpt-5.6-sol", 1000, 1000)[0] == Decimal("0.052800")
    cache.record_failure(V2DisabledError("disabled", pointer_revision=3), monotonic=0)
    assert service.calculate_cost("openai.gpt-5.6-sol", 1000, 1000) == Decimal("0.026400")


@pytest.mark.asyncio
async def test_public_quote_refreshes_through_async_reader(monkeypatch):
    value = generation(2)
    state = RateSourceState(value.rows, "v2_generation:1", 1, 1, from_database=True)
    fetch = AsyncMock(return_value=state)
    monkeypatch.setattr(pricing_v2_reader, "get_rate_state", fetch)
    service = BudgetService(db_session=AsyncMock())
    quote = await service.calculate_cost(CostCalculationRequest(model_name="openai.gpt-5.6-sol", tokens_in=1000, tokens_out=1000))
    fetch.assert_awaited_once()
    assert quote.cost_usd == Decimal("0.052800")
    assert quote.input_cost_per_1k_tokens == Decimal("0.0088")
    assert quote.output_cost_per_1k_tokens == Decimal("0.044")
