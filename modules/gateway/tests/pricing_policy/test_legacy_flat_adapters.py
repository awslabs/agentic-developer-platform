"""The flat compatibility adapters that replaced the three rate literals (#4969, §4.1).

``legacy_flat_rates`` / ``legacy_flat_table`` exist so ``src/budget/pricing.py``,
``lambda/shared/pricing_fallback.py`` and ``src/budget/utils.py`` keep their public
shape while the numbers behind them come from one snapshot. That makes two
properties load-bearing, and both are asserted here:

1. **Ledger neutrality for non-OpenAI traffic.** Parity is measured against
   ``legacy_rate_tables_frozen``, a verbatim copy of the literals from the commit
   before the collapse — never against the live modules, which are now derived
   from the same snapshot and so cannot disagree with it.
2. **The unknown-model boundary does not move.** ``UnknownModelPricing`` must fire
   on exactly the ids it fired on before; gaining or losing coverage silently
   changes what gets billed with no published rate behind it.

The flat shape cannot express geography, service tier or context tier, so its
OpenAI rows are a deliberate conservative projection, not a settlement input.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pricing_policy import (
    Geography,
    ServiceTier,
    is_openai_model,
    legacy_flat_rates,
    legacy_flat_table,
    load_snapshot,
)

from .legacy_rate_tables_frozen import (
    BUDGET_CONFIG_MODEL_PRICING,
    GATEWAY_MODEL_PRICING,
    SETTLEMENT_MODEL_PRICING,
)


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot()


@pytest.fixture(scope="module")
def flat_table():
    return legacy_flat_table()


def _frozen_non_openai(table: dict[str, dict[str, Decimal]]) -> list[str]:
    return sorted(k for k in table if k != "default" and not k.startswith("openai."))


# --------------------------------------------------------------------------
# Parity against what actually billed
# --------------------------------------------------------------------------


@pytest.mark.parametrize("model_id", _frozen_non_openai(SETTLEMENT_MODEL_PRICING))
def test_settlement_rates_unchanged(model_id):
    """Every rate the settlement literal billed is reproduced exactly.

    This is the table that wrote ``budget_usage.total_cost_usd``; a single moved
    digit here is a billing change, so the assertion is per-key equality rather
    than an approximate compare.
    """
    expected = SETTLEMENT_MODEL_PRICING[model_id]
    actual, known = legacy_flat_rates(model_id)
    assert known, f"{model_id} became unknown"
    for key, value in expected.items():
        assert actual.get(key) == value, f"{model_id}.{key}"


#: Ids where the estimator literal disagreed with the settlement literal, and the
#: collapse necessarily resolved the disagreement. Each entry names the reason —
#: per the frozen fixture's contract, a legitimate change is recorded as an
#: exception here rather than by editing the frozen numbers, which would erase the
#: evidence that anything moved.
#:
#: Haiku 4.5: the estimator carried 0.001/0.005 while the settlement table — the
#: one that writes the ledger — carried 0.0008/0.004, so pre-request estimates
#: over-reserved by 25% against the rate the request was actually billed at. The
#: snapshot keeps the settlement value. Tracked as #4978.
_ESTIMATOR_DISAGREEMENTS = {
    "global.anthropic.claude-haiku-4-5-20251001-v1:0": "#4978: estimator was 25% above the billed rate; snapshot keeps the settlement value",
}


@pytest.mark.parametrize("model_id", _frozen_non_openai(GATEWAY_MODEL_PRICING))
def test_gateway_estimator_rates_unchanged(model_id):
    """The estimator literal's non-OpenAI rows are reproduced too.

    It carried profile-prefixed keys (``global.anthropic...``) the settlement table
    did not, so it covers ids the test above never reaches.

    Where the two literals disagreed the collapse had to pick one, so those ids are
    listed in ``_ESTIMATOR_DISAGREEMENTS`` with the reason and are asserted to match
    the settlement literal instead — the estimator never billed anything, and
    keeping its number would have moved the ledger.
    """
    expected = GATEWAY_MODEL_PRICING[model_id]
    actual, known = legacy_flat_rates(model_id)
    assert known, f"{model_id} became unknown"

    if model_id in _ESTIMATOR_DISAGREEMENTS:
        settled = SETTLEMENT_MODEL_PRICING[legacy_resolve_to_settlement_key(model_id)]
        for key, value in settled.items():
            assert actual.get(key) == value, f"{model_id}.{key} ({_ESTIMATOR_DISAGREEMENTS[model_id]})"
        return

    for key, value in expected.items():
        assert actual.get(key) == value, f"{model_id}.{key}"


def legacy_resolve_to_settlement_key(model_id: str) -> str:
    """Strip the cross-region profile prefix to reach the settlement table's key."""
    for prefix in ("us.", "global.", "eu.", "apac."):
        if model_id.startswith(prefix):
            return model_id[len(prefix) :]
    return model_id


