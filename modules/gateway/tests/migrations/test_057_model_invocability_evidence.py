"""Migration evidence for the #5420 model-invocability evidence store.

Covers what the operator requires of this migration: a real
upgrade → downgrade → upgrade round trip, the full 6-column composite primary
key, and the CHECK constraints that stop a row from *reading* as proof when it
is not one.

The revision-graph position (single head on the merge ref, not just on the
branch) is asserted in ``test_revision_graph_single_head`` below, because a
branch-local ``alembic heads`` cannot see the collision that CI hits.
"""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

MIGRATIONS = Path(__file__).resolve().parents[2] / "alembic" / "versions"
MIGRATION_FILE = "057_model_invocability_evidence.py"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, MIGRATIONS / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MIG = _load(MIGRATION_FILE)


def _run(sync_conn, fn):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    with Operations.context(MigrationContext.configure(sync_conn)):
        fn()


async def _engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(_run, MIG.upgrade)
    return engine


_INSERT = (
    "INSERT INTO model_invocability_evidence "
    "(account_id, region, canonical_model_id, compatibility_class, "
    " harness_contract_revision, request_shape_sha256, outcome, "
    " provider_request_id, error_code, verified_at, expires_at, updated_at) "
    "VALUES (:account_id, :region, :model, :cls, :rev, :sha, :outcome, "
    "        :req_id, :error_code, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)


def _row(**overrides):
    row = {
        "account_id": "111111111111",
        "region": "us-east-1",
        "model": "global.anthropic.claude-sonnet-4-6",
        "cls": "claude-agent-sdk",
        "rev": "0.3.220",
        "sha": "a" * 64,
        "outcome": "proven",
        "req_id": "req-1",
        "error_code": None,
    }
    row.update(overrides)
    return row


class TestEvidenceTableShape:
    async def test_composite_primary_key_spans_all_six_dimensions(self):
        """Every dimension is part of the key.

        Dropping any one of them would let an arbitrary row match a lookup —
        e.g. evidence proven in one account would answer for another, or
        evidence gathered under an old harness revision would answer for a new
        one. That is the #2300 class of error this table exists to prevent.
        """
        engine = await _engine()
        async with engine.connect() as conn:
            pk = await conn.run_sync(lambda c: sa_inspect(c).get_pk_constraint("model_invocability_evidence"))
        await engine.dispose()

        assert pk["constrained_columns"] == [
            "account_id",
            "region",
            "canonical_model_id",
            "compatibility_class",
            "harness_contract_revision",
            "request_shape_sha256",
        ]

    async def test_read_path_indexes_exist(self):
        engine = await _engine()
        async with engine.connect() as conn:
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes("model_invocability_evidence"))
        await engine.dispose()

        names = {i["name"] for i in indexes}
        assert "ix_evidence_model_id" in names
        assert "ix_evidence_expires_at" in names


class TestRoundTrip:
    async def test_upgrade_downgrade_upgrade_round_trip(self):
        """The operator requires a real downgrade, not just a forward apply."""
        engine = await _engine()
        async with engine.begin() as conn:
            await conn.run_sync(_run, MIG.downgrade)

        async with engine.connect() as conn:
            tables = await conn.run_sync(lambda c: sa_inspect(c).get_table_names())
            assert "model_invocability_evidence" not in tables, "downgrade left the table behind"

        async with engine.begin() as conn:
            await conn.run_sync(_run, MIG.upgrade)

        async with engine.connect() as conn:
            pk = await conn.run_sync(lambda c: sa_inspect(c).get_pk_constraint("model_invocability_evidence"))
            indexes = await conn.run_sync(lambda c: sa_inspect(c).get_indexes("model_invocability_evidence"))
        await engine.dispose()

        # Re-upgrade must restore the full shape, not a degraded one.
        assert len(pk["constrained_columns"]) == 6
        assert {"ix_evidence_model_id", "ix_evidence_expires_at"} <= {i["name"] for i in indexes}


