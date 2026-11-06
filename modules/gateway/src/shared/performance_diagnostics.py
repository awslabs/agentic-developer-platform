"""Optional, short-lived PostgreSQL lock observation outside the serving pool."""

import asyncio
import logging
from dataclasses import dataclass
from typing import Literal

import asyncpg
from prometheus_client import Counter

_BLOCKER_QUERY = """
SELECT waiter.pid, pg_blocking_pids(waiter.pid) AS blocker_pids,
       GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - waiter.query_start)) * 1000) AS query_age_ms,
       GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - waiter.xact_start)) * 1000) AS waiter_xact_age_ms,
       waiter.query_id AS waiter_query_id,
       (SELECT GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - blocker.xact_start)) * 1000)
        FROM pg_stat_activity AS blocker
        WHERE blocker.pid = ANY(pg_blocking_pids(waiter.pid))
        ORDER BY blocker.xact_start NULLS LAST LIMIT 1) AS blocker_xact_age_ms,
       ARRAY(SELECT blocker.query_id FROM pg_stat_activity AS blocker
             WHERE blocker.pid = ANY(pg_blocking_pids(waiter.pid)) ORDER BY blocker.pid) AS blocker_query_ids
FROM pg_stat_activity AS waiter
WHERE waiter.datname = current_database()
  AND waiter.wait_event_type = 'Lock'
  AND cardinality(pg_blocking_pids(waiter.pid)) > 0
ORDER BY waiter.query_start
LIMIT 100
"""
_capture_lock = asyncio.Lock()
_pressure_task: asyncio.Task[None] | None = None
_pressure_finished: asyncio.Event | None = None
_pressure_started = False
DIAGNOSTIC_DROPS = Counter("bedrock_gateway_diagnostic_drops_total", "Optional diagnostic observations shed when busy")
DIAGNOSTIC_FAILURES = Counter("bedrock_gateway_diagnostic_failures_total", "Optional diagnostic captures that failed")
DIAGNOSTIC_TRIGGER_SECONDS = 1.0
logger = logging.getLogger(__name__)


def get_diagnostic_health() -> dict[str, float]:
    """Return process-local optional capture losses, without request data."""
    return {
        "diagnostic_drops": DIAGNOSTIC_DROPS._value.get(),
        "diagnostic_failures": DIAGNOSTIC_FAILURES._value.get(),
    }


@dataclass(frozen=True)
class Blocker:
    waiter_pid: int
    blocker_pids: tuple[int, ...]
    query_age_ms: int
    waiter_xact_age_ms: int | None = None
    blocker_xact_age_ms: int | None = None
    waiter_query_id: int | None = None
    blocker_query_ids: tuple[int | None, ...] = ()


@dataclass(frozen=True)
class BlockerSample:
    status: Literal["captured", "disabled", "busy", "failed"]
    blockers: tuple[Blocker, ...] = ()


def start_pressure_capture(diagnostic_dsn: str | None, request_id: str) -> asyncio.Event | None:
    """Observe one still-active request at a time; never wait in its serving path."""
    global _pressure_task, _pressure_finished, _pressure_started
    if not diagnostic_dsn:
        return None
    if _pressure_task is not None and not _pressure_task.done():
        DIAGNOSTIC_DROPS.inc()
        return None

    finished = asyncio.Event()
    _pressure_finished = finished
    _pressure_started = False

    async def observe() -> None:
        global _pressure_started
        try:
            await asyncio.wait_for(finished.wait(), timeout=DIAGNOSTIC_TRIGGER_SECONDS)
            return
        except TimeoutError:
            pass
        _pressure_started = True
        sample = await capture_blockers(diagnostic_dsn)
        if sample.blockers or sample.status == "failed":
            logger.warning(
                "gateway database blocker observation",
                extra={
                    "request_id": request_id,
                    "diagnostic_status": sample.status,
                    "blocker_graph": [
                        {
                            "waiter_pid": row.waiter_pid,
                            "blocker_pids": row.blocker_pids,
                            "query_age_ms": row.query_age_ms,
                            "waiter_xact_age_ms": row.waiter_xact_age_ms,
                            "blocker_xact_age_ms": row.blocker_xact_age_ms,
                            "waiter_query_id": row.waiter_query_id,
                            "blocker_query_ids": row.blocker_query_ids,
                        }
                        for row in sample.blockers
                    ],
                },
            )

    _pressure_task = asyncio.create_task(observe())
    return finished


def finish_pressure_capture(finished: asyncio.Event | None) -> None:
    """Cancel an unstarted watcher; let an active, bounded sample finish."""
    global _pressure_task, _pressure_finished
    if finished is None:
        return
    finished.set()
    if finished is _pressure_finished and not _pressure_started and _pressure_task is not None:
        _pressure_task.cancel()
        _pressure_task = None
        _pressure_finished = None


async def capture_blockers(diagnostic_dsn: str | None) -> BlockerSample:
    """Read active lock waits using only an explicitly supplied diagnostic role.

    The connection is never borrowed from the serving engine. No query text,
    parameters, database URL, tenant or request values enter the result.
    """
    if not diagnostic_dsn:
        return BlockerSample("disabled")
    if _capture_lock.locked():
        DIAGNOSTIC_DROPS.inc()
        return BlockerSample("busy")

    async with _capture_lock:
        connection = None
        try:
            async with asyncio.timeout(0.5):
                connection = await asyncpg.connect(
                    dsn=diagnostic_dsn,
                    timeout=0.5,
                    server_settings={
                        "application_name": "gateway-diagnostics",
                        "statement_timeout": "500",
                        "default_transaction_read_only": "on",
                    },
                )
                rows = await connection.fetch(_BLOCKER_QUERY)
                return BlockerSample(
                    "captured",
                    tuple(
                        Blocker(
                            int(row["pid"]),
                            tuple(int(pid) for pid in row["blocker_pids"][:100]),
                            int(row["query_age_ms"]),
                            int(row["waiter_xact_age_ms"]) if row.get("waiter_xact_age_ms") is not None else None,
                            int(row["blocker_xact_age_ms"]) if row.get("blocker_xact_age_ms") is not None else None,
                            int(row["waiter_query_id"]) if row.get("waiter_query_id") is not None else None,
                            tuple(int(query_id) if query_id is not None else None for query_id in row.get("blocker_query_ids", ())[:100]),
                        )
                        for row in rows[:100]
                    ),
                )
        except Exception:
            DIAGNOSTIC_FAILURES.inc()
            return BlockerSample("failed")
        finally:
            if connection is not None:
                try:
                    connection.terminate()
                except Exception:
                    pass
