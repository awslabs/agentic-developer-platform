"""Actual WA transport observations and immutable owner-selected checkpoints."""

from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class WATransportObservation(Base):
    __tablename__ = "l5_wa_transport_observations"
    __table_args__ = (Index("ix_l5_wa_transport_scope", "tenant_id", "configuration_id", "observed_at"),)

    observation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    send_id: Mapped[str] = mapped_column(String(36), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(255), nullable=False)
    configuration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    configuration_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    attempt_id: Mapped[str] = mapped_column(String(36), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    facts: Mapped[dict] = mapped_column(Payload, nullable=False)


class WAPeriodicCheckpoint(Base):
    __tablename__ = "l5_wa_periodic_checkpoints"
    __table_args__ = (Index("ix_l5_wa_periodic_owner", "tenant_id", "configuration_id", "actor_id"),)

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    checkpoint_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    configuration_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot: Mapped[dict] = mapped_column(Payload, nullable=False)
