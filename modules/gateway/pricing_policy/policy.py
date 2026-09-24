"""Decimal pricing math, usage normalization, variant selection and decisions.

Standard library only — see the package docstring for why.

The three things worth reading before changing anything here:

1. **Rates are decimal, end to end.** Every rate is parsed from a decimal string
   and never touches ``float``. ``NUMERIC(14,10)`` in V2 storage represents each
   published rate exactly (design §4.2); a float round-trip would reintroduce the
   very rounding error this release exists to remove. Only the final ledger value
   is quantized, once, to six decimal places.

2. **Two APIs count input differently.** Converse reports ``inputTokens`` as
   NON-cached input, so the raw total is ``input + cache_read + cache_write``.
   The Responses API reports ``input_tokens`` INCLUSIVE of cached tokens (AWS's
   documented example: input 2048, cached 1920, output 256, total 2304). Adding
   cached tokens to a Responses input total double-charges them. ``normalize_usage``
   keeps the two conventions separate and both produce the same billable
   decomposition.

3. **"Unpublished" is not zero.** A NULL cache rate means AWS publishes no price
   for it; the tokens are still charged, at the ordinary input rate, and the
   decision is marked estimated with a reason. Writing a confident zero there
   would silently under-bill. A published zero, if a source ever states one, is a
   different thing and passes validation as a real rate.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------

#: The snapshot new code bootstraps from and the migration seed is frozen against.
CURRENT_SNAPSHOT_VERSION = "2026-09-24.2"

#: The snapshot used to price a legacy OpenAI settlement event that carries no
#: durable pricing decision. Pinned SEPARATELY from CURRENT_SNAPSHOT_VERSION and
#: deliberately never advanced with it: re-reading such an event after a rate
#: publication, a code restart or a pointer rollback must reproduce the same
#: cost. When CURRENT advances, this stays put for as long as pre-decision events
#: are retained (design §4.3).
COMPATIBILITY_SNAPSHOT_VERSION = "2026-09-12.1"

POLICY_VERSION = 2
SUPPORTED_POLICY_VERSIONS = (1, 2)
DECISION_VERSION = 1
CLAUDE_DECISION_VERSION = 2

_SNAPSHOT_DIR = Path(__file__).resolve().parent / "snapshots"

# ---------------------------------------------------------------------------
# Dimension vocabularies. These MUST stay in step with the CHECK constraints in
# Alembic 044 — the migration enforces the same enums at the storage layer.
# ---------------------------------------------------------------------------


class Geography:
    IN_REGION = "in_region"
    GEO_CRIS = "geo_cris"
    GLOBAL_CRIS = "global_cris"
    GOVCLOUD = "govcloud"
    ALL = (IN_REGION, GEO_CRIS, GLOBAL_CRIS, GOVCLOUD)


class ServiceTier:
    STANDARD = "standard"
    PRIORITY = "priority"
    FLEX = "flex"
    BATCH = "batch"
    ALL = (STANDARD, PRIORITY, FLEX, BATCH)


class ContextTier:
    SHORT = "short"
    LONG = "long"
    FLAT = "flat"
    ALL = (SHORT, LONG, FLAT)


class CacheWritePolicy:
    FULL_RATE = "full_rate"
    NO_ADDITIONAL_FEE = "no_additional_fee"
    UNPUBLISHED = "unpublished"
    ALL = (FULL_RATE, NO_ADDITIONAL_FEE, UNPUBLISHED)


class RateSource:
    BULK_CATALOG = "bulk_catalog"
    MODEL_CARD = "model_card"
    BUNDLED_SNAPSHOT = "bundled_snapshot"
    PRICING_PAGE = "pricing_page"
    ALL = (BULK_CATALOG, MODEL_CARD, BUNDLED_SNAPSHOT, PRICING_PAGE)


class Confidence:
    VERIFIED = "verified"
    ESTIMATED = "estimated"


class EstimateReason:
    """Sorted onto every estimated decision, so the reason is never just a flag."""

    UNSUPPORTED_VARIANT = "unsupported_variant"
    UNKNOWN_MODEL = "unknown_model"
    UNCONFIRMED_SERVICE_TIER = "unconfirmed_service_tier"
    UNCONFIRMED_GEOGRAPHY = "unconfirmed_geography"
    UNCONFIRMED_REGION = "unconfirmed_region"
    UNSUPPORTED_REGION = "unsupported_region"
    UNSUPPORTED_CONTEXT = "unsupported_context"
    INVALID_USAGE_COUNTERS = "invalid_usage_counters"
    UNPUBLISHED_CACHE_READ_RATE = "unpublished_cache_read_rate"
    UNPUBLISHED_CACHE_WRITE_RATE = "unpublished_cache_write_rate"
    STALE_RATE_SOURCE = "stale_rate_source"
    CACHE_REFRESH_FAILING = "cache_refresh_failing"
    BOOTSTRAP_FALLBACK = "bootstrap_fallback"
    LEGACY_EVENT = "legacy_event"
    ABSENT_CACHE_COUNTERS = "absent_cache_counters"
    UNCONFIRMED_CACHE_WRITE_DURATION = "unconfirmed_cache_write_duration"
    UNPUBLISHED_CACHE_WRITE_1H_RATE = "unpublished_cache_write_1h_rate"


class UnsupportedVariantError(ValueError):
    """No published row exists for a model at all (not even a fallback)."""


# Beyond these ages a selected row/cache is no longer presented as verified
# (design §4.1).
STALE_ROW_AGE_HOURS = 48
CACHE_REFRESH_FAILURE_MINUTES = 30

_LEDGER_QUANTUM = Decimal("0.000001")
_THOUSAND = Decimal("1000")
_RATE_SCALE = 10  # NUMERIC(14,10)
_RATE_PRECISION = 14


# ---------------------------------------------------------------------------
# Model id normalization
# ---------------------------------------------------------------------------

_GEO_PREFIX_TO_GEOGRAPHY = {
    "us.": Geography.GEO_CRIS,
    "eu.": Geography.GEO_CRIS,
    "apac.": Geography.GEO_CRIS,
    "in.": Geography.GEO_CRIS,
    "global.": Geography.GLOBAL_CRIS,
    "us-gov.": Geography.GOVCLOUD,
    "au.": Geography.GEO_CRIS,
    "jp.": Geography.GEO_CRIS,
}

_RUNTIME_VARIANT_SUFFIX = re.compile(r"-\d+:\d+$")


def normalize_billing_model_id(model_id: str) -> str:
    """Strip inference-profile prefixes and runtime variant suffixes.

    ``us.openai.gpt-5.6-sol`` and ``openai.gpt-oss-120b-1:0`` both normalize onto
    the id the snapshot keys. Note what this deliberately does NOT do: it does
    not tell you the geography. Stripping ``us.`` destroys exactly the
    information rate selection needs, which is why geography is captured from the
    actual forwarded routing configuration instead of re-derived from a stripped
    name (design §4.4). Callers that need both must keep the original id.
    """
    if not model_id:
        return model_id
    normalized = model_id
    for prefix in _GEO_PREFIX_TO_GEOGRAPHY:
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return _RUNTIME_VARIANT_SUFFIX.sub("", normalized)


def geography_from_model_prefix(model_id: str) -> str | None:
    """Geography implied by an id's profile prefix, or None for a bare id.

    A bare id is genuinely ambiguous — it may be invoked in-region or via a
    profile — so this returns None rather than guessing ``in_region``.
    """
    for prefix, geography in _GEO_PREFIX_TO_GEOGRAPHY.items():
        if model_id.startswith(prefix):
            return geography
    return None


def is_openai_model(model_id: str) -> bool:
    """Whether OpenAI variant-dimensioned V2 pricing applies to this id."""
    return normalize_billing_model_id(model_id).startswith("openai.")


def is_anthropic_model(model_id: str) -> bool:
    return normalize_billing_model_id(model_id).startswith("anthropic.claude")


def is_v2_priced_model(model_id: str) -> bool:
    """Families eligible for versioned pricing; not proof of a published variant."""
    return is_openai_model(model_id) or is_anthropic_model(model_id) or normalize_billing_model_id(model_id) == "moonshotai.kimi-k3"


def canonical_billing_model_id(model_id: str, snapshot=None) -> str:
    """Resolve reviewed aliases without losing the caller's separate route evidence."""
    active = snapshot or load_snapshot()
    normalized = normalize_billing_model_id(model_id)
    aliases = active.curated_non_openai.get("aliases", {})
    resolved = active.alias_map.get(model_id) or active.alias_map.get(normalized) or aliases.get(model_id) or aliases.get(normalized) or normalized
    return active.alias_map.get(resolved) or normalize_billing_model_id(resolved)


# ---------------------------------------------------------------------------
# Decimal helpers
# ---------------------------------------------------------------------------


