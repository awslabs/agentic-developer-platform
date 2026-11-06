"""Immutable setup intent and separately mutable operation outcome."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class SetupIntent(Base):
    __tablename__ = "l5_service_setup_intents"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "configuration_id"],
            ["l5_service_configurations.tenant_id", "l5_service_configurations.configuration_id"],
        ),
        UniqueConstraint("tenant_id", "product_id", "project_id", "service_id", "actor_id", "idempotency_key"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(255), nullable=False)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    service_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    configuration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    scope: Mapped[dict] = mapped_column(Payload, nullable=False)
    request: Mapped[dict] = mapped_column(Payload, nullable=False)
    binding_generation: Mapped[int | None] = mapped_column(Integer)
    authorization: Mapped[dict] = mapped_column(Payload, nullable=False)
    owned_resource_receipt: Mapped[dict | None] = mapped_column(Payload)
    intent_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class BindingOperation(Base):
    __tablename__ = "l5_service_binding_operations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["l5_service_setup_intents.tenant_id", "l5_service_setup_intents.operation_id"],
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    binding_id: Mapped[str | None] = mapped_column(String(36))
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    gaps: Mapped[list] = mapped_column(Payload, nullable=False)
    readback_ref: Mapped[dict | None] = mapped_column(Payload)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    reconcile_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    native_resource_ref: Mapped[dict | None] = mapped_column(Payload)
    creation_receipt: Mapped[dict | None] = mapped_column(Payload)
    cleanup_receipt: Mapped[dict | None] = mapped_column(Payload)
    native_candidate_id: Mapped[str | None] = mapped_column(String(255))
    reconcile_key: Mapped[str | None] = mapped_column(String(128))
    reconcile_reason: Mapped[str | None] = mapped_column(String(1000))


class SetupAuthorityContinuation(Base):
    """Append-only owner consent; never rewrites the original setup identity."""

    __tablename__ = "l5_service_setup_authority_continuations"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "operation_id"],
                             ["l5_service_setup_intents.tenant_id", "l5_service_setup_intents.operation_id"]),
        UniqueConstraint("tenant_id", "operation_id", "idempotency_key"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    receipt: Mapped[dict] = mapped_column(Payload, nullable=False)
    receipt_hash: Mapped[str] = mapped_column(String(64), nullable=False)


class SetupWork(Base):
    __tablename__ = "l5_service_setup_work"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["l5_service_setup_intents.tenant_id", "l5_service_setup_intents.operation_id"],
        ),
        UniqueConstraint("tenant_id", "operation_id", "generation"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    work_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatch_owner: Mapped[str | None] = mapped_column(String(255))
    dispatch_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redeliver_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    dispatch_attempt_limit: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(1024))


class SetupResourceReceipt(Base):
    __tablename__ = "l5_service_setup_resource_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "operation_id"],
            ["l5_service_setup_intents.tenant_id", "l5_service_setup_intents.operation_id"],
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    resource_kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    ownership: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    native_resource_ref: Mapped[dict | None] = mapped_column(Payload)
    evidence_digest: Mapped[str | None] = mapped_column(String(64))