class TestStorageLayerRefusesNonProof:
    """CHECK constraints — the last line of defence under the service gate."""

    async def test_proven_row_requires_a_provider_request_id(self):
        """A row cannot read as proven without the provider's own identifier.

        ``record_probe_result`` enforces this too, with a better diagnostic.
        This asserts the storage layer refuses it independently, so a future
        writer that bypasses the service cannot record unfalsifiable proof.
        """
        engine = await _engine()
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(sa.text(_INSERT), _row(outcome="proven", req_id=None))
            await conn.rollback()
        await engine.dispose()

    async def test_outcome_vocabulary_is_closed(self):
        engine = await _engine()
        async with engine.connect() as conn:
            with pytest.raises(IntegrityError):
                await conn.execute(sa.text(_INSERT), _row(outcome="definitely_fine", req_id="req-x"))
            await conn.rollback()
        await engine.dispose()

    @pytest.mark.parametrize("outcome", ["refused", "error"])
    async def test_negative_outcomes_need_no_request_id(self, outcome):
        """Recording the *absence* of proof must stay possible."""
        engine = await _engine()
        async with engine.connect() as conn:
            await conn.execute(
                sa.text(_INSERT),
                _row(outcome=outcome, req_id=None, error_code="AccessDeniedException", sha="b" * 64),
            )
            await conn.commit()
            count = await conn.scalar(sa.text("SELECT COUNT(*) FROM model_invocability_evidence"))
        await engine.dispose()
        assert count == 1

    async def test_same_key_cannot_be_inserted_twice(self):
        """One authoritative row per evidence key."""
        engine = await _engine()
        async with engine.connect() as conn:
            await conn.execute(sa.text(_INSERT), _row())
            await conn.commit()
            with pytest.raises(IntegrityError):
                await conn.execute(sa.text(_INSERT), _row(outcome="refused", req_id=None))
            await conn.rollback()
        await engine.dispose()

    async def test_differing_harness_revision_is_a_distinct_row(self):
        """Design §10: a new harness revision must not reuse old evidence.

        Same destination, same model, same request shape — only the harness
        revision differs, and it must coexist as its own row rather than
        colliding with (or silently inheriting) the old proof.
        """
        engine = await _engine()
        async with engine.connect() as conn:
            await conn.execute(sa.text(_INSERT), _row(rev="0.3.220"))
            await conn.execute(sa.text(_INSERT), _row(rev="0.4.000"))
            await conn.commit()
            count = await conn.scalar(sa.text("SELECT COUNT(*) FROM model_invocability_evidence"))
        await engine.dispose()
        assert count == 2

    async def test_differing_request_shape_is_a_distinct_row(self):
        """Design §10: a changed request shape invalidates, never inherits."""
        engine = await _engine()
        async with engine.connect() as conn:
            await conn.execute(sa.text(_INSERT), _row(sha="a" * 64))
            await conn.execute(sa.text(_INSERT), _row(sha="c" * 64))
            await conn.commit()
            count = await conn.scalar(sa.text("SELECT COUNT(*) FROM model_invocability_evidence"))
        await engine.dispose()
        assert count == 2


class TestRevisionGraph:
    def test_revision_graph_single_head(self):
        """This revision must not fork the graph.

        The original 055_* revision and main's ``055_orch_environment_leases``
        both declared ``down_revision = "054_execution_tenant_guards"``. That
        reads as a single head on either branch alone and becomes two heads on
        the merge ref CI runs — so asserting the parent explicitly is the only
        version of this check that catches the real failure.
        """
        assert MIG.revision == "057_model_invocability_evidence"
        assert MIG.down_revision == "056_persona_model_prefs"
        assert MIG.down_revision != "054_execution_tenant_guards", "reparenting regressed; this forks the graph on the merge ref"

    def test_no_sibling_shares_this_parent(self):
        """Nothing else in the tree claims the same parent.

        This is what would catch the collision reappearing — including at the
        PMM-02 rebase, where this revision must move to 057_* parented on
        ``056_persona_model_prefs``.
        """
        siblings = []
        for path in sorted(MIGRATIONS.glob("*.py")):
            if path.name == MIGRATION_FILE:
                continue
            text = path.read_text()
            if f'down_revision = "{MIG.down_revision}"' in text:
                siblings.append(path.name)
        assert not siblings, f"revisions sharing parent {MIG.down_revision}: {siblings}"