def test_default_row_is_unchanged(flat_table):
    """The conservative fallback row itself must not drift.

    Every unknown model is billed at these rates, so a change here silently
    reprices all unrecognised traffic.
    """
    assert flat_table["default"] == SETTLEMENT_MODEL_PRICING["default"]
    _, known = legacy_flat_rates("definitely-not-a-model")
    assert not known


def test_budget_config_short_names_still_resolve():
    """Every public BudgetConfig short name retains its former explicit rate."""
    for model_id, expected in BUDGET_CONFIG_MODEL_PRICING.items():
        if model_id == "default":
            continue
        actual, known = legacy_flat_rates(model_id)
        assert known, model_id
        assert actual["input"] == expected["input"], model_id
        assert actual["output"] == expected["output"], model_id


def test_four_key_claude_entries_keep_all_four_keys():
    """Cache rates survive the collapse on every entry that had them.

    Agent traffic is cache-dominated and the default row has no cache rates at
    all, so a dropped ``cache_read_input`` misprices most of a request's tokens
    while base-rate parity still passes (#1486).
    """
    checked = 0
    for model_id, expected in SETTLEMENT_MODEL_PRICING.items():
        cache_keys = {k for k in expected if k.startswith("cache_")}
        if not cache_keys or model_id.startswith("openai."):
            continue
        actual, known = legacy_flat_rates(model_id)
        assert known, model_id
        assert cache_keys <= set(actual), f"{model_id} lost {cache_keys - set(actual)}"
        checked += 1
    assert checked > 10


# --------------------------------------------------------------------------
# The unknown-model boundary
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_id",
    [
        "meta.some-unreleased-model-v9:0",
        "amazon.nova-pro-v1:0",
        "cohere.command-r-plus-v9:0",
        "definitely-not-a-model",
        "",
    ],
)
def test_unknown_ids_report_unknown(model_id):
    """``known=False`` on ids the literals could not price, with usable rates."""
    rates, known = legacy_flat_rates(model_id)
    assert not known, model_id
    # Still returns the conservative row: the caller logs and bills, never crashes
    # and never bills zero.
    assert rates["input"] > Decimal("0")
    assert rates["output"] > Decimal("0")


def test_unpriced_openai_id_is_unknown_not_defaulted_silently():
    """A future OpenAI model with no published row reports unknown.

    Being an ``openai.`` id must not confer coverage — the metric has to fire so
    the missing rate is visible, rather than the model quietly billing at the
    generic default forever.
    """
    rates, known = legacy_flat_rates("openai.gpt-9-unreleased")
    assert not known
    assert rates == {k: v for k, v in SETTLEMENT_MODEL_PRICING["default"].items()}


def test_cross_region_prefixes_and_suffix_variants_resolve():
    """The #4592 normalizations both literals implemented separately still apply."""
    base, known = legacy_flat_rates("anthropic.claude-opus-4-8-v1")
    assert known
    for variant in ("us.anthropic.claude-opus-4-8-v1", "global.anthropic.claude-opus-4-8", "anthropic.claude-opus-4-8"):
        rates, variant_known = legacy_flat_rates(variant)
        assert variant_known, variant
        assert rates["input"] == base["input"], variant
        assert rates["output"] == base["output"], variant


# --------------------------------------------------------------------------
# OpenAI projection: conservative, and never inventing a cache rate
# --------------------------------------------------------------------------


