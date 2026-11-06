"""SQL-owned dispatch and execution intent; SQS is only transport."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base


class Work(Base):
    __tablename__ = "l5_service_work"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]
        ),
        UniqueConstraint("tenant_id", "run_id", "kind", "generation", "step"),
        Index("ix_l5_work_dispatch", "completed_at", "due_at", "redeliver_after"),
        Index("ix_l5_work_repair", "completed_at", "blocked_at", "due_at", "lease_until"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    work_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    generation: Mapped[int] = mapped_column(Integer, nullable=False)
    step: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    dispatch_owner: Mapped[str | None] = mapped_column(String(255))
    dispatch_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_enqueued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    redeliver_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(255))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fence: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(1024))
