"""Durable per-tenant request debit, independent of optional conversation logging."""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import and_, or_, select, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from src.budget.metering_activation import mark_legacy_cohort_settled
from src.shared.models.budget import BudgetSettlementReceipt, BudgetUsage
from src.shared.models.metering import MeteringScopeAlias, MeteringScopeGate
from src.shared.models.organization import User


async def lock_legacy_metering_gates(db, *, org_id: str, root_user_id: str | None = None, human_root: bool = False) -> None:
    if db.bind.dialect.name != "postgresql" or await db.scalar(text("SELECT to_regclass('metering_scope_gates')")) is None:
        return
    await db.execute(text("SET LOCAL lock_timeout = '500ms'"))
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"), {"key": f"metering-gates:tenant:{org_id}"})
    relevant = MeteringScopeGate.tenant_id == org_id
    if root_user_id:
        identity = await db.scalar(
            select(User.id).where(or_(User.id == root_user_id, and_(User.org_id == org_id, User.cognito_sub == root_user_id))).limit(1)
        )
        if identity:
            root_user_id = identity
        elif human_root and await db.scalar(
            select(MeteringScopeGate.scope_key).where(MeteringScopeGate.scope_kind == "person", MeteringScopeGate.activation == "active").limit(1)
        ):
            raise ValueError("direct human identity cannot be resolved against active person gates")
        aliases = select(MeteringScopeAlias.scope_key).where(
            MeteringScopeAlias.alias_kind == "person_user", MeteringScopeAlias.stable_alias == root_user_id
        )
        relevant = or_(relevant, MeteringScopeGate.scope_key.in_(aliases))
    gates = await db.scalars(select(MeteringScopeGate).where(relevant).order_by(MeteringScopeGate.scope_key).with_for_update())
    if any(gate.activation == "active" for gate in gates):
        raise ValueError("active metering gate requires a prepared usage receipt")


async def settle_usage(
    db, *, org_id, request_id, user_id, cost, total_tokens, entities, timestamp=None, reservation_scope_keys=None, verified_provider_usage=False
):
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
            reservation_scope_keys=sorted(set(reservation_scope_keys)) if reservation_scope_keys else None,
        )
        .on_conflict_do_nothing(index_elements=["org_id", "request_id"])
        .returning(BudgetSettlementReceipt.request_id)
    )
    if claimed.scalar_one_or_none() is None:
        receipt = (
            await db.execute(
                select(BudgetSettlementReceipt)
                .where(
                    BudgetSettlementReceipt.org_id == org_id,
                    BudgetSettlementReceipt.request_id == request_id,
                )
                .with_for_update()
            )
        ).scalar_one()
        if (
            receipt.user_id != user_id
            or receipt.cost_usd != amount
            or receipt.total_tokens != total_tokens
            or receipt.allocation_key != allocation_key
        ):
            raise ValueError("conflicting settlement replay")
        if reservation_scope_keys:
            receipt.reservation_scope_keys = sorted(set(receipt.reservation_scope_keys or []) | set(reservation_scope_keys))
        if verified_provider_usage:
            await mark_legacy_cohort_settled(db, tenant_id=org_id, request_id=request_id, owner_id=user_id)
        return False
    periods = {"daily": day, "weekly": day - timedelta(days=day.weekday()), "monthly": day.replace(day=1)}
    for entity_type, entity_id in sorted(set(entities)):
        if not entity_id:
            continue
        entity_periods = {**periods, "run": datetime(1970, 1, 1).date()} if entity_type == "project" else periods
        for period_type, period_start in entity_periods.items():
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
    if verified_provider_usage:
        await mark_legacy_cohort_settled(db, tenant_id=org_id, request_id=request_id, owner_id=user_id)
    return True


async def settle_priced_usage(db, *, context, request_id, decision, reservation_usage_known=False):
    """Resolve accounting hierarchy only from the verified caller context."""
    from src.budget.enforcement_service import _unqualify_root_principal_id

    if decision.org_id != context.attributed_org_id or decision.request_id != request_id:
        raise ValueError("pricing decision tenant/request mismatch")
    root = context.attributed_user_id or (context.user_id if context.account_type != "service" else "")
    person_root = root if context.account_type != "service" or root != context.user_id else ""
    await lock_legacy_metering_gates(
        db,
        org_id=context.attributed_org_id,
        root_user_id=_unqualify_root_principal_id(person_root) if person_root and not person_root.startswith("service:") else None,
        human_root=context.account_type != "service",
    )
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
    project = context._project_attribution
    if project is not None:
        if project.tenant_id != context.attributed_org_id:
            raise ValueError("Project settlement tenant mismatch")
        entities.append(("project", project.project_id))
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
        verified_provider_usage=reservation_usage_known and decision.confidence == "verified" and decision.usage.get("valid") is True,
        timestamp=context._budget_request_timestamp,
        reservation_scope_keys=[
            target.key()
            for target in (context._budget_admission_targets or context._run_scope_reservations)
            if target.require_initialization or target.entity_type in {"run", "chain"}
        ]
        if reservation_usage_known
        else None,
    )