def test_openai_flat_row_is_the_in_region_standard_variant(snapshot):
    """The flat row is picked from in_region/standard, and is the dearest of them.

    A flat estimator that under-reserves lets a request overshoot the cap it just
    passed, so where several in-region standard rows exist the dearest is the safe
    projection.
    """
    for model_id in (model for model in snapshot.models if is_openai_model(model)):
        flat, known = legacy_flat_rates(model_id)
        candidates = [r for r in snapshot.rows_for_model(model_id) if r.geography == Geography.IN_REGION and r.service_tier == ServiceTier.STANDARD]
        if not candidates:
            assert not known, model_id
            continue
        assert known, model_id
        assert flat["input"] == max(r.input_price_per_1k_tokens for r in candidates), model_id
        assert flat["input"] in {r.input_price_per_1k_tokens for r in candidates}


def test_openai_flat_row_never_fabricates_a_cache_write_rate(snapshot):
    """``unpublished`` stays absent — not zero, and not the input rate.

    A zero would tell the caller that writing to the cache is free; the input rate
    would look like a published ``no_additional_fee`` policy AWS has not stated.
    Absence makes the caller apply its own documented derivation instead.
    """
    checked = 0
    for model_id in (model for model in snapshot.models if is_openai_model(model)):
        rows = [r for r in snapshot.rows_for_model(model_id) if r.geography == Geography.IN_REGION and r.service_tier == ServiceTier.STANDARD]
        if not rows:
            continue
        flat, _ = legacy_flat_rates(model_id)
        chosen = max(rows, key=lambda r: (r.input_price_per_1k_tokens, r.output_price_per_1k_tokens, r.variant_key))
        if chosen.cache_write_price_per_1k_tokens is None:
            assert "cache_creation_input" not in flat, model_id
            checked += 1
        else:
            assert flat["cache_creation_input"] == chosen.cache_write_price_per_1k_tokens, model_id
        if chosen.cache_read_price_per_1k_tokens is None:
            assert "cache_read_input" not in flat, model_id
        else:
            assert flat["cache_read_input"] == chosen.cache_read_price_per_1k_tokens, model_id
    assert checked > 0, "expected at least one model with an unpublished cache write rate"


def test_gpt_oss_120b_uses_published_rate_not_the_retired_literal():
    """The correction #4969 was filed for, pinned on a concrete model.

    The retired literal billed 0.0001545/0.000618; AWS publishes 0.00015/0.0006.
    """
    rates, known = legacy_flat_rates("openai.gpt-oss-120b")
    assert known
    assert rates["input"] == Decimal("0.00015")
    assert rates["output"] == Decimal("0.0006")
    assert rates["input"] != SETTLEMENT_MODEL_PRICING["openai.gpt-oss-120b"]["input"]


# --------------------------------------------------------------------------
# legacy_flat_table
# --------------------------------------------------------------------------


def test_flat_table_covers_curated_and_openai_models(snapshot, flat_table):
    """Enumerating callers see the curated entries plus every priced OpenAI model."""
    assert "default" in flat_table
    for model_id in snapshot.curated_non_openai["rates"]:
        assert model_id in flat_table, model_id
    for model_id in (model for model in snapshot.models if is_openai_model(model)):
        _, known = legacy_flat_rates(model_id)
        if known:
            assert model_id in flat_table, model_id


def test_flat_table_agrees_with_pointwise_lookup(flat_table):
    """No entry may disagree with ``legacy_flat_rates`` for the same id.

    They are separate code paths over the same snapshot; if they diverged, a
    caller that enumerates would bill differently from one that looks up.
    """
    for model_id, expected in flat_table.items():
        if model_id == "default":
            continue
        actual, known = legacy_flat_rates(model_id)
        assert known, model_id
        assert actual == expected, model_id


def test_flat_table_values_are_decimal(flat_table):
    """Rates are Decimal end to end — a float here reintroduces binary error."""
    for model_id, rates in flat_table.items():
        for key, value in rates.items():
            assert isinstance(value, Decimal), f"{model_id}.{key} is {type(value)}"


def test_openai_and_curated_namespaces_do_not_collide(flat_table):
    """No id is served by both halves of the table.

    An overlap would make the table's contents depend on the order the two halves
    are merged, which is exactly how the original literals drifted.
    """
    curated = set(load_snapshot().curated_non_openai["rates"])
    openai_served = {m for m in load_snapshot().models if is_openai_model(m) and legacy_flat_rates(m)[1]}
    assert not (curated & openai_served)
    for model_id in openai_served:
        assert is_openai_model(model_id), model_id
