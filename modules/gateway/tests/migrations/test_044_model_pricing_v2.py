"""Tests for Alembic migration 044 — V2 Bedrock rate storage.

Issue #4969 (S2). Run against a real PostgreSQL 16 server, not SQLite: every
behavior asserted here (NUMERIC(14,10) round-tripping, JSONB manifests, identity
columns, plpgsql triggers, row locking, two concurrent sessions) is
PostgreSQL-specific, and a SQLite run would report green while exercising none of
it. See tests/migrations/conftest_postgres.py.

What these tests are defending, in order of how much a regression would cost:

1. **Precision.** Scale 6 cannot hold Cyber's published 0.0171875 cache-write
   rate or Luna's GovCloud 0.0000264 cache read — the second rounds with 1.52%
   error. The stored value must come back bit-identical, which is the entire
   reason this table is not `NUMERIC(10,6)` like the legacy one.
2. **Immutable publication.** A settlement that arrives after a refresh must be
   able to price against the generation it was quoted under. That only holds if a
   validated generation cannot be edited afterwards.
3. **Atomic activation.** A generation missing rows must never become active;
   consumers would silently price those requests off a fallback.
4. **Zero is not unpublished.** A zero stored where AWS publishes no rate makes
   cached tokens look free and under-bills them.
"""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

# Fixtures (pg_url, pg_server, connect) come from conftest.py; these are helpers.
from tests.migrations.conftest_postgres import downgrade, run_alembic, upgrade

REVISION = "044_model_pricing_v2"
PREVIOUS = "043_person_anchor_rekey"

SHA = "a" * 64
OTHER_SHA = "b" * 64

# One variant key: (model_id, geography, service_tier, context_tier, region).
KEY = ["openai.gpt-sol-1", "in_region", "standard", "short", "us-west-2"]


@pytest.fixture
def db(pg_url):
    """A database upgraded through 044, plus a live connection to it.

    The upgrade runs the *real* migration chain from base, so 044 is tested in the
    position it will actually occupy — not applied in isolation against an empty
    database where a missing dependency on an earlier revision would go unnoticed.
    """
    upgrade(pg_url, REVISION)
    import psycopg2

    connection = psycopg2.connect(pg_url)
    connection.autocommit = True
    try:
        yield connection
    finally:
        connection.close()


def _generation(
    db,
    *,
    status: str = "building",
    manifest: list | None = None,
    snapshot_version: str = "2026-09-12.1",
    content_sha256: str = SHA,
    policy_version: int = 1,
) -> int:
    manifest = [KEY] if manifest is None else manifest
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO model_pricing_generations
                (schema_version, policy_version, snapshot_version, status,
                 required_variants, content_sha256, validated_at)
            VALUES (2, %s, %s, %s, %s::jsonb, %s, CASE WHEN %s = 'validated' THEN now() ELSE NULL END)
            RETURNING generation_id
            """,
            (policy_version, snapshot_version, status, json.dumps(manifest), content_sha256, status),
        )
        return cursor.fetchone()[0]


def _insert_rate(
    db,
    generation_id: int,
    *,
    key: list | None = None,
    input_rate: str = "0.0022000000",
    output_rate: str = "0.0176000000",
    cache_read=None,
    cache_write=None,
    cache_write_policy: str = "unpublished",
    source: str = "bundled_snapshot",
    snapshot_version: str | None = "2026-09-12.1",
    max_input_tokens: int | None = 272000,
) -> None:
    model_id, geography, service_tier, context_tier, region = key or KEY
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO model_pricing_rates_v2
                (generation_id, model_id, geography, service_tier, context_tier, region,
                 max_input_tokens, input_price_per_1k_tokens, output_price_per_1k_tokens,
                 cache_read_price_per_1k_tokens, cache_write_price_per_1k_tokens,
                 cache_write_policy, source, source_url, source_content_sha256, verified_at,
                 snapshot_version)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    'https://example.invalid/pricing', %s, now(), %s)
            """,
            (
                generation_id,
                model_id,
                geography,
                service_tier,
                context_tier,
                region,
                None if context_tier == "flat" else max_input_tokens,
                input_rate,
                output_rate,
                cache_read,
                cache_write,
                cache_write_policy,
                source,
                SHA,
                snapshot_version,
            ),
        )


def _activate(db, generation_id: int) -> None:
    with db.cursor() as cursor:
        cursor.execute(
            """
            UPDATE model_pricing_active
               SET current_generation_id = %s,
                   pointer_revision = pointer_revision + 1,
                   updated_at = now()
             WHERE singleton
            """,
            (generation_id,),
        )


def _publish(db, **generation_kwargs) -> int:
    """Build → populate → validate → activate, the normal publisher sequence."""
    generation_id = _generation(db, **generation_kwargs)
    _insert_rate(db, generation_id)
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s",
            (generation_id,),
        )
    _activate(db, generation_id)
    return generation_id


# --------------------------------------------------------------------------- #
# Shape
# --------------------------------------------------------------------------- #


def test_migration_creates_the_three_tables_and_leaves_legacy_alone(db):
    """044 is purely additive: the legacy table keeps its shape and its rows.

    The rollout depends on this. An old writer's `ON CONFLICT (model_id)` and an
    old reader's model-keyed dict must retain their exact original meaning while
    V2 is seeded but not yet enabled.
    """
    with db.cursor() as cursor:
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'model_pricing%' ORDER BY 1")
        assert [row[0] for row in cursor.fetchall()] == [
            "model_pricing",
            "model_pricing_active",
            "model_pricing_generations",
            "model_pricing_rates_v2",
        ]

        # Legacy precision unchanged — widening it would change how existing
        # non-OpenAI rows round.
        cursor.execute(
            """
            SELECT numeric_precision, numeric_scale FROM information_schema.columns
             WHERE table_name='model_pricing' AND column_name='input_price_per_1k_tokens'
            """
        )
        assert cursor.fetchone() == (10, 6)


