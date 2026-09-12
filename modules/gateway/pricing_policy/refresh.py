"""Complete immutable publication candidates; network and database agnostic."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from .aws_sources import RATE_FIELDS, SourceValidationError
from .policy import RateRow, VariantKey


@dataclass(frozen=True)
class Candidate:
    rows: tuple[RateRow, ...]
    required: frozenset[VariantKey]
    fresh_keys: frozenset[VariantKey]
    retained_keys: frozenset[VariantKey]
    content_sha256: str


def canonical_content_hash(rows: tuple[RateRow, ...]) -> str:
    """Hash rates and provenance independent of DB numeric padding/timezone syntax."""
    payload = []
    for row in sorted(rows, key=lambda item: item.variant_key):
        value = dict(row.__dict__)
        value.pop("generation_id", None)
        # A null additive field did not exist in policy-one's canonical payload.
        # Omit it so old validated generations retain their exact stored hash.
        if value.get("cache_write_1h_price_per_1k_tokens") is None:
            value.pop("cache_write_1h_price_per_1k_tokens", None)
        for field in set(RATE_FIELDS) | {"cache_write_1h_price_per_1k_tokens"}:
            if field not in value:
                continue
            number = value[field]
            value[field] = None if number is None else format(number.normalize(), "f")
        for field in ("verified_at", "source_effective_at"):
            if value[field] is not None:
                value[field] = datetime.fromisoformat(value[field].replace("Z", "+00:00")).astimezone(UTC).isoformat()
        payload.append(value)
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def assemble_candidate(active: tuple[RateRow, ...], fresh: tuple[RateRow, ...], bundled_required: frozenset[VariantKey]) -> Candidate:
    """Never fabricate fallback rows or advance retained verification timestamps."""
    previous = {row.variant_key: row for row in active}
    if len(previous) != len(active):
        raise SourceValidationError("active generation has duplicate variants")
    if not fresh:
        raise SourceValidationError("zero fresh usable rates; refusing publication")
    validated: dict = {}
    for row in fresh:
        row = RateRow.from_mapping(row.__dict__)
        if row.source not in ("model_card", "bulk_catalog", "pricing_page"):
            raise SourceValidationError("refresh cannot publish bundled fallback values")
        existing = validated.get(row.variant_key)
        if existing and existing != row:
            raise SourceValidationError(f"conflicting fresh variants: {row.variant_key}")
        baseline = previous.get(row.variant_key)
        if baseline:
            # A publisher that lost the pointer race cannot replace a winner's
            # newer verification with content fetched before that winner.
            prior_time = datetime.fromisoformat(baseline.verified_at.replace("Z", "+00:00"))
            fresh_time = datetime.fromisoformat(row.verified_at.replace("Z", "+00:00"))
            if prior_time > fresh_time:
                continue
            for field in set(RATE_FIELDS) | {"cache_write_1h_price_per_1k_tokens"}:
                old, new = getattr(baseline, field), getattr(row, field)
                if old is not None and new is not None and old > 0 and not old * Decimal("0.5") <= new <= old * 2:
                    raise SourceValidationError(
                        f"suspect rate change: {row.variant_key} {field} {old} -> {new}; source={row.source_url} sha256={row.source_content_sha256}"
                    )
        validated[row.variant_key] = row
    if not validated:
        raise SourceValidationError("zero fresh usable rates after rebasing against active generation")
    required = frozenset(previous) | bundled_required | frozenset(validated)
    rows = previous | validated
    missing = required - rows.keys()
    if missing:
        raise SourceValidationError(f"missing required variants: {sorted(missing)}")
    ordered = tuple(rows[key] for key in sorted(rows))
    return Candidate(ordered, required, frozenset(validated), required - validated.keys(), canonical_content_hash(ordered))
