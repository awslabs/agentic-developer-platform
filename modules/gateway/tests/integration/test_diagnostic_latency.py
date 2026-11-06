"""Matched local timing of the optional request-path diagnostic watcher."""

import asyncio
import math
import random
import time
from unittest.mock import AsyncMock

import httpx
from fastapi import FastAPI

from src.shared import performance_diagnostics as diagnostics
from src.shared.middleware.logging_middleware import LoggingMiddleware


async def test_matched_local_diagnostics_on_off_latency(monkeypatch):
    """Measure loopback middleware work, not deployment overhead or a performance SLO."""
    connect = AsyncMock()
    monkeypatch.setattr(diagnostics.asyncpg, "connect", connect)
    monkeypatch.setattr(diagnostics, "DIAGNOSTIC_TRIGGER_SECONDS", 5)
    app = FastAPI()
    app.add_middleware(LoggingMiddleware)

    @app.get("/probe")
    async def probe():
        return {"ok": True}

    warmup_pairs = 16
    measured_pairs = 120
    latency_ns = {False: [], True: []}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test.invalid") as client:
        for pair in range(warmup_pairs + measured_pairs):
            for enabled in (False, True) if pair % 2 == 0 else (True, False):
                if enabled:
                    monkeypatch.setenv("BG_DIAGNOSTIC_DATABASE_URL", "postgresql://private@db.invalid/fixture")
                else:
                    monkeypatch.delenv("BG_DIAGNOSTIC_DATABASE_URL", raising=False)

                started = time.perf_counter_ns()
                response = await client.get("/probe")
                elapsed = time.perf_counter_ns() - started
                assert response.status_code == 200
                assert response.json() == {"ok": True}
                await asyncio.sleep(0)
                if pair >= warmup_pairs:
                    latency_ns[enabled].append(elapsed)

    connect.assert_not_awaited()
    assert len(latency_ns[False]) == len(latency_ns[True]) == measured_pairs

    def p95(samples):
        return sorted(samples)[math.ceil(len(samples) * 0.95) - 1]

    def relative_p95(indices):
        disabled = p95([latency_ns[False][index] for index in indices])
        enabled = p95([latency_ns[True][index] for index in indices])
        return (enabled / disabled - 1) * 100

    rng = random.Random(7035)
    bootstrap = sorted(relative_p95([rng.randrange(measured_pairs) for _ in range(measured_pairs)]) for _ in range(1000))
    estimate = relative_p95(range(measured_pairs))
    lower = bootstrap[math.ceil(len(bootstrap) * 0.025) - 1]
    upper = bootstrap[math.ceil(len(bootstrap) * 0.975) - 1]
    print(
        f"LOCAL LOOPBACK ONLY: {measured_pairs} balanced pairs; "
        f"p95 disabled={p95(latency_ns[False]) / 1_000_000:.3f} ms; "
        f"enabled={p95(latency_ns[True]) / 1_000_000:.3f} ms; "
        f"relative={estimate:+.1f}%; paired-bootstrap 95% CI [{lower:+.1f}%, {upper:+.1f}%]. "
        "No real database, model, export collector or concurrent campaign; interval excludes system-level uncertainty."
    )
