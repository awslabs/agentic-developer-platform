"""Real AWS source shapes and complete-generation failure semantics."""

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from pricing_policy.aws_sources import CARD_SLUGS, SourceValidationError, parse_catalog, parse_model_card
from pricing_policy.policy import RateRow, load_snapshot
from pricing_policy.refresh import assemble_candidate, canonical_content_hash

FIXTURES = Path(__file__).parent / "fixtures" / "aws"
VERIFIED = "2026-09-12T06:00:00+00:00"


def template(model="openai.gpt-5.6-sol", geography="in_region", context="short", region="us-east-1"):
    return RateRow(
        model,
        geography,
        "standard",
        context,
        region,
        Decimal("0.0044"),
        Decimal("0.022"),
        Decimal("0.00044"),
        Decimal("0.0055"),
        "full_rate",
        "bundled_snapshot",
        "https://example.invalid/seed",
        "a" * 64,
        "2026-09-10T00:00:00+00:00",
        272000 if context == "short" else 1000000,
        snapshot_version="test-seed",
    )


def parse_card(slug, templates):
    model = next(model for model, name in CARD_SLUGS.items() if name == slug)
    return parse_model_card(
        (FIXTURES / (slug + ".md")).read_bytes(), model, templates, source_url="https://docs.aws.amazon.com/" + slug, verified_at=VERIFIED
    )


@pytest.mark.parametrize("model,slug", CARD_SLUGS.items())
def test_all_eight_real_card_structures(model, slug):
    rows = parse_card(
        slug,
        tuple(
            template(model, geography, context) for geography in ("in_region", "geo_cris", "global_cris", "govcloud") for context in ("short", "long")
        ),
    )
    assert rows
    assert all(row.verified_at == VERIFIED and row.source == "model_card" and row.snapshot_version is None for row in rows)


def test_scope_precision_and_paid_cache_writes():
    rows = parse_card("gpt-56-luna", (template("openai.gpt-5.6-luna", "govcloud", region="us-gov-west-1"),))
    assert rows[0].cache_read_price_per_1k_tokens == Decimal("0.0000264")
    cyber = parse_card("gpt-56-cyber", (template("openai.gpt-5.6-cyber", region="us-east-2"),))
    assert cyber[0].cache_write_price_per_1k_tokens == Decimal("0.0171875")
    row = parse_card("gpt-55", (template("openai.gpt-5.5"),))[0]
    assert row.cache_write_policy == "no_additional_fee"
    assert row.cache_write_price_per_1k_tokens == row.input_price_per_1k_tokens == Decimal("0.0055")


def test_legacy_bold_scope_and_combined_geography_rows():
    content = (FIXTURES / "astra-bold-scope.md").read_bytes()
    templates = (template("openai.gpt-6-astra", "in_region", region="us-west-2"), template("openai.gpt-6-astra", "geo_cris"))
    rows = parse_model_card(content, "openai.gpt-6-astra", templates, source_url="https://aws.example", verified_at=VERIFIED)
    assert len(rows) == 2
    content = content.replace(b"| In-Region |", b"| In-Region / Geo CRIS |")
    assert len(parse_model_card(content, "openai.gpt-6-astra", templates, source_url="https://aws.example", verified_at=VERIFIED)) == 2


@pytest.mark.parametrize(
    "corruption",
    [
        lambda text: text.replace("Input — cache read", "Cache discount"),
        lambda text: text.replace("30m cache write", "5m cache write"),
        lambda text: text.replace("272K", "300K"),
        lambda text: text.replace("per 1 million tokens", "per token"),
        lambda text: text.replace("| $4.40 | $5.50 |", "| $4.40 | $5.51 |"),
        lambda text: text + "\n### Unknown scope\n",
        lambda text: text.replace("| --- | --- | --- | --- | --- |", ""),
    ],
)
def test_malformed_card_rejects_whole_source(corruption):
    text = (FIXTURES / "gpt-56-sol.md").read_text()
    with pytest.raises(SourceValidationError):
        parse_model_card(corruption(text).encode(), "openai.gpt-5.6-sol", (template(),), source_url="https://aws.example", verified_at=VERIFIED)


def catalog(content=None):
    return parse_catalog(
        content or (FIXTURES / "oss-us-east-1.json").read_bytes(),
        "us-east-1",
        source_url="https://pricing.us-east-1.amazonaws.com/test",
        verified_at=VERIFIED,
    )


