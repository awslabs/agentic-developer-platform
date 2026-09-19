"""BudgetAlert model — tracks budget violations and warnings for workspaces."""

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class BudgetAlert(Base):
    __tablename__ = "budget_alerts"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    org_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False
    )
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("workspaces.id"), nullable=False
    )
    alert_type: Mapped[str] = mapped_column(
        String(50), nullable=False
    )  # budget_warning, budget_exceeded, gpu_limit_exceeded
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, default="warning"
    )  # warning, critical
    threshold_pct: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    current_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    limit_value: Mapped[Decimal | None] = mapped_column(Numeric(12, 2), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
