"""Live budget controls. Only authenticated server assignments select a flow.

These rows change financial enforcement, never the accepted execution policy.
Reads deliberately have no process cache: gateway replicas and scheduled ticks
observe committed changes on their next admission.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, select
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.models.base import Base, utcnow


class BudgetEnforcementSetting(Base):
    __tablename__ = "budget_enforcement_settings"

    # Global settings have no tenant. Flow keys are scoped explicitly below.
    scope_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_by: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class BudgetAccountingGap(Base):
    """A failed observation is unknown usage, including after enforcement resumes.

    No dollar balances live here. Existing reservations and usage logs remain the
    accounting sources; toggling a control cannot erase missing observations.
    """

    __tablename__ = "budget_accounting_gaps"

    request_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


def flow_key(org_id: str, flow_id: str) -> str:
    return f"flow:{org_id}:{flow_id}"


@dataclass(frozen=True)
class EnforcementPosture:
    global_enabled: bool
    flow_enabled: bool | None
    global_revision: int
    flow_revision: int
    accounting_incomplete: bool = False

    @property
    def enabled(self) -> bool:
        # A global off is a master switch. A flow can opt out of global on.
        return self.global_enabled and self.flow_enabled is not False


def environment_default() -> bool:
    return os.environ.get("BUDGET_ENFORCEMENT_ENABLED", "true").lower() == "true"


async def read_enforcement(session, *, org_id: str | None = None, flow_id: str | None = None) -> EnforcementPosture:
    key = flow_key(org_id, flow_id) if org_id and flow_id else None
    keys = ["global"] + ([key] if key else [])
    rows = {row.scope_key: row for row in await session.scalars(select(BudgetEnforcementSetting).where(BudgetEnforcementSetting.scope_key.in_(keys)))}
    global_row, flow_row = rows.get("global"), rows.get(key)
    # Only a gap against THIS flow's accumulator makes its spend unknowable, so only
    # that fails the flow closed. A "global" gap is written for requests with no flow
    # binding; it has no accumulator behind it and stays an audit record. Gating on
    # it blocked every request on the deployment the moment one unbound chat turn was
    # cancelled by a pod restart, with no code path that ever cleared the row.
    gap = await session.scalar(select(BudgetAccountingGap.request_id).where(BudgetAccountingGap.scope_key == key).limit(1)) if key else None
    return EnforcementPosture(
        global_enabled=global_row.enabled if global_row else environment_default(),
        flow_enabled=flow_row.enabled if flow_row else None,
        global_revision=global_row.revision if global_row else 0,
        flow_revision=flow_row.revision if flow_row else 0,
        accounting_incomplete=gap is not None,
    )
