"""Tests for Alembic migration 045 — the frozen 2026-09-12.1 seed.

Issue #4969 (S2). Two things are being defended here.

**The intentional duplicate stays honest.** 045 embeds the rates as a literal
rather than importing `pricing_policy`, because a migration is a historical record:
if it imported the runtime package, editing that package later would silently
change what an already-applied migration means, and two fresh installs a year
apart would seed different rates under the same `alembic_version` row. The price of
that correctness is a duplicate, and these tests are what keep it from drifting.

The comparison is deliberately against the **specific immutable version**
`2026-09-12.1`, read from disk by name — never against
`pricing_policy.CURRENT_SNAPSHOT_VERSION`. Comparing an old migration to a mutable
"current" selector is the mistake that would make this suite fail the day a
*newer* snapshot ships, pressuring someone to "fix" it by editing the frozen
literal — which is exactly the thing that must never change.
`test_parity_is_pinned_to_a_specific_version_not_the_current_selector` proves the
pin holds by advancing the selector and re-checking.

**Convergence is safe.** A retried deploy, or a deploy landing after a refresh has
already run, must never downgrade a freshly fetched rate to a bundled one or
overwrite a bundle it does not recognize.
"""

from __future__ import annotations

import datetime
import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

# Fixtures (pg_url, pg_server, connect) come from conftest.py; these are helpers.
from tests.migrations.conftest_postgres import run_alembic, upgrade

GATEWAY_ROOT = Path(__file__).resolve().parents[2]
SEED_REVISION = "045_pricing_seed_2026_09_12_1"
SCHEMA_REVISION = "044_model_pricing_v2"

# The snapshot this migration froze. A literal, by design: see the module
# docstring. If a newer snapshot ships, this constant does NOT move.
FROZEN_VERSION = "2026-09-12.1"