def test_rate_columns_are_numeric_14_10(db):
    """Scale 10, not 6. This is the defect: 6 cannot represent the real rates."""
    with db.cursor() as cursor:
        cursor.execute(
            """
            SELECT column_name, numeric_precision, numeric_scale
              FROM information_schema.columns
             WHERE table_name='model_pricing_rates_v2' AND data_type='numeric'
             ORDER BY column_name
            """
        )
        rows = dict((name, (precision, scale)) for name, precision, scale in cursor.fetchall())

    for column in (
        "input_price_per_1k_tokens",
        "output_price_per_1k_tokens",
        "cache_read_price_per_1k_tokens",
        "cache_write_price_per_1k_tokens",
    ):
        assert rows[column] == (14, 10), f"{column} must be NUMERIC(14,10), got {rows[column]}"


def test_exactly_one_pointer_row_exists_after_migration(db):
    """The migration seeds the singleton so no publisher races to insert it."""
    with db.cursor() as cursor:
        cursor.execute("SELECT singleton, current_generation_id, pointer_revision, consumers_enabled, refresh_paused FROM model_pricing_active")
        assert cursor.fetchall() == [(True, None, 0, False, False)]


def test_consumers_start_disabled(db):
    """Seeding must be able to happen before anything reads it (§4.2 rollout)."""
    with db.cursor() as cursor:
        cursor.execute("SELECT consumers_enabled FROM model_pricing_active")
        assert cursor.fetchone()[0] is False


def test_a_second_pointer_row_cannot_be_inserted(db):
    """`singleton BOOLEAN PRIMARY KEY CHECK (singleton)` admits exactly one row."""
    import psycopg2

    with pytest.raises(psycopg2.errors.UniqueViolation):
        with db.cursor() as cursor:
            cursor.execute("INSERT INTO model_pricing_active (singleton) VALUES (TRUE)")

    with pytest.raises(psycopg2.errors.CheckViolation):
        with db.cursor() as cursor:
            cursor.execute("INSERT INTO model_pricing_active (singleton) VALUES (FALSE)")


def test_expected_indexes_exist(db):
    with db.cursor() as cursor:
        cursor.execute("SELECT indexname FROM pg_indexes WHERE tablename LIKE 'model_pricing_%' ORDER BY 1")
        names = {row[0] for row in cursor.fetchall()}
    for expected in (
        "ix_model_pricing_generations_validated",
        "ix_rates_v2_generation_model",
        "ix_rates_v2_verified_at",
    ):
        assert expected in names, f"missing index {expected}"


