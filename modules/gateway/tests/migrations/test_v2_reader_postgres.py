"""The psycopg2 V2 reader against a real PostgreSQL 16 server (§6, S3).

The design requires these exact scenarios: "Exercise new readers against pre-044
schema, a missing required V2 column, disabled/empty pointer, failed query and
successful subsequent ledger write on the same connection: the failed probe must
have been rolled back."

Every one of them is about a PostgreSQL behavior SQLite does not have. A failed
statement aborts the whole transaction and every subsequent statement on that
connection returns ``25P02`` until a rollback — so a pricing probe on a database
that has not run 044 yet can stop the tracker from writing ``budget_usage`` at all.
That is strictly worse than the missing rates it was probing for: no metering
instead of approximate metering. A mock cannot demonstrate it either way.

See tests/migrations/README-postgres.md for how the server is provided.
"""

from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

from tests.migrations.conftest_postgres import upgrade

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
if str(GATEWAY_ROOT / "lambda" / "shared") not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT / "lambda" / "shared"))

from pricing_policy import EstimateReason  # noqa: E402
from pricing_policy.storage import (  # noqa: E402
    MissingV2SchemaError,
    V2DisabledError,
    V2QueryFailedError,
)


@pytest.fixture
def reader():
    """The psycopg2 adapter, with its process-wide cache reset per test.

    Reset is mandatory: the cache is a module-level singleton by design (a
    generation read once must survive later failures), so without this the first
    test to load a generation would make every subsequent test's degraded-read
    assertion pass for the wrong reason.
    """
    import pricing_v2_reader

    pricing_v2_reader.reset_for_tests()
    yield pricing_v2_reader
    pricing_v2_reader.reset_for_tests()


def _fetch(conn, reader):
    """Call the private fetch directly, to assert on the exception class."""
    return reader._fetch_active_generation(conn)  # noqa: SLF001 - the classification is the unit under test


# ---------------------------------------------------------------------------
# Pre-044: the schema is simply not there
# ---------------------------------------------------------------------------


def test_pre_044_schema_reports_missing_not_failed(pg_url, connect, reader):
    """42P01 must classify as a schema gap, so the reader falls back deliberately."""
    upgrade(pg_url, "043_person_anchor_rekey")

    with pytest.raises(MissingV2SchemaError):
        _fetch(connect(autocommit=False), reader)


def test_a_completely_empty_database_also_reports_missing(connect, reader):
    """No migrations at all — the state a brand-new database is in.

    Separate from the pre-044 case because it exercises the reader before
    ``alembic_version`` itself exists, which is when a gateway pod that wins the
    race against the migration job starts up.
    """
    with pytest.raises(MissingV2SchemaError):
        _fetch(connect(autocommit=False), reader)


def test_a_failed_probe_leaves_the_connection_usable(pg_url, connect, reader):
    """THE case this file exists for.

    A pre-044 database makes the probe fail. If the reader does not roll back to a
    savepoint, the caller's transaction is aborted and the very next statement —
    the tracker's ``budget_usage`` upsert — fails with 25P02. The result is a
    deployment that meters nothing, rather than one that meters approximately.
    """
    upgrade(pg_url, "043_person_anchor_rekey")
    conn = connect(autocommit=False)

    with pytest.raises(MissingV2SchemaError):
        _fetch(conn, reader)

    # The ledger write that follows the probe in the real handler.
    with conn.cursor() as cur:
        cur.execute("CREATE TEMPORARY TABLE probe_recovery (id integer)")
        cur.execute("INSERT INTO probe_recovery (id) VALUES (1)")
        cur.execute("SELECT count(*) FROM probe_recovery")
        assert cur.fetchone()[0] == 1
    conn.commit()


def test_get_rate_state_bootstraps_on_a_pre_044_database(pg_url, connect, reader):
    """The public entry point must not raise — it prices from the bundle instead."""
    upgrade(pg_url, "043_person_anchor_rekey")
    conn = connect(autocommit=False)

    state = reader.get_rate_state(conn)

    assert not state.from_database
    assert EstimateReason.BOOTSTRAP_FALLBACK in state.reasons
    assert state.rows, "must still have rates to price with"
    assert reader.reader_metrics()["schema_missing"] == 1

    # And the connection is still good.
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# Partial schema: the table exists but a required column does not
# ---------------------------------------------------------------------------


def test_a_missing_required_column_reports_missing_schema(pg_url, connect, reader):
    """42703, the more dangerous gap: the tables exist so the query looks fine.

    Reached by dropping a column a later hand-patch might have missed. The reader
    must treat it exactly like a pre-044 database rather than as a hard failure,
    and must not read the remaining columns and price with a partial row.
    """
    upgrade(pg_url, "head")
    conn = connect(autocommit=False)
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE model_pricing_rates_v2 DROP COLUMN cache_write_policy")
    conn.commit()

    with pytest.raises(MissingV2SchemaError):
        _fetch(conn, reader)

    # Still usable afterwards, same as the 42P01 path.
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1


