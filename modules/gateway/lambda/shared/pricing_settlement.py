"""Settle one chat log to a ledger cost (§4.3, S3/S4).

The tracker used to price every record itself, from a flat `model_pricing` table
keyed on model id alone. That table has no geography, no service tier, no context
tier and no cache columns, so a GovCloud long-context Priority request and an
in-region short-context Flex request of the same model settled at the same rate.
Correcting the rate literals alone would not have fixed that: the dimensions were
not being carried at all.

Two paths, in strict priority order:

1. **The gateway already decided.** When the chat log carries a `pricing_decision`,
   that decision was computed once at response completion against a single
   immutable generation, with the routing evidence only the gateway can observe
   (the tier upstream actually served, the region it actually forwarded to). It is
   verified and reused verbatim. The tracker does NOT recompute it — recomputing
   against whatever generation is active at settlement time is exactly how a
   request gets billed at a rate that was never quoted to it, and it would make
   the settled amount depend on when the S3 event happened to be delivered.

2. **No decision present.** Legacy events, and anything written by a gateway
   older than this change. OpenAI is priced from the pinned compatibility
   snapshot, marked
   estimated with a reason — the routing evidence is gone, so the tier and
   geography are inferred conservatively rather than measured.

A decision that is present but invalid is an error, never silently downgraded to
path 2. Downgrading would re-price the request from current rates and quietly
defeat the whole durable-decision contract, which is the one thing that makes a
historical cost reproducible.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from pricing_policy import (
    COMPATIBILITY_SNAPSHOT_VERSION,
    Confidence,
    EstimateReason,
    RoutingEvidence,
    build_pricing_decision,
    is_openai_model,
    load_snapshot,
    normalize_billing_model_id,
    normalize_usage,
    quantize_ledger,
    resolve_curated_non_openai,
    verify_pricing_decision,
)

# Not in the package's __all__ (they are raised, not called), so imported from the
# module directly rather than re-exported for convenience — a name that exists in
# only one place cannot drift from the one that raises it.
from pricing_policy.policy import InvalidPricingDecisionError, MissingUsageError, staleness_reasons

logger = logging.getLogger(__name__)


class SettlementResult:
    """The settled amount plus how it was arrived at.

    Carries the decision so the caller can record provenance alongside the
    number; a bare Decimal would lose the reason a cost is estimated, which is
    the difference between "this is what it cost" and "this is our best guess".
    """

    __slots__ = ("cost", "decision", "reused", "reasons", "total_tokens")

    def __init__(self, *, cost: Decimal, decision: dict[str, Any] | None, reused: bool, reasons: tuple[str, ...], total_tokens: int):
        self.cost = cost
        self.decision = decision
        self.reused = reused
        self.reasons = reasons
        self.total_tokens = total_tokens

    @property
    def estimated(self) -> bool:
        return bool(self.reasons)


def _legacy_evidence(parsed: dict[str, Any]) -> RoutingEvidence:
    """Routing evidence for an event that carries none.

    Everything measurable is absent by definition here, so nothing is asserted:
    no served tier (so the tier resolves conservatively rather than as standard),
    no confirmed geography or region. Only the model id is real, and even that is
    normalized because a legacy log records the `global.`/`us.` prefixed runtime
    id rather than the billing id.
    """
    original = parsed["model"]
    return RoutingEvidence(
        original_model_id=original,
        billing_model_id=normalize_billing_model_id(original),
    )


def _settle_curated(
    usage,
    *,
    evidence: RoutingEvidence,
    snapshot,
    reasons: tuple[str, ...],
    legacy_rates: dict[str, dict[str, Any]] | None = None,
) -> SettlementResult:
    """Price a curated non-OpenAI model from its flat rates (#1486/#4592 policy).

    Deliberately NOT expressed as a synthetic `RateRow` fed through
    `select_rate_row`: these models publish one flat rate with no geography,
    service tier or context tier, and manufacturing rows with invented dimensions
    to reuse the OpenAI selector would make an unpublished dimension look
    published.

    Cache accounting follows the curated policy exactly (design §7): an explicit
    curated cache rate is used as published, and only where the curated entry
    publishes none is the established multiplier applied. That preserves the
    four-key Claude entries rather than collapsing them to an inferred rate.
    """
    rates, known = resolve_curated_non_openai(evidence.original_model_id, snapshot=snapshot, db_rates=legacy_rates)
    all_reasons = set(reasons)
    if not known:
        all_reasons.add(EstimateReason.UNKNOWN_MODEL)

    input_rate = rates["input"]
    output_rate = rates["output"]
    cache_read_rate = rates.get("cache_read_input")
    cache_write_rate = rates.get("cache_creation_input")

    # Only where the curated entry is silent. An explicit curated rate wins.
    if cache_read_rate is None:
        cache_read_rate = input_rate * Decimal("0.1")
    if cache_write_rate is None:
        cache_write_rate = input_rate * Decimal("1.25")

    # `uncached_input_tokens`, not `total_input_tokens`: the decomposition already
    # separates cache reads and writes out of the inclusive Responses total, and
    # charging the total here would bill every cached token twice.
    thousand = Decimal("1000")
    exact = (
        (Decimal(usage.uncached_input_tokens) / thousand) * input_rate
        + (Decimal(usage.output_tokens) / thousand) * output_rate
        + (Decimal(usage.cache_read_input_tokens) / thousand) * cache_read_rate
        + (Decimal(usage.cache_creation_input_tokens) / thousand) * cache_write_rate
    )
    ledger = quantize_ledger(exact)

    return SettlementResult(
        cost=ledger,
        # No PricingDecision: a decision names a V2 variant key and generation,
        # and this cost has neither. Recording a fabricated one would claim
        # provenance that does not exist.
        decision=None,
        reused=False,
        reasons=tuple(sorted(all_reasons)),
        total_tokens=usage.total_input_tokens + usage.output_tokens,
    )


def settle_chat_log(
    parsed: dict[str, Any],
    *,
    chat_log: dict[str, Any],
    rows: tuple,
    snapshot,
    generation_id: int | None,
    pointer_revision: int | None,
    source_reasons: tuple[str, ...] = (),
    legacy_rates: dict[str, dict[str, Any]] | None = None,
) -> SettlementResult:
    """Return the ledger cost for one parsed chat log.

    Args:
        parsed: `parse_chat_log` output.
        chat_log: The raw log, for the `pricing_decision` the gateway may have put
            there. Read from the raw object rather than `parsed` so this works
            whether or not the parser has been taught about the field yet.
        rows: Rate rows from the active generation (or the bundle at bootstrap).
        snapshot: The snapshot supplying policy — curated non-OpenAI rates and the
            short/long context threshold.
        generation_id, pointer_revision: Provenance of `rows`.
        source_reasons: Reasons already implied by how `rows` were obtained, e.g.
            bootstrap fallback or a stale cache. Merged into the decision so a
            cost priced off degraded rates says so.

    Raises:
        InvalidPricingDecisionError: a present decision failed validation.
        MissingUsageError: no usable token counts, and no decision to fall back on.
    """
    request_id = parsed.get("request_id") or ""
    org_id = parsed["org_id"]

    decision_payload = chat_log.get("pricing_decision")
    if decision_payload is not None:
        # Raises on anything malformed. Deliberately not caught here: see the
        # module docstring on why an invalid decision must not become a re-price.
        cost = verify_pricing_decision(decision_payload, request_id=request_id, org_id=org_id)
        reasons = tuple(decision_payload.get("estimate_reasons") or ())
        logger.info(
            "Reusing gateway pricing decision: request=%s generation=%s variant=%s cost=%s",
            request_id,
            decision_payload.get("generation_id"),
            decision_payload.get("variant_key"),
            cost,
        )
        if normalize_billing_model_id(parsed["model"]) != decision_payload["routing"]["billing_model_id"]:
            raise InvalidPricingDecisionError("decision model differs from chat log")
        measured = decision_payload["usage"]
        return SettlementResult(
            cost=cost,
            decision=decision_payload,
            reused=True,
            reasons=reasons,
            total_tokens=measured["total_input_tokens"] + measured["output_tokens"],
        )

    # ---- No decision: price it here, and say that it is an estimate. --------
    response = chat_log.get("response") or {}
    usage = response.get("usage") or {}

    # Responses input includes cached tokens; Anthropic/Converse input excludes
    # them. Older envelopes without a format use the model family as evidence.
    api_format = chat_log.get("api_format")
    if api_format not in ("openai", "anthropic", "bedrock"):
        api_format = "openai" if is_openai_model(parsed["model"]) else "anthropic"
    normalized = normalize_usage(usage, api_format=api_format)

    evidence = _legacy_evidence(parsed)
    reasons = (*source_reasons, EstimateReason.LEGACY_EVENT)

    # Legacy OpenAI has no durable source binding. Neither the current database
    # generation nor a newer bundled selector may change its amount on retry.
    # Pin rates AND policy/provenance; passing only this snapshot's policy while
    # leaving rows bound to the current generation would still reprice the event.
    if is_openai_model(evidence.billing_model_id):
        snapshot = compatibility_snapshot()
        rows = snapshot.rates
        generation_id = None
        pointer_revision = None
        reasons = (EstimateReason.LEGACY_EVENT, EstimateReason.BOOTSTRAP_FALLBACK)

    # Non-OpenAI models keep their curated flat rates (#1486/#4592). They have no
    # rows in the V2 generation at all — V2 carries the 12 OpenAI models — so
    # routing them through the variant selector would price them off whichever
    # OpenAI row that selector happened to consider closest. That is not a
    # hypothetical: it billed Claude 3.5 Sonnet at 0.055 instead of 0.0105, a 5.2x
    # overcharge, until this branch existed.
    if not is_openai_model(evidence.billing_model_id) or not any(row.model_id == evidence.billing_model_id for row in rows):
        return _settle_curated(
            normalized,
            evidence=evidence,
            snapshot=snapshot,
            reasons=reasons,
            legacy_rates=legacy_rates if not is_openai_model(evidence.billing_model_id) else None,
        )

    decision = build_pricing_decision(
        request_id=request_id,
        org_id=org_id,
        usage=normalized,
        evidence=evidence,
        rows=rows,
        snapshot=snapshot,
        generation_id=generation_id,
        pointer_revision=pointer_revision,
        source_kind="database" if generation_id is not None else "bundled_snapshot",
        extra_reasons=reasons,
    )
    aged = set(decision.estimate_reasons) | set(staleness_reasons(row_verified_at=decision.verified_at, now_iso=datetime.now(UTC).isoformat()))
    decision = replace(decision, estimate_reasons=tuple(sorted(aged)), confidence=Confidence.ESTIMATED if aged else Confidence.VERIFIED)
    return SettlementResult(
        total_tokens=normalized.total_input_tokens + normalized.output_tokens,
        cost=Decimal(decision.ledger_cost_usd),
        decision=decision.to_dict(),
        reused=False,
        reasons=decision.estimate_reasons,
    )


def compatibility_snapshot():
    """The snapshot a legacy event is priced against.

    Pinned to `COMPATIBILITY_SNAPSHOT_VERSION` rather than the current one so
    that re-settling the same legacy event later cannot change its amount as new
    snapshots ship.
    """
    return load_snapshot(COMPATIBILITY_SNAPSHOT_VERSION)


__all__ = [
    "InvalidPricingDecisionError",
    "MissingUsageError",
    "SettlementResult",
    "compatibility_snapshot",
    "quantize_ledger",
    "settle_chat_log",
]
