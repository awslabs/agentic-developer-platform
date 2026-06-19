"""Activity cost aggregation — Postgres query for per-run cost data.

Issue #1616: Batched aggregation of cost/tokens/call_count from usage_logs
grouped by agent_run_id for a page of invocations.

Design note (cross-store read):
The activity module's primary store is DynamoDB (webhook-events table). Cost
data lives in Postgres (usage_logs table, owned by the gateway). This service
provides the Postgres enrichment layer — called after the DDB query returns a
page of invocations, it fetches cost aggregates for those invocation IDs.

Error isolation: if the Postgres query fails, the activity response is still
returned with null cost fields (graceful degradation, never 500).
"""

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.usage import UsageLog

logger = logging.getLogger("bedrockgateway.activity.cost")


async def get_cost_by_run_ids(
    db: AsyncSession,
    run_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Batch-fetch cost aggregates for a list of agent_run_id values.

    Returns a dict keyed by agent_run_id with:
        - total_cost_usd: Decimal (sum of cost_usd)
        - total_tokens: int (sum of input_tokens + output_tokens)
        - call_count: int (number of usage_logs rows)

    Missing/unknown run_ids are simply absent from the result dict.
    """
    if not run_ids:
        return {}

    query = (
        select(
            UsageLog.agent_run_id,
            func.sum(UsageLog.cost_usd).label("total_cost_usd"),
            func.sum(UsageLog.input_tokens + UsageLog.output_tokens).label("total_tokens"),
            func.count(UsageLog.id).label("call_count"),
        )
        .where(UsageLog.agent_run_id.in_(run_ids))
        .group_by(UsageLog.agent_run_id)
    )

    result = await db.execute(query)
    rows = result.all()

    return {
        row.agent_run_id: {
            "total_cost_usd": float(row.total_cost_usd or Decimal("0")),
            "total_tokens": int(row.total_tokens or 0),
            "call_count": int(row.call_count or 0),
        }
        for row in rows
    }


async def get_cost_by_correlation_id(
    db: AsyncSession,
    correlation_id: str,
    run_ids: list[str],
) -> dict[str, Any]:
    """Get total cost for a chain (all runs sharing a correlation_id).

    Uses the provided run_ids to scope the aggregation (these come from
    the DDB chain query, ensuring tenant/user scoping is maintained).

    Returns:
        - total_cost_usd: float
        - total_tokens: int
        - call_count: int
    """
    if not run_ids:
        return {"total_cost_usd": 0.0, "total_tokens": 0, "call_count": 0}

    query = select(
        func.sum(UsageLog.cost_usd).label("total_cost_usd"),
        func.sum(UsageLog.input_tokens + UsageLog.output_tokens).label("total_tokens"),
        func.count(UsageLog.id).label("call_count"),
    ).where(UsageLog.agent_run_id.in_(run_ids))

    result = await db.execute(query)
    row = result.one()

    return {
        "total_cost_usd": float(row.total_cost_usd or Decimal("0")),
        "total_tokens": int(row.total_tokens or 0),
        "call_count": int(row.call_count or 0),
    }
