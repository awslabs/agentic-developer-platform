"""Durable, tenant-bound attempt and scoped admission authority."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import JSON, BigInteger, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint, Index, Numeric, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base


class MeteringScopeGate(Base):
    __tablename__ = "metering_scope_gates"
    __table_args__ = (
        CheckConstraint("activation IN ('legacy','draining','active')", name="ck_metering_gate_activation"),
        CheckConstraint("revision >= 1", name="ck_metering_gate_revision"),
        CheckConstraint(
            "(scope_kind = 'person' AND tenant_id IS NULL) OR (scope_kind <> 'person' AND tenant_id IS NOT NULL)", name="ck_metering_gate_tenant"
        ),
        Index("ix_metering_gate_tenant", "tenant_id", "scope_kind"),
    )

    scope_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str | None] = mapped_column(String(255))
    activation: Mapped[str] = mapped_column(String(16), nullable=False, server_default="legacy")
    readiness: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unverified")
    policy_revision: Mapped[str | None] = mapped_column(String(128))
    binding_revision: Mapped[str | None] = mapped_column(String(128))
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MeteringLegacyAdmission(Base):
    __tablename__ = "metering_legacy_admissions"
    __table_args__ = (
        CheckConstraint("state IN ('pending', 'settled', 'not_dispatched')", name="ck_metering_legacy_admission_state"),
        CheckConstraint(
            "(state = 'not_dispatched') = (disposition IS NOT NULL AND disposed_at IS NOT NULL)",
            name="ck_metering_legacy_admission_disposition",
        ),
        Index("ix_metering_legacy_admission_auth", "authenticated_tenant_id", "state"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    authenticated_tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    disposition: Mapped[str | None] = mapped_column(String(32))
    disposed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MeteringAttempt(Base):
    __tablename__ = "metering_attempts"
    __table_args__ = (
        CheckConstraint("bound_usd >= 0", name="ck_metering_attempt_bound"),
        CheckConstraint("revision >= 1 AND producer_epoch >= 1", name="ck_metering_attempt_revision"),
        CheckConstraint(
            "state IN ('prepared','dispatch_armed','unknown','quarantined','settled','cancelled_unsubmitted')",
            name="ck_metering_attempt_state",
        ),
        CheckConstraint(
            "(admission_version IS NULL AND admission_sha256 IS NULL) OR (admission_version IS NOT NULL AND admission_sha256 IS NOT NULL)",
            name="ck_metering_attempt_admission_receipt",
        ),
        CheckConstraint(
            "(final_version IS NULL AND final_sha256 IS NULL) OR (final_version IS NOT NULL AND final_sha256 IS NOT NULL)",
            name="ck_metering_attempt_final_receipt",
        ),
        CheckConstraint(
            "state <> 'dispatch_armed' OR (admission_version IS NOT NULL AND dispatch_deadline IS NOT NULL)",
            name="ck_metering_attempt_dispatch",
        ),
        CheckConstraint(
            "state <> 'settled' OR final_version IS NOT NULL",
            name="ck_metering_attempt_settled_receipt",
        ),
        Index("ix_metering_attempt_due", "state", "completion_deadline", "tenant_id", "request_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(255), nullable=False)
    model_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    submitted_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    pricing_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    quote_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    attribution_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    bound_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    producer_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="1")
    prepare_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatch_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completion_deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    admission_key: Mapped[str] = mapped_column(String(768), nullable=False)
    admission_version: Mapped[str | None] = mapped_column(String(255))
    admission_sha256: Mapped[str | None] = mapped_column(String(64))
    final_key: Mapped[str] = mapped_column(String(768), nullable=False)
    final_version: Mapped[str | None] = mapped_column(String(255))
    final_sha256: Mapped[str | None] = mapped_column(String(64))
    reconciliation_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MeteringAttemptScope(Base):
    __tablename__ = "metering_attempt_scopes"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "request_id"], ["metering_attempts.tenant_id", "metering_attempts.request_id"], ondelete="RESTRICT"),
        CheckConstraint("bound_usd >= 0", name="ck_metering_scope_bound"),
        Index("ix_metering_scope_exposure", "scope_key", "allocation_period", "tenant_id", "request_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(512), ForeignKey("metering_scope_gates.scope_key", ondelete="RESTRICT"), primary_key=True)
    allocation_period: Mapped[str] = mapped_column(String(64), primary_key=True)
    bound_usd: Mapped[Decimal] = mapped_column(Numeric(20, 6), nullable=False)
    captured_allocation: Mapped[dict] = mapped_column(JSON, nullable=False)


class MeteringScopeAlias(Base):
    __tablename__ = "metering_scope_aliases"
    __table_args__ = (Index("ix_metering_alias_lookup", "alias_kind", "stable_alias"),)

    alias_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    stable_alias: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(512), ForeignKey("metering_scope_gates.scope_key", ondelete="RESTRICT"), primary_key=True)
    identity_revision: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class MeteringReconciliationEvent(Base):
    __tablename__ = "metering_reconciliation_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "request_id"], ["metering_attempts.tenant_id", "metering_attempts.request_id"], ondelete="RESTRICT"),
        UniqueConstraint("tenant_id", "request_id", "idempotency_key", name="uq_metering_event_idempotency"),
        CheckConstraint("new_revision >= prior_revision", name="ck_metering_event_revision"),
        Index("ix_metering_events_attempt", "tenant_id", "request_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    request_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    authority: Mapped[str] = mapped_column(String(128), nullable=False)
    prior_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    new_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    reason: Mapped[str] = mapped_column(String(1024), nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(768), nullable=False)
    evidence_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt_sha256: Mapped[str | None] = mapped_column(String(64))
    disposition: Mapped[str] = mapped_column(String(32), nullable=False)
    result_snapshot: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