def test_a_missing_pointer_column_is_also_a_schema_gap(pg_url, connect, reader):
    upgrade(pg_url, "head")
    conn = connect(autocommit=False)
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE model_pricing_active DROP COLUMN consumers_enabled")
    conn.commit()

    with pytest.raises(MissingV2SchemaError):
        _fetch(conn, reader)


# ---------------------------------------------------------------------------
# Schema present, nothing to serve
# ---------------------------------------------------------------------------


def test_the_seeded_database_serves_a_real_generation(pg_url, connect, reader):
    """After 044+045 the reader must find rates without any manual step.

    This is #1017's cold-start requirement: a fresh deployment prices from the
    database, not from the bundle, before the first 06:00 refresh ever runs.
    """
    upgrade(pg_url, "head")
    conn = connect(autocommit=False)

    state = reader.get_rate_state(conn)

    assert state.from_database, "a seeded database must not serve bundled rates"
    assert state.generation_id is not None
    assert EstimateReason.BOOTSTRAP_FALLBACK not in state.reasons
    assert state.rows

    sol = [r for r in state.rows if r.model_id == "openai.gpt-5.6-sol" and r.service_tier == "standard" and r.geography == "in_region"]
    assert sol, "the seed must carry the frontier standard rates"


def test_a_disabled_pointer_serves_the_bundle_without_error(pg_url, connect, reader):
    """``consumers_enabled = FALSE`` is the operator's rollout gate (§4.6).

    It must behave like a normal fallback, not like a failure: closing the gate is
    how an operator stops consumers reading a suspect generation, and it must not
    page anyone.
    """
    upgrade(pg_url, "head")
    conn = connect(autocommit=False)
    with conn.cursor() as cur:
        cur.execute("UPDATE model_pricing_active SET consumers_enabled = FALSE WHERE singleton IS TRUE")
    conn.commit()

    with pytest.raises(V2DisabledError):
        _fetch(conn, reader)

    state = reader.get_rate_state(conn)
    assert not state.from_database
    assert EstimateReason.CACHE_REFRESH_FAILING not in state.reasons, "a deliberate gate is not an incident"


