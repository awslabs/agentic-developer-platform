"""psycopg2 adapter for reading the active V2 pricing generation (§4.1, S3).

The policy — which SQLSTATEs mean "schema absent", how long a gap is cached, when a
read failure keeps the old rows — lives in ``pricing_policy.storage`` and is shared
with the gateway's async reader. This file adds only the psycopg2 specifics:
issuing the two statements, and turning ``psycopg2.Error.pgcode`` into the right
unavailability class.

Transaction safety is the reason this is not three lines inline in the handler.
PostgreSQL aborts the entire transaction when a statement fails, so every
subsequent statement on that connection returns ``25P02 in_failed_sql_transaction``
until someone rolls back. A pricing probe against a pre-044 database would
therefore poison the connection the tracker is about to write ``budget_usage``
with — turning "this deployment has no V2 rates yet" into "no usage is metered at
all". Every read path here uses a SAVEPOINT so a failed probe rolls back to exactly
where it started and the caller's transaction survives intact.
"""

from __future__ import annotations

import logging
import time
from typing import Any

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

logger = logging.getLogger(__name__)

# Static SQL for savepoint management — these must never interpolate external
# input.  Using literal strings (not f-strings) so static-analysis tools can
# confirm no injection vector exists.  The name is safe because the probe never
# nests inside itself: it is released or rolled back before returning.
_SQL_SAVEPOINT = "SAVEPOINT pricing_v2_probe"
_SQL_ROLLBACK = "ROLLBACK TO SAVEPOINT pricing_v2_probe"
_SQL_RELEASE = "RELEASE SAVEPOINT pricing_v2_probe"

_cache = V2RateCache()
_metrics = ReaderMetrics()


def _sqlstate(exc: Exception) -> str | None:
    """The SQLSTATE from a psycopg2 error, if it carries one.

    ``pgcode`` is None for client-side failures (a dead socket, a DNS error), which
    ``classify_sqlstate`` correctly treats as a query failure rather than a missing
    schema — those must not demote the process to bundled rates.
    """
    return getattr(exc, "pgcode", None)


def _fetch_active_generation(conn) -> ActiveGeneration:
    """Read the active pointer and its rows, or raise a ``V2UnavailableError``.

    Wrapped in a SAVEPOINT rather than a bare try/except: without it, a 42P01 on a
    pre-044 database leaves the caller's transaction aborted, and the tracker's
    subsequent ``budget_usage`` upsert on the same connection fails with 25P02. The
    rollback is unconditional in the failure path for exactly that reason.
    """
    with conn.cursor() as cur:
        cur.execute(_SQL_SAVEPOINT)
        try:
            cur.execute(SQL_ACTIVE_POINTER)
            pointer_row = cur.fetchone()
            columns = [desc[0] for desc in cur.description]
            pointer = dict(zip(columns, pointer_row, strict=True)) if pointer_row else None

            rate_rows: list[dict[str, Any]] = []
            if pointer and pointer.get("consumers_enabled") is True and pointer.get("current_generation_id") is not None:
                cur.execute(SQL_ACTIVE_RATES, {"generation_id": pointer["current_generation_id"]})
                rate_columns = [desc[0] for desc in cur.description]
                rate_rows = [dict(zip(rate_columns, row, strict=True)) for row in cur.fetchall()]
        except Exception as exc:
            # Roll back to the savepoint BEFORE classifying or logging: the
            # connection must be usable again no matter which branch we take, and
            # an exception raised while still in the aborted state would leave the
            # caller holding a dead transaction.
            cur.execute(_SQL_ROLLBACK)
            cur.execute(_SQL_RELEASE)
            failure = classify_sqlstate(_sqlstate(exc))
            if failure is MissingV2SchemaError:
                _metrics.schema_missing += 1
                logger.info("V2 pricing schema not present (%s); using bundled rates until migration 044 lands", _sqlstate(exc))
            else:
                _metrics.query_failed += 1
                logger.warning("V2 pricing read failed (%s): %s", _sqlstate(exc), exc)
            raise failure(str(exc)) from exc
        else:
            cur.execute(_SQL_RELEASE)

    # Outside the except block, so a V2DisabledError from here is not misreported as
    # a query failure and does not need the savepoint dance — nothing failed.
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


def get_rate_state(conn, *, force: bool = False) -> RateSourceState:
    """The rows to price with on this invocation, plus the reasons they are degraded.

    Never raises for an unavailable generation: a Lambda that cannot read V2 must
    still settle the record in front of it, using the last known good rows if it
    ever had any. Explicit disable selects bundled compatibility while retaining
    the last good generation internally.
    """
    monotonic = time.monotonic()
    if force or _cache.needs_refresh(monotonic):
        try:
            generation = _fetch_active_generation(conn)
        except Exception as exc:
            _cache.record_failure(exc, monotonic=time.monotonic())
        else:
            _cache.record_success(generation, monotonic=time.monotonic())

    state = _cache.state(monotonic=time.monotonic(), now_iso=utc_now_iso())
    if state.from_database:
        _metrics.served_from_cache += 1
    else:
        _metrics.served_from_bundle += 1
    return state


def cache_failure_age_seconds() -> float:
    return max(0.0, (_cache.cache_failure_minutes(time.monotonic()) or 0.0) * 60.0)


def reader_metrics() -> dict[str, int]:
    """Counters for this container, for the handler to publish."""
    return _metrics.as_dict()


def reset_for_tests() -> None:
    """Drop cached state. Tests only — a container never wants this."""
    global _cache, _metrics
    _cache = V2RateCache()
    _metrics = ReaderMetrics()


__all__ = [
    "MissingV2SchemaError",
    "V2QueryFailedError",
    "get_rate_state",
    "reader_metrics",
    "reset_for_tests",
]
