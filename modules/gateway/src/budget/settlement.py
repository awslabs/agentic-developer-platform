"""Durable per-tenant request debit, independent of optional conversation logging."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.shared.models.budget import BudgetSettlementReceipt, BudgetUsage


async def settle_usage(db, *, org_id, request_id, user_id, cost, total_tokens, entities, timestamp=None):
    if not org_id or not request_id or not user_id:
        raise ValueError("settlement requires tenant, server request and owner")
    amount = Decimal(str(cost)).quantize(Decimal("0.000001"))
    if not amount.is_finite() or amount < 0 or type(total_tokens) is not int or total_tokens < 0:
        raise ValueError("invalid measured settlement")
    day = (timestamp or datetime.now(UTC)).astimezone(UTC).date()
    entities = sorted({(kind, identity) for kind, identity in entities if identity})
    allocation_key = hashlib.sha256(json.dumps([day.isoformat(), entities], separators=(",", ":")).encode()).hexdigest()
    insert = sqlite_insert if db.bind.dialect.name == "sqlite" else pg_insert
    claimed = await db.execute(
        insert(BudgetSettlementReceipt)
        .values(
            org_id=org_id,
            request_id=request_id,
            user_id=user_id,
            cost_usd=amount,
            total_tokens=total_tokens,
            allocation_key=allocation_key,
        )
        .on_conflict_do_nothing(index_elements=["org_id", "request_id"])
        .returning(BudgetSettlementReceipt.request_id)
    )
    if claimed.scalar_one_or_none() is None:
        receipt = (
            await db.execute(
                select(BudgetSettlementReceipt).where(
                    BudgetSettlementReceipt.org_id == org_id,
                    BudgetSettlementReceipt.request_id == request_id,
                )
            )
        ).scalar_one()
        if (
            receipt.user_id != user_id
            or receipt.cost_usd != amount
            or receipt.total_tokens != total_tokens
            or receipt.allocation_key != allocation_key
        ):
            raise ValueError("conflicting settlement replay")
        return False
    periods = {"daily": day, "weekly": day - timedelta(days=day.weekday()), "monthly": day.replace(day=1)}
    for entity_type, entity_id in sorted(set(entities)):
        if not entity_id:
            continue
        for period_type, period_start in periods.items():
            stmt = insert(BudgetUsage).values(
                id=str(uuid4()),
                org_id=org_id,
                entity_type=entity_type,
                entity_id=entity_id,
                period_type=period_type,
                period_start=period_start,
                total_cost_usd=amount,
                total_tokens=total_tokens,
                request_count=1,
            )
            await db.execute(
                stmt.on_conflict_do_update(
                    index_elements=["org_id", "entity_type", "entity_id", "period_start", "period_type"],
                    set_={
                        "total_cost_usd": BudgetUsage.total_cost_usd + amount,
                        "total_tokens": BudgetUsage.total_tokens + total_tokens,
                        "request_count": BudgetUsage.request_count + 1,
                    },
                )
            )
    return True


async def settle_priced_usage(db, *, context, request_id, decision):
    """Resolve accounting hierarchy only from the verified caller context."""
    from src.budget.enforcement_service import _unqualify_root_principal_id

    if decision.org_id != context.attributed_org_id or decision.request_id != request_id:
        raise ValueError("pricing decision tenant/request mismatch")
    root = context.attributed_user_id
    entities = [("user", context.user_id), ("org", context.attributed_org_id)]
    if context.team_id:
        entities.append(("team", context.team_id))
    if context.department_id:
        entities.append(("department", context.department_id))
    if context.account_type == "service":
        entities.append(("agent", context.user_id))
    if root and _unqualify_root_principal_id(root) != context.user_id:
        entities.append(("root_user", root))
    total_tokens = sum(
        decision.usage[key] for key in ("uncached_input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    return await settle_usage(
        db,
        org_id=context.attributed_org_id,
        request_id=request_id,
        user_id=context.user_id,
        cost=decision.ledger_cost,
        total_tokens=total_tokens,
        entities=entities,
        timestamp=context._budget_request_timestamp,
    )
