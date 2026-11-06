"""Immutable native observations and terminal result readback."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class ProviderObservation(Base):
    __tablename__ = "l5_service_provider_observations"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        UniqueConstraint("tenant_id", "run_id", "work_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    work_id: Mapped[str] = mapped_column(String(36), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(Payload, nullable=False)


class AssessmentResult(Base):
    __tablename__ = "l5_service_results"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        UniqueConstraint("tenant_id", "run_id", "version"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(Payload, nullable=False)
