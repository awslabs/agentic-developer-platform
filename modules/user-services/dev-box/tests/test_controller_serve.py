"""The controller's serve loop must create its AWS clients on the very first tick.

``time.monotonic()`` counts from an arbitrary origin (node boot on Linux). On a node younger than
the 40-minute client-refresh window the old ``refreshed_at = 0`` sentinel made the first tick skip
client creation, and every tick then died with ``UnboundLocalError`` on ``ec2`` (dev, 2026-10-18).
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from controller import worker as WORKER  # noqa: E402


class _Stop(BaseException):
    """Escapes the serve loop after the first tick without being swallowed by its except Exception."""


@pytest.mark.parametrize("monotonic_now", [100.0, 2399.0, 10_000.0])
def test_serve_creates_clients_on_the_first_tick_regardless_of_monotonic_origin(
    monkeypatch, monotonic_now
):
    ec2, ssm, run_store = object(), object(), object()
    seen = []

    async def fake_scan_once(
        session_factory,
        passed_ec2,
        passed_ssm,
        deployment_id,
        launch_template,
        worker_id,
        *,
        run_store=None,
        fence=None,
    ):
        seen.append((passed_ec2, passed_ssm, run_store))
        return 0

    async def stop_sleep(_interval):
        raise _Stop

    monkeypatch.setattr(WORKER.time, "monotonic", lambda: monotonic_now)
    monkeypatch.setattr(WORKER, "scan_once", fake_scan_once)
    monkeypatch.setattr(WORKER.asyncio, "sleep", stop_sleep)

    with pytest.raises(_Stop):
        asyncio.run(
            WORKER.serve(
                object(),
                lambda: (ec2, ssm, run_store),
                "adp-test-dev-box",
                object(),
                interval=0,
            )
        )

    assert seen == [(ec2, ssm, run_store)], (
        "the first tick must run with freshly created clients"
    )
