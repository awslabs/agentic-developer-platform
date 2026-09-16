"""Last-known-good cache semantics for the V2 readers (§4.1, S3).

These are the decisions that decide what a request is billed at when the database
is not answering, so they are tested directly rather than through either driver.
The two adapters supply a callable and translate an exception; everything asserted
here is what they both delegate to.

The monotonic clock is injected, not patched. Sleeping to cross a 900-second TTL
would make the suite unusably slow, and freezing ``time.monotonic`` globally would
affect unrelated code in the same process.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pricing_policy import CURRENT_SNAPSHOT_VERSION, EstimateReason, RateRow, load_snapshot
from pricing_policy.storage import (
    MISSING_SCHEMA_SQLSTATES,
    RATE_CACHE_TTL_SECONDS,
    SCHEMA_REPROBE_SECONDS,
    ActiveGeneration,
    MissingV2SchemaError,
    ReaderMetrics,
    V2DisabledError,
    V2QueryFailedError,
    V2RateCache,
    V2UnavailableError,
    build_active_generation,
    classify_sqlstate,
)

NOW = "2026-09-12T12:00:00+00:00"


def _row(model_id: str = "openai.gpt-5.6-sol", *, input_rate: str = "0.0044", verified_at: str = "2026-09-12T06:00:00+00:00") -> RateRow:
    return RateRow.from_mapping(
        {
            "model_id": model_id,
            "geography": "in_region",
            "service_tier": "standard",
            "context_tier": "short",
            "region": "us-east-1",
            "input_price_per_1k_tokens": input_rate,
            "output_price_per_1k_tokens": "0.022",
            "cache_read_price_per_1k_tokens": "0.00044",
            "cache_write_price_per_1k_tokens": "0.0055",
            "cache_write_policy": "full_rate",
            "source": "bulk_catalog",
            "source_url": "https://pricing.example/catalog",
            "source_content_sha256": "a" * 64,
            "verified_at": verified_at,
        }
    )


def _generation(generation_id: int = 7, *, pointer_revision: int = 3, rows: tuple[RateRow, ...] | None = None) -> ActiveGeneration:
    return ActiveGeneration(
        generation_id=generation_id,
        pointer_revision=pointer_revision,
        snapshot_version=CURRENT_SNAPSHOT_VERSION,
        policy_version=1,
        rows=rows if rows is not None else (_row(),),
        loaded_at=NOW,
    )


def _raise(exc: Exception):
    def _fetch() -> ActiveGeneration:
        raise exc

    return _fetch


# ---------------------------------------------------------------------------
# SQLSTATE classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sqlstate", sorted(MISSING_SCHEMA_SQLSTATES))
def test_only_missing_table_and_column_mean_schema_absent(sqlstate):
    assert classify_sqlstate(sqlstate) is MissingV2SchemaError


@pytest.mark.parametrize(
    "sqlstate",
    [
        None,  # a dead socket carries no SQLSTATE
        "42501",  # insufficient_privilege — the grant is missing, the schema is not
        "40001",  # serialization_failure — retryable, definitely not absent
        "57014",  # query_canceled
        "42601",  # syntax_error, i.e. we broke the SQL
        "25P02",  # in_failed_sql_transaction
        "08006",  # connection_failure
    ],
)
def test_every_other_sqlstate_is_a_query_failure(sqlstate):
    """Misclassifying these as "schema absent" is the dangerous direction.

    A permission error read as a missing table would send the whole fleet to
    bundled rates indefinitely while a perfectly good generation sat unread — and
    it would look like a normal pre-migration deployment in the logs.
    """
    assert classify_sqlstate(sqlstate) is V2QueryFailedError


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def test_bootstrap_uses_the_bundled_snapshot_and_says_so():
    cache = V2RateCache()
    state = cache.state(monotonic=0.0, now_iso=NOW)

    assert not state.from_database
    assert state.rows == load_snapshot().rates
    assert state.source == f"bundled_snapshot:{CURRENT_SNAPSHOT_VERSION}"
    assert EstimateReason.BOOTSTRAP_FALLBACK in state.reasons
    assert state.generation_id is None


def test_bootstrap_reason_clears_once_a_generation_is_read():
    cache = V2RateCache()
    cache.refresh(lambda: _generation(), monotonic=0.0)
    state = cache.state(monotonic=0.0, now_iso=NOW)

    assert state.from_database
    assert EstimateReason.BOOTSTRAP_FALLBACK not in state.reasons
    assert state.generation_id == 7
    assert state.source == "v2_generation:7"


# ---------------------------------------------------------------------------
# The core property: a failure never demotes rates that were already read
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        V2QueryFailedError("connection reset"),
        MissingV2SchemaError("table vanished"),
        RuntimeError("something nobody predicted"),
    ],
)
def test_no_failure_downgrades_a_loaded_generation_to_the_bundle(failure):
    """Retention is the whole point of the cache.

    Database rates are what the daily refresh verified against AWS publications;
    the bundled snapshot is by definition older. Falling back to it mid-outage
    would silently reprice live traffic — the #4969 failure mode.
    """
    cache = V2RateCache()
    cache.refresh(lambda: _generation(), monotonic=0.0)

    cache.refresh(_raise(failure), monotonic=RATE_CACHE_TTL_SECONDS + 1, force=True)
    state = cache.state(monotonic=RATE_CACHE_TTL_SECONDS + 1, now_iso=NOW)

    assert state.from_database
    assert state.generation_id == 7
    assert state.rows[0].input_price_per_1k_tokens == Decimal("0.0044")


def test_a_long_outage_keeps_the_rate_but_marks_it_estimated():
    """Design §6: 'it stays on that rate, becomes estimated and emits staleness'."""
    cache = V2RateCache()
    cache.refresh(lambda: _generation(), monotonic=0.0)

    hours_later = 6 * 3600.0
    for attempt in range(1, 5):
        cache.refresh(_raise(V2QueryFailedError("still down")), monotonic=attempt * 3600.0, force=True)

    state = cache.state(monotonic=hours_later, now_iso=NOW)
    assert state.from_database
    assert state.generation_id == 7
    assert EstimateReason.CACHE_REFRESH_FAILING in state.reasons


def test_a_recovered_read_clears_the_failing_reason():
    cache = V2RateCache()
    cache.refresh(lambda: _generation(), monotonic=0.0)
    cache.refresh(_raise(V2QueryFailedError("down")), monotonic=3600.0, force=True)
    assert EstimateReason.CACHE_REFRESH_FAILING in cache.state(monotonic=7200.0, now_iso=NOW).reasons

    cache.refresh(lambda: _generation(generation_id=8, pointer_revision=4), monotonic=7300.0, force=True)
    state = cache.state(monotonic=7300.0, now_iso=NOW)

    assert state.generation_id == 8
    assert EstimateReason.CACHE_REFRESH_FAILING not in state.reasons


def test_an_expected_unavailability_does_not_raise_the_failing_alarm():
    """A disabled rollout gate is not an incident.

    Alarming on ``consumers_enabled = FALSE`` — a state an operator deliberately
    chose — trains people to ignore the alarm that means the refresh is broken.
    """
    cache = V2RateCache()
    for attempt in range(1, 6):
        cache.refresh(_raise(V2DisabledError("consumers_enabled is false")), monotonic=attempt * 3600.0, force=True)

    state = cache.state(monotonic=6 * 3600.0, now_iso=NOW)
    assert EstimateReason.CACHE_REFRESH_FAILING not in state.reasons
    assert EstimateReason.BOOTSTRAP_FALLBACK in state.reasons


# ---------------------------------------------------------------------------
# Refresh scheduling
# ---------------------------------------------------------------------------


def test_a_healthy_generation_is_not_re_read_before_its_ttl():
    calls = []

    def _fetch():
        calls.append(1)
        return _generation()

    cache = V2RateCache()
    cache.refresh(_fetch, monotonic=0.0)
    cache.refresh(_fetch, monotonic=RATE_CACHE_TTL_SECONDS - 1)
    assert len(calls) == 1

    cache.refresh(_fetch, monotonic=RATE_CACHE_TTL_SECONDS)
    assert len(calls) == 2


def test_a_schema_gap_is_re_probed_on_the_shorter_clock():
    """A fresh deploy races the migration job; pods must pick up rates without a restart.

    Caching the gap for the full rate TTL — or forever — would leave a running
    fleet on bundled rates long after 044/045 landed.
    """
    assert SCHEMA_REPROBE_SECONDS < RATE_CACHE_TTL_SECONDS

    calls = []

    def _fetch():
        calls.append(1)
        raise MissingV2SchemaError("relation does not exist")

    cache = V2RateCache()
    cache.refresh(_fetch, monotonic=0.0)
    assert len(calls) == 1

    cache.refresh(_fetch, monotonic=SCHEMA_REPROBE_SECONDS - 1)
    assert len(calls) == 1, "probed again too early"

    cache.refresh(_fetch, monotonic=SCHEMA_REPROBE_SECONDS)
    assert len(calls) == 2


def test_the_gap_clears_when_the_schema_appears():
    cache = V2RateCache()
    cache.refresh(_raise(MissingV2SchemaError("not yet")), monotonic=0.0)
    assert not cache.state(monotonic=0.0, now_iso=NOW).from_database

    cache.refresh(lambda: _generation(), monotonic=SCHEMA_REPROBE_SECONDS)
    state = cache.state(monotonic=SCHEMA_REPROBE_SECONDS, now_iso=NOW)
    assert state.from_database

    # And reverts to the longer TTL now that there is a generation to hold.
    assert not cache.needs_refresh(SCHEMA_REPROBE_SECONDS + 1)


def test_a_disabled_pointer_does_not_leave_a_schema_gap_recorded():
    """The schema is present, so the 60s probe clock is the wrong one to use.

    Re-probing every minute for a state only an operator can change is pure load on
    the database for no new information.
    """
    cache = V2RateCache()
    cache.refresh(_raise(MissingV2SchemaError("pre-044")), monotonic=0.0)
    cache.refresh(_raise(V2DisabledError("consumers_enabled is false")), monotonic=SCHEMA_REPROBE_SECONDS, force=True)

    assert not cache.needs_refresh(SCHEMA_REPROBE_SECONDS + SCHEMA_REPROBE_SECONDS)


@pytest.mark.parametrize("failure", [V2DisabledError("consumers_enabled is false"), V2QueryFailedError("connection refused")])
def test_a_process_with_no_generation_does_not_query_on_every_request(failure):
    calls = []

    def fetch():
        calls.append(1)
        raise failure

    cache = V2RateCache()
    cache.refresh(fetch, monotonic=0.0)
    delay = RATE_CACHE_TTL_SECONDS if isinstance(failure, V2DisabledError) else 30.0
    for moment in (1.0, 5.0, delay - 1):
        cache.refresh(fetch, monotonic=moment)
    assert len(calls) == 1
    cache.refresh(fetch, monotonic=delay)
    assert len(calls) == 2
    assert not cache.state(monotonic=delay, now_iso=NOW).from_database


def test_an_ignored_stale_read_still_advances_the_attempt_clock():
    """A reader stuck on a lagging replica must not re-query per request either."""
    calls = []

    def _fetch():
        calls.append(1)
        return _generation(generation_id=8, pointer_revision=4)

    cache = V2RateCache()
    cache.refresh(lambda: _generation(generation_id=9, pointer_revision=5), monotonic=0.0)
    cache.refresh(_fetch, monotonic=RATE_CACHE_TTL_SECONDS)
    assert len(calls) == 1

    cache.refresh(_fetch, monotonic=RATE_CACHE_TTL_SECONDS + 1)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Pointer revisions and rollback
# ---------------------------------------------------------------------------


def test_a_rollback_to_an_older_generation_is_adopted():
    """Rollback publishes a NEWER pointer revision pointing at an OLDER generation.

    Comparing generation ids instead of pointer revisions would make an operator
    rollback invisible to every already-running reader — the rollback would appear
    to succeed while the fleet kept charging the bad rates.
    """
    cache = V2RateCache()
    cache.refresh(lambda: _generation(generation_id=9, pointer_revision=5), monotonic=0.0)

    rolled_back = _generation(generation_id=4, pointer_revision=6, rows=(_row(input_rate="0.0040"),))
    cache.refresh(lambda: rolled_back, monotonic=RATE_CACHE_TTL_SECONDS, force=True)

    state = cache.state(monotonic=RATE_CACHE_TTL_SECONDS, now_iso=NOW)
    assert state.generation_id == 4
    assert state.pointer_revision == 6
    assert state.rows[0].input_price_per_1k_tokens == Decimal("0.0040")


def test_a_stale_read_racing_behind_the_pointer_is_ignored():
    """Two readers can observe the pointer at different revisions.

    A read that returns an older revision than one already adopted is a stale
    snapshot of the pointer, not a change; applying it would flap the fleet's rates
    back and forth around a publication.
    """
    cache = V2RateCache()
    cache.refresh(lambda: _generation(generation_id=9, pointer_revision=5), monotonic=0.0)

    cache.refresh(lambda: _generation(generation_id=8, pointer_revision=4), monotonic=RATE_CACHE_TTL_SECONDS, force=True)

    state = cache.state(monotonic=RATE_CACHE_TTL_SECONDS, now_iso=NOW)
    assert state.generation_id == 9
    assert state.pointer_revision == 5


def test_the_same_revision_is_re_adopted_without_complaint():
    """Equal revision is not a regression — a plain TTL re-read hits this constantly."""
    cache = V2RateCache()
    cache.refresh(lambda: _generation(generation_id=9, pointer_revision=5), monotonic=0.0)
    cache.refresh(lambda: _generation(generation_id=9, pointer_revision=5), monotonic=RATE_CACHE_TTL_SECONDS, force=True)

    assert cache.state(monotonic=RATE_CACHE_TTL_SECONDS, now_iso=NOW).generation_id == 9


# ---------------------------------------------------------------------------
# build_active_generation: the "nothing to serve" cases
# ---------------------------------------------------------------------------


def _pointer(**overrides):
    base = {
        "current_generation_id": 7,
        "pointer_revision": 3,
        "consumers_enabled": True,
        "snapshot_version": CURRENT_SNAPSHOT_VERSION,
        "policy_version": 1,
        "schema_version": 2,
        "generation_status": "validated",
    }
    base.update(overrides)
    return base


def _db_row():
    row = _row()
    return {
        "model_id": row.model_id,
        "geography": row.geography,
        "service_tier": row.service_tier,
        "context_tier": row.context_tier,
        "region": row.region,
        "max_input_tokens": None,
        "input_price_per_1k_tokens": Decimal("0.0044000000"),
        "output_price_per_1k_tokens": Decimal("0.0220000000"),
        "cache_read_price_per_1k_tokens": Decimal("0.0004400000"),
        "cache_write_price_per_1k_tokens": Decimal("0.0055000000"),
        "cache_write_policy": "full_rate",
        "source": "bulk_catalog",
        "source_url": "https://pricing.example/catalog",
        "source_content_sha256": "a" * 64,
        "source_effective_at": None,
        "verified_at": "2026-09-12T06:00:00+00:00",
        "snapshot_version": CURRENT_SNAPSHOT_VERSION,
        "generation_id": 7,
    }


def test_a_valid_pointer_and_rows_build_a_generation():
    generation = build_active_generation(pointer=_pointer(), rate_rows=[_db_row()], loaded_at=NOW)
    assert generation.generation_id == 7
    assert generation.pointer_revision == 3
    assert generation.rows[0].input_price_per_1k_tokens == Decimal("0.0044")


@pytest.mark.parametrize(
    ("pointer", "rows", "why"),
    [
        (None, [_db_row()], "no pointer row at all"),
        (_pointer(current_generation_id=None), [_db_row()], "pointer references nothing"),
        (_pointer(consumers_enabled=False), [_db_row()], "rollout gate closed"),
    ],
)
def test_nothing_to_serve_raises_disabled(pointer, rows, why):
    """Absent/disabled pointers deliberately select compatibility."""
    with pytest.raises(V2DisabledError):
        build_active_generation(pointer=pointer, rate_rows=rows, loaded_at=NOW)


def test_disabled_is_an_unavailability_not_a_bug():
    """The adapters catch the base class; a sibling of Exception would escape them."""
    assert issubclass(V2DisabledError, V2UnavailableError)
    assert issubclass(MissingV2SchemaError, V2UnavailableError)
    assert issubclass(V2QueryFailedError, V2UnavailableError)


def test_a_database_row_violating_the_cache_write_policy_is_rejected():
    """The 044 CHECK constraints and the in-memory invariants must agree.

    A row claiming ``unpublished`` while carrying a price would otherwise be loaded
    and used, charging cache writes at a rate AWS never published.
    """
    row = _db_row()
    row["cache_write_policy"] = "unpublished"
    with pytest.raises(ValueError, match="unpublished cache write must store NULL"):
        build_active_generation(pointer=_pointer(), rate_rows=[row], loaded_at=NOW)


def test_numeric_scale_from_the_database_is_preserved_exactly():
    """NUMERIC(14,10) arrives as a trailing-zero Decimal; the value must not shift.

    0.0000264 (Luna GovCloud cache read) is the design's motivating case: quantized
    to scale 6 it becomes 0.000026, a 1.52% error on every cached read.
    """
    row = _db_row()
    row["cache_read_price_per_1k_tokens"] = Decimal("0.0000264000")
    generation = build_active_generation(pointer=_pointer(), rate_rows=[row], loaded_at=NOW)
    assert generation.rows[0].cache_read_price_per_1k_tokens == Decimal("0.0000264")


# ---------------------------------------------------------------------------
# Selection view
# ---------------------------------------------------------------------------


def test_selection_snapshot_uses_database_rows_but_bundled_curated_policy():
    """Curated non-OpenAI rates are policy, not something the refresh publishes.

    If the V2 view dropped them, every Claude model would become unknown the moment
    a generation was adopted.
    """
    cache = V2RateCache()
    cache.refresh(lambda: _generation(), monotonic=0.0)
    state = cache.state(monotonic=0.0, now_iso=NOW)

    view = cache.snapshot_for_selection(state)
    assert view.rates == state.rows
    assert view.curated_non_openai == load_snapshot().curated_non_openai


def test_selection_snapshot_falls_back_to_the_whole_bundle_when_not_from_db():
    cache = V2RateCache()
    state = cache.state(monotonic=0.0, now_iso=NOW)
    assert cache.snapshot_for_selection(state) is load_snapshot()


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


def test_reader_metrics_report_distinct_generations_not_raw_ids():
    """Cardinality matters: generation ids are unbounded over time.

    A per-generation-id CloudWatch dimension would mint a new paid custom metric on
    every daily publication and leave the alarm unbuildable.
    """
    metrics = ReaderMetrics()
    metrics.observed_generations.update({4, 5, 5, 9})
    metrics.schema_missing += 2

    payload = metrics.as_dict()
    assert payload["distinct_generations"] == 3
    assert payload["schema_missing"] == 2
    assert all(isinstance(value, int) for value in payload.values())


def test_warm_disable_serves_compatibility_but_retains_last_good_internally():
    cache = V2RateCache()
    cached = _generation(generation_id=7, pointer_revision=3)
    cache.record_success(cached, monotonic=0)
    cache.record_failure(V2DisabledError("operator disabled", pointer_revision=4), monotonic=900)
    state = cache.state(monotonic=900, now_iso=NOW)
    assert not state.from_database
    assert state.generation_id is None
    assert state.rows == load_snapshot().rates
    assert cache.generation is cached
    # An outage cannot silently reopen the gate.
    cache.record_failure(V2QueryFailedError("connection down"), monotonic=1800)
    assert not cache.state(monotonic=1800, now_iso=NOW).from_database
    # Neither may an enabled response which raced behind the explicit disable.
    cache.record_success(cached, monotonic=2000)
    assert not cache.state(monotonic=2000, now_iso=NOW).from_database
    # Re-enablement is an explicit newer pointer observation.
    cache.record_success(_generation(generation_id=7, pointer_revision=5), monotonic=2100)
    assert cache.state(monotonic=2100, now_iso=NOW).from_database


def test_stale_disabled_observation_cannot_hide_newer_enabled_generation():
    cache = V2RateCache()
    cache.record_success(_generation(pointer_revision=5), monotonic=0)
    cache.record_failure(V2DisabledError("old replica gate", pointer_revision=4), monotonic=900)
    assert cache.state(monotonic=900, now_iso=NOW).from_database


def test_schema_gap_then_connection_failure_obeys_backoff():
    cache = V2RateCache()
    cache.record_failure(MissingV2SchemaError("pre-migration"), monotonic=0)
    assert cache.needs_refresh(60)
    cache.record_failure(V2QueryFailedError("connection refused"), monotonic=60)
    assert not cache.needs_refresh(60.001)
    assert not cache.needs_refresh(89)
    assert cache.needs_refresh(90)
    cache.record_failure(V2QueryFailedError("still refused"), monotonic=90)
    assert not cache.needs_refresh(149)
    assert cache.needs_refresh(150)


def test_connection_failure_retry_delays_are_bounded():
    cache = V2RateCache()
    now = 0
    for expected_delay in (30, 60, 120, 240, 480, 900, 900):
        cache.record_failure(V2QueryFailedError("down"), monotonic=now)
        assert not cache.needs_refresh(now + expected_delay - 1)
        assert cache.needs_refresh(now + expected_delay)
        now += expected_delay


def test_missing_previously_live_schema_counts_as_cache_outage():
    cache = V2RateCache()
    cache.record_success(_generation(), monotonic=0)
    for moment in range(900, 2761, 60):
        cache.record_failure(MissingV2SchemaError("table disappeared"), monotonic=moment)
    state = cache.state(monotonic=2760, now_iso=NOW)
    assert state.from_database
    assert EstimateReason.CACHE_REFRESH_FAILING in state.reasons


def test_continuously_stale_replica_is_a_refresh_failure():
    cache = V2RateCache()
    cache.record_success(_generation(pointer_revision=5), monotonic=0)
    for moment in (900, 1800, 2700):
        cache.record_success(_generation(pointer_revision=4), monotonic=moment)
    assert EstimateReason.CACHE_REFRESH_FAILING in cache.state(monotonic=2700, now_iso=NOW).reasons


@pytest.mark.parametrize(
    "changes",
    [
        {"generation_status": "building"},
        {"schema_version": 1},
        {"policy_version": 3},
        {"pointer_revision": 0},
    ],
)
def test_unsupported_enabled_generation_is_query_failure_not_disable(changes):
    with pytest.raises(V2QueryFailedError):
        build_active_generation(pointer=_pointer(**changes), rate_rows=[_db_row()], loaded_at=NOW)


def test_empty_validated_generation_is_corruption_not_disable():
    with pytest.raises(V2QueryFailedError, match="no rate rows"):
        build_active_generation(pointer=_pointer(), rate_rows=[], loaded_at=NOW)


def test_mixed_generation_rows_and_duplicates_are_rejected():
    row = _db_row()
    row["generation_id"] = 8
    with pytest.raises(V2QueryFailedError, match="belong"):
        build_active_generation(pointer=_pointer(), rate_rows=[row], loaded_at=NOW)
    with pytest.raises(V2QueryFailedError, match="duplicate"):
        build_active_generation(pointer=_pointer(), rate_rows=[_db_row(), _db_row()], loaded_at=NOW)