def test_a_generation_still_being_built_is_invisible(pg_url, connect, reader):
    """Only ``status = 'validated'`` may be served.

    A generation still being written has an incomplete rate set; serving it would
    price whatever has not been inserted yet as an unknown model. Note what this
    test does NOT do: it cannot put the pointer on the building generation, because
    ``trg_model_pricing_active_guard`` refuses to activate a non-validated one. That
    refusal is asserted here too — it means the reader's own ``status = 'validated'``
    filter is defence in depth rather than the only thing standing between a
    half-written generation and live billing.
    """
    upgrade(pg_url, "head")
    conn = connect(autocommit=False)

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO model_pricing_generations
                (schema_version, policy_version, snapshot_version, status,
                 required_variants, content_sha256)
            VALUES (2, 1, 'under-construction', 'building',
                    '[["openai.gpt-5.6-sol","in_region","standard","short","us-east-1"]]'::jsonb,
                    %s)
            RETURNING generation_id
            """,
            ("b" * 64,),
        )
        building_id = cur.fetchone()[0]
    conn.commit()

    # The pointer refuses to move to it at all.
    with conn.cursor() as cur, pytest.raises(Exception, match="not validated"):
        cur.execute(
            "UPDATE model_pricing_active SET current_generation_id = %s WHERE singleton IS TRUE",
            (building_id,),
        )
    conn.rollback()

    # And the reader keeps serving the validated seed, not the new generation.
    state = reader.get_rate_state(conn)
    assert state.from_database
    assert state.generation_id != building_id


def test_a_cleared_pointer_reports_disabled_not_failed(pg_url, connect, reader):
    """``current_generation_id = NULL`` is the other half of the rollout gate.

    Reachable where an empty *validated* generation is not: the immutability trigger
    forbids deleting a validated generation's rows, so "validated but zero rows"
    cannot occur in a real database and is covered by the unit tests on
    ``build_active_generation`` instead. Clearing the pointer is how an operator
    actually takes V2 out of service, and it must read as a deliberate gate.
    """
    upgrade(pg_url, "head")
    conn = connect(autocommit=False)
    with conn.cursor() as cur:
        cur.execute("UPDATE model_pricing_active SET consumers_enabled = FALSE, current_generation_id = NULL WHERE singleton IS TRUE")
    conn.commit()

    with pytest.raises(V2DisabledError):
        _fetch(conn, reader)

    state = reader.get_rate_state(conn)
    assert not state.from_database
    assert EstimateReason.CACHE_REFRESH_FAILING not in state.reasons


# ---------------------------------------------------------------------------
# Hard failures keep the last known good rows
# ---------------------------------------------------------------------------


def test_a_permission_error_is_a_query_failure_not_a_schema_gap(pg_url, connect, reader):
    """42501 must NOT read as "no schema".

    Misclassified, a missing GRANT would send the fleet to bundled rates
    indefinitely while a perfectly good generation sat unread — and the logs would
    look like an ordinary pre-migration deployment.
    """
    upgrade(pg_url, "head")

    import uuid

    from psycopg2 import sql

    admin = connect(autocommit=True)
    role = sql.Identifier(f"pricing_reader_{uuid.uuid4().hex}")
    with admin.cursor() as cur:
        cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(role))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))

    conn = connect(autocommit=False)
    try:
        with conn.cursor() as cur:
            cur.execute(sql.SQL("SET ROLE {}").format(role))
        with pytest.raises(V2QueryFailedError):
            _fetch(conn, reader)
        # Permission failures must preserve the transaction like schema gaps do.
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            assert cur.fetchone()[0] == 1
            cur.execute("RESET ROLE")
        conn.commit()
        assert reader.reader_metrics()["schema_missing"] == 0
    finally:
        conn.rollback()
        with conn.cursor() as cur:
            cur.execute("RESET ROLE")
        conn.commit()
        with admin.cursor() as cur:
            cur.execute(sql.SQL("DROP OWNED BY {}").format(role))
            cur.execute(sql.SQL("DROP ROLE {}").format(role))


def test_a_read_failure_retains_the_generation_it_already_had(pg_url, connect, reader):
    """Design §6: keep the newer DB rate through hours of failure, marked estimated.

    Renaming the table simulates an unreachable source while leaving a real
    generation already in the cache. The rate must not revert to the bundle.
    """
    upgrade(pg_url, "head")
    conn = connect(autocommit=False)

    good = reader.get_rate_state(conn)
    assert good.from_database
    generation_id = good.generation_id
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("ALTER TABLE model_pricing_rates_v2 RENAME TO model_pricing_rates_v2_moved")
    conn.commit()

    degraded = reader.get_rate_state(conn, force=True)

    assert degraded.from_database, "must not downgrade to the bundled snapshot"
    assert degraded.generation_id == generation_id
    assert degraded.rows == good.rows


# ---------------------------------------------------------------------------
# Precision through the driver
# ---------------------------------------------------------------------------


def test_numeric_14_10_survives_the_round_trip(pg_url, connect, reader):
    """The motivating precision cases, read back through psycopg2.

    0.0000264 quantized to scale 6 is 0.000026 — a 1.52% error on every Luna
    GovCloud cache read. The point of NUMERIC(14,10) is that this cannot happen, so
    the assertion is on the exact Decimal, not an approximate compare.
    """
    upgrade(pg_url, "head")
    conn = connect(autocommit=True)

    with conn.cursor() as cur:
        cur.execute("SELECT current_generation_id FROM model_pricing_active WHERE singleton IS TRUE")
        generation_id = cur.fetchone()[0]
    assert generation_id is not None, "the seed must leave the pointer set"

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT input_price_per_1k_tokens, cache_read_price_per_1k_tokens
            FROM model_pricing_rates_v2
            WHERE generation_id = %s AND cache_read_price_per_1k_tokens IS NOT NULL
            LIMIT 1
            """,
            (generation_id,),
        )
        row = cur.fetchone()

    assert row is not None, "the seed must carry at least one published cache-read rate"
    for value in row:
        assert isinstance(value, Decimal), "a float here reintroduces binary rounding error"

    # And through the reader, on the exact motivating case from the design note:
    # Luna GovCloud short-context cache read is 0.0000264, which quantized to scale 6
    # becomes 0.000026 — a 1.52% under-charge on every such read. context_tier is
    # pinned because the long-context row is 0.0000528, and a filter that let both
    # through would pass on the wrong row half the time.
    state = reader.get_rate_state(connect(autocommit=False))
    luna = {
        r.region: r
        for r in state.rows
        if r.model_id == "openai.gpt-5.6-luna" and r.geography == "govcloud" and r.service_tier == "standard" and r.context_tier == "short"
    }
    # Both GovCloud regions are priced, and identically — region is part of the
    # primary key, so a seed that populated only one would leave the other pricing
    # off a fallback row.
    assert set(luna) == {"us-gov-east-1", "us-gov-west-1"}, sorted(luna)
    for region, row in luna.items():
        assert row.cache_read_price_per_1k_tokens == Decimal("0.0000264"), region
        assert row.input_price_per_1k_tokens == Decimal("0.000264"), region


