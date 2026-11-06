"""Run-scoped retention of immutable Product and packaged source artifacts."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class SourceRetention(Base):
    __tablename__ = "l5_service_source_retention"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "run_id"],
                             ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]),
        Index("ix_l5_source_retention_artifact", "tenant_id", "artifact_id"),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    artifact_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False)
    artifact_ref: Mapped[dict] = mapped_column(Payload, nullable=False)
    required_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    policy_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reason: Mapped[str] = mapped_column(String(100), nullable=False)
    last_evidence: Mapped[dict | None] = mapped_column(Payload)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
