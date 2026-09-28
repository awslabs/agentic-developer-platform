"""Independent AWS fixture oracle, strict source validation and lost-rate safety."""

import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from pricing_policy.aws_sources import SourceValidationError
from pricing_policy.claude_sources import parse_claude_pricing, source_digest
from pricing_policy.policy import load_snapshot
from pricing_policy.refresh import assemble_candidate

FIXTURES = Path(__file__).parent / "fixtures" / "aws" / "claude"
VERIFIED = "2026-09-12T18:00:00+00:00"
FIELDS = {
    "input": "input_price_per_1k_tokens",
    "output": "output_price_per_1k_tokens",
    "cache_read": "cache_read_price_per_1k_tokens",
    "cache_write_5m": "cache_write_price_per_1k_tokens",
    "cache_write_1h": "cache_write_1h_price_per_1k_tokens",
}
KEY_FIELDS = ("model_id", "geography", "service_tier", "context_tier", "region")


@pytest.fixture(scope="module")
def snapshot():
    return load_snapshot("2026-09-12.2")


def parse(snapshot, *, page=None, token_map=None, templates=None):
    return parse_claude_pricing(
        page if page is not None else (FIXTURES / "pricing-widgets.html").read_bytes(),
        token_map if token_map is not None else (FIXTURES / "token-map.json").read_bytes(),
        templates if templates is not None else snapshot.rates,
        snapshot.models,
        verified_at=VERIFIED,
    )


def test_complete_source_matches_independent_1006_variant_oracle(snapshot):
    expected = {
        tuple(row["key"][k] for k in KEY_FIELDS): row["usd_per_1k_tokens"] for row in json.loads((FIXTURES / "expected-rates.json").read_text())
    }
    rows = parse(snapshot)
    actual = {row.variant_key: row for row in rows}
    assert len(rows) == len(actual) == len(expected) == 1006
    assert actual.keys() == expected.keys()
    for key, rates in expected.items():
        for oracle_field, row_field in FIELDS.items():
            assert getattr(actual[key], row_field) == (Decimal(rates[oracle_field]) if rates[oracle_field] is not None else None), (key, row_field)
        assert actual[key].source == "pricing_page"
        assert actual[key].verified_at == VERIFIED
        assert actual[key].source_effective_at == "2026-09-11T12:44:10Z"
        assert actual[key].snapshot_version is None
    assert sum(r.service_tier == "standard" for r in rows) == 658
    assert sum(r.service_tier == "batch" for r in rows) == 348


def test_snapshot_preserves_every_openai_row_and_old_bundle(snapshot):
    old = load_snapshot("2026-09-12.1")
    assert tuple(r for r in snapshot.rates if r.model_id.startswith("openai.")) == old.rates
    assert snapshot.curated_non_openai == old.curated_non_openai
    assert snapshot.policy_version == 2
    assert len(snapshot.required_variants) == 1336
    assert len(snapshot.models) == 30


def test_haiku45_real_rate_is_not_35haiku_and_1h_write_is_independent(snapshot):
    rows = {r.variant_key: r for r in parse(snapshot)}
    global_row = rows[("anthropic.claude-haiku-4-5-20251001-v1:0", "global_cris", "standard", "flat", "us-east-1")]
    assert (global_row.input_price_per_1k_tokens, global_row.output_price_per_1k_tokens) == (Decimal("0.001"), Decimal("0.005"))
    assert global_row.cache_write_price_per_1k_tokens == Decimal("0.00125")
    assert global_row.cache_write_1h_price_per_1k_tokens == Decimal("0.002")
    regional = rows[(global_row.model_id, "geo_cris", "standard", "flat", "us-east-1")]
    assert regional.input_price_per_1k_tokens == Decimal("0.0011")


def test_cache_read_does_not_assume_ten_percent(snapshot):
    rows = parse(snapshot)
    for model in ("anthropic.claude-fable-5-1", "anthropic.claude-mythos-5-1"):
        row = next(r for r in rows if r.model_id == model and r.geography == "global_cris" and r.region == "us-east-1")
        assert row.cache_read_price_per_1k_tokens == row.input_price_per_1k_tokens * Decimal("0.025")


@pytest.mark.parametrize(
    "before,after",
    [
        (b"Price per 1M input tokens", b"Price per 1K input tokens"),
        (b"1h cache write", b"30m cache write"),
        (b"Global Cross-region Inference", b"Unknown Inference"),
        (b"{priceOf!", b"{unexpected!"),
    ],
)
def test_changed_units_columns_scopes_or_expressions_reject(snapshot, before, after):
    with pytest.raises(SourceValidationError):
        parse(snapshot, page=(FIXTURES / "pricing-widgets.html").read_bytes().replace(before, after))


