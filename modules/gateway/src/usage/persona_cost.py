"""Preference-owner ledger totals with explicit pricing and coverage uncertainty."""

import json
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.models.usage import UsageLog

COST_SCOPE = "gateway-recorded agent model calls only; excludes build/infra and missing usage rows"
PRINCIPAL_DIMENSION = "preference_owner"


class PersonaCostStatus(StrEnum):
    KNOWN = "known"
    NONE_INCURRED = "none_incurred"
    ESTIMATED = "estimated"
    PARTIAL = "partial"
    UNKNOWN = "unknown"


def _status(calls: int, unpriced: int, estimated: int, amount: Decimal) -> PersonaCostStatus:
    if calls == 0 or unpriced == calls:
        return PersonaCostStatus.UNKNOWN
    if unpriced:
        return PersonaCostStatus.PARTIAL
    if estimated:
        return PersonaCostStatus.ESTIMATED
    return PersonaCostStatus.NONE_INCURRED if amount == 0 else PersonaCostStatus.KNOWN


@dataclass(frozen=True)
class PersonaModelCost:
    persona_key: str
    model_id: str
    amount_usd: Decimal | None
    input_tokens: int
    output_tokens: int
    call_count: int
    unpriced_call_count: int
    estimated_call_count: int
    status: PersonaCostStatus
    partial: bool
    estimate_reasons: tuple[str, ...]


@dataclass(frozen=True)
class PersonaCostReport:
    principal_kind: str
    principal_id: str
    chain_id: str | None
    status: PersonaCostStatus
    amount_usd: Decimal | None
    call_count: int
    unpriced_call_count: int
    estimated_call_count: int
    partial: bool
    entries: tuple[PersonaModelCost, ...]
    estimate_reasons: tuple[str, ...]
    preferences: list[dict] = field(default_factory=list)
    principal_dimension: str = PRINCIPAL_DIMENSION
    scope: str = COST_SCOPE
    caveat: str = "Internal usage-ledger consistency only; provider invoice reconciliation is not established."


async def get_persona_cost_report(
    db: AsyncSession, *, org_id: str, principal_kind: str, principal_id: str, chain_id: str | None = None
) -> PersonaCostReport:
    """Group ledger facts without repricing, re-resolving, or inventing absent cost.

    Authorization precedes this service. Both billing tenant and canonical
    preference owner scope the SQL query. The existing preference projection
    supplies class defaults even when there are no recorded calls.
    """
    from src.admin.persona_models.service import build_preference_list

    conditions = [
        UsageLog.org_id == org_id,
        UsageLog.preference_owner_kind == principal_kind,
        UsageLog.preference_owner_id == principal_id,
        UsageLog.persona_key.is_not(None),
    ]
    if chain_id is not None:
        conditions.append(UsageLog.chain_id == chain_id)
    rows = (
        await db.execute(
            select(
                UsageLog.persona_key,
                UsageLog.model,
                UsageLog.pricing_confidence,
                UsageLog.pricing_estimate_reasons,
                func.sum(UsageLog.cost_usd).label("amount"),
                func.sum(UsageLog.input_tokens).label("inputs"),
                func.sum(UsageLog.output_tokens).label("outputs"),
                func.count(UsageLog.id).label("calls"),
            )
            .where(*conditions)
            .group_by(UsageLog.persona_key, UsageLog.model, UsageLog.pricing_confidence, UsageLog.pricing_estimate_reasons)
        )
    ).all()
    buckets: dict[tuple[str, str], dict] = {}
    for row in rows:
        bucket = buckets.setdefault(
            (row.persona_key, row.model), dict(amount=Decimal(0), inputs=0, outputs=0, calls=0, unpriced=0, estimated=0, reasons=set())
        )
        bucket["inputs"] += int(row.inputs or 0)
        bucket["outputs"] += int(row.outputs or 0)
        bucket["calls"] += row.calls
        if row.pricing_confidence not in {"verified", "estimated"}:
            bucket["unpriced"] += row.calls
            continue
        bucket["amount"] += Decimal(row.amount or 0)
        if row.pricing_confidence == "estimated":
            bucket["estimated"] += row.calls
            try:
                reasons = json.loads(row.pricing_estimate_reasons or "[]")
                if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
                    raise ValueError("invalid pricing reasons")
                bucket["reasons"].update(reasons or ["unspecified_estimate"])
            except (TypeError, ValueError):
                bucket["reasons"].add("estimate_reason_unavailable")
    entries = []
    for (persona, model), bucket in sorted(buckets.items()):
        status = _status(bucket["calls"], bucket["unpriced"], bucket["estimated"], bucket["amount"])
        entries.append(
            PersonaModelCost(
                persona,
                model,
                None if status is PersonaCostStatus.UNKNOWN else bucket["amount"],
                bucket["inputs"],
                bucket["outputs"],
                bucket["calls"],
                bucket["unpriced"],
                bucket["estimated"],
                status,
                bool(bucket["unpriced"] or bucket["estimated"]),
                tuple(sorted(bucket["reasons"])),
            )
        )
    calls = sum(entry.call_count for entry in entries)
    unpriced = sum(entry.unpriced_call_count for entry in entries)
    estimated = sum(entry.estimated_call_count for entry in entries)
    amount = sum((entry.amount_usd or Decimal(0) for entry in entries), Decimal(0))
    status = _status(calls, unpriced, estimated, amount)
    return PersonaCostReport(
        principal_kind=principal_kind,
        principal_id=principal_id,
        chain_id=chain_id,
        status=status,
        amount_usd=None if status is PersonaCostStatus.UNKNOWN else amount,
        call_count=calls,
        unpriced_call_count=unpriced,
        estimated_call_count=estimated,
        partial=bool(unpriced or estimated),
        entries=tuple(entries),
        estimate_reasons=tuple(sorted({reason for entry in entries for reason in entry.estimate_reasons})),
        preferences=await build_preference_list(db, org_id=org_id, principal_kind=principal_kind, principal_id=principal_id),
    )
