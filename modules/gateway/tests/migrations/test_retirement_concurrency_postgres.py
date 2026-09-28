import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.admin.persona_models.retirement import RetirementCandidate, claim_retirement, mark_delivered, mark_failed
from tests.migrations.conftest_postgres import to_async_url, upgrade


@pytest.mark.asyncio
async def test_real_pg_concurrent_claims_and_expired_lease_fencing(pg_url):
    upgrade(pg_url, "head")
    engine = create_async_engine(to_async_url(pg_url))
    factory = async_sessionmaker(engine, expire_on_commit=False)
    candidate = RetirementCandidate(
        preference_id="pref",
        org_id="tenant",
        persona_key="architect",
        owner_kind="service_account",
        owner_id="owner",
        model_id="retired-model",
        lifecycle_revision="revision",
    )
    now = datetime.now(UTC)
    try:
        claims = await asyncio.gather(*(claim_retirement(factory, candidate, now=now, lease_seconds=1) for _ in range(12)))
        winners = [c for c in claims if c is not None]
        assert len(winners) == 1
        first = winners[0]
        retry_time = now + timedelta(seconds=2)
        retries = await asyncio.gather(*(claim_retirement(factory, candidate, now=retry_time, lease_seconds=1) for _ in range(12)))
        winners = [c for c in retries if c is not None]
        assert len(winners) == 1
        retry = winners[0]
        assert retry.retry and retry.claim_token != first.claim_token
        assert not await mark_delivered(factory, first, now=retry_time)
        assert not await mark_failed(factory, first, RuntimeError("stale"), now=retry_time)
        assert await mark_delivered(factory, retry, now=retry_time)
        assert await claim_retirement(factory, candidate, now=retry_time + timedelta(seconds=2)) is None
    finally:
        await engine.dispose()
