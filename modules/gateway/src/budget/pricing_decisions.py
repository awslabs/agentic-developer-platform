"""Bind gateway usage to one immutable source and one durable Decimal amount."""

import asyncio
import hashlib
from dataclasses import replace
from pathlib import Path

from pricing_policy import (
    Confidence,
    EstimateReason,
    RateRow,
    build_pricing_decision,
    load_snapshot,
    normalize_usage,
)
from pricing_policy.policy import staleness_reasons
from pricing_policy.storage import utc_now_iso
from src.budget import pricing_v2_reader
from src.shared.database import get_session_factory
from src.shared.metrics import _emit_emf

PRICING_READ_TIMEOUT_SECONDS = 5.0


def emit_pricing_metrics(reasons, *, cache_age_seconds=0.0):
    """The fleet-level ADP/Gateway metrics watched by the pricing alarms."""
    metrics = {"PricingCacheAgeSeconds": cache_age_seconds}
    if cache_age_seconds > 0:
        metrics["PricingCacheRefreshFailure"] = 1
    for reason, name in (
        (EstimateReason.CACHE_REFRESH_FAILING, "PricingCacheRefreshFailure"),
        (EstimateReason.STALE_RATE_SOURCE, "PricingStaleRate"),
        (EstimateReason.UNSUPPORTED_VARIANT, "PricingUnknownVariant"),
        (EstimateReason.UNKNOWN_MODEL, "UnknownModelPricing"),
    ):
        if reason in reasons:
            metrics[name] = 1
    _emit_emf(metrics=metrics, dimensions=[[]], namespace="ADP/Gateway")


def _unknown_row(model_id, evidence, snapshot):
    """An explicitly estimated generic policy price, with bundled provenance."""
    rates = snapshot.curated_non_openai["rates"]["default"]
    path = Path(__file__).resolve().parents[2] / "pricing_policy" / "snapshots" / f"{snapshot.snapshot_version}.json"
    return RateRow.from_mapping(
        {
            "model_id": model_id,
            "geography": evidence.geography or "in_region",
            "service_tier": evidence.served_service_tier or "standard",
            "context_tier": "flat",
            "region": evidence.endpoint_region or "unknown",
            "input_price_per_1k_tokens": rates["input"],
            "output_price_per_1k_tokens": rates["output"],
            "cache_read_price_per_1k_tokens": None,
            "cache_write_price_per_1k_tokens": None,
            "cache_write_policy": "unpublished",
            "source": "bundled_snapshot",
            "source_url": f"bundled://pricing_policy/snapshots/{snapshot.snapshot_version}.json",
            "source_content_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "verified_at": snapshot.rates[0].verified_at,
            "snapshot_version": snapshot.snapshot_version,
        }
    )


def decision_from_state(*, request_id, org_id, usage, evidence, state):
    snapshot = load_snapshot()
    rows = tuple(row for row in state.rows if row.model_id == evidence.billing_model_id)
    reasons = set(state.reasons)
    database = state.from_database
    if not rows:
        rows = (_unknown_row(evidence.billing_model_id, evidence, snapshot),)
        database = False
        reasons.add(EstimateReason.UNKNOWN_MODEL)
    decision = build_pricing_decision(
        request_id=request_id,
        org_id=org_id,
        usage=usage,
        evidence=evidence,
        rows=rows,
        snapshot=snapshot,
        generation_id=state.generation_id if database else None,
        pointer_revision=state.pointer_revision if database else None,
        source_kind="database" if database else "bundled_snapshot",
        extra_reasons=tuple(reasons),
    )
    # Source age belongs to the selected row, never the publication timestamp or
    # an unrelated model in the same generation. The cost is computed only once.
    reasons.update(decision.estimate_reasons)
    reasons.update(staleness_reasons(row_verified_at=decision.verified_at, now_iso=utc_now_iso()))
    decision = replace(decision, estimate_reasons=tuple(sorted(reasons)), confidence=Confidence.ESTIMATED if reasons else Confidence.VERIFIED)
    emit_pricing_metrics(reasons, cache_age_seconds=pricing_v2_reader.cache_failure_age_seconds())
    return decision


async def price_completed_usage(*, request_id, org_id, raw_usage, evidence, session_factory=None):
    usage = normalize_usage(raw_usage, api_format="openai")
    # The read is after completion, before either persistence output. A failed
    # session acquisition keeps the reader's last good generation or bootstrap.
    try:
        factory = session_factory or get_session_factory()
        async with asyncio.timeout(PRICING_READ_TIMEOUT_SECONDS):
            async with factory() as session:
                state = await pricing_v2_reader.get_rate_state(session)
    except Exception as exc:
        pricing_v2_reader.record_connection_failure(exc)
        state = pricing_v2_reader.cached_rate_state()
    return decision_from_state(request_id=request_id, org_id=org_id, usage=usage, evidence=evidence, state=state)


async def refresh_pricing_cache(*, force=False):
    """Refresh off the inference path; reader owns bounded retry/TTL scheduling."""
    if not force and not pricing_v2_reader.refresh_due():
        return pricing_v2_reader.cached_rate_state()
    try:
        async with asyncio.timeout(PRICING_READ_TIMEOUT_SECONDS):
            async with get_session_factory()() as session:
                state = await pricing_v2_reader.get_rate_state(session, force=force)
    except Exception as exc:
        pricing_v2_reader.record_connection_failure(exc)
        state = pricing_v2_reader.cached_rate_state()
    emit_pricing_metrics(state.reasons, cache_age_seconds=pricing_v2_reader.cache_failure_age_seconds())
    return state


async def maintain_pricing_cache():
    """Every worker discovers publications, rollback, and schema availability."""
    while True:
        await refresh_pricing_cache()
        await asyncio.sleep(30)