def _load_migration_module():
    """Import 045 directly, without importing the alembic package machinery.

    A file-path import rather than `from alembic.versions...` because the
    directory is not a package and the revision ids are not valid identifiers.
    """
    path = GATEWAY_ROOT / "alembic" / "versions" / "045_pricing_seed_2026_09_12_1.py"
    spec = importlib.util.spec_from_file_location("migration_045", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_frozen_snapshot() -> dict:
    """Read the pinned snapshot JSON straight off disk, by explicit version.

    Deliberately NOT `pricing_policy.load_snapshot()`: that resolves through the
    package's current-version default, and this test must compare against one
    specific immutable file no matter what the runtime currently prefers.
    """
    path = GATEWAY_ROOT / "pricing_policy" / "snapshots" / f"{FROZEN_VERSION}.json"
    return json.loads(path.read_text())


@pytest.fixture(scope="module")
def migration():
    return _load_migration_module()


@pytest.fixture(scope="module")
def snapshot():
    return _load_frozen_snapshot()


def _variant_key(row: dict) -> tuple:
    return (row["model_id"], row["geography"], row["service_tier"], row["context_tier"], row["region"])


def _snapshot_rows_by_key(snapshot: dict) -> dict[tuple, dict]:
    return {_variant_key(row): row for row in snapshot["rates"]}


def _migration_rows_by_key(migration) -> dict[tuple, dict]:
    rows = {}
    for row in migration.SEED_ROWS:
        rows[(row[0], row[1], row[2], row[3], row[4])] = {
            "model_id": row[0],
            "geography": row[1],
            "service_tier": row[2],
            "context_tier": row[3],
            "region": row[4],
            "max_input_tokens": row[5],
            "input_price_per_1k_tokens": row[6],
            "output_price_per_1k_tokens": row[7],
            "cache_read_price_per_1k_tokens": row[8],
            "cache_write_price_per_1k_tokens": row[9],
            "cache_write_policy": row[10],
            "source": row[11],
            "source_url": row[12],
            "source_content_sha256": row[13],
            "source_effective_at": row[14],
        }
    return rows


# --------------------------------------------------------------------------- #
# Frozen-seed parity
# --------------------------------------------------------------------------- #


def test_migration_does_not_import_runtime_pricing_code(migration):
    """The seed must be self-contained: no `pricing_policy`, no `lambda/shared`.

    Checked by walking the AST for every `import` node, including ones nested
    inside functions — a deferred import would not appear as a module attribute
    and would still make this migration's meaning depend on mutable runtime code.

    Deliberately NOT a substring search over the source: the module docstring
    legitimately *names* `pricing_policy` to explain why it does not import it, and
    a text match would fail on the explanation itself. Matching imports is both
    more precise and harder to evade.
    """
    import ast

    path = GATEWAY_ROOT / "alembic" / "versions" / "045_pricing_seed_2026_09_12_1.py"
    tree = ast.parse(path.read_text(), filename=str(path))

    forbidden_roots = {"pricing_policy", "src", "lambda", "shared"}
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    offenders = [name for name in imported if name.split(".")[0] in forbidden_roots or "pricing_fallback" in name]
    assert not offenders, f"045 imports runtime code {offenders}; a migration cannot depend on code that may change after it is applied"

    # Only Alembic + its normal DB dependencies + the stdlib, so the pod can run
    # `alembic upgrade head` without the application package installed.
    assert {name.split(".")[0] for name in imported} <= {"alembic", "sqlalchemy", "collections", "datetime", "decimal", "hashlib", "json"}


def test_migration_declares_the_frozen_version(migration):
    assert migration.SNAPSHOT_VERSION == FROZEN_VERSION
    assert migration.SCHEMA_VERSION == 2
    assert migration.POLICY_VERSION == 1
    assert migration.down_revision == SCHEMA_REVISION


def test_every_variant_key_matches_the_frozen_snapshot(migration, snapshot):
    """Coverage is a set of variant keys, not a model count.

    A count check passes while a model is missing its long-context or GovCloud
    rate — the exact gap that would silently price those requests off a fallback.
    """
    migration_keys = set(_migration_rows_by_key(migration))
    snapshot_keys = set(_snapshot_rows_by_key(snapshot))

    assert migration_keys == snapshot_keys, (
        f"seed/snapshot variant keys diverged.\nonly in migration: {sorted(migration_keys - snapshot_keys)}\n"
        f"only in snapshot: {sorted(snapshot_keys - migration_keys)}"
    )
    assert len(migration_keys) == 330, "the frozen inventory is 330 variants; a change here needs a new snapshot version, not an edit"


def test_every_rate_matches_the_frozen_snapshot_exactly(migration, snapshot):
    """Rate-by-rate equality as Decimal — this is the whole point of the duplicate.

    Compared as `Decimal`, so '0.0022' and '0.00220' agree, but 0.0171875 and
    0.017188 do not. A float comparison here could mask the very rounding error
    NUMERIC(14,10) exists to prevent.
    """
    migration_rows = _migration_rows_by_key(migration)
    snapshot_rows = _snapshot_rows_by_key(snapshot)

    mismatches = []
    for key, snapshot_row in snapshot_rows.items():
        migration_row = migration_rows[key]
        for column in (
            "input_price_per_1k_tokens",
            "output_price_per_1k_tokens",
            "cache_read_price_per_1k_tokens",
            "cache_write_price_per_1k_tokens",
        ):
            expected = snapshot_row.get(column)
            actual = migration_row.get(column)
            if expected is None or actual is None:
                # NULL is 'unpublished' and must match NULL exactly — never zero.
                if (expected is None) != (actual is None):
                    mismatches.append(f"{key} {column}: snapshot={expected!r} migration={actual!r} (NULL/zero confusion)")
                continue
            if Decimal(str(expected)) != Decimal(str(actual)):
                mismatches.append(f"{key} {column}: snapshot={expected!r} migration={actual!r}")

    assert not mismatches, "frozen seed drifted from snapshot " + FROZEN_VERSION + ":\n" + "\n".join(mismatches)


def test_policies_bounds_and_provenance_match_the_frozen_snapshot(migration, snapshot):
    """Not just prices: the cache tri-state, context bounds and source evidence.

    An operator auditing a charge needs to reach the AWS publication the number
    came from, so the original URL and content hash are part of the contract — a
    seed that kept the rates but lost the evidence would be unauditable.
    """
    migration_rows = _migration_rows_by_key(migration)
    snapshot_rows = _snapshot_rows_by_key(snapshot)

    for key, snapshot_row in snapshot_rows.items():
        migration_row = migration_rows[key]
        for column in (
            "cache_write_policy",
            "max_input_tokens",
            "source",
            "source_url",
            "source_content_sha256",
            "source_effective_at",
        ):
            assert migration_row.get(column) == snapshot_row.get(column), f"{key} {column} diverged from snapshot {FROZEN_VERSION}"


def test_required_variants_are_derived_from_the_rows_not_hand_listed(migration):
    """A hand-maintained manifest would drift from the rows it describes."""
    derived = {(row[0], row[1], row[2], row[3], row[4]) for row in migration.SEED_ROWS}
    assert set(migration.REQUIRED_VARIANTS) == derived
    assert len(migration.REQUIRED_VARIANTS) == len(migration.SEED_ROWS), "no duplicate variant keys"


def test_required_variants_match_the_snapshots_manifest(migration, snapshot):
    assert sorted(migration.REQUIRED_VARIANTS) == sorted(tuple(key) for key in snapshot["required_variants"])


def test_seed_content_hash_is_stable(migration):
    """The recorded content hash must depend only on content, not tuple order.

    It is written onto the generation row as provenance; if it moved with
    incidental ordering, two identical seeds would look different and an operator
    could not use it to confirm what a generation contains.
    """
    first = migration._canonical_content_hash(migration.SEED_ROWS)
    shuffled = tuple(reversed(migration.SEED_ROWS))
    assert migration._canonical_content_hash(shuffled) == first, "hash must be order-independent"
    assert len(first) == 64 and set(first) <= set("0123456789abcdef")

    # And it must actually distinguish content.
    mutated = list(migration.SEED_ROWS)
    row = list(mutated[0])
    row[6] = "9.9999"
    mutated[0] = tuple(row)
    assert migration._canonical_content_hash(tuple(mutated)) != first, "hash must change when a rate changes"


def test_verified_at_is_the_recorded_verification_time_not_deployment_now(migration, snapshot):
    """Backdating is deliberate: staleness detection depends on it.

    Stamping deployment `now()` would make a months-old seed look freshly
    verified, so a long-dormant branch would report stale rates as current — and
    the estimated-vs-verified distinction the ledger carries would be a lie.
    """
    assert migration.VERIFIED_AT == datetime.datetime(2026, 9, 12, tzinfo=datetime.UTC)
    assert migration.VERIFIED_AT.tzinfo is not None, "a naive timestamp must not reach a TIMESTAMPTZ column"
    assert migration.VERIFIED_AT_ISO == snapshot["provenance"]["verified_at"]
    for row in snapshot["rates"]:
        assert row["verified_at"] == migration.VERIFIED_AT_ISO


def test_rates_in_the_literal_are_strings_never_floats(migration):
    """`float('0.0000264')` is not exactly representable; a float here is a bug.

    Catching it in the literal matters more than catching it at the boundary: a
    float written into this file would be silently slightly wrong forever.
    """
    for row in migration.SEED_ROWS:
        for index in (6, 7, 8, 9):
            value = row[index]
            assert not isinstance(value, float), f"{row[0]} rate at index {index} is a float ({value!r}); rates must be decimal strings"
            if value is not None:
                assert isinstance(value, str)
                Decimal(value)  # must parse


def test_rate_coercion_refuses_floats(migration):
    """The binding helper must reject a float rather than quietly convert it."""
    assert migration._as_rate("0.0000264") == Decimal("0.0000264")
    assert migration._as_rate(None) is None
    with pytest.raises(TypeError):
        migration._as_rate(0.0000264)


def test_timestamp_coercion_refuses_naive_datetimes(migration):
    with pytest.raises(ValueError):
        migration._as_timestamp(datetime.datetime(2026, 9, 12))
    with pytest.raises(ValueError):
        migration._as_timestamp("2026-09-12T00:00:00")
    assert migration._as_timestamp("2026-09-12T00:00:00+00:00") == migration.VERIFIED_AT


def test_parity_is_pinned_to_a_specific_version_not_the_current_selector(migration, snapshot, monkeypatch):
    """Advancing the runtime's current-version selector must not affect the seed.

    This is the test the design note asks for explicitly. It simulates a future
    release shipping `2026-12-01.1` and re-runs the parity comparison; the frozen
    literal must still match `2026-09-12.1`, unchanged. If parity had been written
    against `CURRENT_SNAPSHOT_VERSION`, this would fail — and the tempting "fix"
    would be to edit the frozen rates, corrupting the historical record.
    """
    import pricing_policy.policy as policy

    monkeypatch.setattr(policy, "CURRENT_SNAPSHOT_VERSION", "2026-12-01.1")
    assert policy.CURRENT_SNAPSHOT_VERSION != FROZEN_VERSION

    # The migration is unaffected...
    reloaded = _load_migration_module()
    assert reloaded.SNAPSHOT_VERSION == FROZEN_VERSION

    # ...and parity still holds against the pinned file read by name.
    migration_rows = _migration_rows_by_key(reloaded)
    snapshot_rows = _snapshot_rows_by_key(_load_frozen_snapshot())
    assert set(migration_rows) == set(snapshot_rows)
    for key, snapshot_row in snapshot_rows.items():
        assert Decimal(migration_rows[key]["input_price_per_1k_tokens"]) == Decimal(snapshot_row["input_price_per_1k_tokens"])


def test_bundle_revision_ordering_is_explicit_not_lexicographic(migration):
    """String comparison of versions picks the wrong bundle.

    `'2026-09-12.10' < '2026-09-12.9'` as text, so a lexicographic check would
    treat revision 10 as older than 9 and refuse to apply a newer seed — or worse,
    overwrite newer data with older. The declared integer revision is what makes
    the ordering correct.
    """
    assert isinstance(migration.BUNDLE_REVISION, int)
    assert migration.BUNDLE_REVISION == 1
    assert migration.SUPPORTED_PREDECESSOR_VERSIONS == {}, "2026-09-12.1 is the first bundle; it supersedes no earlier bundled row"


# --------------------------------------------------------------------------- #
# Bootstrap on a clean database
# --------------------------------------------------------------------------- #


@pytest.fixture
def seeded(pg_url):
    """A database upgraded to the frozen 045 revision, with a live connection."""
    upgrade(pg_url, SEED_REVISION)
    import psycopg2

    connection = psycopg2.connect(pg_url)
    connection.autocommit = True
    try:
        yield connection
    finally:
        connection.close()


def test_a_fresh_deployment_is_priced_before_any_refresh_runs(seeded, migration):
    """The #1017 cold-start requirement: no scheduled tick needed to work.

    A fresh install must be able to price an OpenAI request the moment migrations
    finish. If this needed the daily EventBridge rule to fire first, every new
    deployment would mis-price for up to 24 hours.
    """
    with seeded.cursor() as cursor:
        cursor.execute("SELECT current_generation_id, consumers_enabled, refresh_paused FROM model_pricing_active")
        generation_id, enabled, paused = cursor.fetchone()

    assert generation_id is not None, "a fresh deployment must have an active generation"
    assert enabled is True, "consumers must be enabled in the same transaction as the seed"
    assert paused is False

    with seeded.cursor() as cursor:
        cursor.execute(
            "SELECT status, snapshot_version, jsonb_array_length(required_variants) FROM model_pricing_generations WHERE generation_id=%s",
            (generation_id,),
        )
        status, version, manifest_size = cursor.fetchone()

    assert status == "validated"
    assert version == FROZEN_VERSION
    assert manifest_size == len(migration.SEED_ROWS)


def test_seeded_rows_carry_bundled_source_with_original_aws_evidence(seeded):
    """`source` says how it got here; source_url/hash say where the number is from.

    Both are needed: the refresh must be able to tell a bundled placeholder from a
    fetched rate, and an operator must be able to reach the publication.
    """
    with seeded.cursor() as cursor:
        cursor.execute("SELECT DISTINCT source, snapshot_version FROM model_pricing_rates_v2")
        assert cursor.fetchall() == [("bundled_snapshot", FROZEN_VERSION)]

        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE source_url LIKE 'https://%' AND source_content_sha256 ~ '^[0-9a-f]{64}$'")
        total = cursor.fetchone()[0]
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2")
        assert total == cursor.fetchone()[0], "every seeded row must retain real AWS provenance"


def test_seed_hash_matches_runtime_publication_encoding(seeded):
    from psycopg2.extras import RealDictCursor

    from pricing_policy import RateRow
    from pricing_policy.refresh import canonical_content_hash

    with seeded.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute("SELECT current_generation_id FROM model_pricing_active")
        generation_id = cursor.fetchone()["current_generation_id"]
        cursor.execute("SELECT * FROM model_pricing_rates_v2 WHERE generation_id=%s", (generation_id,))
        rates = tuple(RateRow.from_mapping(dict(row)) for row in cursor.fetchall())
        cursor.execute("SELECT content_sha256 FROM model_pricing_generations WHERE generation_id=%s", (generation_id,))
        assert cursor.fetchone()["content_sha256"] == canonical_content_hash(rates)


def test_seeded_verified_at_is_backdated_in_the_database(seeded, migration):
    with seeded.cursor() as cursor:
        cursor.execute("SELECT DISTINCT verified_at FROM model_pricing_rates_v2")
        assert [row[0] for row in cursor.fetchall()] == [migration.VERIFIED_AT]


def test_precision_critical_seeded_rates_survive_the_round_trip(seeded):
    """The two rates that scale 6 gets wrong must be exact in the database.

    This is the end-to-end version of the precision claim: not "the column is wide
    enough" but "the actual published number is in there, exactly".
    """
    with seeded.cursor() as cursor:
        cursor.execute(
            """
            SELECT model_id, geography, cache_write_price_per_1k_tokens
              FROM model_pricing_rates_v2
             WHERE cache_write_price_per_1k_tokens = 0.0171875
            """
        )
        cyber = cursor.fetchall()
        assert cyber, "the 0.0171875 cache-write rate must be present exactly (scale 6 rounds it to 0.017188)"
        assert all(value == Decimal("0.0171875") for _, _, value in cyber)

        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE cache_read_price_per_1k_tokens = 0.0000264")
        assert cursor.fetchone()[0] > 0, "the 0.0000264 GovCloud cache-read rate must be present exactly (scale 6 rounds it, 1.52% error)"


def test_seeded_cache_policies_preserve_the_tristate(seeded):
    """All three states must reach the database, with NULL never becoming zero."""
    with seeded.cursor() as cursor:
        cursor.execute("SELECT DISTINCT cache_write_policy FROM model_pricing_rates_v2 ORDER BY 1")
        assert [row[0] for row in cursor.fetchall()] == ["full_rate", "no_additional_fee", "unpublished"]

        cursor.execute(
            "SELECT count(*) FROM model_pricing_rates_v2 WHERE cache_write_policy='unpublished' AND cache_write_price_per_1k_tokens IS NOT NULL"
        )
        assert cursor.fetchone()[0] == 0, "unpublished must be NULL, never zero — a zero makes cached tokens look free"

        cursor.execute(
            """
            SELECT count(*) FROM model_pricing_rates_v2
             WHERE cache_write_policy='no_additional_fee'
               AND cache_write_price_per_1k_tokens <> input_price_per_1k_tokens
            """
        )
        assert cursor.fetchone()[0] == 0


def test_the_legacy_table_is_untouched_by_the_seed(seeded):
    """Known-wrong legacy OpenAI rows stay physically put; V2 simply stops using them.

    Deliberate: rewriting history in the legacy table is out of scope, and the
    rollback path needs it intact.
    """
    with seeded.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing")
        assert cursor.fetchone()[0] == 0, "045 must not write to the legacy table at all"


# --------------------------------------------------------------------------- #
# Idempotence and convergence
# --------------------------------------------------------------------------- #


def test_repeated_upgrade_frozen_seed_publishes_no_new_generation(seeded, pg_url):
    """Repeated deploys must converge, not accumulate generations."""
    with seeded.cursor() as cursor:
        cursor.execute("SELECT count(*), max(generation_id) FROM model_pricing_generations")
        before = cursor.fetchone()

    upgrade(pg_url, SEED_REVISION)  # no-op: already at the frozen seed revision

    # Force 045 to actually re-execute, which is what a retried deploy does.
    result = run_alembic(pg_url, "stamp", SCHEMA_REVISION)
    assert result.returncode == 0, result.stderr
    upgrade(pg_url, SEED_REVISION)

    with seeded.cursor() as cursor:
        cursor.execute("SELECT count(*), max(generation_id) FROM model_pricing_generations")
        assert cursor.fetchone() == before, "re-running the seed must not create a second generation"
        cursor.execute("SELECT pointer_revision FROM model_pricing_active")
        assert cursor.fetchone()[0] == 1, "an identical candidate must not bump the pointer revision"


def test_running_the_seed_three_times_stays_at_one_generation(seeded, pg_url):
    for _ in range(3):
        assert run_alembic(pg_url, "stamp", SCHEMA_REVISION).returncode == 0
        upgrade(pg_url, SEED_REVISION)

    with seeded.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_generations")
        assert cursor.fetchone()[0] == 1
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2")
        assert cursor.fetchone()[0] == 330


def test_a_freshly_fetched_rate_is_never_downgraded_to_the_bundled_value(seeded, pg_url, migration):
    """The most costly convergence mistake: undoing a real refresh.

    Scenario: the daily refresh has already published corrected rates from AWS,
    then a deploy re-runs the seed. If the bundled value won, the platform would
    silently revert to month-old prices — and because the seed's `verified_at` is
    backdated, it would also start reporting them as stale.
    """
    # Simulate a refresh: publish a generation whose rows are `model_card`-sourced
    # with a different rate and a later verification time.
    fetched_rate = Decimal("0.0099000000")
    fetched_at = datetime.datetime(2026, 10, 1, 12, 0, tzinfo=datetime.UTC)
    key = next(key for key, row in migration._seed_candidate().items() if row["cache_write_policy"] == "full_rate")

    with seeded.cursor() as cursor:
        cursor.execute("SELECT current_generation_id FROM model_pricing_active")
        seed_generation = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO model_pricing_generations
                (schema_version, policy_version, snapshot_version, status, required_variants, content_sha256, validated_at)
            SELECT schema_version, policy_version, 'refresh-2026-10-01', 'building', required_variants, %s, NULL
              FROM model_pricing_generations WHERE generation_id = %s
            RETURNING generation_id
            """,
            ("c" * 64, seed_generation),
        )
        refresh_generation = cursor.fetchone()[0]
        # Copy the seed's rows, then mark them all as freshly fetched.
        cursor.execute(
            """
            INSERT INTO model_pricing_rates_v2
                (generation_id, model_id, geography, service_tier, context_tier, region, max_input_tokens,
                 input_price_per_1k_tokens, output_price_per_1k_tokens, cache_read_price_per_1k_tokens,
                 cache_write_price_per_1k_tokens, cache_write_policy, source, source_url,
                 source_content_sha256, source_effective_at, verified_at, snapshot_version)
            SELECT %s, model_id, geography, service_tier, context_tier, region, max_input_tokens,
                   CASE WHEN (model_id, geography, service_tier, context_tier, region) = (%s,%s,%s,%s,%s)
                        THEN %s ELSE input_price_per_1k_tokens END,
                   output_price_per_1k_tokens, cache_read_price_per_1k_tokens,
                   cache_write_price_per_1k_tokens, cache_write_policy, 'model_card', source_url,
                   source_content_sha256, source_effective_at, %s, NULL
              FROM model_pricing_rates_v2 WHERE generation_id = %s
            """,
            (refresh_generation, *key, fetched_rate, fetched_at, seed_generation),
        )
        cursor.execute(
            "UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s",
            (refresh_generation,),
        )
        cursor.execute(
            "UPDATE model_pricing_active SET current_generation_id=%s, pointer_revision=pointer_revision+1 WHERE singleton",
            (refresh_generation,),
        )

    # Now a deploy re-runs the seed.
    assert run_alembic(pg_url, "stamp", SCHEMA_REVISION).returncode == 0
    upgrade(pg_url, SEED_REVISION)

    with seeded.cursor() as cursor:
        cursor.execute("SELECT current_generation_id FROM model_pricing_active")
        active = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT source, input_price_per_1k_tokens, verified_at, snapshot_version
              FROM model_pricing_rates_v2
             WHERE generation_id=%s AND (model_id, geography, service_tier, context_tier, region) = (%s,%s,%s,%s,%s)
            """,
            (active, *key),
        )
        source, rate, verified_at, snapshot_version = cursor.fetchone()

    assert source == "model_card", "a fetched rate must not be replaced by the bundled seed"
    assert rate == fetched_rate, "the refreshed price must survive a seed re-run"
    assert verified_at == fetched_at, "the fetched row's verification time must be preserved, not backdated"
    assert snapshot_version is None


