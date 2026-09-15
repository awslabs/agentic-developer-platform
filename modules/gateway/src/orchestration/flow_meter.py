"""Use the existing reservation accumulator for all model calls in a flow.

The protected authority store records initialization once. It stores no billing
totals. A missing Redis accumulator after that is unknown usage, never a new
zero allowance. Admission holds and model charges use separate reservation keys.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from decimal import ROUND_UP, Decimal
from urllib.parse import unquote

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


def estimate_policy_model_cost(body: bytes, path: str) -> Decimal:
    """Conservative text-request quote with explicit output and published rates.

    Stateful server history, server tools and non-text payloads need provider
    token-count support. They cannot use the ordinary heuristic/default quote
    under an accepted hard allowance. The forwarded bytes remain unchanged.
    """
    from pricing_policy import canonical_billing_model_id, is_anthropic_model, load_snapshot
    from pricing_policy.policy import model_rate_candidates
    from src.budget.pricing_v2_reader import cached_rate_state

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("ambiguous request field")
            value[key] = item
        return value

    request = json.loads(body, object_pairs_hook=unique_object)
    if not isinstance(request, dict):
        raise ValueError("unsupported model request")
    model = request.get("model")
    if path.startswith("/model/"):
        model = unquote(path[len("/model/") :].rsplit("/", 1)[0])
    elif path != "/v1/messages":
        raise ValueError("provider token-count capability required")
    if not isinstance(model, str) or not is_anthropic_model(canonical_billing_model_id(model)):
        raise ValueError("published bounded model quote unavailable")
    output = request.get("max_tokens")
    if isinstance(output, bool) or not isinstance(output, int) or output <= 0:
        raise ValueError("explicit output bound required")
    if any(request.get(key) for key in ("mcp_servers", "container", "context_management", "previous_response_id")):
        raise ValueError("stateful input cannot be bounded locally")
    if any(tool.get("type") not in (None, "custom") for tool in request.get("tools", [])):
        raise ValueError("server tool costs require a scoped quote")

    def text_content(content):
        if isinstance(content, str):
            return
        if not isinstance(content, list):
            raise ValueError("unsupported content")
        for block in content:
            if not isinstance(block, dict) or block.get("type") not in {"text", "thinking", "tool_use", "tool_result"}:
                raise ValueError("non-text token-count capability required")
            if block.get("type") == "tool_result":
                text_content(block.get("content", ""))

    text_content(request.get("system", ""))
    messages = request.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("explicit input required")
    for message in messages:
        text_content(message["content"])
    rows = model_rate_candidates(load_snapshot().rates + cached_rate_state().rows, canonical_billing_model_id(model))
    if not rows:
        raise ValueError("published model pricing unavailable")
    # Price every possible input token at the most expensive published input or
    # cache-write rate, across context/geography variants. Full UTF-8 bytes plus
    # protocol framing reserve substantially more than the ordinary chars/4
    # heuristic; output uses the actual requested maximum, including thinking.
    input_rate = max(
        max(row.input_price_per_1k_tokens, row.cache_write_price_per_1k_tokens or Decimal(0), row.cache_write_1h_price_per_1k_tokens or Decimal(0))
        for row in rows
    )
    output_rate = max(row.output_price_per_1k_tokens for row in rows)
    cost = (Decimal(len(body) + 8192) * input_rate + Decimal(output) * output_rate) / 1000
    if not cost.is_finite() or cost <= 0:
        raise ValueError("model price unavailable")
    return cost.quantize(Decimal("0.000001"), rounding=ROUND_UP)