def parse_rate(value: Any, *, field_name: str = "rate") -> Decimal:
    """Parse a rate from a decimal string (or int/Decimal), never a float.

    Rejects float input outright. A float rate is how a published 0.0171875
    becomes 0.017188 — the 0.003% error this release removes — so the type is
    refused at the boundary rather than silently coerced.
    """
    if isinstance(value, float):
        raise TypeError(f"{field_name} must be a decimal string, not float: {value!r}")
    if isinstance(value, Decimal):
        parsed = value
    else:
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{field_name} is not a valid decimal: {value!r}") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite: {value!r}")
    if parsed < 0:
        raise ValueError(f"{field_name} must not be negative: {value!r}")
    exponent = parsed.as_tuple().exponent
    if isinstance(exponent, int) and -exponent > _RATE_SCALE:
        raise ValueError(f"{field_name} exceeds NUMERIC(14,{_RATE_SCALE}) scale and would be silently rounded: {value!r}")
    if parsed >= Decimal(10) ** (_RATE_PRECISION - _RATE_SCALE) or len(parsed.as_tuple().digits) > _RATE_PRECISION:
        raise ValueError(f"{field_name} exceeds NUMERIC({_RATE_PRECISION},{_RATE_SCALE}) precision: {value!r}")
    return parsed


def rate_to_string(value: Decimal) -> str:
    """Canonical decimal string for a rate (no exponent form, no float)."""
    return format(value, "f")