# --------------------------------------------------------------------------- #
# Precision — the defect this release exists to fix
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("rate", "why"),
    [
        ("0.0171875000", "Cyber cache write; scale 6 rounds to 0.017188, a 0.003% error"),
        ("0.0000264000", "Luna GovCloud cache read; scale 6 rounds to 0.000026, a 1.52% error"),
        ("0.0002000000", "a rate whose exact value scale 6 does happen to hold"),
    ],
)
def test_precision_critical_rates_round_trip_exactly(db, rate, why):
    """Stored and returned values must be identical Decimals, not merely close.

    Asserted with `==` on `Decimal` and on the string form: `Decimal('0.017188')
    != Decimal('0.0171875')`, so a silently narrowed column fails here rather
    than showing up as a cent of drift per million tokens on an invoice.
    """
    generation_id = _generation(db)
    _insert_rate(db, generation_id, cache_read=rate, cache_write=None, cache_write_policy="unpublished")

    with db.cursor() as cursor:
        cursor.execute("SELECT cache_read_price_per_1k_tokens FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation_id,))
        stored = cursor.fetchone()[0]

    assert isinstance(stored, Decimal), f"driver returned {type(stored)}; a float would defeat the point ({why})"
    assert stored == Decimal(rate), why
    assert str(stored) == rate, f"exact digits must survive, not just the value ({why})"


def test_a_rate_needing_more_than_scale_10_is_rejected_not_rounded(db):
    """Overflow must be an error, because a silent round is an unnoticed misprice.

    PostgreSQL rounds on assignment to a NUMERIC of lower scale rather than
    raising, so this pins the observed behavior explicitly: an 11th decimal digit
    is not silently retained. If a real published rate ever needs scale 11, the
    column must widen — this test is the tripwire that forces that decision
    instead of letting the digit vanish.
    """
    generation_id = _generation(db)
    _insert_rate(db, generation_id, input_rate="0.00000000015")  # scale 11

    with db.cursor() as cursor:
        cursor.execute("SELECT input_price_per_1k_tokens FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation_id,))
        stored = cursor.fetchone()[0]

    assert stored == Decimal("0.0000000002"), "scale-11 input is rounded to scale 10 by PostgreSQL; it is not preserved"
    assert stored != Decimal("0.00000000015")


def test_integral_range_is_not_sacrificed_for_scale(db):
    """Precision 14 with scale 10 leaves 4 integral digits — enough for any per-1k rate."""
    generation_id = _generation(db)
    _insert_rate(db, generation_id, input_rate="9999.9999999999", output_rate="1234.5678901234")

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT input_price_per_1k_tokens, output_price_per_1k_tokens FROM model_pricing_rates_v2 WHERE generation_id=%s",
            (generation_id,),
        )
        assert cursor.fetchone() == (Decimal("9999.9999999999"), Decimal("1234.5678901234"))


# --------------------------------------------------------------------------- #
# Cache-write policy tri-state — zero is not unpublished
# --------------------------------------------------------------------------- #


def test_unpublished_cache_write_requires_null_not_zero(db):
    """A zero here would make cached writes free and under-bill them silently."""
    import psycopg2

    generation_id = _generation(db)
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        _insert_rate(db, generation_id, cache_write="0.0000000000", cache_write_policy="unpublished")
    assert "ck_rates_v2_cache_write_policy_agrees" in str(excinfo.value)


def test_full_rate_cache_write_requires_a_price(db):
    import psycopg2

    generation_id = _generation(db)
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        _insert_rate(db, generation_id, cache_write=None, cache_write_policy="full_rate")
    assert "ck_rates_v2_cache_write_policy_agrees" in str(excinfo.value)


def test_no_additional_fee_rejects_null_write_price(db):
    import psycopg2.errors

    generation_id = _generation(db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_rate(db, generation_id, cache_write=None, cache_write_policy="no_additional_fee")


@pytest.mark.parametrize("field", ["input_rate", "output_rate", "cache_read", "cache_write"])
def test_nan_rates_are_rejected(db, field):
    import psycopg2.errors

    generation_id = _generation(db)
    values = {field: "NaN"}
    if field == "cache_write":
        values["cache_write_policy"] = "full_rate"
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_rate(db, generation_id, **values)


def test_no_additional_fee_requires_the_write_rate_to_equal_the_input_rate(db):
    """'No additional fee' means charged as input — not free, and not something else.

    Storing the input rate rather than NULL is what lets a consumer price the
    write without a special case, and the constraint is what stops the two from
    drifting apart.
    """
    import psycopg2

    generation_id = _generation(db)
    _insert_rate(db, generation_id, input_rate="0.0022000000", cache_write="0.0022000000", cache_write_policy="no_additional_fee")

    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        _insert_rate(
            db,
            generation_id,
            key=["openai.gpt-sol-1", "in_region", "priority", "short", "us-west-2"],
            input_rate="0.0022000000",
            cache_write="0.0000000000",
            cache_write_policy="no_additional_fee",
        )
    assert "ck_rates_v2_cache_write_policy_agrees" in str(excinfo.value)


def test_a_published_zero_cache_read_is_representable(db):
    """Zero and NULL must be distinguishable in the read direction too.

    If AWS ever publishes a genuine zero cache-read rate, it must be storable as
    zero — the tri-state exists so that "free" and "unknown" are different facts,
    which only works if both can be recorded.
    """
    generation_id = _generation(db)
    _insert_rate(db, generation_id, cache_read="0.0000000000")

    with db.cursor() as cursor:
        cursor.execute("SELECT cache_read_price_per_1k_tokens FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation_id,))
        stored = cursor.fetchone()[0]
    assert stored == Decimal("0")
    assert stored is not None


def test_negative_rates_are_rejected(db):
    import psycopg2

    generation_id = _generation(db)
    for kwargs, constraint in (
        ({"input_rate": "-0.0001000000"}, "ck_rates_v2_input_positive"),
        ({"output_rate": "-0.0001000000"}, "ck_rates_v2_output_positive"),
        ({"cache_read": "-0.0001000000"}, "ck_rates_v2_cache_read_non_negative"),
    ):
        with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
            _insert_rate(db, generation_id, **kwargs)
        assert constraint in str(excinfo.value)


def test_zero_input_or_output_is_rejected_as_a_parse_failure(db):
    """Input and output are always charged, so zero means the parser lost the value."""
    import psycopg2

    generation_id = _generation(db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _insert_rate(db, generation_id, input_rate="0.0000000000")


def test_context_tier_bounds_must_agree_with_the_tier(db):
    """Flat models have no window boundary; tiered ones need one to detect overflow."""
    import psycopg2

    generation_id = _generation(db, manifest=[KEY])

    # A tiered row without a maximum: overflow could never be detected.
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO model_pricing_rates_v2
                    (generation_id, model_id, geography, service_tier, context_tier, region,
                     max_input_tokens, input_price_per_1k_tokens, output_price_per_1k_tokens,
                     cache_write_policy, source, source_url, source_content_sha256, verified_at, snapshot_version)
                VALUES (%s, 'openai.gpt-sol-1', 'in_region', 'standard', 'short', 'us-west-2',
                        NULL, 0.0022, 0.0176, 'unpublished', 'bundled_snapshot',
                        'https://example.invalid/p', %s, now(), '2026-09-12.1')
                """,
                (generation_id, SHA),
            )
    assert "ck_rates_v2_context_tier_bounds" in str(excinfo.value)

    # A flat row WITH a maximum: the tier says there is no boundary.
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO model_pricing_rates_v2
                    (generation_id, model_id, geography, service_tier, context_tier, region,
                     max_input_tokens, input_price_per_1k_tokens, output_price_per_1k_tokens,
                     cache_write_policy, source, source_url, source_content_sha256, verified_at, snapshot_version)
                VALUES (%s, 'openai.gpt-oss-120b', 'in_region', 'standard', 'flat', 'us-west-2',
                        272000, 0.00015, 0.0006, 'unpublished', 'bundled_snapshot',
                        'https://example.invalid/p', %s, now(), '2026-09-12.1')
                """,
                (generation_id, SHA),
            )
    assert "ck_rates_v2_context_tier_bounds" in str(excinfo.value)


def test_a_bundled_row_must_name_its_snapshot(db):
    """A later refresh has to tell a seeded placeholder from a fetched rate."""
    import psycopg2

    generation_id = _generation(db)
    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        _insert_rate(db, generation_id, source="bundled_snapshot", snapshot_version=None)
    assert "ck_rates_v2_bundled_requires_version" in str(excinfo.value)


def test_dimension_vocabularies_are_constrained(db):
    """Typos must fail at write time, not resolve to a missing rate at read time."""
    import psycopg2

    generation_id = _generation(db)
    for key, constraint in (
        (["openai.gpt-sol-1", "cross_region", "standard", "short", "us-west-2"], "ck_rates_v2_geography"),
        (["openai.gpt-sol-1", "in_region", "realtime", "short", "us-west-2"], "ck_rates_v2_service_tier"),
        (["openai.gpt-sol-1", "in_region", "standard", "medium", "us-west-2"], "ck_rates_v2_context_tier"),
    ):
        with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
            _insert_rate(db, generation_id, key=key)
        assert constraint in str(excinfo.value)


def test_the_full_variant_tuple_is_the_primary_key(db):
    """One model has many rates; only a full-dimension duplicate is a conflict.

    The legacy table's one-row-per-model key is exactly what made the wrong Sol
    rates unrepresentable, so this pins that the new key is the whole tuple.
    """
    import psycopg2

    generation_id = _generation(db)
    _insert_rate(db, generation_id)

    # Same model, different dimensions — all must coexist.
    for key in (
        ["openai.gpt-sol-1", "in_region", "standard", "long", "us-west-2"],
        ["openai.gpt-sol-1", "geo_cris", "standard", "short", "us-west-2"],
        ["openai.gpt-sol-1", "in_region", "batch", "short", "us-west-2"],
        ["openai.gpt-sol-1", "in_region", "standard", "short", "eu-west-1"],
    ):
        _insert_rate(db, generation_id, key=key)

    with db.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE model_id='openai.gpt-sol-1'")
        assert cursor.fetchone()[0] == 5

    with pytest.raises(psycopg2.errors.UniqueViolation):
        _insert_rate(db, generation_id)


def test_the_same_variant_may_exist_in_two_generations(db):
    """Generations sit side by side so a late settlement can read its own.

    This is what makes the storage safe for the #4968 independent-settlement path:
    a settlement quoted under generation A must still resolve after B publishes.
    """
    first = _generation(db)
    _insert_rate(db, first, input_rate="0.0022000000")
    second = _generation(db)
    _insert_rate(db, second, input_rate="0.0044000000")

    with db.cursor() as cursor:
        cursor.execute(
            "SELECT generation_id, input_price_per_1k_tokens FROM model_pricing_rates_v2 WHERE model_id='openai.gpt-sol-1' ORDER BY generation_id"
        )
        assert cursor.fetchall() == [(first, Decimal("0.0022000000")), (second, Decimal("0.0044000000"))]


def test_rates_require_an_existing_generation(db):
    import psycopg2

    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _insert_rate(db, 999999)


# --------------------------------------------------------------------------- #
# Generation lifecycle + immutability
# --------------------------------------------------------------------------- #


def test_generation_ids_are_server_assigned_and_monotonic(db):
    """`GENERATED ALWAYS AS IDENTITY`: a client cannot choose or reuse an id."""
    import psycopg2

    first = _generation(db)
    second = _generation(db)
    assert second > first

    # 428C9 / GeneratedAlways: the server refuses a client-supplied identity value.
    with pytest.raises(psycopg2.errors.GeneratedAlways):
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO model_pricing_generations
                    (generation_id, schema_version, policy_version, snapshot_version, status,
                     required_variants, content_sha256)
                VALUES (500, 2, 1, 'x', 'building', '[["m","in_region","standard","short","r"]]'::jsonb, %s)
                """,
                (SHA,),
            )


def test_validated_generation_requires_validated_at(db):
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO model_pricing_generations
                    (schema_version, policy_version, snapshot_version, status, required_variants, content_sha256, validated_at)
                VALUES (2, 1, 'x', 'validated', '[["m","in_region","standard","short","r"]]'::jsonb, %s, NULL)
                """,
                (SHA,),
            )
    assert "ck_generations_validated_at_consistent" in str(excinfo.value)


def test_building_generation_cannot_claim_a_validation_time(db):
    """A crashed publisher must not leave a row that looks publishable."""
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        with db.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO model_pricing_generations
                    (schema_version, policy_version, snapshot_version, status, required_variants, content_sha256, validated_at)
                VALUES (2, 1, 'x', 'building', '[["m","in_region","standard","short","r"]]'::jsonb, %s, now())
                """,
                (SHA,),
            )
    assert "ck_generations_validated_at_consistent" in str(excinfo.value)


def test_an_empty_manifest_is_rejected(db):
    """An empty manifest makes the pointer's completeness check vacuously true."""
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        _generation(db, manifest=[])
    assert "ck_generations_manifest_nonempty" in str(excinfo.value)


def test_rates_are_writable_while_building(db):
    """Immutability starts at validation, or a publisher could never populate."""
    generation_id = _generation(db)
    _insert_rate(db, generation_id, input_rate="0.0022000000")
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_rates_v2 SET input_price_per_1k_tokens=0.0033 WHERE generation_id=%s", (generation_id,))
        assert cursor.rowcount == 1
        cursor.execute("DELETE FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation_id,))
        assert cursor.rowcount == 1


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE model_pricing_rates_v2 SET input_price_per_1k_tokens = 9.9 WHERE generation_id = %(gen)s",
        "DELETE FROM model_pricing_rates_v2 WHERE generation_id = %(gen)s",
    ],
)
def test_validated_generation_rates_cannot_be_changed(db, statement):
    """The core immutability guarantee: a published price never moves.

    Without this, a settlement arriving minutes after a refresh could be priced
    against a rate that had been edited since it was quoted — the ledger and the
    quote would disagree with no record of why.
    """
    import psycopg2

    generation_id = _publish(db)
    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        with db.cursor() as cursor:
            cursor.execute(statement, {"gen": generation_id})
    assert "immutable" in str(excinfo.value)


def test_a_rate_cannot_be_added_to_a_validated_generation(db):
    """Backfilling a published generation would change what it means retroactively."""
    import psycopg2

    generation_id = _publish(db)
    with pytest.raises(psycopg2.errors.RaiseException):
        _insert_rate(db, generation_id, key=["openai.gpt-sol-1", "in_region", "flex", "short", "us-west-2"])


def test_a_validated_rate_cannot_move_to_a_building_generation(db):
    import psycopg2

    published = _publish(db)
    building = _generation(db)
    with pytest.raises(psycopg2.errors.RaiseException), db.cursor() as cursor:
        cursor.execute(
            "UPDATE model_pricing_rates_v2 SET generation_id=%s WHERE generation_id=%s",
            (building, published),
        )
    with db.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE generation_id=%s", (published,))
        assert cursor.fetchone()[0] == 1


def test_rate_write_waits_for_concurrent_validation_then_rejects(db, pg_url):
    import threading

    import psycopg2

    generation_id = _generation(db)
    validating = psycopg2.connect(pg_url)
    writing = psycopg2.connect(pg_url)
    writing.autocommit = True
    errors = []
    started = threading.Event()

    def write_rate():
        started.set()
        try:
            _insert_rate(writing, generation_id)
        except psycopg2.Error as exc:
            errors.append(exc)

    thread = threading.Thread(target=write_rate)
    try:
        with validating.cursor() as cursor:
            cursor.execute(
                "UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s",
                (generation_id,),
            )
        with writing.cursor() as cursor:
            cursor.execute("SET statement_timeout='5s'")
        thread.start()
        assert started.wait(2)
        thread.join(0.1)
        assert thread.is_alive(), "A rate write bypassed the uncommitted validation"
        validating.commit()
        thread.join(5)
        assert not thread.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], psycopg2.errors.RaiseException)
        with db.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation_id,))
            assert cursor.fetchone()[0] == 0
    finally:
        validating.rollback()
        validating.close()
        thread.join(6)
        writing.close()