@pytest.mark.parametrize("mutation", ["currency", "service", "timestamp", "nan", "negative", "zero", "number", "sku"])
def test_malformed_map_rejects_whole_publication(snapshot, mutation):
    data = json.loads((FIXTURES / "token-map.json").read_text())
    if mutation == "currency":
        data["manifest"]["currencyCode"] = "EUR"
    elif mutation == "service":
        data["manifest"]["serviceId"] = "other"
    elif mutation == "timestamp":
        data["manifest"]["hawkFilePublicationDate"] = "2026-09-11"
    else:
        for region in data["regions"].values():
            for value in region.values():
                if mutation == "sku":
                    value.pop("rateCode")
                else:
                    value["price"] = {"nan": "NaN", "negative": "-1", "zero": "0", "number": 1}.get(mutation)
    with pytest.raises(SourceValidationError):
        parse(snapshot, token_map=json.dumps(data).encode())


def test_duplicate_map_key_rejects(snapshot):
    content = (FIXTURES / "token-map.json").read_bytes()
    with pytest.raises(SourceValidationError):
        parse(snapshot, token_map=b'{"regions":{},' + content[1:])


def test_missing_region_retains_old_full_variant_without_renewing_age(snapshot):
    raw = json.loads((FIXTURES / "token-map.json").read_text())
    del raw["regions"]["US East (N. Virginia)"]
    fresh = parse(snapshot, token_map=json.dumps(raw).encode())
    prior = tuple(r for r in snapshot.rates if r.model_id.startswith("anthropic."))
    candidate = assemble_candidate(prior, fresh, frozenset(r.variant_key for r in prior))
    expected_retained = {r.variant_key for r in prior if r.region == "us-east-1"}
    assert candidate.retained_keys == expected_retained
    prior_by_key = {r.variant_key: r for r in prior}
    assert all(r == prior_by_key[r.variant_key] for r in candidate.rows if r.variant_key in expected_retained)


def test_missing_cache_token_retains_complete_row(snapshot):
    raw = json.loads((FIXTURES / "token-map.json").read_text())
    # Remove all $2 1h creation rates, while leaving input/output present.
    for entries in raw["regions"].values():
        for token in [t for t, value in entries.items() if Decimal(value["price"]) == 2]:
            del entries[token]
    fresh = parse(snapshot, token_map=json.dumps(raw).encode())
    assert not any(
        r.model_id == "anthropic.claude-haiku-4-5-20251001-v1:0"
        and r.region == "us-east-1"
        and r.geography == "global_cris"
        and r.service_tier == "standard"
        for r in fresh
    )


def test_no_unsupported_variant_or_batch_cache_rate_is_invented(snapshot):
    assert all(r.geography != "global_cris" or not r.region.startswith("us-gov-") for r in parse(snapshot))
    assert all(
        r.cache_write_1h_price_per_1k_tokens is None and r.cache_write_price_per_1k_tokens is None and r.cache_read_price_per_1k_tokens is None
        for r in parse(snapshot)
        if r.service_tier == "batch"
    )
    absent = replace(next(r for r in snapshot.rates if r.model_id.startswith("anthropic.")), region="invalid-1")
    assert not any(r.region == "invalid-1" for r in parse(snapshot, templates=snapshot.rates + (absent,)))


def test_provenance_hash_identifies_both_original_documents(snapshot):
    audit = snapshot.provenance["claude_audit"]
    manifest = {"pricing_page_sha256": audit["sources"]["pricing_page"]["sha256"], "token_map_sha256": audit["sources"]["token_map"]["sha256"]}
    expected = hashlib.sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert expected == audit["combined_source_sha256"]
    assert all(r.source_content_sha256 == expected for r in snapshot.rates if r.model_id.startswith("anthropic."))
    assert source_digest(b"page1", b"map") != source_digest(b"page2", b"map")
    assert source_digest(b"page1", b"map") != source_digest(b"page1", b"changed-map")


def test_opus55_rates_match_current_aws_widget_and_supported_endpoints():
    snapshot = load_snapshot("2026-09-24.1")
    templates = tuple(r for r in snapshot.rates if r.model_id == "anthropic.claude-opus-5-5")
    folder = Path(__file__).parent / "fixtures/aws/claude"
    rows = parse_claude_pricing(
        (folder / "opus55-widgets.html").read_bytes(),
        (folder / "opus55-token-map.json").read_bytes(),
        templates,
        snapshot.models,
        verified_at="2026-09-24T00:00:00Z",
    )
    assert len(rows) == 53
    us = {r.geography: r for r in rows if r.region == "us-east-1"}
    assert us["global_cris"].input_price_per_1k_tokens == Decimal("0.004")
    assert us["global_cris"].output_price_per_1k_tokens == Decimal("0.020")
    assert us["global_cris"].cache_read_price_per_1k_tokens == Decimal("0.0002")
    assert us["global_cris"].cache_write_price_per_1k_tokens == Decimal("0.005")
    assert us["global_cris"].cache_write_1h_price_per_1k_tokens == Decimal("0.008")
    assert us["geo_cris"].input_price_per_1k_tokens == Decimal("0.0044")
    assert us["geo_cris"].output_price_per_1k_tokens == Decimal("0.022")
    assert all(r.service_tier == "standard" for r in rows)