def test_reader_rows_match_the_snapshot_version_the_seed_recorded(pg_url, connect, reader):
    """The generation must identify which immutable snapshot it came from.

    Without it there is no way to tell, after the fact, which published bundle a
    historical cost was computed against.
    """
    upgrade(pg_url, "head")
    conn = connect(autocommit=False)

    state = reader.get_rate_state(conn)
    assert state.from_database
    versions = {row.snapshot_version for row in state.rows if row.snapshot_version}
    assert versions == {"2026-09-12.1", "2026-09-12.2"}, versions
    assert {row.snapshot_version for row in state.rows if row.model_id.startswith("openai.")} == {"2026-09-12.1"}
    assert {row.snapshot_version for row in state.rows if row.model_id.startswith("anthropic.")} == {"2026-09-12.2"}


def test_warm_reader_honors_disable_even_when_rate_table_is_unavailable(pg_url, connect, reader):
    upgrade(pg_url, "head")
    conn = connect(autocommit=False)
    initial = reader.get_rate_state(conn)
    assert initial.from_database
    conn.commit()

    # An explicit operator gate must remain observable without querying rates.
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE model_pricing_rates_v2 RENAME TO model_pricing_rates_v2_hidden")
        cur.execute("UPDATE model_pricing_active SET consumers_enabled = FALSE, pointer_revision = pointer_revision + 1")
    conn.commit()
    disabled = reader.get_rate_state(conn, force=True)
    assert not disabled.from_database
    assert disabled.generation_id is None
    assert reader.reader_metrics()["disabled"] == 1
    assert reader.reader_metrics()["schema_missing"] == 0
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("ALTER TABLE model_pricing_rates_v2_hidden RENAME TO model_pricing_rates_v2")
        cur.execute("UPDATE model_pricing_active SET consumers_enabled = TRUE, pointer_revision = pointer_revision + 1")
    conn.commit()
    enabled = reader.get_rate_state(conn, force=True)
    assert enabled.from_database
    assert enabled.generation_id == initial.generation_id
    assert enabled.pointer_revision == initial.pointer_revision + 2


@pytest.mark.asyncio
async def test_async_missing_schema_probe_preserves_outer_transaction(pg_url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from src.budget import pricing_v2_reader as async_reader
    from tests.migrations.conftest_postgres import to_async_url

    engine = create_async_engine(to_async_url(pg_url))
    async_reader.reset_for_tests()
    try:
        async with AsyncSession(engine) as session:
            await session.execute(text("CREATE TEMPORARY TABLE probe_outer_write (id integer)"))
            await session.execute(text("INSERT INTO probe_outer_write VALUES (1)"))
            state = await async_reader.get_rate_state(session)
            assert not state.from_database
            assert async_reader.reader_metrics()["schema_missing"] == 1
            # The savepoint rollback must preserve preceding writes and allow new ones.
            await session.execute(text("INSERT INTO probe_outer_write VALUES (2)"))
            result = await session.execute(text("SELECT count(*) FROM probe_outer_write"))
            assert result.scalar_one() == 2
            await session.commit()
    finally:
        async_reader.reset_for_tests()
        await engine.dispose()


@pytest.mark.asyncio
async def test_async_warm_cache_disable_and_reenable(pg_url, connect):
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

    from src.budget import pricing_v2_reader as async_reader
    from tests.migrations.conftest_postgres import to_async_url

    upgrade(pg_url, "head")
    admin = connect(autocommit=True)
    engine = create_async_engine(to_async_url(pg_url))
    async_reader.reset_for_tests()
    try:
        async with AsyncSession(engine) as session:
            initial = await async_reader.get_rate_state(session)
            assert initial.from_database
            await session.commit()
            with admin.cursor() as cur:
                cur.execute("UPDATE model_pricing_active SET consumers_enabled = FALSE, pointer_revision = pointer_revision + 1")
            disabled = await async_reader.get_rate_state(session, force=True)
            assert not disabled.from_database
            assert async_reader.reader_metrics()["disabled"] == 1
            await session.commit()
            with admin.cursor() as cur:
                cur.execute("UPDATE model_pricing_active SET consumers_enabled = TRUE, pointer_revision = pointer_revision + 1")
            enabled = await async_reader.get_rate_state(session, force=True)
            assert enabled.from_database
            assert enabled.generation_id == initial.generation_id
            assert enabled.pointer_revision == initial.pointer_revision + 2
    finally:
        async_reader.reset_for_tests()
        await engine.dispose()
