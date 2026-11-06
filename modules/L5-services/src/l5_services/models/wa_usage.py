"""Durable receipt of an entitled later design task's consumer retrieval and use."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class WellArchitectedUsage(Base):
    __tablename__ = "l5_service_wa_usage"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "publication_id"],
                             ["l5_service_publications.tenant_id", "l5_service_publications.publication_id"]),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    publication_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    result_id: Mapped[str] = mapped_column(String(36), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(255), nullable=False)
    retrieval_receipt: Mapped[dict] = mapped_column(Payload, nullable=False)
    usage_receipt: Mapped[dict] = mapped_column(Payload, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
