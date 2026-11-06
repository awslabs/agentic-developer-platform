"""Tenant-wide tool grants and optional team restrictions."""

from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base


class ToolEntitlement(Base):
    __tablename__ = "tool_entitlements"

    org_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    team_id: Mapped[str] = mapped_column(String(255), primary_key=True, default="")
    tool_name: Mapped[str] = mapped_column(String(128), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    max_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(12, 6), nullable=True)
    revision: Mapped[int] = mapped_column(default=1, nullable=False)

    __table_args__ = (CheckConstraint("max_cost_usd IS NULL OR max_cost_usd > 0", name="ck_tool_entitlement_positive_cost"),)