def quantize_ledger(exact: Decimal) -> Decimal:
    """Quantize an exact cost to the ledger's six decimal places, half-up.

    This is the ONLY rounding step. Intermediate per-component costs stay exact;
    quantizing each component separately would accumulate error.
    """
    return exact.quantize(_LEDGER_QUANTUM, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------------------
# Usage normalization
# ---------------------------------------------------------------------------


def _measured_int(value: Any) -> int | None:
    """A non-negative integer counter, or None when absent/invalid.

    ``None`` means "not reported"; it is NOT a measured zero, and the two are
    kept distinct all the way into the recorded evidence. Booleans are rejected
    because ``isinstance(True, int)`` would otherwise make ``True`` one token.
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        try:
            parsed = Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        if not parsed.is_finite() or parsed != parsed.to_integral_value() or parsed < 0:
            return None
        return int(parsed)
    if isinstance(value, float):
        # Non-integral float counters are invalid, not roundable.
        if not math.isfinite(value) or value < 0 or value != int(value):
            return None
        return int(value)
    if isinstance(value, Decimal):
        if not value.is_finite() or value < 0 or value != value.to_integral_value():
            return None
        return int(value)
    return None


def _raw_counter_evidence(value: Any) -> Any:
    """Retain invalid evidence while keeping the durable decision valid JSON."""
    if isinstance(value, Decimal) or (isinstance(value, float) and not math.isfinite(value)):
        return str(value)
    if isinstance(value, dict):
        return {key: _raw_counter_evidence(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_raw_counter_evidence(item) for item in value]
    return value


@dataclass(frozen=True)
class NormalizedUsage:
    """Billable token decomposition plus the raw evidence it came from.

    Invariant on every valid instance: ``uncached + cache_read + cache_creation
    == total_input``, and no component exceeds ``total_input``. The decomposition
    is bounded even for self-contradictory upstream counters, so a bad counter
    can never charge more input than the request actually reported.
    """

    api_format: str
    input_semantics: str  # "inclusive_of_cache" (Responses) | "additive" (Converse)
    total_input_tokens: int
    uncached_input_tokens: int
    cache_read_input_tokens: int
    cache_creation_input_tokens: int
    output_tokens: int
    raw_input_tokens: int | None
    raw_output_tokens: int | None
    raw_cache_read_input_tokens: Any
    raw_cache_creation_input_tokens: Any
    valid: bool
    estimate_reasons: tuple[str, ...] = ()
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0
    cache_creation_unconfirmed_input_tokens: int = 0
    raw_cache_creation: Any = None
    confirmed_cache_write_ttl: str | None = None

    def to_dict(self, *, include_write_durations: bool = False) -> dict[str, Any]:
        payload = {
            "api_format": self.api_format,
            "input_semantics": self.input_semantics,
            "total_input_tokens": self.total_input_tokens,
            "uncached_input_tokens": self.uncached_input_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "output_tokens": self.output_tokens,
            "raw": {
                "input_tokens": self.raw_input_tokens,
                "output_tokens": self.raw_output_tokens,
                "cache_read_input_tokens": self.raw_cache_read_input_tokens,
                "cache_creation_input_tokens": self.raw_cache_creation_input_tokens,
            },
            "valid": self.valid,
        }
        if include_write_durations:
            for field in ("cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens", "cache_creation_unconfirmed_input_tokens"):
                payload[field] = getattr(self, field)
            payload["raw"]["cache_creation"] = self.raw_cache_creation
            payload["raw"]["confirmed_cache_write_ttl"] = self.confirmed_cache_write_ttl
        return payload


class MissingUsageError(ValueError):
    """Input or output token count is absent or invalid.

    Deliberately raised rather than defaulted to zero: the existing missing-usage
    path emits an anomaly and writes no settled row, and #4968's behavior of not
    fabricating a settled zero must survive this release (design §4.3).
    """


def normalize_usage(usage: dict[str, Any] | None, *, api_format: str, cache_write_ttl: str | None = None) -> NormalizedUsage:
    """Normalize an upstream usage block into a bounded billable decomposition.

    Args:
        usage: The upstream usage object. Responses uses ``input_tokens`` /
            ``output_tokens`` / ``cache_read_input_tokens`` /
            ``cache_creation_input_tokens``; Converse's additive ``inputTokens``
            spelling is accepted too.
        api_format: ``"openai"`` (Responses, inclusive input) or
            ``"bedrock"``/``"anthropic"`` (Converse, additive input).

    Raises:
        MissingUsageError: input or output is absent/invalid. Not a zero.
    """
    if not usage:
        raise MissingUsageError("no usage reported")

    inclusive = api_format == "openai"
    semantics = "inclusive_of_cache" if inclusive else "additive"

    raw_input = _measured_int(usage.get("input_tokens", usage.get("inputTokens")))
    raw_output = _measured_int(usage.get("output_tokens", usage.get("outputTokens")))
    details = usage.get("input_tokens_details") if inclusive else None
    details = details if isinstance(details, dict) else {}
    read_present = "cache_read_input_tokens" in usage or "cacheReadInputTokens" in usage or "cached_tokens" in details
    write_present = "cache_creation_input_tokens" in usage or "cacheWriteInputTokens" in usage or "cache_write_tokens" in details
    read_evidence = usage.get("cache_read_input_tokens", usage.get("cacheReadInputTokens", details.get("cached_tokens")))
    write_evidence = usage.get("cache_creation_input_tokens", usage.get("cacheWriteInputTokens", details.get("cache_write_tokens")))
    raw_read = _measured_int(read_evidence)
    raw_write = _measured_int(write_evidence)
    creation = usage.get("cache_creation")
    creation = creation if isinstance(creation, dict) else None
    duration_fields = ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens")
    duration_values = [_measured_int(creation.get(key)) if creation and key in creation else None for key in duration_fields]
    if (
        not write_present
        and creation
        and all(key in creation and value is not None for key, value in zip(duration_fields, duration_values, strict=True))
    ):
        raw_write = sum(duration_values)
        write_evidence = raw_write
        write_present = True
    if cache_write_ttl not in (None, "5m", "1h"):
        raise ValueError("confirmed cache write TTL must be 5m or 1h")

    if raw_input is None or raw_output is None:
        raise MissingUsageError(
            f"input/output token counts missing or invalid: input={usage.get('input_tokens')!r} output={usage.get('output_tokens')!r}"
        )

    reasons: set[str] = set()

    # Absent cache counters: assume no reported cache activity, but say so —
    # we cannot establish that none occurred. An API's explicitly measured zero
    # is certain and must NOT produce this reason.
    if not read_present or not write_present:
        reasons.add(EstimateReason.ABSENT_CACHE_COUNTERS)

    # A counter that was reported but did not parse is invalid evidence, which is
    # different from an absent one.
    invalid = (read_present and raw_read is None) or (write_present and raw_write is None)

    read = raw_read or 0
    write = raw_write or 0

    if inclusive:
        # Responses: reported input already contains cached tokens.
        total = raw_input
    else:
        # Converse: reported input excludes cache, so the raw total is additive.
        # Malformed cache counters contribute zero to the total rather than
        # poisoning it, and are retained as raw evidence.
        total = raw_input + read + write

    # Bounded, conservative decomposition. Writes take precedence over
    # discounted reads, so an overlap resolves toward the more expensive class
    # and C + W can never exceed T.
    bounded_write = min(write, total)
    bounded_read = min(read, total - bounded_write)
    uncached = total - bounded_write - bounded_read

    if invalid or bounded_write != write or bounded_read != read:
        reasons.add(EstimateReason.INVALID_USAGE_COUNTERS)

    valid = not invalid and bounded_write == write and bounded_read == read
    five, hour = duration_values
    bad_duration = bool(creation) and any(key in creation and value is None for key, value in zip(duration_fields, duration_values, strict=True))
    five, hour = five or 0, hour or 0
    if five + hour > bounded_write:
        bad_duration = True
    # Honor measured one-hour writes first if inconsistent counters exceed the
    # aggregate, preserving a bounded total and the invalid-evidence marker.
    hour = min(hour, bounded_write)
    five = min(five, bounded_write - hour)
    unconfirmed = bounded_write - five - hour
    if not creation and cache_write_ttl is not None:
        five, hour = (bounded_write, 0) if cache_write_ttl == "5m" else (0, bounded_write)
        unconfirmed = 0
    if bad_duration:
        reasons.add(EstimateReason.INVALID_USAGE_COUNTERS)
        valid = False

    return NormalizedUsage(
        api_format=api_format,
        input_semantics=semantics,
        total_input_tokens=total,
        uncached_input_tokens=uncached,
        cache_read_input_tokens=bounded_read,
        cache_creation_input_tokens=bounded_write,
        output_tokens=raw_output,
        raw_input_tokens=raw_input,
        raw_output_tokens=raw_output,
        raw_cache_read_input_tokens=_raw_counter_evidence(read_evidence),
        raw_cache_creation_input_tokens=_raw_counter_evidence(write_evidence),
        valid=valid,
        estimate_reasons=tuple(sorted(reasons)),
        cache_creation_5m_input_tokens=five,
        cache_creation_1h_input_tokens=hour,
        cache_creation_unconfirmed_input_tokens=unconfirmed,
        raw_cache_creation=_raw_counter_evidence(creation),
        confirmed_cache_write_ttl=cache_write_ttl,
    )


# ---------------------------------------------------------------------------
# Rate rows and snapshots
# ---------------------------------------------------------------------------

VariantKey = tuple[str, str, str, str, str]  # model, geography, tier, context, region


@dataclass(frozen=True)
class RateRow:
    """One published rate for one exact variant.

    ``cache_write_price_per_1k_tokens`` is ALWAYS a full price when present, per
    design §4.3: for ``no_additional_fee`` it equals the input rate (newly
    written tokens remain ordinary paid input; only the uplift is zero), and for
    ``full_rate`` it is the published write rate charged instead of input on
    those tokens. ``unpublished`` stores NULL — never a zero.
    """

    model_id: str
    geography: str
    service_tier: str
    context_tier: str
    region: str
    input_price_per_1k_tokens: Decimal
    output_price_per_1k_tokens: Decimal
    cache_read_price_per_1k_tokens: Decimal | None
    cache_write_price_per_1k_tokens: Decimal | None
    cache_write_policy: str
    source: str
    source_url: str
    source_content_sha256: str
    verified_at: str
    max_input_tokens: int | None = None
    source_effective_at: str | None = None
    snapshot_version: str | None = None
    generation_id: int | None = None
    cache_write_1h_price_per_1k_tokens: Decimal | None = None

    @property
    def variant_key(self) -> VariantKey:
        return (self.model_id, self.geography, self.service_tier, self.context_tier, self.region)

    @classmethod
    def from_mapping(cls, row: dict[str, Any]) -> RateRow:
        """Build from a snapshot entry or a database row, validating as we go."""
        policy = row["cache_write_policy"]
        if policy not in CacheWritePolicy.ALL:
            raise ValueError(f"unknown cache_write_policy: {policy!r}")
        for name, allowed in (
            ("geography", Geography.ALL),
            ("service_tier", ServiceTier.ALL),
            ("context_tier", ContextTier.ALL),
            ("source", RateSource.ALL),
        ):
            if row[name] not in allowed:
                raise ValueError(f"unknown {name}: {row[name]!r}")

        input_rate = parse_rate(row["input_price_per_1k_tokens"], field_name="input_price_per_1k_tokens")
        output_rate = parse_rate(row["output_price_per_1k_tokens"], field_name="output_price_per_1k_tokens")
        if input_rate <= 0 or output_rate <= 0:
            raise ValueError(f"input/output rates must be positive: {row['model_id']} {input_rate}/{output_rate}")

        raw_read = row.get("cache_read_price_per_1k_tokens")
        raw_write = row.get("cache_write_price_per_1k_tokens")
        read_rate = None if raw_read is None else parse_rate(raw_read, field_name="cache_read_price_per_1k_tokens")
        write_rate = None if raw_write is None else parse_rate(raw_write, field_name="cache_write_price_per_1k_tokens")
        raw_hour = row.get("cache_write_1h_price_per_1k_tokens")
        hour_rate = None if raw_hour is None else parse_rate(raw_hour, field_name="cache_write_1h_price_per_1k_tokens")

        # Policy/rate agreement. These mirror the 044 CHECK constraints, so a row
        # that would be rejected by the database is also rejected in memory —
        # bundled snapshots and refresh candidates get the same check.
        if policy == CacheWritePolicy.UNPUBLISHED:
            if write_rate is not None:
                raise ValueError(f"unpublished cache write must store NULL, got {write_rate}")
        elif write_rate is None:
            raise ValueError(f"cache_write_policy={policy} requires a full write price")
        elif policy == CacheWritePolicy.NO_ADDITIONAL_FEE and write_rate != input_rate:
            raise ValueError(f"no_additional_fee write price must equal the input rate ({input_rate}), got {write_rate}")

        max_input = row.get("max_input_tokens")
        if max_input is not None and (not isinstance(max_input, int) or isinstance(max_input, bool) or max_input <= 0):
            raise ValueError(f"max_input_tokens must be a positive integer when present: {max_input!r}")

        return cls(
            model_id=row["model_id"],
            geography=row["geography"],
            service_tier=row["service_tier"],
            context_tier=row["context_tier"],
            region=row["region"],
            input_price_per_1k_tokens=input_rate,
            output_price_per_1k_tokens=output_rate,
            cache_read_price_per_1k_tokens=read_rate,
            cache_write_price_per_1k_tokens=write_rate,
            cache_write_policy=policy,
            source=row["source"],
            source_url=row["source_url"],
            source_content_sha256=row["source_content_sha256"],
            verified_at=_as_iso(row["verified_at"]),
            max_input_tokens=max_input,
            source_effective_at=_as_iso(row.get("source_effective_at")),
            snapshot_version=row.get("snapshot_version"),
            generation_id=row.get("generation_id"),
            cache_write_1h_price_per_1k_tokens=hour_rate,
        )


def _as_iso(value: Any) -> str | None:
    """Render a timestamp as an ISO 8601 string without importing a DB driver."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    isoformat = getattr(value, "isoformat", None)
    if isoformat is not None:
        return isoformat()
    return str(value)


@dataclass(frozen=True)
class Snapshot:
    """An immutable published rate bundle plus its curated non-OpenAI policies."""

    snapshot_version: str
    policy_version: int
    bundle_revision: int
    supported_predecessor_versions: tuple[str, ...]
    short_context_max_input_tokens: int
    models: dict[str, dict[str, Any]]
    rates: tuple[RateRow, ...]
    required_variants: frozenset[VariantKey]
    curated_non_openai: dict[str, Any]
    provenance: dict[str, Any]

    @property
    def alias_map(self) -> dict[str, str]:
        """Alias id → canonical snapshot model id, for OpenAI ids."""
        mapping: dict[str, str] = {}
        for model_id, meta in self.models.items():
            for alias in meta.get("aliases", ()):
                mapping[alias] = model_id
        return mapping

    def rows_for_model(self, model_id: str) -> tuple[RateRow, ...]:
        return tuple(r for r in self.rates if r.model_id == model_id)


# Retained name for the packaged bundle, so consumers can say what they mean.
PricingSnapshot = Snapshot


@lru_cache(maxsize=8)
def load_snapshot(version: str = CURRENT_SNAPSHOT_VERSION) -> Snapshot:
    """Load and validate a published snapshot by version.

    Cached: the file is immutable, so parsing it once per process is correct.
    """
    path = _SNAPSHOT_DIR / f"{version}.json"
    if not path.is_file():
        raise FileNotFoundError(f"no pricing snapshot for version {version!r} at {path}")
    with path.open(encoding="utf-8") as handle:
        raw = json.load(handle)
    return snapshot_from_mapping(raw)


def snapshot_from_mapping(raw: dict[str, Any]) -> Snapshot:
    """Validate a snapshot mapping into a Snapshot (shared by loader and tests)."""
    rates = tuple(RateRow.from_mapping(row) for row in raw["rates"])
    required = frozenset(tuple(key) for key in raw["required_variants"])

    present = {row.variant_key for row in rates}
    missing = required - present
    if missing:
        raise ValueError(f"snapshot {raw['snapshot_version']} is missing required variants: {sorted(missing)}")
    if len(present) != len(rates):
        raise ValueError(f"snapshot {raw['snapshot_version']} contains duplicate variant keys")

    return Snapshot(
        snapshot_version=raw["snapshot_version"],
        policy_version=raw["policy_version"],
        bundle_revision=raw["bundle_revision"],
        supported_predecessor_versions=tuple(raw.get("supported_predecessor_versions", ())),
        short_context_max_input_tokens=raw["short_context_max_input_tokens"],
        models=raw["models"],
        rates=rates,
        required_variants=required,
        curated_non_openai=raw.get("curated_non_openai", {"rates": {}, "aliases": {}}),
        provenance=raw.get("provenance", {}),
    )


# ---------------------------------------------------------------------------
# Non-OpenAI resolution (design §7)
# ---------------------------------------------------------------------------


def resolve_curated_non_openai(
    model_id: str,
    *,
    snapshot: Snapshot,
    db_rates: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Decimal], bool]:
    """Resolve a non-OpenAI model's rates, preserving existing curated policy.

    Merges legacy per-model input/output rows with the curated cache policies,
    PER MODEL rather than by whole-dict truthiness. The pre-#4976 tracker picked
    its DB dict whenever the dict was non-empty, which bypassed the curated
    four-key Claude entries entirely — a model present in the DB with only three
    columns lost its explicit cache prices to an inferred universal multiplier.
    Here a DB row overrides only input/output, and explicit curated cache rates
    survive it.

    Returns:
        ``(rates, known)`` where ``rates`` has ``input``/``output`` and, when the
        curated entry publishes them, ``cache_read_input``/``cache_creation_input``.
        ``known`` is False when the id fell through to the generic default, so the
        caller can emit ``UnknownModelPricing`` exactly as it does today.
    """
    curated: dict[str, dict[str, str]] = snapshot.curated_non_openai.get("rates", {})
    aliases: dict[str, str] = snapshot.curated_non_openai.get("aliases", {})

    # This resolver settles old no-decision events. Freeze the pre-extension
    # prefix list; newly reviewed AU/JP aliases apply only to versioned pricing.
    legacy_normalized = model_id
    for prefix in ("us.", "eu.", "apac.", "in.", "global.", "us-gov."):
        if legacy_normalized.startswith(prefix):
            legacy_normalized = legacy_normalized[len(prefix) :]
            break
    legacy_normalized = _RUNTIME_VARIANT_SUFFIX.sub("", legacy_normalized)
    resolved_id = aliases.get(model_id) or legacy_normalized

    entry = curated.get(resolved_id)
    if entry is None:
        lowered = resolved_id.lower()
        for key, value in curated.items():
            if key.lower() == lowered:
                entry, resolved_id = value, key
                break

    if entry is None:
        # Issue #4592's suffix-variant retry: callers and the table disagree about
        # version suffixes (bare `-4-8` vs keyed `-4-8-v1`, and `:0`-suffixed
        # arrivals for bare keys). One mechanism for the whole class.
        for candidate in (
            f"{resolved_id}-v1",
            resolved_id.removesuffix(":0"),
            resolved_id.removesuffix("-v1:0"),
            resolved_id.removesuffix("-v1"),
        ):
            if candidate != resolved_id and candidate in curated:
                entry, resolved_id = curated[candidate], candidate
                break

    known = entry is not None
    if entry is None:
        entry = curated["default"]
        resolved_id = "default"

    rates = {name: parse_rate(value, field_name=name) for name, value in entry.items()}

    # A DB row overrides base rates only. Curated cache rates are policy, not a
    # cache of the DB, so a three-column row must not erase them.
    if db_rates:
        db_entry = db_rates.get(resolved_id) or db_rates.get(model_id)
        if db_entry:
            for name in ("input", "output"):
                if db_entry.get(name) is not None:
                    rates[name] = parse_rate(db_entry[name], field_name=name)

    return rates, known


# ---------------------------------------------------------------------------
# Variant selection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RoutingEvidence:
    """What the gateway actually observed about how a request was served.

    Every field is evidence, not inference. ``served_service_tier`` is an
    upstream-confirmed tier; ``requested_service_tier`` is what the client asked
    for and is never sufficient on its own. ``geography``/``region`` come from
    the resolved forwarding configuration, not from a stripped model name.
    """

    original_model_id: str
    billing_model_id: str
    forwarded_model_id: str | None = None
    endpoint_region: str | None = None
    execution_region: str | None = None
    endpoint_host: str | None = None
    geography: str | None = None
    requested_service_tier: str | None = None
    served_service_tier_raw: str | None = None

    @property
    def served_service_tier(self) -> str | None:
        """The confirmed served tier, or None when unconfirmed.

        ``default``/``auto`` are explicitly NOT equated with ``standard``: no
        captured AWS contract for this endpoint says they are, so treating them
        as standard would present a guess as a verified measurement (design §4.4).
        """
        raw = (self.served_service_tier_raw or "").strip().lower()
        return raw if raw in ServiceTier.ALL else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_model_id": self.original_model_id,
            "billing_model_id": self.billing_model_id,
            "forwarded_model_id": self.forwarded_model_id,
            "endpoint_region": self.endpoint_region,
            "execution_region": self.execution_region,
            "endpoint_host": self.endpoint_host,
            "geography": self.geography,
            "requested_service_tier": self.requested_service_tier,
            "served_service_tier_raw": self.served_service_tier_raw,
            "served_service_tier": self.served_service_tier,
        }


def select_context_tier(total_input_tokens: int, rows: tuple[RateRow, ...], *, short_threshold: int) -> tuple[str, bool]:
    """Choose a context tier from the RAW total input, before decomposition.

    Uses T (input + cache reads + writes), so a 400,000-token request that was
    320,000 cache reads is still long context — the model loaded the whole
    window regardless of what it charged for. Returns
    ``(context_tier, overflowed)``; ``overflowed`` means the total exceeds the
    longest published window, which uses the longest rate and marks estimated
    rather than silently certifying an unsupported context.
    """
    available = {row.context_tier for row in rows}
    if available == {ContextTier.FLAT} or not available:
        return ContextTier.FLAT, False

    if ContextTier.LONG not in available:
        # Short-only model (Cyber). Beyond its window there is no longer rate.
        only = ContextTier.SHORT if ContextTier.SHORT in available else next(iter(sorted(available)))
        limits = [r.max_input_tokens for r in rows if r.context_tier == only and r.max_input_tokens]
        return only, bool(limits) and total_input_tokens > max(limits)

    # The selected route supplies its own context boundary (Claude can differ
    # from OpenAI). With incomplete route evidence, the lowest published short
    # boundary avoids silently treating a potentially long request as short.
    short_limits = [r.max_input_tokens for r in rows if r.context_tier == ContextTier.SHORT and r.max_input_tokens]
    threshold = min(short_limits) if short_limits else short_threshold
    tier = ContextTier.SHORT if total_input_tokens <= threshold else ContextTier.LONG
    if tier == ContextTier.SHORT:
        return tier, False

    long_limits = [r.max_input_tokens for r in rows if r.context_tier == ContextTier.LONG and r.max_input_tokens]
    return tier, bool(long_limits) and total_input_tokens > max(long_limits)


def _row_total_cost(row: RateRow, usage: NormalizedUsage) -> Decimal:
    """This request's exact cost under one candidate row (for fallback ranking)."""
    cost, _ = price_from_rate_row(row, usage)
    return cost


def model_rate_candidates(rows: tuple[RateRow, ...], model_id: str, *, served_service_tier: str | None = None) -> tuple[RateRow, ...]:
    """Current selection candidates, excluding offline Claude batch ambiguity.

    A missing/unknown online serving tier does not make offline batch pricing
    eligible. Explicit batch evidence remains available to generic/offline
    callers. OpenAI selection and saved-decision replay retain their contracts.
    """
    exclude_batch = is_anthropic_model(model_id) and served_service_tier != ServiceTier.BATCH
    return tuple(row for row in rows if row.model_id == model_id and not (exclude_batch and row.service_tier == ServiceTier.BATCH))


