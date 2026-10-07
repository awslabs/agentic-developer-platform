"""Use the existing reservation accumulator for all model calls in a flow.

The protected authority store records initialization once. It stores no billing
totals. A missing Redis accumulator after that is unknown usage, never a new
zero allowance. Admission holds and model charges use separate reservation keys.
"""

from __future__ import annotations

import os
from dataclasses import replace
from decimal import Decimal

from starlette.concurrency import run_in_threadpool

from .execution_policy import ExecutionPolicy, flow_budget_binding
from .flow_budget import get_flow_reservations
from .models import NodeKind
from .state import NodeState


def meter_target(*, org_id: str, flow_id: str, policy: ExecutionPolicy):
    from src.budget.reservations import ReservationTarget
    from src.shared.schemas.budget import EntityType, PeriodType

    return ReservationTarget(
        org_id=org_id,
        entity_type=EntityType.FLOW.value,
        entity_id=flow_budget_binding(flow_id) + ":models",
        period_type=PeriodType.RUN.value,
        period_start="lifetime",
        headroom_usd=policy.limits.max_spend_usd,
        # The flow's wall-clock limit is at most 24h from its first dispatch.
        # Two days cannot drop a settled charge while that flow can still act.
        ttl_seconds=172800,
        require_initialization=True,
    )


async def _claim_initialization(*, org_id: str, flow_id: str, allow_create: bool) -> bool:
    from src.agentauth.engine import get_engine_authority_writer

    if os.environ.get("AGENT_AUTHORITY_ENABLED", "false").lower() != "true":
        raise RuntimeError("policy model budgets require protected authority")
    return await run_in_threadpool(
        get_engine_authority_writer().store.claim_policy_budget_initialization,
        tenant_id=org_id,
        flow_id=flow_id,
        allow_create=allow_create,
    )


async def prepare_flow_meter(*, org_id: str, flow_id: str, policy: ExecutionPolicy, nodes: list) -> bool:
    from src.budget.config import budget_config

    if not budget_config.budget_reservation_enabled:
        return False
    store = get_flow_reservations()
    if not store.enabled:
        return False
    try:
        fresh = await _claim_initialization(
            org_id=org_id,
            flow_id=flow_id,
            allow_create=all(
                n.attempts == 0 and (n.kind == NodeKind.GATE.value or n.state in {NodeState.PENDING.value, NodeState.READY.value}) for n in nodes
            ),
        )
        target = meter_target(org_id=org_id, flow_id=flow_id, policy=policy)
        if fresh:
            outcome = await store.reserve("__initialized__", Decimal(0), [replace(target, require_initialization=False)])
            if outcome is None or not outcome.admitted:
                return False
        return await store.snapshot(target) is not None
    except Exception:
        return False


async def read_flow_meter(*, org_id: str, flow_id: str, policy: ExecutionPolicy):
    from src.budget.config import budget_config

    store = get_flow_reservations()
    if not budget_config.budget_reservation_enabled or not store.enabled:
        return None
    return await store.snapshot(meter_target(org_id=org_id, flow_id=flow_id, policy=policy))


async def reconcile_flow_meter(session, *, org_id: str, flow_id: str, policy: ExecutionPolicy):
    """Repair missed settlement before admitting subsequent work, never infer $0.

    Receipt and debit commit together. Only the proxy's explicit strict-usage
    attestation binds a receipt to a reservation key; legacy/diagnostic rows are
    insufficient. A failed retry leaves the original conservative meter in place.
    """
    import logging

    from sqlalchemy import select

    from src.budget.config import budget_config
    from src.shared.models.budget import BudgetSettlementReceipt

    store = get_flow_reservations()
    if not budget_config.budget_reservation_enabled or not store.enabled:
        return None
    target = meter_target(org_id=org_id, flow_id=flow_id, policy=policy)
    try:
        ids = await store.unresolved_requests(target)
        # Batch IDs rather than limit matching rows: legacy/untrusted receipts
        # must not starve a later recoverable request.
        for offset in range(0, len(ids), 200):
            receipts = await session.scalars(
                select(BudgetSettlementReceipt).where(
                    BudgetSettlementReceipt.org_id == org_id,
                    BudgetSettlementReceipt.request_id.in_(ids[offset : offset + 200]),
                    BudgetSettlementReceipt.reservation_scope_keys.is_not(None),
                )
            )
            for receipt in receipts:
                if target.key() in (receipt.reservation_scope_keys or []):
                    await store.reconcile_receipt(receipt.request_id, receipt.cost_usd, target)
    except Exception:
        logging.getLogger(__name__).warning("Flow receipt reconciliation deferred; unreconciled reservations retained", exc_info=True)
    return await read_flow_meter(org_id=org_id, flow_id=flow_id, policy=policy)


def estimate_policy_model_cost(body: bytes, path: str) -> Decimal:
    """The bounded upper-bound amount for a policy-governed model request.

    Issue #5225 moved the bound itself into ``provider_quotes``, where it is one
    typed, request-bound adapter among the providers still to come. This remains
    as the amount-only view for callers that need just the number, and delegates
    so there is exactly one implementation of the safety margin.

    Prefer ``provider_quotes.quote_request`` in new code: it returns the typed
    quote, which carries the request-byte hash, billing model and pricing
    revision needed to prove the amount belongs to THIS request. A refusal here
    is still a ``ValueError``, so existing callers are unchanged.
    """
    from .provider_quotes import AnthropicTextQuoteAdapter, QuoteRefusedError, QuoteRequest

    # Bound synchronously against the one adapter whose bound needs no provider
    # round trip. Async-only adapters (#5226/#5227) are reachable solely through
    # ``quote_request``; this sync view cannot silently serve them an estimate.
    adapter = AnthropicTextQuoteAdapter()
    if not adapter.handles(path):
        raise ValueError("provider token-count capability required")
    try:
        return adapter.bound(QuoteRequest(body=body, path=path)).total_usd
    except QuoteRefusedError as exc:
        raise ValueError(exc.refusal.detail or exc.refusal.reason) from None
