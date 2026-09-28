"""Async SQLAlchemy adapter for the active V2 pricing generation (§4.1, S3).

Mirror of ``lambda/shared/pricing_v2_reader.py``. Both delegate every decision to
``pricing_policy.storage``; the only thing that differs is the driver, because no
import path exists between ``src/`` and ``lambda/`` and this reader must run inside
the gateway's async engine while that one runs on psycopg2.

The savepoint is not optional here either. An ``asyncpg``/SQLAlchemy session whose
statement fails is left in an aborted transaction, so a pricing probe against a
pre-044 database would break whatever the request handler does next on that
session. ``session.begin_nested()`` issues the SAVEPOINT and rolls back to it on
exception, keeping the outer transaction usable.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from pricing_policy.storage import (
    SQL_ACTIVE_POINTER,
    SQL_ACTIVE_RATES,
    ActiveGeneration,
    MissingV2SchemaError,
    RateSourceState,
    ReaderMetrics,
    V2DisabledError,
    V2QueryFailedError,
    V2RateCache,
    build_active_generation,
    classify_sqlstate,
    utc_now_iso,
)
from src.shared.logging import get_logger

logger = get_logger(__name__)

# Process-wide, like the psycopg2 side: the point of the cache is that a generation
# read once is retained across requests even if the database later becomes
# unreachable. A per-request cache would re-probe on every call and lose exactly
# the property this exists for.
_cache = V2RateCache()
_metrics = ReaderMetrics()
_refresh_lock = asyncio.Lock()
_refresh_serial = 0

# The shared SQL uses psycopg2's %(name)s placeholders. SQLAlchemy's text() wants
# :name, so translate here rather than keeping two copies of the statements —
# duplicated SQL is how the two readers would drift apart on which columns they
# require.
_SQL_ACTIVE_POINTER = text(SQL_ACTIVE_POINTER)
_SQL_ACTIVE_RATES = text(SQL_ACTIVE_RATES.replace("%(generation_id)s", ":generation_id"))


def _sqlstate(exc: BaseException) -> str | None:
    """Dig the SQLSTATE out of a SQLAlchemy wrapper.

    SQLAlchemy wraps the driver error, so ``pgcode`` is on ``__cause__`` /
    ``orig``. Returning None when it cannot be found is the safe default:
    ``classify_sqlstate`` then reports a query failure, which RETAINS cached rates
    rather than demoting to the bundle.
    """
    for candidate in (getattr(exc, "orig", None), exc.__cause__, exc):
        code = getattr(candidate, "pgcode", None) or getattr(candidate, "sqlstate", None)
        if code:
            return str(code)
    return None


async def _fetch_active_generation(session: AsyncSession) -> ActiveGeneration:
    """Read the pointer and its rows inside a savepoint."""
    try:
        async with session.begin_nested():
            pointer_result = await session.execute(_SQL_ACTIVE_POINTER)
            pointer_row = pointer_result.mappings().first()
            pointer: dict[str, Any] | None = dict(pointer_row) if pointer_row else None

            rate_rows: list[dict[str, Any]] = []
            if pointer and pointer.get("consumers_enabled") is True and pointer.get("current_generation_id") is not None:
                rates_result = await session.execute(_SQL_ACTIVE_RATES, {"generation_id": pointer["current_generation_id"]})
                rate_rows = [dict(row) for row in rates_result.mappings()]
    except (DBAPIError, SQLAlchemyError) as exc:
        # begin_nested() has already rolled back to the savepoint by the time this
        # runs, so `session` is usable again and the caller's transaction is intact.
        failure = classify_sqlstate(_sqlstate(exc))
        if failure is MissingV2SchemaError:
            _metrics.schema_missing += 1
            logger.info(f"V2 pricing schema not present ({_sqlstate(exc)}); using bundled rates until migration 044 lands")
        else:
            _metrics.query_failed += 1
            logger.warning(f"V2 pricing read failed ({_sqlstate(exc)}): {exc}")
        raise failure(str(exc)) from exc

    try:
        generation = build_active_generation(pointer=pointer, rate_rows=rate_rows, loaded_at=utc_now_iso())
    except V2DisabledError:
        _metrics.disabled += 1
        raise
    except (ValueError, V2QueryFailedError):
        _metrics.query_failed += 1
        raise
    _metrics.refreshed += 1
    _metrics.observed_generations.add(generation.generation_id)
    return generation


async def get_rate_state(session: AsyncSession, *, force: bool = False) -> RateSourceState:
    """The rows to price with, and the estimate reasons the read state implies.

    Does not raise for an unavailable generation. A request that has already been
    served must still be priced; refusing to price it would turn a pricing-read
    problem into a user-visible failure, and pricing it silently at bundled rates
    without a reason attached is what #4969 is about.

    The shared cache is synchronous, so this awaits the read itself and then hands
    the outcome to ``record_success``/``record_failure``. Those are the same code
    paths the psycopg2 adapter reaches, so both readers classify
    a schema gap, a disabled pointer and a hard failure identically.
    """
    global _refresh_serial
    monotonic = time.monotonic()
    observed_serial = _refresh_serial
    if force or _cache.needs_refresh(monotonic):
        async with _refresh_lock:
            # Concurrent callers wait for one refresh, then consume its result.
            # The serial also coalesces forced probes which overlapped that read.
            if observed_serial == _refresh_serial and (force or _cache.needs_refresh(time.monotonic())):
                try:
                    generation = await _fetch_active_generation(session)
                except Exception as exc:
                    _cache.record_failure(exc, monotonic=time.monotonic())
                else:
                    _cache.record_success(generation, monotonic=time.monotonic())
                _refresh_serial += 1

    state = _cache.state(monotonic=time.monotonic(), now_iso=utc_now_iso())
    if state.from_database:
        _metrics.served_from_cache += 1
    else:
        _metrics.served_from_bundle += 1
    return state


def reader_metrics() -> dict[str, int]:
    return _metrics.as_dict()


def reset_for_tests() -> None:
    """Drop cached state. Tests only."""
    global _cache, _metrics, _refresh_lock, _refresh_serial
    _cache = V2RateCache()
    _metrics = ReaderMetrics()
    _refresh_lock = asyncio.Lock()
    _refresh_serial = 0


__all__ = ["get_rate_state", "reader_metrics", "reset_for_tests"]


def cached_rate_state() -> RateSourceState:
    """Read the current generation without I/O, for synchronous estimators."""
    return _cache.state(monotonic=time.monotonic(), now_iso=utc_now_iso())


def cache_failure_age_seconds() -> float:
    return (_cache.cache_failure_minutes(time.monotonic()) or 0.0) * 60.0


def record_connection_failure(exc: Exception) -> None:
    _metrics.query_failed += 1
    _cache.record_failure(V2QueryFailedError(str(exc)), monotonic=time.monotonic())


def refresh_due() -> bool:
    return _cache.needs_refresh(time.monotonic())