def test_validated_generation_metadata_is_frozen(db):
    import psycopg2

    generation_id = _publish(db)
    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        with db.cursor() as cursor:
            cursor.execute("UPDATE model_pricing_generations SET content_sha256=%s WHERE generation_id=%s", (OTHER_SHA, generation_id))
    assert "immutable" in str(excinfo.value)


def test_a_validated_generation_cannot_be_deleted(db):
    import psycopg2

    generation_id = _publish(db)
    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        with db.cursor() as cursor:
            cursor.execute("DELETE FROM model_pricing_generations WHERE generation_id=%s", (generation_id,))
    assert "cannot be deleted" in str(excinfo.value)


def test_the_manifest_cannot_be_rewritten_to_match_altered_rates(db):
    """Closes the obvious attack on the coverage check.

    If the manifest were editable while building, a publisher that failed to fetch
    the GovCloud rates could shrink its own contract to match what it did fetch and
    activate a generation that silently prices GovCloud off a fallback row. Only
    `status` may move.
    """
    import psycopg2

    generation_id = _generation(db, manifest=[KEY, ["openai.gpt-sol-1", "govcloud", "standard", "short", "us-gov-west-1"]])
    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE model_pricing_generations SET required_variants=%s::jsonb WHERE generation_id=%s",
                (json.dumps([KEY]), generation_id),
            )
    assert "may only transition status" in str(excinfo.value)