def test_an_unrecognized_bundled_version_is_retained_not_overwritten(seeded, pg_url, migration):
    """An unknown bundle version means an assumption is missing.

    Guessing is how you lose data, so the row is kept and reported. The seed
    declares which predecessor versions it may supersede; anything else is
    untouched.
    """
    unknown_rate = Decimal("0.0077000000")
    key = next(key for key, row in migration._seed_candidate().items() if row["cache_write_policy"] == "full_rate")

    with seeded.cursor() as cursor:
        cursor.execute("SELECT current_generation_id FROM model_pricing_active")
        seed_generation = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO model_pricing_generations
                (schema_version, policy_version, snapshot_version, status, required_variants, content_sha256, validated_at)
            SELECT schema_version, policy_version, '2099-01-01.7', 'building', required_variants, %s, NULL
              FROM model_pricing_generations WHERE generation_id=%s
            RETURNING generation_id
            """,
            ("d" * 64, seed_generation),
        )
        other = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO model_pricing_rates_v2
                (generation_id, model_id, geography, service_tier, context_tier, region, max_input_tokens,
                 input_price_per_1k_tokens, output_price_per_1k_tokens, cache_read_price_per_1k_tokens,
                 cache_write_price_per_1k_tokens, cache_write_policy, source, source_url,
                 source_content_sha256, source_effective_at, verified_at, snapshot_version)
            SELECT %s, model_id, geography, service_tier, context_tier, region, max_input_tokens,
                   CASE WHEN (model_id, geography, service_tier, context_tier, region) = (%s,%s,%s,%s,%s)
                        THEN %s ELSE input_price_per_1k_tokens END,
                   output_price_per_1k_tokens, cache_read_price_per_1k_tokens,
                   cache_write_price_per_1k_tokens, cache_write_policy, 'bundled_snapshot', source_url,
                   source_content_sha256, source_effective_at, verified_at, '2099-01-01.7'
              FROM model_pricing_rates_v2 WHERE generation_id=%s
            """,
            (other, *key, unknown_rate, seed_generation),
        )
        cursor.execute("UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s", (other,))
        cursor.execute("UPDATE model_pricing_active SET current_generation_id=%s, pointer_revision=pointer_revision+1 WHERE singleton", (other,))

    assert run_alembic(pg_url, "stamp", SCHEMA_REVISION).returncode == 0
    result = run_alembic(pg_url, "upgrade", SEED_REVISION)
    assert result.returncode == 0, result.stderr

    combined = result.stdout + result.stderr
    assert "unrecognized version" in combined, "an unrecognized bundle version must be reported, not silently handled"

    with seeded.cursor() as cursor:
        cursor.execute("SELECT current_generation_id FROM model_pricing_active")
        active = cursor.fetchone()[0]
        cursor.execute(
            """
            SELECT input_price_per_1k_tokens, snapshot_version FROM model_pricing_rates_v2
             WHERE generation_id=%s AND (model_id, geography, service_tier, context_tier, region)=(%s,%s,%s,%s,%s)
            """,
            (active, *key),
        )
        rate, version = cursor.fetchone()

    assert rate == unknown_rate, "an unrecognized bundled version must be retained, not overwritten"
    assert version == "2099-01-01.7"


