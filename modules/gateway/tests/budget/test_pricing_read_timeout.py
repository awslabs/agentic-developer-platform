"""A stalled database read cannot stall completed-request metering indefinitely."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest

from pricing_policy import RoutingEvidence, load_snapshot
from pricing_policy.storage import ActiveGeneration, V2RateCache
from src.budget import pricing_decisions, pricing_v2_reader


@pytest.mark.asyncio
@pytest.mark.parametrize("completion", [False, True])
async def test_stalled_read_times_out_retains_generation_and_releases_lock(monkeypatch, capsys, completion):
    snapshot = load_snapshot()
    generation = ActiveGeneration(7, 9, snapshot.snapshot_version, 1, snapshot.rates, "2026-09-12T00:00:00+00:00")
    # Exercise the stalled refresh even when the runner uptime is below the cache TTL.
    cache = V2RateCache(ttl_seconds=0)
    cache.record_success(generation, monotonic=time.monotonic())
    monkeypatch.setattr(pricing_v2_reader, "_cache", cache)
    monkeypatch.setattr(pricing_v2_reader, "_refresh_lock", asyncio.Lock())
    cancelled = asyncio.Event()

    async def stalled(session):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(pricing_v2_reader, "_fetch_active_generation", stalled)
    monkeypatch.setattr(pricing_decisions, "PRICING_READ_TIMEOUT_SECONDS", 0.01)

    def factory():
        return AsyncMock()

    monkeypatch.setattr(pricing_decisions, "get_session_factory", lambda: factory)
    if completion:
        operation = pricing_decisions.price_completed_usage(
            request_id="stalled",
            org_id="tenant",
            session_factory=factory,
            raw_usage={"input_tokens": 1000, "output_tokens": 1000, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            evidence=RoutingEvidence(
                original_model_id="openai.gpt-5.6-sol",
                billing_model_id="openai.gpt-5.6-sol",
                geography="in_region",
                endpoint_region="us-east-1",
                served_service_tier_raw="standard",
            ),
        )
    else:
        operation = pricing_decisions.refresh_pricing_cache(force=True)
    result = await asyncio.wait_for(operation, timeout=0.5)
    assert cancelled.is_set()
    assert result.generation_id == 7
    assert result.pointer_revision == 9
    assert cache.cache_failure_minutes(time.monotonic()) is not None
    emitted = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert emitted["PricingCacheRefreshFailure"] == 1
    assert emitted["PricingCacheAgeSeconds"] > 0
    assert emitted["_aws"]["CloudWatchMetrics"][0]["Namespace"] == "ADP/Gateway"
    assert emitted["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [[]]

    # The cancelled probe cannot leave the process refresh lock wedged.
    monkeypatch.setattr(pricing_v2_reader, "_fetch_active_generation", AsyncMock(return_value=generation))
    recovered = await asyncio.wait_for(pricing_v2_reader.get_rate_state(None, force=True), timeout=0.5)
    assert recovered.from_database
    assert pricing_v2_reader.cache_failure_age_seconds() == 0
