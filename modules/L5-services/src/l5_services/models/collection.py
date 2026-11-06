"""Scoped, retained checkpoints for independently collected billing pages."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKeyConstraint, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class CostCollection(Base):
    __tablename__ = "l5_service_cost_collections"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        CheckConstraint("request_count >= 0 AND page_count >= 0 AND page_count <= request_count",
                        name="ck_l5_cost_collection_counts"),
        CheckConstraint("status IN ('collecting', 'complete', 'partial', 'superseded')",
                        name="ck_l5_cost_collection_status"),
        Index("ix_l5_cost_collection_run", "tenant_id", "run_id", "status"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    collection_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    query_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    scope_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checkpoint_ref: Mapped[dict | None] = mapped_column(Payload)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    coverage: Mapped[str] = mapped_column(String(16), nullable=False, default="unavailable")
    reason: Mapped[str | None] = mapped_column(String(64))
    supersedes_id: Mapped[str | None] = mapped_column(String(36))
