"""Frozen Claude seed and upgrade compatibility on real PostgreSQL."""

import ast
import importlib.util
import json
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest
import sqlalchemy as sa

from alembic.script import ScriptDirectory
from pricing_policy import RateRow, load_snapshot
from pricing_policy.refresh import canonical_content_hash
from tests.migrations.conftest_postgres import downgrade, upgrade
from tests.migrations.test_pricing_publication import adapter

ROOT = Path(__file__).resolve().parents[2]
PATH = ROOT / "alembic/versions/047_claude_pricing_v2.py"
PRIOR = "046_merge_pricing_ratelimit"
REVISION = "047_claude_pricing_v2"
spec = importlib.util.spec_from_file_location("frozen_claude_seed", PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def _seed(url):
    engine = sa.create_engine(url)
    try:
        with engine.begin() as connection:
            migration._seed(connection)
    finally:
        engine.dispose()


def test_seed_is_frozen_and_independent_of_runtime_selector():
    frozen = json.loads((ROOT / "pricing_policy/snapshots/2026-09-12.2.json").read_text())
    expected = {
        tuple(row[key] for key in ("model_id", "geography", "service_tier", "context_tier", "region")): row
        for row in frozen["rates"]
        if row["model_id"].startswith("anthropic.")
    }
    candidate = migration._seed_candidate()
    assert len(candidate) == 1006 and set(candidate) == set(expected) == set(migration.REQUIRED_VARIANTS)
    for key, actual in candidate.items():
        original = expected[key]
        assert actual["source"] == "bundled_snapshot" and actual["_origin_source"] == "pricing_page"
        for column, value in original.items():
            if column != "source":
                assert actual[column] == value, (key, column)
    imports = []
    for node in ast.walk(ast.parse(PATH.read_text())):
        if isinstance(node, ast.Import):
            imports.extend(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module.split(".")[0])
    assert set(imports) <= {"datetime", "collections", "decimal", "sqlalchemy", "alembic", "json", "hashlib"}
    assert migration.POLICY_VERSION == migration.BUNDLE_REVISION == 2


def test_seed_keeps_fetched_unknown_and_newer_existing_rows():
    candidate = migration._seed_candidate()
    key = next(iter(candidate))
    original = candidate[key]
    for current in (
        {**original, "source": "pricing_page", "input_price_per_1k_tokens": ".009"},
        {**original, "source": "bulk_catalog"},
        {**original, "source": "model_card"},
        {**original, "snapshot_version": "future-unrecognized"},
        {**original, "snapshot_version": "2026-09-12.2"},
    ):
        merged, _ = migration._merge({key: original}, {key: current})
        assert merged[key] == current
    prior = {**original, "snapshot_version": "2026-09-12.1", "input_price_per_1k_tokens": ".009"}
    assert migration._merge({key: original}, {key: prior})[0][key] == original


def test_fresh_chain_seeds_complete_combined_generation_and_retries_are_noop(pg_url, connect):
    upgrade(pg_url, "head")
    conn = connect()
    state = adapter.read_active(conn)
    assert len(state.rows) == 1336
    assert len([row for row in state.rows if row.model_id.startswith("anthropic.")]) == 1006
    with conn.cursor() as cursor:
        cursor.execute("SELECT policy_version,content_sha256 FROM model_pricing_generations WHERE generation_id=%s", (state.generation_id,))
        policy, digest = cursor.fetchone()
        assert policy == 2 and digest == canonical_content_hash(state.rows)
        cursor.execute("SELECT to_jsonb(t) FROM model_pricing t ORDER BY model_id")
        legacy = cursor.fetchall()
        cursor.execute("SELECT count(*) FROM model_pricing_generations")
        count = cursor.fetchone()[0]
    _seed(pg_url)
    after = adapter.read_active(conn)
    assert (after.generation_id, after.revision) == (state.generation_id, state.revision)
    with conn.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_generations")
        assert cursor.fetchone()[0] == count
        cursor.execute("SELECT to_jsonb(t) FROM model_pricing t ORDER BY model_id")
        assert cursor.fetchall() == legacy
    frozen = {row.variant_key: row for row in load_snapshot("2026-09-12.2").rates}
    for row in state.rows:
        if row.model_id.startswith("anthropic."):
            expected = replace(frozen[row.variant_key], source="bundled_snapshot", generation_id=state.generation_id)
            assert datetime.fromisoformat(row.verified_at) == datetime.fromisoformat(expected.verified_at)
            assert datetime.fromisoformat(row.source_effective_at) == datetime.fromisoformat(expected.source_effective_at)
            assert replace(row, verified_at=expected.verified_at, source_effective_at=expected.source_effective_at) == expected


def test_policy_one_column_read_and_old_generation_hash_survive_upgrade(pg_url, connect):
    upgrade(pg_url, PRIOR)
    conn = connect()
    before = adapter.read_active(conn)
    assert len(before.rows) == 330 and all(row.cache_write_1h_price_per_1k_tokens is None for row in before.rows)
    with conn.cursor() as cursor:
        cursor.execute("SELECT content_sha256 FROM model_pricing_generations WHERE generation_id=%s", (before.generation_id,))
        old_hash = cursor.fetchone()[0]
    upgrade(pg_url, REVISION)
    with conn.cursor() as cursor:
        from psycopg2.extras import RealDictCursor

        with conn.cursor(cursor_factory=RealDictCursor) as rows_cursor:
            rows_cursor.execute("SELECT * FROM model_pricing_rates_v2 WHERE generation_id=%s", (before.generation_id,))
            old_rows = tuple(RateRow.from_mapping(dict(row)) for row in rows_cursor.fetchall())
        assert old_rows == before.rows
        assert canonical_content_hash(old_rows) == old_hash
    after = {row.variant_key: row for row in adapter.read_active(conn).rows}
    for row in before.rows:
        assert replace(after[row.variant_key], generation_id=row.generation_id) == row


@pytest.mark.parametrize("state_sql", ["refresh_paused=true", "consumers_enabled=false"])
def test_operator_pause_or_disable_is_preserved_and_seed_deferred(pg_url, connect, state_sql):
    upgrade(pg_url, PRIOR)
    conn = connect()
    with conn.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_active SET " + state_sql)
        cursor.execute("SELECT current_generation_id,pointer_revision,consumers_enabled,refresh_paused FROM model_pricing_active")
        before = cursor.fetchone()
    upgrade(pg_url, REVISION)
    with conn.cursor() as cursor:
        cursor.execute("SELECT current_generation_id,pointer_revision,consumers_enabled,refresh_paused FROM model_pricing_active")
        assert cursor.fetchone() == before
        cursor.execute("UPDATE model_pricing_active SET refresh_paused=false,consumers_enabled=true")
    # An intentional unpause permits the same idempotent seed/convergence rules.
    _seed(pg_url)
    assert len(adapter.read_active(conn).rows) == 1336


def test_policy_one_active_generation_can_publish_policy_two_after_schema_upgrade(pg_url, connect):
    upgrade(pg_url, PRIOR)
    conn = connect()
    before = adapter.read_active(conn)
    with conn.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_active SET refresh_paused=true")
    upgrade(pg_url, REVISION)
    with conn.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_active SET refresh_paused=false")
    conn.autocommit = False
    rows = tuple(
        replace(row, source="pricing_page" if row.model_id.startswith("anthropic.") else row.source, snapshot_version=None)
        for row in load_snapshot().rates
    )
    generation, _, candidate = adapter.publish(conn, before.revision, rows, frozenset(row.variant_key for row in rows))
    conn.commit()
    assert len(adapter.read_active(conn).rows) == 1336
    with conn.cursor() as cursor:
        cursor.execute("SELECT policy_version,content_sha256 FROM model_pricing_generations WHERE generation_id=%s", (generation,))
        assert cursor.fetchone() == (2, candidate.content_sha256)


def test_one_hour_precision_and_invalid_values_are_enforced_by_postgres(pg_url, connect):
    import psycopg2

    upgrade(pg_url, REVISION)
    conn = connect()
    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO model_pricing_generations(schema_version,policy_version,snapshot_version,status,required_variants,content_sha256) "
            "SELECT 2,2,'test-building','building',required_variants,%s FROM model_pricing_generations LIMIT 1 RETURNING generation_id",
            ("a" * 64,),
        )
        generation = cursor.fetchone()[0]
        cursor.execute(
            "INSERT INTO model_pricing_rates_v2 SELECT %s,model_id,geography,service_tier,context_tier,region,max_input_tokens,"
            "input_price_per_1k_tokens,output_price_per_1k_tokens,cache_read_price_per_1k_tokens,cache_write_price_per_1k_tokens,"
            "cache_write_policy,source,source_url,source_content_sha256,source_effective_at,verified_at,snapshot_version,"
            "cache_write_1h_price_per_1k_tokens FROM model_pricing_rates_v2 LIMIT 1",
            (generation,),
        )
        cursor.execute(
            "UPDATE model_pricing_rates_v2 SET cache_write_1h_price_per_1k_tokens=%s,source=%s WHERE generation_id=%s",
            ("0.0000123456", "pricing_page", generation),
        )
        cursor.execute("SELECT cache_write_1h_price_per_1k_tokens FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation,))
        assert cursor.fetchone()[0] == Decimal("0.0000123456")
        for value in ("-0.001", "NaN", "Infinity", "-Infinity"):
            with pytest.raises((psycopg2.errors.CheckViolation, psycopg2.errors.NumericValueOutOfRange)):
                cursor.execute("UPDATE model_pricing_rates_v2 SET cache_write_1h_price_per_1k_tokens=%s WHERE generation_id=%s", (value, generation))
        for value in (None, Decimal("0")):
            cursor.execute("UPDATE model_pricing_rates_v2 SET cache_write_1h_price_per_1k_tokens=%s WHERE generation_id=%s", (value, generation))
            cursor.execute("SELECT cache_write_1h_price_per_1k_tokens FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation,))
            assert cursor.fetchone()[0] == value


def test_upgrade_preserves_fetched_openai_prices_age_and_provenance(pg_url, connect):
    from psycopg2.extras import execute_values

    upgrade(pg_url, PRIOR)
    conn = connect()
    old = adapter.read_active(conn)
    fetched = replace(
        old.rows[0],
        input_price_per_1k_tokens=Decimal("0.0001234567"),
        cache_read_price_per_1k_tokens=None,
        cache_write_price_per_1k_tokens=None,
        cache_write_policy="unpublished",
        source="model_card",
        snapshot_version=None,
        verified_at="2026-09-13T06:00:00+00:00",
        source_content_sha256="f" * 64,
    )
    rows = (fetched, *old.rows[1:])
    columns = (
        "model_id",
        "geography",
        "service_tier",
        "context_tier",
        "region",
        "max_input_tokens",
        "input_price_per_1k_tokens",
        "output_price_per_1k_tokens",
        "cache_read_price_per_1k_tokens",
        "cache_write_price_per_1k_tokens",
        "cache_write_policy",
        "source",
        "source_url",
        "source_content_sha256",
        "source_effective_at",
        "verified_at",
        "snapshot_version",
    )
    with conn.cursor() as cursor:
        cursor.execute(
            "INSERT INTO model_pricing_generations(schema_version,policy_version,snapshot_version,status,required_variants,content_sha256) "
            "VALUES(2,1,'test-fetched','building',%s::jsonb,%s) RETURNING generation_id",
            (json.dumps([row.variant_key for row in rows]), canonical_content_hash(rows)),
        )
        generation = cursor.fetchone()[0]
        execute_values(
            cursor,
            "INSERT INTO model_pricing_rates_v2(generation_id," + ",".join(columns) + ") VALUES %s",
            [(generation, *(getattr(row, column) for column in columns)) for row in rows],
        )
        cursor.execute("UPDATE model_pricing_generations SET status='validated',validated_at=now() WHERE generation_id=%s", (generation,))
        cursor.execute("UPDATE model_pricing_active SET current_generation_id=%s,pointer_revision=pointer_revision+1", (generation,))
    upgrade(pg_url, REVISION)
    current = {row.variant_key: row for row in adapter.read_active(conn).rows}
    actual = current[fetched.variant_key]
    assert datetime.fromisoformat(actual.verified_at) == datetime.fromisoformat(fetched.verified_at)
    assert replace(actual, generation_id=fetched.generation_id, verified_at=fetched.verified_at) == fetched
    assert len(current) == 1336


@pytest.mark.parametrize("operator_state", ["enabled", "paused", "disabled"])
def test_downgrade_and_reupgrade_preserve_schema_history_and_operator_state(pg_url, connect, operator_state):
    from psycopg2.extras import register_default_jsonb

    # Keep exercising the entire current chain when later migrations are added;
    # upgrading to head no longer implies that 047 is the version-table value.
    expected_head = ScriptDirectory(str(ROOT / "alembic")).get_current_head()
    upgrade(pg_url, "head")
    conn = connect()
    # PostgreSQL JSON numerics must stay Decimal when verifying historical hashes.
    register_default_jsonb(conn, loads=lambda value: json.loads(value, parse_float=Decimal))
    with conn.cursor() as cursor:
        if operator_state == "paused":
            cursor.execute("UPDATE model_pricing_active SET refresh_paused=true")
        elif operator_state == "disabled":
            cursor.execute("UPDATE model_pricing_active SET consumers_enabled=false")

    def history():
        with conn.cursor() as cursor:
            cursor.execute("SELECT to_jsonb(g) FROM model_pricing_generations g ORDER BY generation_id")
            generations = cursor.fetchall()
            cursor.execute(
                "SELECT to_jsonb(r) FROM model_pricing_rates_v2 r ORDER BY generation_id,model_id,geography,service_tier,context_tier,region"
            )
            rates = cursor.fetchall()
            cursor.execute("SELECT to_jsonb(a) FROM model_pricing_active a")
            pointer = cursor.fetchall()
        by_generation = {}
        for (row,) in rates:
            by_generation.setdefault(row["generation_id"], []).append(RateRow.from_mapping(row))
        for (generation,) in generations:
            assert canonical_content_hash(tuple(by_generation[generation["generation_id"]])) == generation["content_sha256"]
        return generations, rates, pointer

    before = history()
    for _ in range(2):
        downgrade(pg_url, PRIOR)
        assert history() == before
        with conn.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone()[0] == PRIOR
            cursor.execute(
                "SELECT numeric_precision,numeric_scale,is_nullable FROM information_schema.columns "
                "WHERE table_name='model_pricing_rates_v2' AND column_name='cache_write_1h_price_per_1k_tokens'"
            )
            assert cursor.fetchone() == (14, 10, "YES")
        upgrade(pg_url, "head")
        assert history() == before
        with conn.cursor() as cursor:
            cursor.execute("SELECT version_num FROM alembic_version")
            assert cursor.fetchone()[0] == expected_head
