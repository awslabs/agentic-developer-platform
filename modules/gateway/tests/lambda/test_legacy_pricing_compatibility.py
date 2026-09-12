"""Legacy replay pricing is pinned; non-OpenAI DB base rates retain cache policy."""

import importlib
from dataclasses import replace
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from pricing_policy import load_snapshot

from ._handler_loader import load_handler
from .test_pricing_settlement_contract import event

handler = load_handler("budget-usage-tracker")
legacy_reader = importlib.import_module("pricing_legacy_reader")
settlement = importlib.import_module("pricing_settlement")


@pytest.fixture(autouse=True)
def reset_reader():
    legacy_reader.reset_for_tests()
    yield
    legacy_reader.reset_for_tests()


def test_legacy_openai_replays_ignore_changed_active_rates_and_current_snapshot():
    pinned = load_snapshot()
    log = event("openai.gpt-oss-120b", input_tokens=1000, output_tokens=1000, cache_read_input_tokens=0, cache_creation_input_tokens=0)
    parsed = handler.parse_chat_log(log)
    before = settlement.settle_chat_log(parsed, chat_log=log, rows=pinned.rates, snapshot=pinned, generation_id=7, pointer_revision=9)
    dearer = tuple(
        replace(row, input_price_per_1k_tokens=row.input_price_per_1k_tokens * 2, output_price_per_1k_tokens=row.output_price_per_1k_tokens * 2)
        for row in pinned.rates
    )
    changed_snapshot = replace(pinned, snapshot_version="2099-01-01.1", rates=dearer)
    after = settlement.settle_chat_log(
        parsed,
        chat_log=log,
        rows=dearer,
        snapshot=changed_snapshot,
        generation_id=100,
        pointer_revision=101,
        source_reasons=("cache_refresh_failing",),
    )
    assert before.cost == after.cost == Decimal("0.001313")
    assert before.decision == after.decision
    assert after.decision["snapshot_version"] == "2026-09-12.1"
    assert after.decision["generation_id"] is None and after.decision["pointer_revision"] is None
    assert after.decision["source_kind"] == "bundled_snapshot"
    assert "legacy_event" in after.reasons and "bootstrap_fallback" in after.reasons


def test_non_openai_db_base_prices_keep_explicit_curated_cache_rates(monkeypatch):
    model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    log = event(f"us.{model}", "anthropic", input_tokens=1000, output_tokens=500, cache_read_input_tokens=200, cache_creation_input_tokens=400)
    monkeypatch.setattr(handler, "get_legacy_rates", lambda conn: {model: {"input": Decimal("0.009"), "output": Decimal("0.045")}})
    writes = MagicMock()
    monkeypatch.setattr(handler, "upsert_budget_usage", writes)
    monkeypatch.setattr(handler, "bridge_cost_to_usage_logs", MagicMock())
    from pricing_policy.storage import V2RateCache

    state = V2RateCache().state(monotonic=0, now_iso="2026-09-12T06:00:00+00:00")
    handler.process_chat_log(MagicMock(), log, state)
    # U=1000*9/M + O=500*45/M + C=200*.3/M + W=400*3.75/M.
    # Deriving cache rates from overridden input would incorrectly charge .03618.
    assert writes.call_count == 6
    assert all(call.args[6] == Decimal("0.033060") and call.args[7] == 2100 for call in writes.call_args_list)


class MissingSchemaError(Exception):
    pgcode = "42P01"


class Cursor:
    def __init__(self, rows=(), error=None):
        self.rows = rows
        self.error = error
        self.commands = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.commands.append((sql, params))
        if "FROM model_pricing" in sql and self.error:
            raise self.error

    def fetchone(self):
        return ("17s",)

    def fetchall(self):
        return self.rows


def connection(cursor):
    return MagicMock(cursor=MagicMock(return_value=cursor))


def test_missing_schema_rolls_back_probe_without_committing_or_rolling_back_outer_transaction(monkeypatch):
    monkeypatch.setattr(legacy_reader.time, "monotonic", lambda: 100)
    cur = Cursor(error=MissingSchemaError("missing legacy table"))
    conn = connection(cur)
    assert legacy_reader.get_legacy_rates(conn) == {}
    sql = [statement for statement, _ in cur.commands]
    assert sql[-2:] == ["ROLLBACK TO SAVEPOINT pricing_legacy_probe", "RELEASE SAVEPOINT pricing_legacy_probe"]
    conn.commit.assert_not_called()
    conn.rollback.assert_not_called()
    count = len(sql)
    legacy_reader.get_legacy_rates(conn)
    assert len(cur.commands) == count  # Cached schema gap, not a per-event query.


def test_successful_empty_table_is_cached_and_timeout_is_restored(monkeypatch):
    monkeypatch.setattr(legacy_reader.time, "monotonic", lambda: 100)
    cur = Cursor()
    conn = connection(cur)
    assert legacy_reader.get_legacy_rates(conn) == {}
    assert ("SELECT set_config('statement_timeout', %s, true)", ("17s",)) in cur.commands
    before = len(cur.commands)
    legacy_reader.get_legacy_rates(conn)
    assert len(cur.commands) == before


def test_failures_retain_good_overrides_and_openai_rows_are_excluded(monkeypatch):
    monkeypatch.setattr(legacy_reader.time, "monotonic", lambda: 100)
    model = "anthropic.claude-3-5-sonnet-20241022-v2:0"
    good = Cursor(rows=[(f"us.{model}", Decimal(".009"), Decimal(".045")), ("openai.gpt-5.6-sol", Decimal(".1"), Decimal(".1"))])
    rates = legacy_reader.get_legacy_rates(connection(good))
    assert rates[model]["input"] == Decimal(".009")
    assert not any(key.startswith("openai.") for key in rates)
    assert legacy_reader.get_legacy_rates(connection(Cursor(error=RuntimeError("DB unavailable"))), force=True) == rates