def select_rate_row(
    *,
    rows: tuple[RateRow, ...],
    usage: NormalizedUsage,
    evidence: RoutingEvidence,
    short_threshold: int,
) -> tuple[RateRow, tuple[str, ...]]:
    """Pick the row to price with, plus the reasons that make it an estimate.

    Exact match on confirmed evidence is verified. Anything unconfirmed or
    unpublished uses the deterministic conservative fallback from design §4.4:
    among the candidate rows still consistent with what we DO know, take the one
    that costs the most for THIS request, breaking ties lexicographically on the
    canonical variant key. That bounds the estimate inside published rates
    instead of inventing one, and it never mixes one row's input rate with
    another's output rate.

    Raises:
        UnsupportedVariantError: no rows at all for this model.
    """
    # Enforce the "this model" in the contract above rather than trusting callers
    # to have pre-filtered. Every dimension below (context, geography, tier,
    # region) falls back to a wider candidate set when the narrower one is empty,
    # and model id is the one dimension where that widening is never acceptable:
    # given all 54 rows of a generation, an unfiltered selector picks the DEAREST
    # row across every model. That priced a gpt-5.5 request off a gpt-5.6-cyber
    # row at 0.09625 instead of 0.0385 — a 2.5x overcharge, from a call site that
    # passed the whole generation because the signature let it.
    rows = model_rate_candidates(rows, evidence.billing_model_id, served_service_tier=evidence.served_service_tier)

    if not rows:
        raise UnsupportedVariantError(f"no published rates for {evidence.billing_model_id}")

    reasons: set[str] = set()

    # Choose context within the observed route when that route is published.
    # A short-only GovCloud route must not borrow a commercial long-context row.
    route_rows = tuple(
        row
        for row in rows
        if all(
            value is None or getattr(row, name) == value
            for name, value in (
                ("geography", evidence.geography),
                ("region", evidence.endpoint_region),
                ("service_tier", evidence.served_service_tier),
            )
        )
    )
    context_rows = route_rows or rows
    context_tier, overflowed = select_context_tier(usage.total_input_tokens, context_rows, short_threshold=short_threshold)
    if overflowed:
        reasons.add(EstimateReason.UNSUPPORTED_CONTEXT)

    served_tier = evidence.served_service_tier
    if served_tier is None:
        reasons.add(EstimateReason.UNCONFIRMED_SERVICE_TIER)
    if evidence.geography is None:
        reasons.add(EstimateReason.UNCONFIRMED_GEOGRAPHY)
    if evidence.endpoint_region is None:
        reasons.add(EstimateReason.UNCONFIRMED_REGION)

    # Context is measured; then narrow by the joint routing evidence. A failed
    # narrowing is an unsupported conjunction, even if each dimension occurs in
    # some other row. Preserve the last compatible set for conservative fallback.
    candidates = tuple(r for r in context_rows if r.context_tier == context_tier) or context_rows
    exact = candidates
    for name, value in (
        ("geography", evidence.geography),
        ("region", evidence.endpoint_region),
        ("service_tier", served_tier),
    ):
        if value is None:
            continue
        subset = tuple(r for r in exact if getattr(r, name) == value)
        if subset:
            exact = subset
        else:
            reasons.add(EstimateReason.UNSUPPORTED_VARIANT)
            if name == "region":
                reasons.add(EstimateReason.UNSUPPORTED_REGION)

    # Conservative pick among whatever remains ambiguous. With a single candidate
    # this is that candidate; with several it is the dearest for this request.
    # Dearest first, then the lexicographically smallest variant key. Written as a
    # min() over a negated cost so the tie-break reads in its natural direction.
    chosen = min(exact, key=lambda r: (-_row_total_cost(r, usage), r.variant_key))

    if chosen.cache_read_price_per_1k_tokens is None and usage.cache_read_input_tokens > 0:
        reasons.add(EstimateReason.UNPUBLISHED_CACHE_READ_RATE)
    if chosen.cache_write_policy == CacheWritePolicy.UNPUBLISHED and usage.cache_creation_input_tokens > 0:
        reasons.add(EstimateReason.UNPUBLISHED_CACHE_WRITE_RATE)
    if is_anthropic_model(chosen.model_id):
        if usage.cache_creation_unconfirmed_input_tokens:
            reasons.add(EstimateReason.UNCONFIRMED_CACHE_WRITE_DURATION)
        if usage.cache_creation_1h_input_tokens and chosen.cache_write_1h_price_per_1k_tokens is None:
            reasons.add(EstimateReason.UNPUBLISHED_CACHE_WRITE_1H_RATE)

    reasons.update(usage.estimate_reasons)
    return chosen, tuple(sorted(reasons))


# ---------------------------------------------------------------------------
# Costing
# ---------------------------------------------------------------------------


def price_from_rate_row(row: RateRow, usage: NormalizedUsage) -> tuple[Decimal, dict[str, str]]:
    """Exact cost of one request under one rate row, plus the rates applied.

    ``exact = (U*input + C*read + W*write + O*output) / 1000``, all Decimal.
    Unpublished cache rates fall back to the ordinary input rate — the tokens
    were consumed and must be charged; the decision records the substitution as
    an estimate reason rather than pretending the price is zero.
    """
    input_rate = row.input_price_per_1k_tokens
    output_rate = row.output_price_per_1k_tokens
    read_rate = row.cache_read_price_per_1k_tokens
    write_rate = row.cache_write_price_per_1k_tokens

    effective_read = input_rate if read_rate is None else read_rate
    # For no_additional_fee the stored write price already equals the input rate,
    # so one expression covers all three policies.
    effective_write = input_rate if write_rate is None else write_rate
    hour_rate = row.cache_write_1h_price_per_1k_tokens
    effective_hour = max(input_rate, effective_write) if hour_rate is None else hour_rate
    effective_unknown = max(input_rate, effective_write, effective_hour)
    write_cost = Decimal(usage.cache_creation_input_tokens) * effective_write
    if is_anthropic_model(row.model_id):
        components = (usage.cache_creation_5m_input_tokens, usage.cache_creation_1h_input_tokens, usage.cache_creation_unconfirmed_input_tokens)
        if any(type(value) is not int or value < 0 for value in components) or sum(components) != usage.cache_creation_input_tokens:
            raise ValueError("cache creation duration counters do not decompose")
        write_cost = sum(Decimal(count) * rate for count, rate in zip(components, (effective_write, effective_hour, effective_unknown), strict=True))

    exact = (
        Decimal(usage.uncached_input_tokens) * input_rate
        + Decimal(usage.cache_read_input_tokens) * effective_read
        + write_cost
        + Decimal(usage.output_tokens) * output_rate
    ) / _THOUSAND

    applied = {
        "input_price_per_1k_tokens": rate_to_string(input_rate),
        "output_price_per_1k_tokens": rate_to_string(output_rate),
        "cache_read_price_per_1k_tokens": None if read_rate is None else rate_to_string(read_rate),
        "cache_write_price_per_1k_tokens": None if write_rate is None else rate_to_string(write_rate),
        "cache_write_policy": row.cache_write_policy,
        "effective_cache_read_price_per_1k_tokens": rate_to_string(effective_read),
        "effective_cache_write_price_per_1k_tokens": rate_to_string(effective_write),
    }
    if is_anthropic_model(row.model_id):
        applied.update(
            cache_write_1h_price_per_1k_tokens=None if hour_rate is None else rate_to_string(hour_rate),
            effective_cache_write_1h_price_per_1k_tokens=rate_to_string(effective_hour),
            effective_cache_write_unconfirmed_price_per_1k_tokens=rate_to_string(effective_unknown),
        )
    return exact, applied


# ---------------------------------------------------------------------------
# The durable pricing decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PricingDecision:
    """One server-produced pricing decision, carried in the settlement event.

    This is the sole pricing input to settlement for its request (design §4.3).
    The tracker validates and reuses it; it must NOT re-select rates from its own
    cache, because that cache may have swapped generations between inference and
    settlement. Since the rates are embedded, a retry after a publication, a
    process restart or a pointer rollback reproduces the same amount.

    ``content_sha256`` is a corruption diagnostic, NOT authentication. The trusted
    transport is the gateway-written S3 object and its IAM restrictions; a
    client-supplied field of this shape is never accepted.
    """

    decision_version: int
    policy_version: int
    request_id: str
    org_id: str
    generation_id: int | None
    pointer_revision: int | None
    snapshot_version: str | None
    source_kind: str
    variant_key: VariantKey
    rates: dict[str, Any]
    verified_at: str
    source: str
    source_url: str
    source_content_sha256: str
    usage: dict[str, Any]
    routing: dict[str, Any]
    confidence: str
    estimate_reasons: tuple[str, ...]
    exact_cost_usd: str
    ledger_cost_usd: str
    content_sha256: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "decision_version": self.decision_version,
            "policy_version": self.policy_version,
            "request_id": self.request_id,
            "org_id": self.org_id,
            "generation_id": self.generation_id,
            "pointer_revision": self.pointer_revision,
            "snapshot_version": self.snapshot_version,
            "source_kind": self.source_kind,
            "variant_key": list(self.variant_key),
            "rates": self.rates,
            "verified_at": self.verified_at,
            "source": self.source,
            "source_url": self.source_url,
            "source_content_sha256": self.source_content_sha256,
            "usage": self.usage,
            "routing": self.routing,
            "confidence": self.confidence,
            "estimate_reasons": list(self.estimate_reasons),
            "exact_cost_usd": self.exact_cost_usd,
            "ledger_cost_usd": self.ledger_cost_usd,
        }
        payload["content_sha256"] = _decision_content_hash(payload)
        return payload

    @property
    def ledger_cost(self) -> Decimal:
        return Decimal(self.ledger_cost_usd)


