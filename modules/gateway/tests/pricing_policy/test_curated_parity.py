"""Prove the merged curated table reproduces the previously billed non-OpenAI rates.

The curated section of ``2026-09-12.1.json`` was mechanically merged from the two
pre-#4976 rate literals. This module checks that merge against the literal that
actually billed: ``lambda/shared/pricing_fallback``'s table, whose values write
``budget_usage`` and ``usage_logs.cost_usd``. Every model it could resolve must
resolve to the same rates through the shared policy package, so the collapse into
one table is provably ledger-neutral for non-OpenAI traffic (design §7).

The comparison reads ``legacy_rate_tables_frozen.SETTLEMENT_MODEL_PRICING``, a
verbatim copy of that literal taken at the commit before the collapse — NOT the
live module. Since #4969 the live ``pricing_fallback.MODEL_PRICING`` is itself
derived from this snapshot, so importing it here would compare the snapshot to
itself and pass no matter what the merge did to the numbers. The lookup semantics
it had (cross-region prefix strip, case-insensitive match, #4592 suffix-variant
retry) are reproduced below against the frozen table for the same reason.

OpenAI models are excluded by design — their pricing moves to the
variant-dimensioned snapshot rates, and correcting those is the point of #4969.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pricing_policy import load_snapshot, resolve_curated_non_openai

from .legacy_rate_tables_frozen import SETTLEMENT_MODEL_PRICING


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot()


def _legacy_resolve_model_id(model_id: str) -> str:
    """``pricing_fallback.resolve_model_id`` as it stood before the collapse."""
    for prefix in ("us.", "global.", "eu.", "apac."):
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


def _legacy_get_model_pricing(model_id: str) -> dict[str, Decimal] | None:
    """``pricing_fallback.get_model_pricing`` against the FROZEN table.

    Returns ``None`` where the original returned its ``default`` row, so a caller
    can tell "priced" from "fell through" without comparing against the default's
    values — two models legitimately sharing the default's rates would otherwise
    read as unknown.
    """
    resolved_id = _legacy_resolve_model_id(model_id)

    if resolved_id in SETTLEMENT_MODEL_PRICING:
        return SETTLEMENT_MODEL_PRICING[resolved_id]

    model_lower = resolved_id.lower()
    for key in SETTLEMENT_MODEL_PRICING:
        if key.lower() == model_lower:
            return SETTLEMENT_MODEL_PRICING[key]

    # Issue #4592 suffix-variant retry, reproduced verbatim.
    for candidate in (
        f"{resolved_id}-v1",
        resolved_id.removesuffix(":0"),
        resolved_id.removesuffix("-v1:0"),
        resolved_id.removesuffix("-v1"),
    ):
        if candidate != resolved_id and candidate in SETTLEMENT_MODEL_PRICING:
            return SETTLEMENT_MODEL_PRICING[candidate]

    return None


def _non_openai_ids() -> list[str]:
    return sorted(k for k in SETTLEMENT_MODEL_PRICING if k != "default" and not k.startswith("openai."))


def test_every_billed_non_openai_rate_is_preserved(snapshot):
    """Exact per-model parity with the literal that wrote the ledger before #4969."""
    ids = _non_openai_ids()
    assert len(ids) > 20, "expected the curated table to be substantial"

    mismatches = []
    for model_id in ids:
        expected = _legacy_get_model_pricing(model_id)
        assert expected is not None, model_id
        actual, known = resolve_curated_non_openai(model_id, snapshot=snapshot)
        if not known:
            mismatches.append((model_id, "resolved to default", None))
            continue
        for key, value in expected.items():
            if actual.get(key) != value:
                mismatches.append((model_id, key, f"{actual.get(key)} != {value}"))

    assert not mismatches, f"curated merge changed billed rates: {mismatches}"


def test_cache_keys_are_not_dropped(snapshot):
    """Every four-key Claude entry keeps all four keys.

    Agent traffic is cache-dominated and the default row carries no cache rates,
    so silently losing ``cache_read_input``/``cache_creation_input`` on a Claude
    model would misprice the majority of its tokens while base-rate parity still
    passed.
    """
    checked = 0
    for model_id in _non_openai_ids():
        expected = _legacy_get_model_pricing(model_id)
        cache_keys = {k for k in expected if k.startswith("cache_")}
        if not cache_keys:
            continue
        actual, known = resolve_curated_non_openai(model_id, snapshot=snapshot)
        assert known, model_id
        assert cache_keys <= set(actual), f"{model_id} lost {cache_keys - set(actual)}"
        checked += 1
    assert checked > 10, "expected many four-key cache entries to verify"


def test_cross_region_prefixes_resolve_identically(snapshot):
    """`us.`/`global.` profile prefixes must land on the same rates as the bare id."""
    for model_id in _non_openai_ids()[:15]:
        bare, known = resolve_curated_non_openai(model_id, snapshot=snapshot)
        assert known, model_id
        for prefix in ("us.", "global.", "eu.", "apac."):
            prefixed, prefixed_known = resolve_curated_non_openai(f"{prefix}{model_id}", snapshot=snapshot)
            assert prefixed_known, f"{prefix}{model_id}"
            assert prefixed == bare, f"{prefix}{model_id}"


def test_unknown_ids_agree_on_falling_through_to_default(snapshot):
    """The unknown-model boundary must not move, in either direction.

    A model the legacy resolver could not price must still be unknown here (so
    `UnknownModelPricing` keeps firing), and vice versa — silently gaining or
    losing coverage would change billing without any published rate behind it.
    """
    probes = [
        "meta.some-unreleased-model-v9:0",
        "amazon.nova-pro-v1:0",
        "cohere.command-r-plus-v9:0",
        "definitely-not-a-model",
    ]
    for model_id in probes:
        legacy_known = _legacy_get_model_pricing(model_id) is not None
        _, known = resolve_curated_non_openai(model_id, snapshot=snapshot)
        assert known == legacy_known, model_id


def test_openai_ids_are_not_served_by_the_curated_table(snapshot):
    """OpenAI pricing must come from the variant-dimensioned rates only.

    If an OpenAI id could still resolve through the curated path it would keep
    the old wrong flat rates alive behind the corrected ones.
    """
    for model_id in ("openai.gpt-5.6-sol", "openai.gpt-oss-120b", "us.openai.gpt-6-astra"):
        _, known = resolve_curated_non_openai(model_id, snapshot=snapshot)
        assert not known, model_id
