"""Scoped, immutable event decisions pending admission and dispatch."""

from datetime import datetime
from hashlib import sha256

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class EventReceipt(Base):
    __tablename__ = "l5_service_event_receipts"
    __table_args__ = (Index("ix_l5_event_admission", "tenant_id", "idempotency_key"),
                      Index("ix_l5_event_retry", "state", "retry_at", "received_at"))

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    service_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    trigger_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[dict | None] = mapped_column(Payload)
    source_cursor: Mapped[str | None] = mapped_column(String(2048))
    causal_origin: Mapped[dict | None] = mapped_column(Payload)
    configuration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    target: Mapped[dict | None] = mapped_column(Payload)
    run_input: Mapped[dict | None] = mapped_column(Payload)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(36))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recovery_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def semantic_key(receipt: EventReceipt) -> str:
    return sha256("\0".join((receipt.tenant_id, receipt.product_id, receipt.project_id,
                             receipt.trigger_id, receipt.event_id)).encode()).hexdigest()
