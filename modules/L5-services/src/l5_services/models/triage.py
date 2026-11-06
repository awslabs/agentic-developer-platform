"""Append-only human decisions, distinct from native validation."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class Triage(Base):
    __tablename__ = "l5_service_triage"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "finding_id"],
                             ["l5_service_findings.tenant_id", "l5_service_findings.finding_id"]),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    finding_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision: Mapped[dict] = mapped_column(Payload, nullable=False)
