"""
Unit tests for the bounded fail-open grace window (Issue #4075).

The window is the only thing standing between "fail-closed" and a total
inference outage on every transient DB blip, so its boundary behaviour is
asserted directly rather than only through the enforcement service.

All timing uses an injected clock — never ``sleep`` — so the allow→deny
transition is asserted deterministically.
"""

import pytest

from src.budget.grace_window import _FALLBACK_GRACE_SECONDS, GraceWindow


class TestInProcessWindow:
    """Behaviour with no Redis configured (local dev / Redis-down fallback)."""

    @pytest.mark.asyncio
    async def test_first_failure_is_inside_the_window(self):
        now = [100.0]
        window = GraceWindow(grace_seconds=30, redis_url=None, clock=lambda: now[0])

        assert await window.register_failure() is True

    @pytest.mark.asyncio
    async def test_failure_past_the_bound_is_outside_the_window(self):
        """The window must actually expire — an unbounded one is fail-open."""
        now = [100.0]
        window = GraceWindow(grace_seconds=30, redis_url=None, clock=lambda: now[0])

        assert await window.register_failure() is True
        now[0] += _FALLBACK_GRACE_SECONDS + 1
        assert await window.register_failure() is False

    @pytest.mark.asyncio
    async def test_fallback_bound_is_shorter_than_the_configured_window(self):
        """Per-process fallback stays short because it runs in all 8 processes.

        With 2 replicas x 4 uvicorn workers each keeping its own fallback
        window, aggregate exposure is ~8x this bound — so it must not inherit
        the full 30s.
        """
        now = [100.0]
        window = GraceWindow(grace_seconds=300, redis_url=None, clock=lambda: now[0])

        assert await window.register_failure() is True
        now[0] += _FALLBACK_GRACE_SECONDS + 1
        assert await window.register_failure() is False, "the fallback must cap at _FALLBACK_GRACE_SECONDS, not grace_seconds"

    @pytest.mark.asyncio
    async def test_zero_grace_denies_on_the_first_failure(self):
        """grace_seconds=0 disables grace entirely."""
        window = GraceWindow(grace_seconds=0, redis_url=None, clock=lambda: 100.0)

        assert await window.register_failure() is False

    @pytest.mark.asyncio
    async def test_clear_resets_the_streak(self):
        """The window tracks CONSECUTIVE failures, not lifetime failures.

        Without a reset on recovery, blips hours apart accumulate into one
        long-expired streak and the first failure after a healthy week denies
        outright.
        """
        now = [100.0]
        window = GraceWindow(grace_seconds=30, redis_url=None, clock=lambda: now[0])

        assert await window.register_failure() is True
        now[0] += _FALLBACK_GRACE_SECONDS + 1
        assert await window.register_failure() is False

        await window.clear()
        assert await window.register_failure() is True


class TestRedisUnavailableFallback:
    """Redis and RDS share a VPC and can fail together."""

    @pytest.mark.asyncio
    async def test_unreachable_redis_falls_back_instead_of_raising(self):
        """A dead Redis must not break the failure path that keeps us safe.

        If this raised, the exception would escape the enforcement service's
        handler and the request would fail in an undefined way — so the
        grace path must never depend on a second store being healthy.
        """
        now = [100.0]
        window = GraceWindow(
            grace_seconds=30,
            redis_url="redis://127.0.0.1:1/0",  # nothing listening
            clock=lambda: now[0],
        )

        assert await window.register_failure() is True  # short fallback window
        now[0] += _FALLBACK_GRACE_SECONDS + 1
        assert await window.register_failure() is False, "the fallback window must still be bounded"

    @pytest.mark.asyncio
    async def test_clear_with_unreachable_redis_does_not_raise(self):
        window = GraceWindow(grace_seconds=30, redis_url="redis://127.0.0.1:1/0", clock=lambda: 100.0)

        await window.register_failure()
        await window.clear()  # must not raise
