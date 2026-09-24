"""New pricing coverage preserves historical rates and converges on PostgreSQL."""

import importlib.util
from decimal import Decimal
from pathlib import Path

from pricing_policy import load_snapshot
from tests.migrations.conftest_postgres import upgrade
from tests.migrations.test_pricing_publication import adapter

PATH = Path(__file__).parents[2] / "alembic/versions/070_opus55_pricing.py"
spec = importlib.util.spec_from_file_location("opus55_seed", PATH)
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def test_frozen_seed_matches_audited_opus_snapshot():
    expected = {r.variant_key: r for r in load_snapshot("2026-09-24.1").rates if r.model_id == "anthropic.claude-opus-5-5"}
    actual = migration._seed_candidate()
    assert len(actual) == 53 and actual.keys() == expected.keys()
    for key, row in actual.items():
        original = expected[key]
        for field in (
            "input_price_per_1k_tokens",
            "output_price_per_1k_tokens",
            "cache_read_price_per_1k_tokens",
            "cache_write_price_per_1k_tokens",
            "cache_write_1h_price_per_1k_tokens",
        ):
            assert Decimal(row[field]) == getattr(original, field)
        assert row["verified_at"] == original.verified_at
        assert row["source_content_sha256"] == original.source_content_sha256


def test_upgrade_preserves_existing_prices_and_repeated_seed_is_noop(pg_url, connect):
    import sqlalchemy as sa

    upgrade(pg_url, "069_aws_verification_binding")
    conn = connect()
    before = adapter.read_active(conn)
    upgrade(pg_url, "head")
    after = adapter.read_active(conn)
    assert len(after.rows) == len(before.rows) + 53
    indexed = {r.variant_key: r for r in after.rows}
    for prior in before.rows:
        new = indexed[prior.variant_key]
        assert new.input_price_per_1k_tokens == prior.input_price_per_1k_tokens
        assert new.verified_at == prior.verified_at
    engine = sa.create_engine(pg_url)
    try:
        with engine.begin() as c:
            migration._seed(c)
    finally:
        engine.dispose()
    assert adapter.read_active(conn).revision == after.revision
