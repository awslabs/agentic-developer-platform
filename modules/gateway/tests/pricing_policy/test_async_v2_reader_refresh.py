"""Concurrent requests share one V2 refresh instead of stampeding the database."""

import asyncio

import pytest

from pricing_policy import load_snapshot
from pricing_policy.storage import ActiveGeneration, MissingV2SchemaError, V2QueryFailedError
from src.budget import pricing_v2_reader as reader


@pytest.fixture(autouse=True)
def reset_reader():
    reader.reset_for_tests()
    yield
    reader.reset_for_tests()


def generation():
    snapshot = load_snapshot()
    return ActiveGeneration(
        generation_id=7,
        pointer_revision=3,
        snapshot_version=snapshot.snapshot_version,
        policy_version=1,
        rows=snapshot.rates,
        loaded_at="2026-09-12T00:00:00+00:00",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("failure", [None, MissingV2SchemaError("missing"), V2QueryFailedError("down")])
async def test_overlapping_requests_share_one_refresh(monkeypatch, force, failure):
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fetch(session):
        calls.append(session)
        entered.set()
        await release.wait()
        if failure:
            raise failure
        return generation()

    monkeypatch.setattr(reader, "_fetch_active_generation", fetch)
    first = asyncio.create_task(reader.get_rate_state("first", force=force))
    await entered.wait()
    rest = [asyncio.create_task(reader.get_rate_state(str(index), force=force)) for index in range(20)]
    await asyncio.sleep(0)  # Let all callers observe the in-flight refresh.
    assert calls == ["first"]
    release.set()
    states = await asyncio.gather(first, *rest)
    assert calls == ["first"]
    assert all(state.from_database is (failure is None) for state in states)


@pytest.mark.asyncio
async def test_cancelled_refresh_releases_waiters_and_uses_their_own_session(monkeypatch):
    entered = asyncio.Event()
    blocker = asyncio.Event()
    calls = []

    async def fetch(session):
        calls.append(session)
        if session == "cancelled-owner":
            entered.set()
            await blocker.wait()
        return generation()

    monkeypatch.setattr(reader, "_fetch_active_generation", fetch)
    first = asyncio.create_task(reader.get_rate_state("cancelled-owner"))
    await entered.wait()
    next_caller = asyncio.create_task(reader.get_rate_state("next-owner"))
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    result = await next_caller
    assert result.from_database
    assert calls == ["cancelled-owner", "next-owner"]