def test_missing_variant_keys_are_filled_on_convergence(seeded, pg_url, migration):
    """An active older manifest gains seed variants while retaining fetched rows."""
    with seeded.cursor() as cursor:
        cursor.execute("SELECT current_generation_id FROM model_pricing_active")
        seed_generation = cursor.fetchone()[0]
        cursor.execute("SELECT content_sha256 FROM model_pricing_generations WHERE generation_id=%s", (seed_generation,))
        seed_hash = cursor.fetchone()[0]
        cursor.execute(
            """
            INSERT INTO model_pricing_generations
                (schema_version, policy_version, snapshot_version, status, required_variants, content_sha256, validated_at)
            SELECT schema_version, policy_version, %s, 'building',
                   (SELECT jsonb_agg(k ORDER BY n)
                      FROM jsonb_array_elements(required_variants) WITH ORDINALITY AS keys(k,n)
                     WHERE n > 3), %s, NULL
              FROM model_pricing_generations WHERE generation_id=%s
            RETURNING generation_id
            """,
            (FROZEN_VERSION, "e" * 64, seed_generation),
        )
        partial = cursor.fetchone()[0]
        # Copy all but three rows, so the generation is genuinely incomplete.
        cursor.execute(
            """
            INSERT INTO model_pricing_rates_v2
                (generation_id, model_id, geography, service_tier, context_tier, region, max_input_tokens,
                 input_price_per_1k_tokens, output_price_per_1k_tokens, cache_read_price_per_1k_tokens,
                 cache_write_price_per_1k_tokens, cache_write_policy, source, source_url,
                 source_content_sha256, source_effective_at, verified_at, snapshot_version)
            SELECT %s, model_id, geography, service_tier, context_tier, region, max_input_tokens,
                   input_price_per_1k_tokens * 2, output_price_per_1k_tokens, cache_read_price_per_1k_tokens,
                   CASE WHEN cache_write_policy='no_additional_fee' THEN input_price_per_1k_tokens * 2
                        ELSE cache_write_price_per_1k_tokens END,
                   cache_write_policy, 'model_card', source_url,
                   source_content_sha256, source_effective_at, verified_at, snapshot_version
              FROM model_pricing_rates_v2 WHERE generation_id=%s
             ORDER BY model_id, geography, service_tier, context_tier, region
             OFFSET 3
            """,
            (partial, seed_generation),
        )
        cursor.execute("UPDATE model_pricing_generations SET status='validated', validated_at=now() WHERE generation_id=%s", (partial,))
        # This manifest reflects the older published inventory. Activate it so
        # convergence actually encounters missing seed keys and retained rates.
        cursor.execute(
            "UPDATE model_pricing_active SET current_generation_id=%s, pointer_revision=pointer_revision+1 WHERE singleton",
            (partial,),
        )
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE generation_id=%s", (partial,))
        assert cursor.fetchone()[0] == len(migration.SEED_ROWS) - 3

    assert run_alembic(pg_url, "stamp", SCHEMA_REVISION).returncode == 0
    upgrade(pg_url, SEED_REVISION)

    with seeded.cursor() as cursor:
        cursor.execute("SELECT current_generation_id FROM model_pricing_active")
        active = cursor.fetchone()[0]
        assert active != partial, "convergence must publish a complete generation"
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE generation_id=%s", (active,))
        assert cursor.fetchone()[0] == len(migration.SEED_ROWS), "convergence must fill every missing required key"
        cursor.execute("SELECT content_sha256 FROM model_pricing_generations WHERE generation_id=%s", (active,))
        assert cursor.fetchone()[0] != seed_hash, "retained rates change the published content and must change its hash"
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2 WHERE generation_id=%s AND source='model_card'", (active,))
        assert cursor.fetchone()[0] == len(migration.SEED_ROWS) - 3