def test_status_may_transition_building_to_validated(db):
    """The one legal update. Everything else about the row must stay put."""
    generation_id = _generation(db)
    _insert_rate(db, generation_id)
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s",
            (generation_id,),
        )
        assert cursor.rowcount == 1


def test_status_cannot_transition_back_to_building(db):
    """Un-validating would reopen a published generation for edits."""
    import psycopg2

    generation_id = _publish(db)
    with pytest.raises(psycopg2.errors.RaiseException):
        with db.cursor() as cursor:
            cursor.execute(
                "UPDATE model_pricing_generations SET status='building', validated_at=NULL WHERE generation_id=%s",
                (generation_id,),
            )


def test_deleting_a_building_generation_cascades_to_its_rates(db):
    """Abandoned builds must be cleanable without orphaning rows."""
    generation_id = _generation(db)
    _insert_rate(db, generation_id)
    with db.cursor() as cursor:
        cursor.execute("DELETE FROM model_pricing_generations WHERE generation_id=%s", (generation_id,))
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation_id,))
        assert cursor.fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# Activation guard
# --------------------------------------------------------------------------- #


def test_an_unvalidated_generation_cannot_become_active(db):
    """Nothing outside the publishing transaction may price against a half-built set."""
    import psycopg2

    generation_id = _generation(db)
    _insert_rate(db, generation_id)
    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        _activate(db, generation_id)
    assert "not validated" in str(excinfo.value)


