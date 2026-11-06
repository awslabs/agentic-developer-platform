"""Idempotency receipts for admission, retained alongside runs."""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKeyConstraint, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base
from .configuration import Payload


class RunReceipt(Base):
    __tablename__ = "l5_service_receipts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "run_id"], ["l5_service_runs.tenant_id", "l5_service_runs.run_id"]
        ),
    )

    tenant_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    product_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    project_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    service_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    request: Mapped[dict | None] = mapped_column(Payload)
    response: Mapped[dict | None] = mapped_column(Payload)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
