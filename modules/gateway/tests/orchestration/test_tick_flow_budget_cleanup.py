"""Warm ticks keep Redis balances while retiring clients before loop teardown."""

import asyncio
import threading
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import fakeredis
import pytest
import redis

from src.budget.config import budget_config
from src.orchestration import flow_budget, tick_handler
from src.orchestration.flow_meter import meter_target, read_flow_meter
from src.orchestration.tick import TickReport


class TickFailureError(RuntimeError):
    pass


@pytest.fixture
def tcp_meter(monkeypatch):
    # The production redis.asyncio TCP streams, unlike an in-memory FakeRedis
    # client, retain their event-loop identity and reproduce warm Lambda reuse.
    server = fakeredis.TcpFakeServer(("127.0.0.1", 0))
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    monkeypatch.setenv("BG_REDIS_URL", f"redis://127.0.0.1:{port}/0")
    monkeypatch.setenv("BG_REDIS_IAM_AUTH", "false")
    monkeypatch.setattr(budget_config, "budget_reservation_enabled", True)
    monkeypatch.setattr(budget_config, "budget_reservation_backend", "redis")
    monkeypatch.setattr(flow_budget, "_reservations", None)
    policy = SimpleNamespace(limits=SimpleNamespace(max_spend_usd=Decimal("250")))
    target = meter_target(org_id="org-warm-tick", flow_id="flow-warm-tick", policy=policy)
    deadline = time.time() + 3600
    entries = {"__initialized__": f"0:{deadline}", "settled-call": f"12.75:{deadline}"}
    client = redis.Redis(host="127.0.0.1", port=port, decode_responses=True)
    client.hset(target.key(), mapping=entries)
    try:
        yield policy, target, client, entries
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("first_tick_fails", [False, True])
def test_warm_ticks_preserve_nonzero_meter_across_event_loops(tcp_meter, monkeypatch, first_tick_fails):
    policy, target, client, entries = tcp_meter
    loops, stores, snapshots = [], [], []
    original_error = TickFailureError("original tick failure")

    async def run():
        loops.append(asyncio.get_running_loop())
        stores.append(flow_budget.get_flow_reservations())
        snapshots.append(await read_flow_meter(org_id="org-warm-tick", flow_id="flow-warm-tick", policy=policy))
        if first_tick_fails and len(loops) == 1:
            raise original_error
        return TickReport()

    monkeypatch.setattr(tick_handler, "_run", run)
    monkeypatch.setattr(tick_handler, "_emit_metrics", lambda report: None)
    if first_tick_fails:
        with pytest.raises(TickFailureError) as raised:
            tick_handler.handler()
        assert raised.value is original_error
    else:
        assert tick_handler.handler()["status"] == "ok"
    assert tick_handler.handler()["status"] == "ok"

    assert loops[0] is not loops[1] and all(loop.is_closed() for loop in loops)
    assert [snapshot.total_usd for snapshot in snapshots] == [Decimal("12.75"), Decimal("12.75")]
    assert stores[0] is not stores[1]
    assert all(store._client is None for store in stores)
    assert flow_budget._reservations is None
    assert client.hgetall(target.key()) == entries


@pytest.mark.parametrize("tick_fails", [False, True])
def test_cleanup_failure_preserves_tick_outcome_and_discards_client(monkeypatch, caplog, tick_fails):
    original_error = TickFailureError("original tick failure")

    async def run():
        if tick_fails:
            raise original_error
        return TickReport()

    store = SimpleNamespace(close=AsyncMock(side_effect=RuntimeError("cleanup failure")))
    monkeypatch.setattr(flow_budget, "_reservations", store)
    monkeypatch.setattr(tick_handler, "_run", run)
    monkeypatch.setattr(tick_handler, "_emit_metrics", lambda report: None)
    if tick_fails:
        with pytest.raises(TickFailureError) as raised:
            tick_handler.handler()
        assert raised.value is original_error
    else:
        assert tick_handler.handler()["status"] == "ok"
    assert flow_budget._reservations is None
    store.close.assert_awaited_once()
    assert "local flow budget client cleanup failed" in caplog.text
