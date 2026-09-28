"""GPT-6 publication preserves earlier prices, flags, and immutable generations."""

import importlib.util
from dataclasses import replace
from pathlib import Path

import sqlalchemy as sa

from pricing_policy import load_snapshot
from tests.migrations.conftest_postgres import upgrade
from tests.migrations.test_pricing_publication import adapter

PATH = Path(__file__).resolve().parents[2] / "alembic/versions/078_gpt6_sol_luna_pricing.py"
spec = importlib.util.spec_from_file_location("kimi_seed", PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_frozen_seed_matches_reviewed_snapshot():
    expected = {r.variant_key: r for r in load_snapshot("2026-09-28.1").rates if r.model_id in {"openai.gpt-6-sol", "openai.gpt-6-luna"}}
    seed = migration._seed_candidate()
    assert len(seed) == 152 and set(seed) == set(expected)
    for key, row in seed.items():
        from decimal import Decimal

        assert Decimal(row["input_price_per_1k_tokens"]) == expected[key].input_price_per_1k_tokens
        assert Decimal(row["cache_write_price_per_1k_tokens"]) == expected[key].cache_write_price_per_1k_tokens
        assert row["source_content_sha256"] == expected[key].source_content_sha256


def test_upgrade_preserves_prices_and_is_idempotent(pg_url, connect):
    upgrade(pg_url, "077_codex_runtime_posture")
    conn = connect()
    before = adapter.read_active(conn)
    upgrade(pg_url, "078_gpt6_sol_luna_pricing")
    after = adapter.read_active(conn)
    assert len(after.rows) == len(before.rows) + 152
    rows = {r.variant_key: r for r in after.rows}
    for row in before.rows:
        assert replace(rows[row.variant_key], generation_id=row.generation_id) == row
    engine = sa.create_engine(pg_url)
    with engine.begin() as c:
        migration._seed(c)
    engine.dispose()
    assert adapter.read_active(conn).generation_id == after.generation_id


def test_paused_refresh_stays_paused(pg_url, connect):
    upgrade(pg_url, "077_codex_runtime_posture")
    conn = connect()
    with conn.cursor() as c:
        c.execute("UPDATE model_pricing_active SET refresh_paused=true")
        c.execute("SELECT current_generation_id,pointer_revision FROM model_pricing_active")
        before = c.fetchone()
    upgrade(pg_url, "078_gpt6_sol_luna_pricing")
    with conn.cursor() as c:
        c.execute("SELECT current_generation_id,pointer_revision,refresh_paused FROM model_pricing_active")
        assert c.fetchone() == (*before, True)
