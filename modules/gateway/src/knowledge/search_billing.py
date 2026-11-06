"""Admit one bounded embedding and settle only the trusted Door usage receipt.

Unknown outcomes retain their calendar-period bound. Completed calls persist
through the normal usage/settlement transaction before shortening that hold.
No query text or retrieved content is written to the accounting surfaces.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from dataclasses import dataclass, replace
from decimal import ROUND_CEILING, Decimal
from importlib.resources import files
from uuid import uuid4

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool

from pricing_policy import RateRow, RoutingEvidence, build_pricing_decision, load_snapshot, normalize_usage, resolve_curated_non_openai
from src.activity.service import ActivityService
from src.budget.enforcement_service import BudgetEnforcementService
from src.budget.settlement import lock_legacy_metering_gates
from src.internal.tools_authorize import require_ordinary_tool_identity
from src.ratelimit.routes import get_rate_limit_service
from src.shared.database import get_accounting_session_factory
from src.shared.schemas.budget import DenyReason
from src.usage.persistence import persist_usage_with_retry
from src.usage.service import UsageService

logger = logging.getLogger(__name__)
MODEL = "amazon.titan-embed-text-v2:0"
TOOL = "knowledge___search"
MAX_INPUT_TOKENS = 8192


@dataclass(frozen=True)
class SearchPrice:
    snapshot: object
    row: RateRow

    @classmethod
    def load(cls):
        snapshot = load_snapshot()
        rates, known = resolve_curated_non_openai(MODEL, snapshot=snapshot)
        if not known or not rates["input"].is_finite() or rates["input"] <= 0 or rates["output"] != 0:
            raise ValueError("Embedding pricing is unavailable")
        source = files("pricing_policy").joinpath("snapshots", f"{snapshot.snapshot_version}.json")
        row = RateRow.from_mapping(
            {
                "model_id": MODEL,
                "geography": "in_region",
                "service_tier": "standard",
                "context_tier": "flat",
                "region": "unknown",
                "input_price_per_1k_tokens": rates["input"],
                "output_price_per_1k_tokens": rates["output"],
                "cache_read_price_per_1k_tokens": None,
                "cache_write_price_per_1k_tokens": None,
                "cache_write_policy": "unpublished",
                "source": "bundled_snapshot",
                "source_url": f"bundled://pricing_policy/snapshots/{snapshot.snapshot_version}.json",
                "source_content_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "verified_at": snapshot.provenance["verified_at"],
                "snapshot_version": snapshot.snapshot_version,
            }
        )
        return cls(snapshot, row)

    @property
    def ceiling(self):
        # Titan rejects input beyond 8,192 tokens. Reserve the entire provider
        # maximum, not a character/token guess, rounded up to ledger precision.
        return (self.row.input_price_per_1k_tokens * MAX_INPUT_TOKENS / 1000).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)

    def decision(self, context, request_id, tokens):
        return build_pricing_decision(
            request_id=request_id,
            org_id=context.org_id,
            usage=normalize_usage({"input_tokens": tokens, "output_tokens": 0}, api_format="openai"),
            evidence=RoutingEvidence(original_model_id=MODEL, billing_model_id=MODEL, forwarded_model_id=MODEL),
            rows=(self.row,),
            snapshot=self.snapshot,
        )


class SearchAdmission:
    def __init__(self, context, price, budget, activity, human_id):
        self.context = context
        self.human_id = human_id
        self.price = price
        self.budget = budget
        self.activity = activity
        self.request_id = str(uuid4())
        self.started_at = time.monotonic()
        self.dispatched = False
        self.result = None
        self.recorded = False

    async def finish(self):
        receipt = self.result.get("embedding_usage") if isinstance(self.result, dict) else None
        tokens = None
        if not self.dispatched:
            tokens = 0
        elif isinstance(receipt, dict) and receipt.get("model") == MODEL and type(receipt.get("version")) is int and receipt["version"] == 1:
            if receipt.get("status") == "not_started":
                tokens = 0
            elif (
                receipt.get("status") == "measured" and type(receipt.get("input_tokens")) is int and 1 <= receipt["input_tokens"] <= MAX_INPUT_TOKENS
            ):
                tokens = receipt["input_tokens"]

        if tokens is None:
            if self.recorded:
                await run_in_threadpool(self.activity.record_tool_accounting, **self._activity_identity(), status="pending")
            # No inference about usage from a timeout, HTTP error, missing
            # receipt, or a successful response from an older reader.
            await self.budget.reconcile_reservation(self.context, self.request_id, MODEL, 0, 0, usage_known=False)
            return False

        decision = self.price.decision(self.context, self.request_id, tokens)
        if self.dispatched:

            async def write_usage():
                async with get_accounting_session_factory()() as db:
                    await UsageService(db).log_request(
                        context=self.context,
                        model=MODEL,
                        input_tokens=tokens,
                        output_tokens=0,
                        cost_usd=decision.ledger_cost,
                        latency_ms=max(0, int((time.monotonic() - self.started_at) * 1000)),
                        status_code=503 if self.result.get("error") else 200,
                        request_id=self.request_id,
                        pricing_decision=decision,
                        reservation_usage_known=True,
                    )

            await persist_usage_with_retry(write_usage, request_id=self.request_id, retry_safe=True)
        # Durable settlement now owns calendar spend. Keep the standard short
        # overlap window, not the full-period hold used for uncertain tools.
        self.context._budget_admission_targets = [
            replace(target, ttl_seconds=None)
            if target.period_type in {"daily", "weekly", "monthly"} and not target.require_initialization
            else target
            for target in (self.context._budget_admission_targets or [])
        ]
        await self.budget.reconcile_reservation(
            self.context, self.request_id, MODEL, tokens, 0, actual_cost_usd=decision.ledger_cost, usage_known=True
        )
        if self.recorded:
            await run_in_threadpool(
                self.activity.record_tool_accounting,
                **self._activity_identity(),
                status="settled" if tokens else "not_started",
                decision=decision.to_dict(),
            )
        return True

    def _activity_identity(self):
        return {"receipt_id": self.request_id, "tenant_id": self.context.org_id, "user_id": self.human_id, "tool": TOOL}


async def admit_search(request, context, *, human_id, subject, aliases=()):
    """The context is resolved from live membership, never a search-body field."""
    aliases = tuple(value for value in {context.user_id, human_id, *aliases} if value)
    await require_ordinary_tool_identity(request, human_id=human_id, subject_id=subject, aliases=aliases)
    price = SearchPrice.load()
    # Refuse active/unknown metering modes before spending. The normal usage
    # writer performs this check again in its settlement transaction.
    async with get_accounting_session_factory()() as db:
        await lock_legacy_metering_gates(db, org_id=context.org_id, root_user_id=context.user_id, human_root=True)
    rate = await get_rate_limit_service(request).consume_tool_rate_limit(context, TOOL)
    if not rate.allowed:
        raise HTTPException(429, "Context search rate limit reached", headers={"Retry-After": str(max(1, rate.retry_after_seconds or 1))})
    table = os.environ.get("WEBHOOK_EVENTS_TABLE")
    if not table:
        raise ValueError("Knowledge activity is unavailable")
    admission = SearchAdmission(context, price, BudgetEnforcementService(), ActivityService(table_name=table), human_id)
    result = await admission.budget.check_budget_hierarchy(context, price.ceiling, request_id=admission.request_id, tool_estimate=True)
    if not result.allowed:
        if result.deny_reason == DenyReason.BUDGET_EXCEEDED:
            raise HTTPException(429, "Context search budget is exhausted", headers={"Retry-After": "60"})
        raise HTTPException(503, "Context search budget is unavailable")
    try:
        await run_in_threadpool(
            admission.activity.record_tool_call,
            **admission._activity_identity(),
            team_id=context.team_id,
            estimated_cost_usd=price.ceiling,
            run_id=None,
            request_id=admission.request_id,
        )
        admission.recorded = True
        await require_ordinary_tool_identity(request, human_id=human_id, subject_id=subject, aliases=aliases)
    except BaseException:
        await finalize_search(admission)
        raise
    return admission


async def finalize_search(admission):
    """Bound finalization while shielding it from client disconnect cancellation."""

    async def finish():
        try:
            async with asyncio.timeout(8):
                return await admission.finish()
        except Exception:
            logger.warning("Knowledge search accounting remains pending", extra={"request_id": admission.request_id}, exc_info=True)
            if admission.recorded:
                try:
                    async with asyncio.timeout(1):
                        await run_in_threadpool(admission.activity.record_tool_accounting, **admission._activity_identity(), status="pending")
                except Exception:
                    logger.warning("Knowledge pending activity update unavailable", extra={"request_id": admission.request_id})
            return False

    task = asyncio.create_task(finish())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise
