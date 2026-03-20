from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Date, DateTime, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TenantMixin, new_uuid, utcnow


class BudgetConfig(Base, TenantMixin):
    __tablename__ = "budget_configs"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)  # org/department/team/user/service_account
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)  # daily/weekly/monthly
    budget_amount_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    enforcement_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="hard")  # soft/hard
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    __table_args__ = (UniqueConstraint("org_id", "entity_type", "entity_id", "period_type", name="uq_budget_config"),)


class BudgetUsage(Base, TenantMixin):
    __tablename__ = "budget_usage"

    id: Mapped[str] = mapped_column(String(255), primary_key=True, default=new_uuid)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(255), nullable=False)
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_type: Mapped[str] = mapped_column(String(10), nullable=False)
    total_cost_usd: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(default=0)
    request_count: Mapped[int] = mapped_column(default=0)

    __table_args__ = (UniqueConstraint("org_id", "entity_type", "entity_id", "period_start", "period_type", name="uq_budget_usage"),)
