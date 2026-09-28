"""ReconcileLock model — prevents concurrent reconciliation (section 6.1)."""

from datetime import datetime

from sqlalchemy import DateTime, PrimaryKeyConstraint, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ReconcileLock(Base):
    __tablename__ = "reconcile_locks"

    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    locked_by: Mapped[str] = mapped_column(String(255), nullable=False)
    locked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (PrimaryKeyConstraint("resource_type", "resource_id"),)
