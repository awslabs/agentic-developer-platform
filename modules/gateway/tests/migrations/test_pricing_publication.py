"""Real PostgreSQL publication, retained provenance and concurrency regression."""

import importlib.util
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from pricing_policy.refresh import canonical_content_hash
from tests.migrations.conftest_postgres import upgrade

pytest.importorskip("psycopg2")
import psycopg2

path = Path(__file__).resolve().parents[2] / "lambda" / "pricing-refresh" / "publication.py"
spec = importlib.util.spec_from_file_location("pricing_publication_test_adapter", path)
adapter = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = adapter
spec.loader.exec_module(adapter)


@pytest.fixture
def database(pg_url):
    upgrade(pg_url, "head")
    with psycopg2.connect(pg_url) as connection:
        yield connection


def fresh(row, verified=None):
    # Publication rejects older verification: fresh fixtures must follow the seed.
    if verified is None:
        verified = (datetime.fromisoformat(row.verified_at.replace("Z", "+00:00")) + timedelta(days=1)).isoformat()
    return replace(row, source="bulk_catalog" if ".gpt-oss-" in row.model_id else "model_card", snapshot_version=None, verified_at=verified)


def test_full_generation_readback_hash_and_immutability(database):
    before = adapter.read_active(database)
    rows = tuple(fresh(row) for row in before.rows)
    required = frozenset(row.variant_key for row in before.rows)
    generation, revision, candidate = adapter.publish(database, before.revision, rows, required)
    database.commit()
    current = adapter.read_active(database)
    assert current.generation_id == generation and current.revision == revision == before.revision + 1
    assert canonical_content_hash(current.rows) == candidate.content_sha256
    assert not candidate.retained_keys
    with pytest.raises(psycopg2.Error):
        with database.cursor() as cursor:
            cursor.execute("UPDATE model_pricing_rates_v2 SET verified_at=now() WHERE generation_id=%s", (generation,))
    database.rollback()


def test_partial_preserves_retained_ages_and_complete_coverage(database):
    before = adapter.read_active(database)
    required = frozenset(row.variant_key for row in before.rows)
    _, _, candidate = adapter.publish(database, before.revision, (fresh(before.rows[0]),), required)
    database.commit()
    after = {row.variant_key: row for row in adapter.read_active(database).rows}
    assert set(after) == required
    for row in before.rows[1:]:
        assert after[row.variant_key].verified_at == row.verified_at
        assert after[row.variant_key].source_content_sha256 == row.source_content_sha256
        assert after[row.variant_key].snapshot_version == row.snapshot_version
    assert len(candidate.retained_keys) == len(before.rows) - 1


def test_stale_revision_cannot_overwrite_winner_or_undo_pause(database):
    before = adapter.read_active(database)
    rows = tuple(fresh(row) for row in before.rows)
    required = frozenset(row.variant_key for row in before.rows)
    winner, _, _ = adapter.publish(database, before.revision, rows, required)
    database.commit()
    with pytest.raises(adapter.PointerConflictError):
        adapter.publish(database, before.revision, rows, required)
    database.rollback()
    assert adapter.read_active(database).generation_id == winner
    with database.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_active SET refresh_paused=true")
    database.commit()
    with pytest.raises(adapter.RefreshDeferredError):
        adapter.publish(database, before.revision + 1, rows, required)
    database.rollback()


def test_publication_failure_rolls_back_pointer_and_building_rows(database, monkeypatch):
    before = adapter.read_active(database)
    database.commit()
    with database.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_generations")
        before_count = cursor.fetchone()[0]
    monkeypatch.setattr(
        adapter,
        "canonical_content_hash",
        lambda rows: canonical_content_hash(rows) if rows[0].generation_id == before.generation_id else "wrong-stored-hash",
    )
    with pytest.raises(RuntimeError, match="content hash mismatch"):
        adapter.publish(database, before.revision, tuple(fresh(row) for row in before.rows), frozenset(row.variant_key for row in before.rows))
    database.rollback()
    assert adapter.read_active(database).generation_id == before.generation_id
    with database.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_generations")
        assert cursor.fetchone()[0] == before_count


def _activate_cloned_generation(database, *, snapshot_version, schema_version=2, content_hash=None):
    """Build another generation legally, then activate only after its rows exist."""
    before = adapter.read_active(database)
    with database.cursor() as cursor:
        cursor.execute(
            """INSERT INTO model_pricing_generations
                (schema_version,policy_version,snapshot_version,status,required_variants,content_sha256)
                SELECT %s,policy_version,%s,'building',required_variants,COALESCE(%s,content_sha256)
                FROM model_pricing_generations WHERE generation_id=%s RETURNING generation_id""",
            (schema_version, snapshot_version, content_hash, before.generation_id),
        )
        generation = cursor.fetchone()[0]
        columns = (
            "model_id,geography,service_tier,context_tier,region,max_input_tokens,"
            "input_price_per_1k_tokens,output_price_per_1k_tokens,cache_read_price_per_1k_tokens,"
            "cache_write_price_per_1k_tokens,cache_write_policy,source,source_url,source_content_sha256,"
            "source_effective_at,verified_at,snapshot_version"
        )
        cursor.execute(
            "INSERT INTO model_pricing_rates_v2 (generation_id,"
            + columns
            + ") SELECT %s,"
            + columns
            + " FROM model_pricing_rates_v2 WHERE generation_id=%s",
            (generation, before.generation_id),
        )
        cursor.execute("UPDATE model_pricing_generations SET status='validated',validated_at=now() WHERE generation_id=%s", (generation,))
        cursor.execute("UPDATE model_pricing_active SET current_generation_id=%s,pointer_revision=pointer_revision+1", (generation,))


@pytest.mark.parametrize("snapshot_version", ["2026-09-12.1", "refresh-invalid"])
def test_active_seed_and_refresh_hashes_are_both_validated(database, snapshot_version):
    _activate_cloned_generation(database, snapshot_version=snapshot_version, content_hash="0" * 64)
    with pytest.raises(RuntimeError, match="active generation content hash mismatch"):
        adapter.read_active(database)


def test_future_schema_is_rejected_before_pricing(database):
    # Simulate a future migration permitting schema 3. This isolated test does
    # not disable immutability triggers or modify an existing validated row.
    with database.cursor() as cursor:
        cursor.execute("ALTER TABLE model_pricing_generations DROP CONSTRAINT ck_generations_schema_version")
    _activate_cloned_generation(database, snapshot_version="future-schema", schema_version=3)
    with pytest.raises(RuntimeError, match="incompatible schema version"):
        adapter.read_active(database)