def test_actual_separate_catalog_skus_join_all_sixteen_variants():
    rows = catalog()
    assert len(rows) == 16
    row = next(row for row in rows if row.model_id == "openai.gpt-oss-safeguard-120b" and row.service_tier == "priority")
    assert row.input_price_per_1k_tokens == Decimal("0.00026")
    assert row.output_price_per_1k_tokens == Decimal("0.00105")
    assert row.cache_read_price_per_1k_tokens is None
    assert all(row.source_effective_at == "2026-09-11T12:44:08Z" for row in rows)


@pytest.mark.parametrize("mutation", ["unit", "conflict", "missing_output", "empty", "nan"])
def test_invalid_catalog_rejects_instead_of_fallback(mutation):
    raw = json.loads((FIXTURES / "oss-us-east-1.json").read_text())
    sku = next(iter(raw["products"]))
    dimension = next(iter(next(iter(raw["terms"]["OnDemand"][sku].values()))["priceDimensions"].values()))
    if mutation == "unit":
        dimension["unit"] = "tokens"
    elif mutation == "conflict":
        dimension["pricePerUnit"]["USD"] = "0.02"
    elif mutation == "nan":
        dimension["pricePerUnit"]["USD"] = "NaN"
    elif mutation == "missing_output":
        raw["products"] = {
            key: value for key, value in raw["products"].items() if not value["attributes"]["inferenceType"].lower().startswith("output")
        }
    else:
        raw["products"] = {}
    with pytest.raises((SourceValidationError, ValueError)):
        catalog(json.dumps(raw).encode())


def test_partial_retains_original_provenance_and_timestamp():
    old, other = template(), template("openai.gpt-5.6-terra")
    fresh = replace(old, source="model_card", verified_at=VERIFIED, snapshot_version=None)
    result = assemble_candidate((old, other), (fresh,), frozenset((old.variant_key, other.variant_key)))
    assert result.retained_keys == {other.variant_key}
    assert next(row for row in result.rows if row.model_id == other.model_id) == other
    assert result.content_sha256 == canonical_content_hash(tuple(reversed(result.rows)))


def test_missing_floor_zero_fresh_suspect_price_and_fallback_are_rejected():
    old = template()
    fresh = replace(old, source="model_card", verified_at=VERIFIED)
    for rows, required in (
        ((), frozenset()),
        ((fresh,), frozenset((template("missing").variant_key,))),
        ((replace(fresh, input_price_per_1k_tokens=Decimal("0.1")),), frozenset()),
        ((old,), frozenset()),
    ):
        with pytest.raises(SourceValidationError):
            assemble_candidate((old,), rows, required)


def test_canonical_hash_survives_database_decimal_padding_and_z_timestamp():
    row = template()
    changed = replace(row, input_price_per_1k_tokens=Decimal("0.0044000000"), verified_at=row.verified_at.replace("+00:00", "Z"))
    assert canonical_content_hash((row,)) == canonical_content_hash((changed,))


def test_rebase_cannot_replace_winner_with_older_fetched_content():
    winner = replace(template(), source="model_card", verified_at="2026-09-14T00:00:00+00:00")
    earlier = replace(winner, verified_at="2026-09-13T00:00:00+00:00", input_price_per_1k_tokens=Decimal("0.0045"))
    with pytest.raises(SourceValidationError, match="zero fresh usable"):
        assemble_candidate((winner,), (earlier,), frozenset((winner.variant_key,)))


def test_canonical_hash_ignores_database_session_timezone():
    row = template()
    offset = replace(row, verified_at="2026-09-10T01:00:00+01:00")
    assert canonical_content_hash((row,)) == canonical_content_hash((offset,))


def test_all_published_cards_and_catalog_cover_every_reviewed_endpoint_variant():
    snapshot = load_snapshot("2026-09-12.1")
    parsed = []
    for model, slug in CARD_SLUGS.items():
        if model in snapshot.models:
            parsed.extend(parse_card(slug, snapshot.rates))
    parsed.extend(catalog())
    actual = {row.variant_key: row for row in parsed}
    expected = {row.variant_key: row for row in snapshot.rates}
    assert len(actual) == len(parsed) == 330
    assert actual.keys() == expected.keys() == snapshot.required_variants
    for key, row in actual.items():
        baseline = expected[key]
        for field in (
            "input_price_per_1k_tokens",
            "output_price_per_1k_tokens",
            "cache_read_price_per_1k_tokens",
            "cache_write_price_per_1k_tokens",
            "cache_write_policy",
        ):
            assert getattr(row, field) == getattr(baseline, field), (key, field)
