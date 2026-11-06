"""Durable provider effect identity shared across worker leases."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class WorkerAttempt(Base):
    __tablename__ = "l5_service_worker_attempts"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        UniqueConstraint("tenant_id", "run_id", "operation"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    first_work_id: Mapped[str] = mapped_column(String(36), nullable=False)
    first_fence: Mapped[int] = mapped_column(nullable=False)
    idempotency_token: Mapped[str] = mapped_column(String(64), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    question_hash: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reconcile_checks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    native_job_ref: Mapped[dict | None] = mapped_column(Payload)
    prepared_request: Mapped[dict | None] = mapped_column(Payload)