def test_a_generation_missing_manifest_rows_cannot_become_active(db):
    """The coverage check is the difference between a gap and a silent misprice.

    A generation that fetched the short-context rate but not the long one would
    otherwise go live and price every long-context request off whatever row
    happened to resolve.
    """
    import psycopg2

    manifest = [KEY, ["openai.gpt-sol-1", "in_region", "standard", "long", "us-west-2"]]
    generation_id = _generation(db, manifest=manifest)
    _insert_rate(db, generation_id, key=KEY)  # only one of the two required keys
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s", (generation_id,))

    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        _activate(db, generation_id)
    message = str(excinfo.value)
    assert "missing 1 required variant key" in message


def test_a_fully_covered_validated_generation_activates(db):
    manifest = [KEY, ["openai.gpt-sol-1", "in_region", "standard", "long", "us-west-2"]]
    generation_id = _generation(db, manifest=manifest)
    _insert_rate(db, generation_id, key=manifest[0])
    _insert_rate(db, generation_id, key=manifest[1])
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s", (generation_id,))
    _activate(db, generation_id)

    with db.cursor() as cursor:
        cursor.execute("SELECT current_generation_id, pointer_revision FROM model_pricing_active")
        assert cursor.fetchone() == (generation_id, 1)


def test_activation_ignores_extra_rows_beyond_the_manifest(db):
    """The manifest is a floor, not a ceiling — extra coverage is not an error."""
    generation_id = _generation(db, manifest=[KEY])
    _insert_rate(db, generation_id, key=KEY)
    _insert_rate(db, generation_id, key=["openai.gpt-sol-1", "govcloud", "standard", "short", "us-gov-west-1"])
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s", (generation_id,))
    _activate(db, generation_id)


def test_a_nonexistent_generation_cannot_become_active(db):
    """The FK catches it; the trigger's own message is the backstop."""
    import psycopg2

    with pytest.raises((psycopg2.errors.RaiseException, psycopg2.errors.ForeignKeyViolation)):
        _activate(db, 999999)


def test_the_pointer_row_cannot_be_deleted(db):
    """Deleting it would leave consumers with no pointer at all."""
    import psycopg2

    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        with db.cursor() as cursor:
            cursor.execute("DELETE FROM model_pricing_active")
    assert "cannot be deleted" in str(excinfo.value)


def test_consumers_cannot_be_enabled_without_a_generation(db):
    """Enabling with nothing to read would send every request down the bootstrap
    path while the rollout reported itself complete."""
    import psycopg2

    with pytest.raises(psycopg2.errors.CheckViolation) as excinfo:
        with db.cursor() as cursor:
            cursor.execute("UPDATE model_pricing_active SET consumers_enabled=TRUE WHERE singleton")
    assert "ck_active_enabled_requires_generation" in str(excinfo.value)


def test_consumers_can_be_enabled_once_a_generation_is_active(db):
    generation_id = _publish(db)
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_active SET consumers_enabled=TRUE WHERE singleton")
        cursor.execute("SELECT consumers_enabled, current_generation_id FROM model_pricing_active")
        assert cursor.fetchone() == (True, generation_id)


def test_flags_can_be_toggled_without_naming_a_generation(db):
    """`refresh_paused` is the operator's stop button; the guard must not block it.

    The trigger only inspects the generation when it *changes*, so pausing an
    already-active pointer must not re-run (and potentially fail) the coverage
    check.
    """
    _publish(db)
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_active SET refresh_paused=TRUE WHERE singleton")
        cursor.execute("SELECT refresh_paused FROM model_pricing_active")
        assert cursor.fetchone()[0] is True


# --------------------------------------------------------------------------- #
# Rollback semantics
# --------------------------------------------------------------------------- #


def test_rollback_targets_an_explicit_validated_id_not_generation_minus_one(db):
    """`generation - 1` is wrong whenever the sequence has gaps.

    Abandoned builds leave holes in an identity sequence, so decrementing can land
    on a deleted or never-validated id. This pins that an explicit id chosen from
    the validated set works across a gap, and that the arithmetic guess does not.
    """
    import psycopg2

    first = _publish(db)

    abandoned = _generation(db)  # creates a gap
    with db.cursor() as cursor:
        cursor.execute("DELETE FROM model_pricing_generations WHERE generation_id=%s", (abandoned,))

    second = _generation(db, manifest=[KEY], content_sha256=OTHER_SHA)
    _insert_rate(db, second)
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s", (second,))
    _activate(db, second)

    assert second - 1 == abandoned, "the gap is the premise of this test"

    # The naive guess targets the deleted generation and is refused.
    with pytest.raises((psycopg2.errors.RaiseException, psycopg2.errors.ForeignKeyViolation)):
        _activate(db, second - 1)

    # The explicit, validated id works.
    _activate(db, first)
    with db.cursor() as cursor:
        cursor.execute("SELECT current_generation_id FROM model_pricing_active")
        assert cursor.fetchone()[0] == first


def test_rolled_back_generation_rates_are_still_intact(db):
    """Rollback is a pointer move, not a data change: the old rates are still there."""
    first = _publish(db)
    with db.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE generation_id=%s", (first,))
        before = cursor.fetchone()[0]

    second = _generation(db, manifest=[KEY], content_sha256=OTHER_SHA)
    _insert_rate(db, second)
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s", (second,))
    _activate(db, second)
    _activate(db, first)

    with db.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE generation_id=%s", (first,))
        assert cursor.fetchone()[0] == before


def test_pointer_revision_increments_on_every_move(db):
    """The counter is what optimistic concurrency keys on."""
    first = _publish(db)
    second = _generation(db, manifest=[KEY], content_sha256=OTHER_SHA)
    _insert_rate(db, second)
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s", (second,))
    _activate(db, second)
    _activate(db, first)

    with db.cursor() as cursor:
        cursor.execute("SELECT pointer_revision FROM model_pricing_active")
        assert cursor.fetchone()[0] == 3


