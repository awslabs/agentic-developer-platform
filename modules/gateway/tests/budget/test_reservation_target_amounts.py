"""Per-target reservation amounts — Issue #9536.

A Project meter holds a provider quote's full-context ceiling for an unknown
outcome, but the run, chain, flow and hierarchy caps admit the request-sized
estimate. Before ``ReservationTarget.reserve_usd`` the ceiling was pushed into
the one shared amount, so a 25 USD run cap refused a reviewer's first request
whose ceiling was 30 USD while nothing had been spent.

The Lua runs for real against ``fakeredis`` + ``lupa``: the per-target amount is
read inside the script, so mocking the store would mock the thing under test.
"""

from decimal import Decimal

import fakeredis.aioredis
import pytest

from src.budget.reservations import ReservationStore, ReservationTarget


def _target(entity_type: str, headroom: str, **overrides) -> ReservationTarget:
    return ReservationTarget(
        org_id="org-1",
        entity_type=entity_type,
        entity_id=f"{entity_type}-1",
        period_type="run",
        period_start="2026-11-04",
        headroom_usd=Decimal(headroom),
        **overrides,
    )


@pytest.fixture
def store():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    return ReservationStore(redis_url=None, ttl_seconds=120, clock=lambda: 1_000.0, client=client)


@pytest.mark.asyncio
async def test_a_target_with_its_own_amount_reserves_that_amount_only_for_itself(store):
    run = _target("run", "25")
    project = _target("project", "100", reserve_usd=Decimal("30"))

    outcome = await store.reserve("req-1", Decimal("0.50"), [run, project])

    assert outcome is not None and outcome.admitted
    assert await store.live_request_amounts(run) == {"req-1": Decimal("0.50")}
    assert await store.live_request_amounts(project) == {"req-1": Decimal("30")}


@pytest.mark.asyncio
async def test_the_shared_estimate_is_not_raised_to_another_targets_ceiling(store):
    """GATE: the pre-#9536 behaviour reserved 30 against the 25 USD run cap and denied."""
    run = _target("run", "25")
    project = _target("project", "100", reserve_usd=Decimal("30"))

    outcome = await store.reserve("req-1", Decimal("0.50"), [run, project])

    assert outcome is not None and outcome.admitted, "the run cap must admit the request-sized estimate"


@pytest.mark.asyncio
async def test_a_target_amount_above_its_own_headroom_denies_that_target(store):
    run = _target("run", "25")
    project = _target("project", "20", reserve_usd=Decimal("30"))

    outcome = await store.reserve("req-1", Decimal("0.50"), [run, project])

    assert outcome is not None and not outcome.admitted
    assert outcome.exhausted is project
    assert await store.live_request_amounts(run) is None, "a denial must leave no partial reservation"


@pytest.mark.asyncio
async def test_targets_without_an_override_still_share_the_estimate(store):
    first = _target("run", "25")
    second = _target("chain", "50")

    outcome = await store.reserve("req-1", Decimal("2"), [first, second])

    assert outcome is not None and outcome.admitted
    assert await store.live_request_amounts(first) == {"req-1": Decimal("2")}
    assert await store.live_request_amounts(second) == {"req-1": Decimal("2")}
