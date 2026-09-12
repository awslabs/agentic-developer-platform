"""Independent accounting oracles for legacy formats and durable settlement."""

import copy
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from pricing_policy import RoutingEvidence, build_pricing_decision, load_snapshot, normalize_usage
from pricing_policy.policy import InvalidPricingDecisionError
from pricing_policy.storage import V2RateCache

from ._handler_loader import load_handler


def event(model="openai.gpt-5.6-sol", api_format="openai", **usage):
    return {
        "org_id": "org",
        "user_id": "user",
        "model": model,
        "api_format": api_format,
        "request_id": "req-oracle",
        "timestamp": datetime.now(UTC).isoformat(),
        "response": {"usage": usage},
    }


def settle(log, monkeypatch):
    handler = load_handler("budget-usage-tracker")
    writes = MagicMock()
    monkeypatch.setattr(handler, "upsert_budget_usage", writes)
    monkeypatch.setattr(handler, "bridge_cost_to_usage_logs", MagicMock())
    state = V2RateCache().state(monotonic=0, now_iso=datetime.now(UTC).isoformat())
    handler.process_chat_log(MagicMock(), log, state)
    return writes


@pytest.mark.parametrize("api_format", ["anthropic", "bedrock", None])
def test_claude_additive_cache_cost_and_tokens_preserved(monkeypatch, api_format):
    # Published Sonnet rates:3/15/.3/3.75 per million, inputs exclude cache.
    log = event(
        "anthropic.claude-3-5-sonnet-20241022-v2:0",
        api_format,
        input_tokens=1000,
        output_tokens=500,
        cache_read_input_tokens=200,
        cache_creation_input_tokens=400,
    )
    writes = settle(log, monkeypatch)
    assert writes.call_count == 6
    assert all(c.args[6] == Decimal("0.012060") and c.args[7] == 2100 for c in writes.call_args_list)


def test_legacy_openai_token_total_does_not_double_count_cache(monkeypatch):
    log = event(input_tokens=2048, output_tokens=256, cache_read_input_tokens=1920, cache_creation_input_tokens=0)
    writes = settle(log, monkeypatch)
    assert all(c.args[7] == 2304 for c in writes.call_args_list)
    assert all(c.args[6] > 0 for c in writes.call_args_list)


def decision_log():
    usage = {"input_tokens": 2048, "output_tokens": 256, "input_tokens_details": {"cached_tokens": 1920, "cache_write_tokens": 0}}
    snapshot = load_snapshot()
    decision = build_pricing_decision(
        request_id="req-oracle",
        org_id="org",
        usage=normalize_usage(usage, api_format="openai"),
        evidence=RoutingEvidence(
            original_model_id="openai.gpt-5.6-sol",
            billing_model_id="openai.gpt-5.6-sol",
            served_service_tier_raw="standard",
            geography="in_region",
            endpoint_region="us-east-1",
        ),
        rows=snapshot.rates,
        snapshot=snapshot,
    )
    log = event(input_tokens=128, output_tokens=256, cache_read_input_tokens=1920, cache_creation_input_tokens=0)
    log["pricing_decision"] = decision.to_dict()
    return log, decision


def test_durable_cost_and_tokens_reused_even_with_different_fallback(monkeypatch):
    log, _ = decision_log()
    writes = settle(log, monkeypatch)
    assert all(c.args[6] == Decimal("0.007040") and c.args[7] == 2304 for c in writes.call_args_list)


def test_malformed_decision_never_becomes_fallback_charge(monkeypatch):
    log, _ = decision_log()
    log["pricing_decision"]["ledger_cost_usd"] = "0.000001"
    with pytest.raises(InvalidPricingDecisionError):
        settle(log, monkeypatch)


def test_decision_for_other_model_is_rejected(monkeypatch):
    log, _ = decision_log()
    log["model"] = "openai.gpt-5.5"
    with pytest.raises(InvalidPricingDecisionError, match="model"):
        settle(log, monkeypatch)


def test_unknown_legacy_openai_remains_explicit_estimate():
    handler = load_handler("budget-usage-tracker")
    log = event("openai.future-unknown", input_tokens=1000, output_tokens=500)
    result = handler.settle_chat_log(
        handler.parse_chat_log(log), chat_log=log, rows=(), snapshot=load_snapshot(), generation_id=None, pointer_revision=None
    )
    assert result.estimated and "unknown_model" in result.reasons and "legacy_event" in result.reasons
    assert result.cost == Decimal("0.010500")


def test_legacy_openai_uses_pinned_row_provenance_instead_of_active_source_age():
    handler = load_handler("budget-usage-tracker")
    snapshot = load_snapshot()
    rows = tuple(replace(row, verified_at="2020-01-01T00:00:00Z") for row in snapshot.rates)
    log = event(input_tokens=1000, output_tokens=500)
    result = handler.settle_chat_log(handler.parse_chat_log(log), chat_log=log, rows=rows, snapshot=snapshot, generation_id=7, pointer_revision=9)
    assert result.decision["source_kind"] == "bundled_snapshot"
    selected = next(row for row in snapshot.rates if list(row.variant_key) == result.decision["variant_key"])
    assert result.decision["verified_at"] == selected.verified_at
    assert result.decision["generation_id"] is None


@pytest.mark.parametrize("writer", ["ledger", "usage_bridge"])
def test_database_bindings_preserve_exact_decimal(writer):
    handler = load_handler("budget-usage-tracker")
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cost = Decimal("0.007040")
    if writer == "ledger":
        handler.upsert_budget_usage(connection, "org", "user", "user", datetime.now(UTC), "daily", cost, 2304)
        bound = cursor.execute.call_args.args[1][6]
    else:
        handler.bridge_cost_to_usage_logs(connection, "req", cost, "log.json")
        bound = cursor.execute.call_args.args[1][0]
    assert isinstance(bound, Decimal)
    assert bound == Decimal("0.007040")


@pytest.mark.parametrize("legacy_rates, expected", [(None, "0.012060"), ({"input": "0.004", "output": "0.020"}, "0.015560")])
def test_legacy_claude_ignores_new_snapshot_rates_and_aliases(legacy_rates, expected):
    handler = load_handler("budget-usage-tracker")
    model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    current = load_snapshot()
    changed_curated = copy.deepcopy(current.curated_non_openai)
    changed_curated["aliases"][model] = "default"
    changed_curated["rates"]["default"] = {"input": "9", "output": "9"}
    changed = replace(current, snapshot_version="future-snapshot", curated_non_openai=changed_curated)
    log = event(model, "anthropic", input_tokens=1000, output_tokens=500, cache_read_input_tokens=200, cache_creation_input_tokens=400)
    result = handler.settle_chat_log(
        handler.parse_chat_log(log),
        chat_log=log,
        rows=current.rates,
        snapshot=changed,
        generation_id=7,
        pointer_revision=9,
        legacy_rates={model: legacy_rates} if legacy_rates else None,
    )
    assert result.cost == Decimal(expected)
    assert result.decision is None and "legacy_event" in result.reasons
