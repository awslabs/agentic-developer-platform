"""Read-only reservation visibility for an already-authorized invocation."""

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from src.budget.enforcement_service import budget_enforcement_service
from src.budget.reservations import ReservationTarget
from src.shared.models.usage import UsageLog

logger = logging.getLogger(__name__)


async def enrich_budget_usage(db, item, *, tenant_id: str):
    if item.channel == "tool":
        return item
    try:
        async with asyncio.timeout(1):
            target = ReservationTarget(tenant_id, "run", item.invocation_id, "run", "lifetime", Decimal(0))
            store = budget_enforcement_service._get_reservations()
            if not store.enabled:
                return item
            amounts = await store.live_request_amounts(target)
            if not amounts:
                return item
            rows = await db.execute(
                select(UsageLog.request_id, func.sum(UsageLog.cost_usd))
                .where(UsageLog.org_id == tenant_id, UsageLog.agent_run_id == item.invocation_id, UsageLog.request_id.in_(amounts))
                .group_by(UsageLog.request_id)
            )
            recorded = dict(rows.all())
            item.budget_usage = {
                "counter_total_usd": float(sum(amounts.values(), Decimal(0))),
                "outstanding_reservations_usd": float(
                    sum((max(Decimal(0), value - recorded.get(key, Decimal(0))) for key, value in amounts.items()), Decimal(0))
                ),
                "observed_at": datetime.now(UTC).isoformat(),
            }
    except Exception:
        # A display failure must not stop the run, alter reservations or report $0.
        logger.debug("Invocation reservation display unavailable", extra={"invocation_id": item.invocation_id})
    return item