def _decision_content_hash(payload: dict[str, Any]) -> str:
    """Stable hash over canonical decision content, excluding the hash itself."""
    body = {k: v for k, v in payload.items() if k != "content_sha256"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_pricing_decision(
    *,
    request_id: str,
    org_id: str,
    usage: NormalizedUsage,
    evidence: RoutingEvidence,
    rows: tuple[RateRow, ...],
    snapshot: Snapshot,
    generation_id: int | None = None,
    pointer_revision: int | None = None,
    source_kind: str | None = None,
    extra_reasons: tuple[str, ...] = (),
) -> PricingDecision:
    """Compute the one decision for a completed request.

    Called ONCE at response completion (including final streaming metadata),
    against a single immutable cache generation, before both the S3 settlement
    emission and the usage-row write. Both receive this same object; a later
    cache swap cannot change it.
    """
    row, reasons = select_rate_row(
        rows=rows,
        usage=usage,
        evidence=evidence,
        short_threshold=snapshot.short_context_max_input_tokens,
    )
    all_reasons = set(reasons) | set(extra_reasons)
    context_max = snapshot.models.get(evidence.billing_model_id, {}).get("context_max_input_tokens")
    if is_anthropic_model(evidence.billing_model_id) and isinstance(context_max, int) and usage.total_input_tokens > context_max:
        all_reasons.add(EstimateReason.UNSUPPORTED_CONTEXT)
    if source_kind is None:
        source_kind = "database" if generation_id is not None or pointer_revision is not None else "bundled_snapshot"
    if source_kind == "database":
        if not all(type(value) is int and value > 0 for value in (generation_id, pointer_revision)):
            raise ValueError("database decisions require positive generation_id and pointer_revision")
        if row.generation_id is not None and row.generation_id != generation_id:
            raise ValueError("selected row does not belong to the decision generation")
    elif source_kind == "bundled_snapshot":
        if generation_id is not None or pointer_revision is not None:
            raise ValueError("bundled decisions must not claim a database generation")
        all_reasons.add(EstimateReason.BOOTSTRAP_FALLBACK)
    else:
        raise ValueError(f"unknown decision source_kind: {source_kind!r}")

    exact, applied = price_from_rate_row(row, usage)
    if is_anthropic_model(row.model_id):
        applied["context_max_input_tokens"] = context_max
    ledger = quantize_ledger(exact)

    confidence = Confidence.ESTIMATED if all_reasons else Confidence.VERIFIED

    decision = PricingDecision(
        decision_version=CLAUDE_DECISION_VERSION if is_anthropic_model(row.model_id) else DECISION_VERSION,
        policy_version=POLICY_VERSION if is_anthropic_model(row.model_id) else 1,
        request_id=request_id,
        org_id=org_id,
        generation_id=generation_id,
        pointer_revision=pointer_revision,
        snapshot_version=row.snapshot_version or snapshot.snapshot_version,
        source_kind=source_kind,
        variant_key=row.variant_key,
        rates=applied,
        verified_at=row.verified_at,
        source=row.source,
        source_url=row.source_url,
        source_content_sha256=row.source_content_sha256,
        usage=usage.to_dict(include_write_durations=is_anthropic_model(row.model_id)),
        routing=evidence.to_dict(),
        confidence=confidence,
        estimate_reasons=tuple(sorted(all_reasons)),
        exact_cost_usd=format(exact, "f"),
        ledger_cost_usd=format(ledger, "f"),
    )
    return decision


class InvalidPricingDecisionError(ValueError):
    """A present decision failed validation.

    Always an error for investigation — never silently downgraded to "treat as a
    legacy event", which would re-price the request from current rates and
    quietly defeat the whole durable-decision contract.
    """


def _verify_decision_binding(payload: dict[str, Any], *, input_rate: Decimal, read_rate: Decimal, write_rate: Decimal) -> None:
    """Validate version-one provenance and policy without fetching historical rows."""
    required = {
        "generation_id",
        "pointer_revision",
        "snapshot_version",
        "source_kind",
        "variant_key",
        "verified_at",
        "source",
        "source_url",
        "source_content_sha256",
        "routing",
        "confidence",
        "estimate_reasons",
        "content_sha256",
    }
    missing = required - payload.keys()
    if missing:
        raise InvalidPricingDecisionError(f"decision binding fields are missing: {sorted(missing)}")

    key = payload["variant_key"]
    if not isinstance(key, list) or len(key) != 5 or not all(isinstance(value, str) and value for value in key):
        raise InvalidPricingDecisionError("decision variant_key is malformed")
    supported_model = is_openai_model(key[0]) if payload["decision_version"] == 1 else is_v2_priced_model(key[0])
    if not supported_model or key[1] not in Geography.ALL or key[2] not in ServiceTier.ALL or key[3] not in ContextTier.ALL:
        raise InvalidPricingDecisionError("decision variant_key contains unsupported dimensions")

    reasons = payload["estimate_reasons"]
    if not isinstance(reasons, list) or not all(isinstance(reason, str) and reason for reason in reasons) or reasons != sorted(set(reasons)):
        raise InvalidPricingDecisionError("decision estimate_reasons must be sorted unique strings")
    if payload["confidence"] != (Confidence.ESTIMATED if reasons else Confidence.VERIFIED):
        raise InvalidPricingDecisionError("decision confidence disagrees with estimate_reasons")

    source_kind = payload["source_kind"]
    if source_kind == "database":
        if not all(type(payload[name]) is int and payload[name] > 0 for name in ("generation_id", "pointer_revision")):
            raise InvalidPricingDecisionError("database decision is missing its generation binding")
    elif source_kind == "bundled_snapshot":
        if payload["generation_id"] is not None or payload["pointer_revision"] is not None or EstimateReason.BOOTSTRAP_FALLBACK not in reasons:
            raise InvalidPricingDecisionError("bundled decision requires bootstrap_fallback and no database binding")
        if not isinstance(payload["snapshot_version"], str) or not payload["snapshot_version"]:
            raise InvalidPricingDecisionError("bundled decision requires snapshot_version")
    else:
        raise InvalidPricingDecisionError("decision source_kind is unsupported")

    if payload["snapshot_version"] is not None and not isinstance(payload["snapshot_version"], str):
        raise InvalidPricingDecisionError("decision snapshot_version is malformed")
    valid_source_url = isinstance(payload["source_url"], str) and (
        payload["source_url"].startswith("https://")
        or (
            payload["source"] == RateSource.BUNDLED_SNAPSHOT
            and payload["source_url"] == f"bundled://pricing_policy/snapshots/{payload['snapshot_version']}.json"
        )
    )
    if payload["source"] not in RateSource.ALL or not valid_source_url:
        raise InvalidPricingDecisionError("decision source provenance is malformed")
    if not isinstance(payload["source_content_sha256"], str) or re.fullmatch(r"[0-9a-f]{64}", payload["source_content_sha256"]) is None:
        raise InvalidPricingDecisionError("decision source_content_sha256 is malformed")
    try:
        verified = datetime.fromisoformat(payload["verified_at"])
        if verified.tzinfo is None:
            raise ValueError("verification timestamp requires a timezone")
    except (TypeError, ValueError) as exc:
        raise InvalidPricingDecisionError("decision verified_at is malformed") from exc

    routing = payload["routing"]
    route_fields = {
        "original_model_id",
        "billing_model_id",
        "forwarded_model_id",
        "endpoint_region",
        "execution_region",
        "geography",
        "requested_service_tier",
        "served_service_tier_raw",
        "served_service_tier",
    }
    if not isinstance(routing, dict) or not route_fields <= routing.keys():
        raise InvalidPricingDecisionError("decision routing is malformed")
    if any(value is not None and not isinstance(value, str) for value in routing.values()):
        raise InvalidPricingDecisionError("decision routing values must be strings or null")
    if not routing["original_model_id"] or routing["billing_model_id"] != key[0]:
        raise InvalidPricingDecisionError("decision routing model disagrees with variant_key")
    raw_tier = (routing["served_service_tier_raw"] or "").strip().lower()
    served = raw_tier if raw_tier in ServiceTier.ALL else None
    if routing["served_service_tier"] != served:
        raise InvalidPricingDecisionError("decision served tier disagrees with upstream evidence")
    if served is None and EstimateReason.UNCONFIRMED_SERVICE_TIER not in reasons:
        raise InvalidPricingDecisionError("unconfirmed served tier requires an estimate reason")
    if routing["geography"] is None and EstimateReason.UNCONFIRMED_GEOGRAPHY not in reasons:
        raise InvalidPricingDecisionError("unconfirmed geography requires an estimate reason")
    if routing["endpoint_region"] is None and EstimateReason.UNCONFIRMED_REGION not in reasons:
        raise InvalidPricingDecisionError("unconfirmed region requires an estimate reason")
    for actual, selected in ((routing["geography"], key[1]), (served, key[2]), (routing["endpoint_region"], key[4])):
        if actual is not None and actual != selected and EstimateReason.UNSUPPORTED_VARIANT not in reasons:
            raise InvalidPricingDecisionError("decision routing differs from variant without unsupported_variant reason")

    usage = payload["usage"]
    if type(usage.get("valid")) is not bool or not isinstance(usage.get("raw"), dict):
        raise InvalidPricingDecisionError("decision raw usage evidence is malformed")
    if not {"input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"} <= usage["raw"].keys():
        raise InvalidPricingDecisionError("decision raw usage counters are missing")
    if usage.get("api_format") not in ("openai", "bedrock", "anthropic"):
        raise InvalidPricingDecisionError("decision usage api_format is unsupported")
    semantics = "inclusive_of_cache" if usage["api_format"] == "openai" else "additive"
    if usage.get("input_semantics") != semantics:
        raise InvalidPricingDecisionError("decision input semantics disagree with api_format")
    if not usage["valid"] and EstimateReason.INVALID_USAGE_COUNTERS not in reasons:
        raise InvalidPricingDecisionError("invalid usage requires an estimate reason")

    rates = payload["rates"]
    try:
        # Reuse row validation for full write policy and published-rate agreement.
        row = RateRow.from_mapping(
            {
                **rates,
                "model_id": key[0],
                "geography": key[1],
                "service_tier": key[2],
                "context_tier": key[3],
                "region": key[4],
                "source": payload["source"],
                "source_url": payload["source_url"],
                "source_content_sha256": payload["source_content_sha256"],
                "verified_at": payload["verified_at"],
            }
        )
        if "cache_read_price_per_1k_tokens" not in rates or "cache_write_price_per_1k_tokens" not in rates:
            raise ValueError("published cache rates must be present, including null")
        expected_read = input_rate if row.cache_read_price_per_1k_tokens is None else row.cache_read_price_per_1k_tokens
        expected_write = input_rate if row.cache_write_price_per_1k_tokens is None else row.cache_write_price_per_1k_tokens
        if read_rate != expected_read or write_rate != expected_write:
            raise ValueError("effective cache rates disagree with published rates/policy")
        if payload["decision_version"] == 2:
            if "cache_write_1h_price_per_1k_tokens" not in rates:
                raise ValueError("published one-hour rate must be present, including null")
            expected_hour = row.cache_write_1h_price_per_1k_tokens
            if expected_hour is None:
                expected_hour = max(input_rate, expected_write)
            expected_unknown = max(input_rate, expected_write, expected_hour)
            if parse_rate(rates["effective_cache_write_1h_price_per_1k_tokens"], field_name="effective hour") != expected_hour:
                raise ValueError("effective one-hour rate disagrees with published rate")
            if parse_rate(rates["effective_cache_write_unconfirmed_price_per_1k_tokens"], field_name="effective unconfirmed") != expected_unknown:
                raise ValueError("unconfirmed write rate is not conservative")
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidPricingDecisionError(f"decision cache policy is malformed: {exc}") from exc
    if (
        row.cache_read_price_per_1k_tokens is None
        and usage["cache_read_input_tokens"] > 0
        and EstimateReason.UNPUBLISHED_CACHE_READ_RATE not in reasons
    ):
        raise InvalidPricingDecisionError("unpublished cache read requires an estimate reason")
    if (
        row.cache_write_policy == CacheWritePolicy.UNPUBLISHED
        and usage["cache_creation_input_tokens"] > 0
        and EstimateReason.UNPUBLISHED_CACHE_WRITE_RATE not in reasons
    ):
        raise InvalidPricingDecisionError("unpublished cache write requires an estimate reason")
    if payload["decision_version"] == 2:
        if "context_max_input_tokens" not in rates:
            raise InvalidPricingDecisionError("Claude decision requires frozen context limit evidence")
        context_max = rates["context_max_input_tokens"]
        if context_max is not None and (type(context_max) is not int or context_max <= 0):
            raise InvalidPricingDecisionError("decision context limit is malformed")
        if context_max is not None and usage["total_input_tokens"] > context_max and EstimateReason.UNSUPPORTED_CONTEXT not in reasons:
            raise InvalidPricingDecisionError("context overflow requires an estimate reason")
        if usage["cache_creation_unconfirmed_input_tokens"] and EstimateReason.UNCONFIRMED_CACHE_WRITE_DURATION not in reasons:
            raise InvalidPricingDecisionError("unconfirmed cache write duration requires an estimate reason")
        if (
            usage["cache_creation_1h_input_tokens"]
            and row.cache_write_1h_price_per_1k_tokens is None
            and EstimateReason.UNPUBLISHED_CACHE_WRITE_1H_RATE not in reasons
        ):
            raise InvalidPricingDecisionError("unpublished one-hour rate requires an estimate reason")


def verify_pricing_decision(
    payload: dict[str, Any],
    *,
    request_id: str,
    org_id: str,
) -> Decimal:
    """Validate a decision from a settlement event and return its ledger cost.

    Checks version, tenant/request identity, bounded usage, rate format and the
    exact arithmetic, reproducing the amount from the EMBEDDED rates. It never
    consults the current cache or database: an unavailable historical generation
    is not grounds to reprice, because everything needed is in the event.
    """
    if not isinstance(payload, dict):
        raise InvalidPricingDecisionError("pricing_decision is not an object")

    version = payload.get("decision_version")
    if type(version) is not int or version not in (1, 2):
        raise InvalidPricingDecisionError(f"unsupported decision_version: {version!r}")
    if type(payload.get("policy_version")) is not int or payload.get("policy_version") != version:
        raise InvalidPricingDecisionError(f"unsupported policy_version: {payload.get('policy_version')!r}")

    if payload.get("request_id") != request_id:
        raise InvalidPricingDecisionError("decision request_id does not match its event")
    if payload.get("org_id") != org_id:
        raise InvalidPricingDecisionError("decision org_id does not match its event")

    usage_payload = payload.get("usage") or {}
    try:
        counters = [
            usage_payload[key]
            for key in ("total_input_tokens", "uncached_input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "output_tokens")
        ]
        if any(type(value) is not int for value in counters):
            raise ValueError("normalized counters must be integers")
        total, uncached, read, write, output = counters
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidPricingDecisionError(f"decision usage is malformed: {exc}") from exc

    if min(total, uncached, read, write, output) < 0:
        raise InvalidPricingDecisionError("decision usage contains negative counters")
    if uncached + read + write != total:
        raise InvalidPricingDecisionError(f"decision usage does not decompose: {uncached}+{read}+{write} != {total}")
    if version == 2:
        components = tuple(
            usage_payload.get(key)
            for key in ("cache_creation_5m_input_tokens", "cache_creation_1h_input_tokens", "cache_creation_unconfirmed_input_tokens")
        )
        if any(type(value) is not int or value < 0 for value in components) or sum(components) != write:
            raise InvalidPricingDecisionError("decision cache duration counters do not decompose")

    rates = payload.get("rates") or {}
    try:
        input_rate = parse_rate(rates["input_price_per_1k_tokens"], field_name="input_price_per_1k_tokens")
        output_rate = parse_rate(rates["output_price_per_1k_tokens"], field_name="output_price_per_1k_tokens")
        read_rate = parse_rate(rates["effective_cache_read_price_per_1k_tokens"], field_name="effective_cache_read_price_per_1k_tokens")
        write_rate = parse_rate(rates["effective_cache_write_price_per_1k_tokens"], field_name="effective_cache_write_price_per_1k_tokens")
    except (KeyError, TypeError, ValueError) as exc:
        raise InvalidPricingDecisionError(f"decision rates are malformed: {exc}") from exc

    recomputed = (
        Decimal(uncached) * input_rate + Decimal(read) * read_rate + Decimal(write) * write_rate + Decimal(output) * output_rate
    ) / _THOUSAND
    if version == 2:
        try:
            hour_rate = parse_rate(rates["effective_cache_write_1h_price_per_1k_tokens"], field_name="effective hour")
            unknown_rate = parse_rate(rates["effective_cache_write_unconfirmed_price_per_1k_tokens"], field_name="effective unconfirmed")
        except (KeyError, TypeError, ValueError) as exc:
            raise InvalidPricingDecisionError(f"decision duration rates are malformed: {exc}") from exc
        recomputed = (
            Decimal(uncached) * input_rate
            + Decimal(read) * read_rate
            + Decimal(output) * output_rate
            + sum(Decimal(count) * rate for count, rate in zip(components, (write_rate, hour_rate, unknown_rate), strict=True))
        ) / _THOUSAND

    try:
        claimed_exact = Decimal(str(payload["exact_cost_usd"]))
        claimed_ledger = Decimal(str(payload["ledger_cost_usd"]))
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise InvalidPricingDecisionError(f"decision cost fields are malformed: {exc}") from exc

    if not claimed_exact.is_finite() or not claimed_ledger.is_finite():
        raise InvalidPricingDecisionError("decision cost fields must be finite")
    if recomputed != claimed_exact:
        raise InvalidPricingDecisionError(f"decision arithmetic does not reproduce: computed {recomputed}, claimed {claimed_exact}")
    if quantize_ledger(claimed_exact) != claimed_ledger:
        raise InvalidPricingDecisionError(f"decision ledger value is not the quantized exact cost: {claimed_ledger}")

    _verify_decision_binding(payload, input_rate=input_rate, read_rate=read_rate, write_rate=write_rate)
    try:
        expected_hash = _decision_content_hash(payload)
    except (TypeError, ValueError) as exc:
        raise InvalidPricingDecisionError("decision content is not JSON serializable") from exc
    if payload.get("content_sha256") != expected_hash:
        raise InvalidPricingDecisionError("decision content_sha256 does not match its content")
    return claimed_ledger


# ---------------------------------------------------------------------------
# Flat-table compatibility adapters
# ---------------------------------------------------------------------------
#
# The three retired literals (src/budget/pricing.py, lambda/shared/pricing_fallback.py,
# src/budget/config.py) all had the same shape: model id -> {"input": ..., "output":
# ..., optional cache keys}. Their public callers still want that shape, so rather
# than rewrite every caller at once, the shape is now DERIVED from the snapshot
# here (design §4.1: "remove the independent pricing literals ... preserve their
# public adapters where callers require them").
#
# These are compatibility adapters, not the pricing path. They are flat — no
# geography, service tier or context tier — so they CANNOT express correct OpenAI
# pricing, which is exactly what #4969 is fixing. `legacy_flat_rates` therefore
# serves OpenAI ids from the conservative in-region standard variant purely so a
# pre-request estimate is not absurd, and the callers that settle money use
# `build_pricing_decision` instead. Never route a settlement through here.


def _flat_from_curated(entry: dict[str, str]) -> dict[str, Decimal]:
    return {name: parse_rate(value, field_name=name) for name, value in entry.items()}


def _openai_flat_row(snapshot: Snapshot, model_id: str) -> dict[str, Decimal] | None:
    """Flatten a model's OpenAI variants into one conservative in-region row.

    Picks the dearest published input rate among in-region standard rows, so a
    flat-shaped estimator over-reserves rather than under-reserves. Returns None
    when the model publishes no such row, so the caller can fall through to the
    curated default instead of inventing a rate.
    """
    rows = [r for r in snapshot.rows_for_model(model_id) if r.geography == Geography.IN_REGION and r.service_tier == ServiceTier.STANDARD]
    if not rows:
        return None
    chosen = max(rows, key=lambda r: (r.input_price_per_1k_tokens, r.output_price_per_1k_tokens, r.variant_key))
    flat: dict[str, Decimal] = {
        "input": chosen.input_price_per_1k_tokens,
        "output": chosen.output_price_per_1k_tokens,
    }
    if chosen.cache_read_price_per_1k_tokens is not None:
        flat["cache_read_input"] = chosen.cache_read_price_per_1k_tokens
    # Only a genuinely published write price is exposed. `unpublished` stores NULL
    # and must stay absent here: emitting a zero would tell a caller that writing
    # to the cache is free, and emitting the input rate would look like a
    # published no-additional-fee policy that AWS has not stated.
    if chosen.cache_write_price_per_1k_tokens is not None:
        flat["cache_creation_input"] = chosen.cache_write_price_per_1k_tokens
    return flat


def legacy_flat_rates(
    model_id: str,
    *,
    snapshot: Snapshot | None = None,
    db_rates: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Decimal], bool]:
    """Flat per-1K rates for any model id, in the retired literals' shape.

    Non-OpenAI ids resolve through ``resolve_curated_non_openai``, preserving the
    curated four-key Claude entries, the suffix/prefix normalization and the
    unknown-model boundary exactly as before (#1486/#4592, design §7).

    Returns ``(rates, known)``. ``known`` is False when the id fell through to the
    generic default, so callers keep emitting ``UnknownModelPricing`` on the same
    inputs they do today — the observability boundary must not move silently.
    """
    active = snapshot or load_snapshot()
    if is_openai_model(model_id):
        canonical = active.alias_map.get(model_id) or normalize_billing_model_id(model_id)
        flat = _openai_flat_row(active, canonical)
        if flat is not None:
            return flat, True
        # An OpenAI id with no published in-region standard row is genuinely
        # unknown, not a curated model; fall through to the default and report it.
        return _flat_from_curated(active.curated_non_openai["rates"]["default"]), False
    return resolve_curated_non_openai(model_id, snapshot=active, db_rates=db_rates)


def legacy_flat_table(*, snapshot: Snapshot | None = None) -> dict[str, dict[str, Decimal]]:
    """The whole flat table, for the few callers that enumerate rather than look up.

    Includes ``"default"``, matching the retired literals. OpenAI entries carry
    the same conservative in-region standard flattening as ``legacy_flat_rates``.
    """
    active = snapshot or load_snapshot()
    table = {model_id: _flat_from_curated(entry) for model_id, entry in active.curated_non_openai["rates"].items()}
    for model_id in active.models:
        if not is_openai_model(model_id):
            continue
        flat = _openai_flat_row(active, model_id)
        if flat is not None:
            table[model_id] = flat
    return table


def staleness_reasons(*, row_verified_at: str | None, now_iso: str, cache_failure_minutes: float | None = None) -> tuple[str, ...]:
    """Estimate reasons implied by source/cache age (design §4.1).

    A generation's publication time never makes an individually stale row fresh,
    so this looks at the selected ROW's own ``verified_at``.
    """
    reasons: set[str] = set()
    if cache_failure_minutes is not None and cache_failure_minutes >= CACHE_REFRESH_FAILURE_MINUTES:
        reasons.add(EstimateReason.CACHE_REFRESH_FAILING)
    if row_verified_at:
        try:
            verified = datetime.fromisoformat(row_verified_at)
            now = datetime.fromisoformat(now_iso)
        except ValueError:
            return tuple(sorted(reasons))
        if verified.tzinfo is None or now.tzinfo is None:
            return tuple(sorted(reasons))
        if (now - verified).total_seconds() / 3600.0 > STALE_ROW_AGE_HOURS:
            reasons.add(EstimateReason.STALE_RATE_SOURCE)
    return tuple(sorted(reasons))


__all__ = [
    "COMPATIBILITY_SNAPSHOT_VERSION",
    "CURRENT_SNAPSHOT_VERSION",
    "DECISION_VERSION",
    "CLAUDE_DECISION_VERSION",
    "SUPPORTED_POLICY_VERSIONS",
    "POLICY_VERSION",
    "CacheWritePolicy",
    "Confidence",
    "ContextTier",
    "EstimateReason",
    "Geography",
    "InvalidPricingDecisionError",
    "MissingUsageError",
    "NormalizedUsage",
    "PricingDecision",
    "PricingSnapshot",
    "RateRow",
    "RateSource",
    "RoutingEvidence",
    "ServiceTier",
    "Snapshot",
    "UnsupportedVariantError",
    "VariantKey",
    "build_pricing_decision",
    "canonical_billing_model_id",
    "geography_from_model_prefix",
    "is_openai_model",
    "is_anthropic_model",
    "is_v2_priced_model",
    "legacy_flat_rates",
    "legacy_flat_table",
    "load_snapshot",
    "normalize_billing_model_id",
    "normalize_usage",
    "parse_rate",
    "price_from_rate_row",
    "quantize_ledger",
    "rate_to_string",
    "resolve_curated_non_openai",
    "select_context_tier",
    "select_rate_row",
    "snapshot_from_mapping",
    "staleness_reasons",
    "verify_pricing_decision",
]
