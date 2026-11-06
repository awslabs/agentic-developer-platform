"""Durable capture parameters, including retention, before admission commits."""

from datetime import datetime

from sqlalchemy import DateTime, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class AdmissionPreparation(Base):
    __tablename__ = "l5_service_admission_preparations"

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    service_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    verified: Mapped[dict] = mapped_column(Payload, nullable=False)
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