def test_consumers_are_re_enabled_if_a_prior_run_left_them_off(seeded, pg_url):
    """A deploy that seeded and then died before enabling must be recoverable.

    Without this the re-run would find the data already correct, return early, and
    leave the platform reading nothing — a silent outage that looks like success.
    """
    with seeded.cursor() as cursor:
        cursor.execute("UPDATE model_pricing_active SET consumers_enabled=FALSE WHERE singleton")

    assert run_alembic(pg_url, "stamp", SCHEMA_REVISION).returncode == 0
    upgrade(pg_url, SEED_REVISION)

    with seeded.cursor() as cursor:
        cursor.execute("SELECT consumers_enabled FROM model_pricing_active")
        assert cursor.fetchone()[0] is True


def test_the_seeded_generation_survives_a_downgrade_of_045(seeded, pg_url):
    """045's downgrade is intentionally a no-op.

    Deleting the generation would violate 044's immutability contract — a validated
    generation is permanent and settlements may still reference it. Rollback for
    this release is a pointer move, not a migration downgrade.
    """
    result = run_alembic(pg_url, "downgrade", SCHEMA_REVISION)
    assert result.returncode == 0, result.stderr

    with seeded.cursor() as cursor:
        cursor.execute("SELECT count(*) FROM model_pricing_generations")
        assert cursor.fetchone()[0] == 1, "the validated seed generation must not be deleted by a downgrade"
        cursor.execute("SELECT count(*) FROM model_pricing_rates_v2")
        assert cursor.fetchone()[0] == 330
