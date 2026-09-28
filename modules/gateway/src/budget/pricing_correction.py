"""Audited credits for GPT-6 requests charged with the unknown-model fallback.

Original settlement receipts stay immutable so gateway/S3 retries retain their
original identity. The diagnostic row and every settled budget rung change in
one transaction; the unique audit record makes the correction idempotent.
"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, or_, select

from pricing_policy import RoutingEvidence, build_pricing_decision, load_snapshot, normalize_usage, verify_pricing_decision
from src.shared.models.budget import BudgetPricingCorrection, BudgetSettlementReceipt, BudgetUsage
from src.shared.models.usage import UsageLog

CORRECTION = "gpt6-published-rates-2026-09-28"
SNAPSHOT = "2026-09-28.1"


def corrected_decision(payload):
    """Use captured usage and routing, never current model routing preferences."""
    original = verify_pricing_decision(payload, request_id=payload["request_id"], org_id=payload["org_id"])
    if payload["variant_key"][0] not in {"openai.gpt-6-sol", "openai.gpt-6-luna"} or "unknown_model" not in payload["estimate_reasons"]:
        raise ValueError("not an affected fallback decision")
    u = payload["usage"]
    if not u["valid"] or u["api_format"] != "openai":
        raise ValueError("unsupported usage evidence")
    usage = normalize_usage(
        {
            "input_tokens": u["total_input_tokens"],
            "output_tokens": u["output_tokens"],
            "input_tokens_details": {"cached_tokens": u["cache_read_input_tokens"], "cache_write_tokens": u["cache_creation_input_tokens"]},
        },
        api_format="openai",
    )
    evidence = RoutingEvidence(**{key: payload["routing"].get(key) for key in RoutingEvidence.__dataclass_fields__})
    snapshot = load_snapshot(SNAPSHOT)
    decision = build_pricing_decision(
        request_id=payload["request_id"],
        org_id=payload["org_id"],
        usage=usage,
        evidence=evidence,
        rows=snapshot.rates,
        snapshot=snapshot,
        source_kind="bundled_snapshot",
    )
    if set(decision.estimate_reasons) - {"bootstrap_fallback", "stale_rate_source", "unconfirmed_service_tier"}:
        raise ValueError("published rate cannot be established from captured routing")
    # This incident repair grants credits only. Zero would let a legacy S3
    # cost bridge replace the corrected value; those records require review.
    if not Decimal(0) < decision.ledger_cost < original:
        raise ValueError("not a positive historical credit")
    return decision


def allocation_from_log(log):
    from src.budget.enforcement_service import _unqualify_root_principal_id

    day = datetime.fromisoformat(log["timestamp"].replace("Z", "+00:00")).astimezone(UTC).date()
    org, user = log["org_id"], log["user_id"]
    entities = [("user", user), ("org", org)]
    for kind in ("team", "department"):
        if log.get(kind + "_id"):
            entities.append((kind, log[kind + "_id"]))
    if log["account_type"] == "service":
        entities.append(("agent", user))
    root = log.get("root_human_id")
    if root and _unqualify_root_principal_id(root) != user:
        entities.append(("root_user", root))
    entities = sorted(set(entities))
    digest = hashlib.sha256(json.dumps([day.isoformat(), entities], separators=(",", ":")).encode()).hexdigest()
    return day, entities, digest


async def correct_request(db, *, org_id, request_id, log, source_key, actor, apply=False):
    """Caller owns commit/rollback. Trusted S3 allocation must match the debit hash."""
    if not actor or log.get("org_id") != org_id or log.get("request_id") != request_id:
        raise ValueError("correction identity mismatch")
    receipt_query = select(BudgetSettlementReceipt).where(
        BudgetSettlementReceipt.org_id == org_id,
        BudgetSettlementReceipt.request_id == request_id,
    )
    receipt = (await db.execute(receipt_query.with_for_update() if apply else receipt_query)).scalar_one()
    existing = await db.get(BudgetPricingCorrection, (org_id, request_id, CORRECTION))
    if existing:
        return {"status": "already_corrected", "credit_usd": str(existing.credit_usd)}
    usage_query = select(UsageLog).where(UsageLog.org_id == org_id, UsageLog.request_id == request_id)
    row = (await db.execute(usage_query.with_for_update() if apply else usage_query)).scalar_one()
    original = log["pricing_decision"]
    amount = verify_pricing_decision(original, org_id=org_id, request_id=request_id)
    day, entities, allocation_key = allocation_from_log(log)
    if receipt.user_id != log["user_id"] or row.user_id != receipt.user_id or receipt.allocation_key != allocation_key:
        raise ValueError("captured allocation does not match original settlement")
    if row.pricing_decision != original or row.cost_usd != amount or receipt.cost_usd != amount:
        raise ValueError("original pricing evidence does not match ledger")
    if receipt.total_tokens != original["usage"]["total_input_tokens"] + original["usage"]["output_tokens"]:
        raise ValueError("original settlement token mismatch")
    decision = corrected_decision(original)
    credit = amount - decision.ledger_cost
    periods = {"daily": day, "weekly": day - timedelta(days=day.weekday()), "monthly": day.replace(day=1)}
    allocation_filters = [
        and_(BudgetUsage.entity_type == kind, BudgetUsage.entity_id == identity, BudgetUsage.period_type == period, BudgetUsage.period_start == start)
        for kind, identity in entities
        for period, start in periods.items()
    ]
    balance_query = (
        select(BudgetUsage)
        .where(BudgetUsage.org_id == org_id, or_(*allocation_filters))
        .order_by(
            BudgetUsage.entity_type,
            BudgetUsage.entity_id,
            case({"daily": 0, "weekly": 1, "monthly": 2}, value=BudgetUsage.period_type),
        )
    )
    balances = (await db.scalars(balance_query.with_for_update() if apply else balance_query)).all()
    if len(balances) != len(allocation_filters):
        raise ValueError("original budget allocation is incomplete")
    if any(balance.total_cost_usd < credit for balance in balances):
        raise ValueError("credit exceeds settled balance")
    if apply:
        db.add(
            BudgetPricingCorrection(
                org_id=org_id,
                request_id=request_id,
                correction_id=CORRECTION,
                credit_usd=credit,
                original_decision=original,
                corrected_decision=decision.to_dict(),
                allocation_key=allocation_key,
                source_key=source_key,
                actor=actor,
            )
        )
        row.cost_usd = decision.ledger_cost
        row.pricing_decision = decision.to_dict()
        row.pricing_confidence = decision.confidence
        row.pricing_estimate_reasons = json.dumps(list(decision.estimate_reasons))
        row.pricing_source_kind = decision.source_kind
        row.pricing_generation_id = decision.generation_id
        row.pricing_pointer_revision = decision.pointer_revision
        row.pricing_snapshot_version = decision.snapshot_version
        row.pricing_policy_version = decision.policy_version
        for balance in balances:
            balance.total_cost_usd -= credit
        await db.flush()
    return {
        "status": "corrected" if apply else "would_correct",
        "credit_usd": str(credit),
        "original_usd": str(amount),
        "corrected_usd": str(decision.ledger_cost),
        "balances": len(balances),
    }
