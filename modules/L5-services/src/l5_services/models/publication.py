"""Durable Security contribution and consumer acknowledgement."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class Publication(Base):
    __tablename__ = "l5_service_publications"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "result_id"],
                             ["l5_service_results.tenant_id", "l5_service_results.result_id"]),
        UniqueConstraint("tenant_id", "result_id", "result_version", "destination", "schema_version"),
        Index("ix_l5_publication_due", "state", "next_due_at", "lease_until"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    publication_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(36), nullable=False)
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    destination: Mapped[str] = mapped_column(String(100), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    contribution_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(Payload, nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    receipt: Mapped[dict | None] = mapped_column(Payload)
    invalidation: Mapped[dict | None] = mapped_column(Payload)
    invalidation_receipt: Mapped[dict | None] = mapped_column(Payload)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalidation_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    next_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_owner: Mapped[str | None] = mapped_column(String(36))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(32))
    invalidation_watermark: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_watermark: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