# --------------------------------------------------------------------------- #
# Transactional atomicity + concurrency
# --------------------------------------------------------------------------- #


def test_a_failed_publication_leaves_no_partial_generation(db, pg_url):
    """A whole publication is one transaction: it lands completely or not at all.

    The failure injected here is the realistic one — a bad row rejected by a CHECK
    partway through populating. What must not survive is a generation row with a
    subset of its rates, which a later operator could mistake for a usable set.
    """
    import psycopg2

    connection = psycopg2.connect(pg_url)
    connection.autocommit = False
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO model_pricing_generations
                    (schema_version, policy_version, snapshot_version, status, required_variants, content_sha256)
                VALUES (2, 1, '2026-09-12.1', 'building', %s::jsonb, %s)
                RETURNING generation_id
                """,
                (json.dumps([KEY]), SHA),
            )
            generation_id = cursor.fetchone()[0]
            cursor.execute(
                """
                INSERT INTO model_pricing_rates_v2
                    (generation_id, model_id, geography, service_tier, context_tier, region,
                     max_input_tokens, input_price_per_1k_tokens, output_price_per_1k_tokens,
                     cache_write_policy, source, source_url, source_content_sha256, verified_at, snapshot_version)
                VALUES (%s, 'openai.gpt-sol-1', 'in_region', 'standard', 'short', 'us-west-2',
                        272000, 0.0022, 0.0176, 'unpublished', 'bundled_snapshot',
                        'https://example.invalid/p', %s, now(), '2026-09-12.1')
                """,
                (generation_id, SHA),
            )
            with pytest.raises(psycopg2.errors.CheckViolation):
                # cache_write_policy='unpublished' with a zero price — the
                # under-billing shape the constraint exists to reject.
                cursor.execute(
                    """
                    INSERT INTO model_pricing_rates_v2
                        (generation_id, model_id, geography, service_tier, context_tier, region,
                         max_input_tokens, input_price_per_1k_tokens, output_price_per_1k_tokens,
                         cache_write_price_per_1k_tokens, cache_write_policy, source, source_url,
                         source_content_sha256, verified_at, snapshot_version)
                    VALUES (%s, 'openai.gpt-sol-1', 'in_region', 'standard', 'long', 'us-west-2',
                            1000000, 0.0044, 0.0352, 0, 'unpublished', 'bundled_snapshot',
                            'https://example.invalid/p', %s, now(), '2026-09-12.1')
                    """,
                    (generation_id, SHA),
                )
        connection.rollback()
    finally:
        connection.close()

    with db.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_generations")
        assert cursor.fetchone()[0] == 0, "the generation row must not survive its failed publication"
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2")
        assert cursor.fetchone()[0] == 0
        cursor.execute("SELECT current_generation_id FROM model_pricing_active")
        assert cursor.fetchone()[0] is None


def test_two_racing_publishers_serialize_on_the_pointer_lock(db, pg_url):
    """`SELECT ... FOR UPDATE` on the singleton is what makes publication atomic.

    Two refresh Lambdas can overlap (a retry alongside the scheduled run). Both
    build their own generation — that part is safe and independent — but the
    pointer move must serialize, so the loser waits and then observes the winner's
    revision rather than clobbering it. Both generations survive; exactly one is
    active; the revision counter reflects both moves in order.
    """
    import psycopg2

    first = _generation(db, manifest=[KEY])
    _insert_rate(db, first)
    second = _generation(db, manifest=[KEY], content_sha256=OTHER_SHA)
    _insert_rate(db, second)
    with db.cursor() as cursor:
        cursor.execute(
            "UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id = ANY(%s)",
            ([first, second],),
        )

    publisher_a = psycopg2.connect(pg_url)
    publisher_b = psycopg2.connect(pg_url)
    publisher_a.autocommit = False
    publisher_b.autocommit = False
    try:
        with publisher_a.cursor() as cursor_a:
            cursor_a.execute("SELECT pointer_revision FROM model_pricing_active WHERE singleton FOR UPDATE")
            revision_a = cursor_a.fetchone()[0]

        # B blocks on the same lock. A short timeout proves it is genuinely
        # waiting rather than reading through, without hanging the suite.
        with publisher_b.cursor() as cursor_b:
            cursor_b.execute("SET LOCAL lock_timeout = '400ms'")
            with pytest.raises(psycopg2.errors.LockNotAvailable):
                cursor_b.execute("SELECT pointer_revision FROM model_pricing_active WHERE singleton FOR UPDATE")
        publisher_b.rollback()

        with publisher_a.cursor() as cursor_a:
            cursor_a.execute(
                "UPDATE model_pricing_active SET current_generation_id=%s, pointer_revision=%s, updated_at=now() WHERE singleton",
                (first, revision_a + 1),
            )
        publisher_a.commit()

        # Now B proceeds and sees A's committed revision, not the stale one.
        with publisher_b.cursor() as cursor_b:
            cursor_b.execute("SELECT pointer_revision, current_generation_id FROM model_pricing_active WHERE singleton FOR UPDATE")
            revision_b, active_b = cursor_b.fetchone()
            assert revision_b == revision_a + 1, "the loser must observe the winner's revision"
            assert active_b == first
            cursor_b.execute(
                "UPDATE model_pricing_active SET current_generation_id=%s, pointer_revision=%s, updated_at=now() WHERE singleton",
                (second, revision_b + 1),
            )
        publisher_b.commit()
    finally:
        publisher_a.close()
        publisher_b.close()

    with db.cursor() as cursor:
        cursor.execute("SELECT current_generation_id, pointer_revision FROM model_pricing_active")
        assert cursor.fetchone() == (second, 2)
        cursor.execute("SELECT count(*) FROM model_pricing_generations")
        assert cursor.fetchone()[0] == 2, "both publishers' generations survive; only the pointer serializes"


def test_an_operator_rollback_is_not_undone_by_an_in_flight_refresh(db, pg_url):
    """`refresh_paused` has to be observed *inside* the publisher's lock.

    The scenario: an operator rolls back to a known-good generation and pauses
    refresh, while a refresh Lambda is already mid-run. If the Lambda checked the
    flag before taking the lock, it would re-activate the bad generation and undo
    the rollback — the operator's stop button would not work. Reading the flag
    under the same `FOR UPDATE` that guards the move is what closes that window.
    """
    import psycopg2

    good = _publish(db)
    bad = _generation(db, manifest=[KEY], content_sha256=OTHER_SHA)
    _insert_rate(db, bad)
    with db.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s", (bad,))

    operator = psycopg2.connect(pg_url)
    refresher = psycopg2.connect(pg_url)
    operator.autocommit = False
    refresher.autocommit = False
    try:
        # Operator: roll back to `good` and pause, in one transaction.
        with operator.cursor() as cursor:
            cursor.execute("SELECT pointer_revision FROM model_pricing_active WHERE singleton FOR UPDATE")
            revision = cursor.fetchone()[0]
            cursor.execute(
                "UPDATE model_pricing_active SET current_generation_id=%s, refresh_paused=TRUE, pointer_revision=%s WHERE singleton",
                (good, revision + 1),
            )
        operator.commit()

        # Refresher takes the lock afterwards and re-reads the flag there.
        with refresher.cursor() as cursor:
            cursor.execute("SELECT refresh_paused, current_generation_id FROM model_pricing_active WHERE singleton FOR UPDATE")
            paused, active = cursor.fetchone()
            assert paused is True, "the flag must be visible to the publisher inside its own lock"
            assert active == good
            if not paused:  # pragma: no cover - the publisher's own guard
                cursor.execute("UPDATE model_pricing_active SET current_generation_id=%s WHERE singleton", (bad,))
        refresher.rollback()
    finally:
        operator.close()
        refresher.close()

    with db.cursor() as cursor:
        cursor.execute("SELECT current_generation_id, refresh_paused FROM model_pricing_active")
        assert cursor.fetchone() == (good, True), "the rollback must survive the in-flight refresh"


# --------------------------------------------------------------------------- #
# Reversibility + repeatability
# --------------------------------------------------------------------------- #


def test_downgrade_removes_only_what_this_migration_created(db, pg_url):
    """The legacy table and its rows must be untouched in both directions."""
    with db.cursor() as cursor:
        cursor.execute(
            """
            INSERT INTO model_pricing (model_id, input_price_per_1k_tokens, output_price_per_1k_tokens, source, updated_at)
            VALUES ('anthropic.claude-sonnet-4-5-20250929-v1:0', 0.003, 0.015, 'fallback', now())
            ON CONFLICT (model_id) DO NOTHING
            """
        )
        cursor.execute("SELECT count(*) FROM model_pricing")
        legacy_rows = cursor.fetchone()[0]

    downgrade(pg_url, PREVIOUS)

    with db.cursor() as cursor:
        cursor.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='public' AND table_name LIKE 'model_pricing%' ORDER BY 1")
        assert [row[0] for row in cursor.fetchall()] == ["model_pricing"]

        cursor.execute("SELECT count(*) FROM model_pricing")
        assert cursor.fetchone()[0] == legacy_rows

        # The trigger functions go with the tables; leaving them behind would make
        # a re-upgrade's CREATE OR REPLACE silently inherit a stale definition.
        cursor.execute("SELECT count(*) FROM pg_proc WHERE proname LIKE 'model_pricing_%'")
        assert cursor.fetchone()[0] == 0


def test_upgrade_downgrade_upgrade_is_clean(db, pg_url):
    """Re-applying after a rollback must not trip over leftovers."""
    downgrade(pg_url, PREVIOUS)
    upgrade(pg_url, REVISION)

    with db.cursor() as cursor:
        cursor.execute("SELECT singleton, current_generation_id, pointer_revision FROM model_pricing_active")
        assert cursor.fetchall() == [(True, None, 0)]
        cursor.execute("SELECT count(*) FROM information_schema.triggers WHERE event_object_table LIKE 'model_pricing_%'")
        assert cursor.fetchone()[0] > 0


def test_the_chain_reaches_044_from_base_and_continues_to_head(pg_url):
    """044 must sit correctly *in* the real chain, not just apply alone.

    Applying a migration against an empty database hides ordering mistakes, so this
    runs every revision in order. Deliberately not asserting that 044 is head: 045
    follows it, and pinning "044 is the last revision" would turn every future
    migration into a failure here. What matters is that stopping at 044 works and
    that continuing past it also works.
    """
    upgrade(pg_url, REVISION)
    result = run_alembic(pg_url, "current")
    assert result.returncode == 0, result.stderr
    assert REVISION in result.stdout, f"expected {REVISION} to be applied, got: {result.stdout}"

    upgrade(pg_url, "head")
    result = run_alembic(pg_url, "current")
    assert result.returncode == 0, result.stderr
    assert "(head)" in result.stdout, f"the chain must reach head from 044, got: {result.stdout}"
