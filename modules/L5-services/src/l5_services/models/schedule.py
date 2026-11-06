"""Durable shared-repair schedule cursors and append-only event observations."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class ScheduleState(Base):
    __tablename__ = "l5_service_schedule_states"
    __table_args__ = (Index("ix_l5_schedule_due", "service_id", "next_due_at"),)

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    configuration_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    configuration_revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    service_id: Mapped[str] = mapped_column(String(255), nullable=False)
    initialized_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    triggers: Mapped[dict] = mapped_column(Payload, nullable=False)


class EventObservation(Base):
    __tablename__ = "l5_service_event_observations"
    __table_args__ = (Index("ix_l5_event_observed", "tenant_id", "idempotency_key", "observed_at"),)

    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(64))
    run_id: Mapped[str | None] = mapped_column(String(36))
