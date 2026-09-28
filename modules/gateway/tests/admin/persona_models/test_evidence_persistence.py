"""Evidence rows re-loaded in a fresh session — Issue #5420 (PMM-03).

Every other evidence test writes and reads through a single ``session``
fixture.  That is not a faithful test of a persisted row: SQLAlchemy's
identity map hands back the same Python object that was added, so column
values keep their in-memory types and never make the round trip through the
database.  A row written tz-aware therefore *appears* tz-aware on read even
when the backend cannot store an offset.

Production never has that shape.  A probe records evidence in one request and
a later catalogue read or save-validation loads it in a new session.  These
tests use the ``new_session`` fixture to reproduce that, which is what makes
them regression tests rather than restatements of the implementation.

The concrete defect they pin: ``is_stale`` compared ``datetime.now(UTC)``
against a column value that comes back naive from SQLite, raising
``TypeError: can't compare offset-naive and offset-aware datetimes``.  That
exception escaped ``is_stale``, ``_invocable_state``, ``build_model_catalogue``
and ``validate_selection`` — turning a read that §6.3 requires to return a
value into a 500, and doing so precisely on the fail-closed staleness path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from src.admin.persona_models.catalogue import (
    COMPATIBILITY_CLASS_CLAUDE,
    HARNESS_CONTRACT_REVISION,
)
from src.admin.persona_models.catalogue_service import (
    _invocable_state,
    build_model_catalogue,
    compute_request_shape_sha256,
    lookup_evidence,
    validate_selection,
)
from src.shared.models.persona_model_catalogue import ModelInvocabilityEvidence

from .conftest import DEFAULT_MODEL_ID, DEST_ACCOUNT, DEST_REGION, make_evidence


async def _reload(new_session) -> ModelInvocabilityEvidence:
    """Load the single evidence row through an independent session."""
    async with new_session() as s:
        return (await s.scalars(select(ModelInvocabilityEvidence))).one()


class TestStoredRepresentation:
    """What the database actually hands back."""

    @pytest.mark.asyncio
    async def test_reloaded_column_is_naive_on_sqlite(self, session, new_session):
        """Document the storage reality these tests exist to defend against.

        If this ever fails because the row comes back tz-aware, the naivety
        hazard is gone on this backend and the guards below are belt-and-braces
        rather than load-bearing.  It should fail loudly in that case so the
        reason for the normalization is re-examined, not silently pass.
        """
        session.add(make_evidence())
        await session.commit()

        row = await _reload(new_session)
        assert row.expires_at.tzinfo is None
        assert row.verified_at.tzinfo is None

    @pytest.mark.asyncio
    async def test_utc_properties_normalize_naive_values(self, session, new_session):
        session.add(make_evidence())
        await session.commit()

        row = await _reload(new_session)
        assert row.expires_at_utc.tzinfo is not None
        assert row.verified_at_utc.tzinfo is not None
        # Reinterpreted as UTC, not shifted by a local-time guess.
        assert row.expires_at_utc.replace(tzinfo=None) == row.expires_at
        assert row.verified_at_utc.replace(tzinfo=None) == row.verified_at


class TestStalenessAcrossSessions:
    """``is_stale`` must return a bool, never raise (§6.3)."""

    @pytest.mark.asyncio
    async def test_fresh_evidence_not_stale(self, session, new_session):
        session.add(make_evidence(expires_at=datetime.now(UTC) + timedelta(hours=24)))
        await session.commit()

        row = await _reload(new_session)
        assert row.is_stale is False
        assert _invocable_state(row) is True

    @pytest.mark.asyncio
    async def test_expired_evidence_is_stale(self, session, new_session):
        session.add(make_evidence(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
        await session.commit()

        row = await _reload(new_session)
        assert row.is_stale is True
        # Stale proven evidence is *unproven*, not refused — §4.7 tri-state.
        assert _invocable_state(row) is None

    @pytest.mark.asyncio
    async def test_staleness_boundary_is_inclusive(self, session):
        """Evidence exactly at its expiry is not fresh proof."""
        boundary = datetime.now(UTC) - timedelta(microseconds=1)
        evidence = make_evidence(expires_at=boundary)
        assert evidence.is_stale is True


class TestReadPathsDoNotRaiseOnReloadedRows:
    """The gates that consume evidence, exercised against a re-loaded row."""

    @pytest.mark.asyncio
    async def test_lookup_evidence_returns_row(self, session, new_session):
        session.add(make_evidence())
        await session.commit()

        async with new_session() as s:
            found = await lookup_evidence(
                s,
                account_id=DEST_ACCOUNT,
                region=DEST_REGION,
                canonical_model_id=DEFAULT_MODEL_ID,
                compatibility_class=COMPATIBILITY_CLASS_CLAUDE,
                harness_contract_revision=HARNESS_CONTRACT_REVISION,
                request_shape_sha256=compute_request_shape_sha256(DEFAULT_MODEL_ID),
            )
            assert found is not None
            # The staleness read is the one that used to raise.
            assert found.is_stale is False

    @pytest.mark.asyncio
    async def test_model_catalogue_reports_stale_without_raising(self, session, new_session):
        session.add(make_evidence(expires_at=datetime.now(UTC) - timedelta(hours=1)))
        await session.commit()

        async with new_session() as s:
            rows = await build_model_catalogue(
                s,
                persona_key="developer",
                account_id=DEST_ACCOUNT,
                region=DEST_REGION,
            )

        row = next(r for r in rows if r.canonical_model_id == DEFAULT_MODEL_ID)
        assert row.evidence is not None, "stale evidence must stay actionable, not be dropped"
        assert row.evidence.stale is True
        assert row.evidence.expires_at.tzinfo is not None, "serialized timestamps must carry an offset"
        assert row.invocable is None

    @pytest.mark.asyncio
    async def test_validate_selection_refuses_stale_as_a_value(self, session, new_session):
        """A read must not raise — it returns a rejection value (§6.3)."""
        session.add(make_evidence(expires_at=datetime.now(UTC) - timedelta(hours=1)))
        await session.commit()

        async with new_session() as s:
            result = await validate_selection(
                s,
                persona_key="developer",
                model=DEFAULT_MODEL_ID,
                account_id=DEST_ACCOUNT,
                region=DEST_REGION,
                org_id="org-5420-acme",
                principal_kind="human",
                canonical_principal_id="sub-5420-member",
            )

        assert result.reason == "evidence_stale", f"expected evidence_stale, got {result!r}"
        # Distinguishable from "nothing was ever tried".
        assert result.reason != "probing_disabled"
