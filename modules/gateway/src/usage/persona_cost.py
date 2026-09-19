"""Per-persona cost attribution over the usage ledger (#5426).

This view is internally consistent with ``usage_logs``; it is not an invoice
reconciliation and does not read ``budget_usage``.  One usage row is one model
call at one hop, so grouping the ledger directly cannot double-count a chain.
"""

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.usage import UsageLog

COST_SCOPE = "gateway-recorded agent model calls only; excludes build/infra and missing usage rows"
PRINCIPAL_DIMENSION = "preference_owner"


class PersonaCostStatus(StrEnum):
    KNOWN = "known"
    NONE_INCURRED = "none_incurred"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PersonaModelCost:
    persona_key: str
    model_id: str
    amount_usd: Decimal
    input_tokens: int
    output_tokens: int
    call_count: int
    unpriced_call_count: int


@dataclass(frozen=True)
class PersonaCostReport:
    principal_kind: str
    principal_id: str
    chain_id: str | None
    status: PersonaCostStatus
    amount_usd: Decimal | None
    call_count: int
    unpriced_call_count: int
    partial: bool
    entries: tuple[PersonaModelCost, ...]
    principal_dimension: str = PRINCIPAL_DIMENSION
    scope: str = COST_SCOPE
    caveat: str = "Internal usage-ledger consistency only; provider invoice reconciliation is not established."


async def get_persona_cost_report(
    db: AsyncSession,
    *,
    org_id: str,
    principal_kind: str,
    principal_id: str,
    chain_id: str | None = None,
) -> PersonaCostReport:
    """Return one tenant/principal report using one grouped Postgres query.

    ``org_id`` is the billing tenant already persisted by the usage writer, not
    the caller's authorization tenant.  Authorization must happen before this
    service is called.  The principal dimension is separately and explicitly
    the PMM preference owner; approving-human audit identity is never used.
    """
    conditions = [
        UsageLog.org_id == org_id,
        UsageLog.preference_owner_kind == principal_kind,
        UsageLog.preference_owner_id == principal_id,
        UsageLog.persona_key.is_not(None),
    ]
    if chain_id is not None:
        conditions.append(UsageLog.chain_id == chain_id)

    query = (
        select(
            UsageLog.persona_key,
            UsageLog.model,
            func.sum(UsageLog.cost_usd).label("amount_usd"),
            func.sum(UsageLog.input_tokens).label("input_tokens"),
            func.sum(UsageLog.output_tokens).label("output_tokens"),
            func.count(UsageLog.id).label("call_count"),
            func.sum(case((UsageLog.pricing_source_kind.is_(None), 1), else_=0)).label("unpriced_call_count"),
        )
        .where(*conditions)
        .group_by(UsageLog.persona_key, UsageLog.model)
        .order_by(UsageLog.persona_key, UsageLog.model)
    )
    rows = (await db.execute(query)).all()
    entries = tuple(
        PersonaModelCost(
            persona_key=row.persona_key,
            model_id=row.model,
            amount_usd=Decimal(row.amount_usd or 0),
            input_tokens=int(row.input_tokens or 0),
            output_tokens=int(row.output_tokens or 0),
            call_count=int(row.call_count or 0),
            unpriced_call_count=int(row.unpriced_call_count or 0),
        )
        for row in rows
    )
    call_count = sum(entry.call_count for entry in entries)
    unpriced = sum(entry.unpriced_call_count for entry in entries)
    if call_count == 0 or unpriced == call_count:
        status = PersonaCostStatus.UNKNOWN
        amount = None
    else:
        amount = sum((entry.amount_usd for entry in entries), Decimal(0))
        status = PersonaCostStatus.NONE_INCURRED if amount == 0 else PersonaCostStatus.KNOWN
    return PersonaCostReport(
        principal_kind=principal_kind,
        principal_id=principal_id,
        chain_id=chain_id,
        status=status,
        amount_usd=amount,
        call_count=call_count,
        unpriced_call_count=unpriced,
        partial=unpriced > 0,
        entries=entries,
    )
