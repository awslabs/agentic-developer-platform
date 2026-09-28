"""SQLAlchemy models for persona-model catalogue — Issue #5420 (PMM-03).

Design: ``docs/design-notes/5420-persona-and-model-catalogue.md`` §4.1a/§4.1b.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base


class ModelInvocabilityEvidence(Base):
    """Durable authoritative invocability evidence store.

    One row per (destination, model, class, revision, request-shape) combination.
    The five-part composite primary key ensures exactly one authoritative record
    per evidence key.  Everything else that reports invocability is a reader.

    See ``docs/design-notes/5420-persona-and-model-catalogue.md`` §4.1a.
    """

    __tablename__ = "model_invocability_evidence"

    # --- Key part 1: Destination ---
    account_id: Mapped[str] = mapped_column(String(12), primary_key=True)
    region: Mapped[str] = mapped_column(String(32), primary_key=True)

    # --- Key part 2: Canonical model ---
    canonical_model_id: Mapped[str] = mapped_column(String(255), primary_key=True)

    # --- Key part 3: Compatibility class ---
    compatibility_class: Mapped[str] = mapped_column(String(64), primary_key=True)

    # --- Key part 4: Harness/contract revision ---
    harness_contract_revision: Mapped[str] = mapped_column(String(32), primary_key=True)

    # --- Key part 5: Request-shape revision ---
    request_shape_sha256: Mapped[str] = mapped_column(String(64), primary_key=True)

    # --- Evidence payload ---
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    verified_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "outcome = 'proven' OR outcome = 'refused' OR outcome = 'error'",
            name="ck_evidence_outcome_values",
        ),
        CheckConstraint(
            "outcome != 'proven' OR provider_request_id IS NOT NULL",
            name="ck_evidence_status_consistent",
        ),
        Index("ix_evidence_model_id", "canonical_model_id"),
        Index("ix_evidence_expires_at", "expires_at"),
    )

    @property
    def is_proven(self) -> bool:
        """Whether this evidence records a successful invocation."""
        return self.outcome == "proven"

    @property
    def verified_at_utc(self) -> datetime:
        """``verified_at`` as a timezone-aware UTC datetime.

        Same backend-dependent naivety as :attr:`expires_at_utc`.  Serializing
        a naive value emits an ISO-8601 string with no offset, which a client
        is entitled to read as local time — so normalize before it leaves.
        """
        value = self.verified_at
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @property
    def expires_at_utc(self) -> datetime:
        """``expires_at`` as a timezone-aware UTC datetime.

        Not every backend round-trips timezone awareness.  SQLite has no
        native timestamptz, so a row loaded in a *fresh* session comes back
        naive even though it was written tz-aware; PostgreSQL preserves the
        offset.  Comparing a naive value against ``datetime.now(UTC)`` raises
        ``TypeError: can't compare offset-naive and offset-aware datetimes``.

        Every value in this column is written as UTC (see ``verified_at`` /
        ``expires_at`` producers in the probe recorder), so a naive value is
        unambiguously UTC and is reinterpreted as such rather than guessed at.

        Staleness is a fail-closed decision: it must return a bool, never
        raise.  An exception here would propagate out of a read path that
        §6.3 requires to return a value.
        """
        value = self.expires_at
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @property
    def is_stale(self) -> bool:
        """Whether this evidence has expired.

        Compares against :attr:`expires_at_utc` so a naive column value
        cannot raise.  Expiry is inclusive: evidence is stale the instant it
        reaches ``expires_at``, because evidence exactly at its boundary is
        not fresh proof.
        """
        return datetime.now(UTC) >= self.expires_at_utc


class ModelProbeCycle(Base):
    """A durable spend and concurrency envelope for one probe cycle."""

    __tablename__ = "model_probe_cycles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cycle_key: Mapped[str] = mapped_column(String(255), nullable=False)
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    max_slots: Mapped[int] = mapped_column(Integer, nullable=False)
    budget_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    reserved_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    started_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False, default=Decimal("0"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("cycle_key", name="uq_model_probe_cycle_key"),
        CheckConstraint("trigger IN ('scheduled', 'manual', 'change')", name="ck_model_probe_cycle_trigger"),
        CheckConstraint("status IN ('active', 'completed', 'expired')", name="ck_model_probe_cycle_status"),
        CheckConstraint("max_slots >= 0", name="ck_model_probe_cycle_slots"),
        CheckConstraint(
            "budget_usd >= 0 AND reserved_usd >= 0 AND started_usd >= 0 AND reserved_usd <= budget_usd AND started_usd <= reserved_usd",
            name="ck_model_probe_cycle_spend",
        ),
        Index("ix_model_probe_cycle_status_expiry", "status", "expires_at"),
    )


class ModelProbeSlot(Base):
    """One Gateway-selected, at-most-once paid probe attempt."""

    __tablename__ = "model_probe_slots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(36), ForeignKey("model_probe_cycles.id", ondelete="CASCADE"), nullable=False)
    destination_id: Mapped[str] = mapped_column(String(255), nullable=False)
    account_id: Mapped[str] = mapped_column(String(12), nullable=False)
    region: Mapped[str] = mapped_column(String(32), nullable=False)
    canonical_model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    compatibility_class: Mapped[str] = mapped_column(String(64), nullable=False)
    harness_contract_revision: Mapped[str] = mapped_column(String(32), nullable=False)
    expected_request_shape_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    reserved_budget_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="reserved")
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_token_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    paid_attempt_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    observed_request_shape_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    completion_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    indeterminate_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "cycle_id",
            "destination_id",
            "canonical_model_id",
            "compatibility_class",
            "harness_contract_revision",
            "expected_request_shape_sha256",
            name="uq_model_probe_slot_candidate",
        ),
        CheckConstraint(
            "status IN ('reserved', 'started', 'completed', 'indeterminate', 'expired')",
            name="ck_model_probe_slot_status",
        ),
        CheckConstraint("reserved_budget_usd > 0", name="ck_model_probe_slot_budget"),
        CheckConstraint(
            "(status = 'reserved' AND paid_attempt_started_at IS NULL AND completed_at IS NULL) OR "
            "(status = 'started' AND paid_attempt_started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND paid_attempt_started_at IS NOT NULL AND completed_at IS NOT NULL "
            " AND outcome IS NOT NULL AND completion_fingerprint IS NOT NULL) OR "
            "(status = 'indeterminate' AND paid_attempt_started_at IS NOT NULL) OR "
            "(status = 'expired' AND paid_attempt_started_at IS NULL)",
            name="ck_model_probe_slot_state",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ('proven', 'refused', 'error')",
            name="ck_model_probe_slot_outcome",
        ),
        Index("ix_model_probe_slot_cycle_status", "cycle_id", "status"),
        Index("ix_model_probe_slot_lease_expiry", "lease_expires_at"),
    )
