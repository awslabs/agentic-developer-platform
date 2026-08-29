"""
Tests for Budget Usage Tracker Lambda Handler.

Issue #234: Budget Usage Tracking Lambda
"""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from ._handler_loader import load_handler


def _has_psycopg2() -> bool:
    """Check if psycopg2 is installed."""
    try:
        import psycopg2  # noqa: F401

        return True
    except ImportError:
        return False


from pricing_fallback import (  # noqa: E402
    MODEL_PRICING,
    calculate_cost,
    get_model_pricing,
    resolve_model_id,
)

# Issue #4391: the Lambda's mirror of the root-principal helpers. `lambda/shared`
# is on sys.path via `._handler_loader` above, same as `pricing_fallback`.
from root_principal import (  # noqa: E402
    SERVICE_PRINCIPAL_PREFIX,
    unqualify_root_principal_id,
)


class TestResolveModelId:
    """Tests for cross-region inference profile model ID resolution."""

    def test_resolve_us_prefix(self):
        """Test resolving us. prefix."""
        model_id = "us.anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert resolve_model_id(model_id) == "anthropic.claude-3-5-sonnet-20241022-v2:0"

    def test_resolve_global_prefix(self):
        """Test resolving global. prefix."""
        model_id = "global.anthropic.claude-sonnet-4-20250514-v1:0"
        assert resolve_model_id(model_id) == "anthropic.claude-sonnet-4-20250514-v1:0"

    def test_resolve_eu_prefix(self):
        """Test resolving eu. prefix."""
        model_id = "eu.anthropic.claude-3-haiku-20240307-v1:0"
        assert resolve_model_id(model_id) == "anthropic.claude-3-haiku-20240307-v1:0"

    def test_resolve_apac_prefix(self):
        """Test resolving apac. prefix."""
        model_id = "apac.anthropic.claude-3-5-haiku-20241022-v1:0"
        assert resolve_model_id(model_id) == "anthropic.claude-3-5-haiku-20241022-v1:0"

    def test_no_prefix(self):
        """Test model ID without cross-region prefix."""
        model_id = "anthropic.claude-3-5-sonnet-20241022-v2:0"
        assert resolve_model_id(model_id) == model_id

    def test_amazon_model(self):
        """Test Amazon model ID."""
        model_id = "amazon.titan-text-express-v1"
        assert resolve_model_id(model_id) == model_id


class TestGetModelPricing:
    """Tests for pricing lookup."""

    def test_known_model(self):
        """Test getting pricing for a known model."""
        pricing = get_model_pricing("anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert pricing["input"] == Decimal("0.003")
        assert pricing["output"] == Decimal("0.015")

    def test_haiku_model(self):
        """Test getting pricing for Haiku model."""
        pricing = get_model_pricing("anthropic.claude-3-5-haiku-20241022-v1:0")
        assert pricing["input"] == Decimal("0.0008")
        assert pricing["output"] == Decimal("0.004")

    def test_unknown_model_returns_default(self):
        """Test that unknown models return default pricing."""
        pricing = get_model_pricing("unknown.model-v1")
        assert pricing == MODEL_PRICING["default"]

    def test_cross_region_model(self):
        """Test that cross-region models are resolved correctly."""
        pricing = get_model_pricing("us.anthropic.claude-3-5-sonnet-20241022-v2:0")
        assert pricing["input"] == Decimal("0.003")
        assert pricing["output"] == Decimal("0.015")


class TestCalculateCost:
    """Tests for cost calculation."""

    def test_calculate_cost_sonnet(self):
        """Test cost calculation for Sonnet model."""
        cost = calculate_cost(
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=1000,
            output_tokens=500,
        )
        # input: 1000 * 0.003 / 1000 = 0.003
        # output: 500 * 0.015 / 1000 = 0.0075
        # total: 0.0105
        assert cost == Decimal("0.0105")

    def test_calculate_cost_haiku(self):
        """Test cost calculation for Haiku model."""
        cost = calculate_cost(
            "anthropic.claude-3-5-haiku-20241022-v1:0",
            input_tokens=10000,
            output_tokens=2000,
        )
        # input: 10000 * 0.0008 / 1000 = 0.008
        # output: 2000 * 0.004 / 1000 = 0.008
        # total: 0.016
        assert cost == Decimal("0.016")

    def test_calculate_cost_cross_region(self):
        """Test cost calculation with cross-region model ID."""
        cost = calculate_cost(
            "us.anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=1000,
            output_tokens=500,
        )
        assert cost == Decimal("0.0105")

    def test_calculate_cost_zero_tokens(self):
        """Test cost calculation with zero tokens."""
        cost = calculate_cost(
            "anthropic.claude-3-5-sonnet-20241022-v2:0",
            input_tokens=0,
            output_tokens=0,
        )
        assert cost == Decimal("0")

    def test_calculate_cost_with_custom_pricing_table(self):
        """Test cost calculation with custom pricing table."""
        custom_pricing = {
            "custom-model": {
                "input": Decimal("0.01"),
                "output": Decimal("0.02"),
            }
        }
        cost = calculate_cost(
            "custom-model",
            input_tokens=1000,
            output_tokens=500,
            pricing_table=custom_pricing,
        )
        # input: 1000 * 0.01 / 1000 = 0.01
        # output: 500 * 0.02 / 1000 = 0.01
        # total: 0.02
        assert cost == Decimal("0.02")


class TestPeriodCalculation:
    """Tests for period start date calculation."""

    @pytest.mark.skipif(
        not _has_psycopg2(),
        reason="psycopg2 not installed (Lambda-only dependency)",
    )
    def test_import_handler(self):
        """Test that handler module can be imported."""
        # This tests the handler loads cleanly under its unique module name.
        get_period_starts = load_handler("budget-usage-tracker").get_period_starts

        # Test with a known date: Wednesday, February 26, 2026
        timestamp = datetime(2026, 2, 26, 12, 30, 0, tzinfo=UTC)
        periods = get_period_starts(timestamp)

        # Daily: same day
        assert periods["daily"].day == 26
        assert periods["daily"].month == 2
        assert periods["daily"].year == 2026

        # Weekly: Monday of that week (Feb 23, 2026)
        assert periods["weekly"].weekday() == 0  # Monday
        assert periods["weekly"].day == 23
        assert periods["weekly"].month == 2

        # Monthly: first of month
        assert periods["monthly"].day == 1
        assert periods["monthly"].month == 2
        assert periods["monthly"].year == 2026


@pytest.mark.skipif(
    not _has_psycopg2(),
    reason="psycopg2 not installed (Lambda-only dependency)",
)
class TestChatLogParsing:
    """Tests for chat log parsing."""

    def test_parse_valid_chat_log(self):
        """Test parsing a valid chat log."""
        parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

        chat_log = {
            "request_id": "test-123",
            "timestamp": "2026-02-26T12:38:54.589534Z",
            "org_id": "acme",
            "user_id": "94c8f418-90d1-701c-e93d-a65df61d91d9",
            "team_id": "platform-team",
            "model": "us.anthropic.claude-haiku-4-5-20251001-v1:0",
            "response": {
                "usage": {
                    "input_tokens": 353,
                    "output_tokens": 32,
                }
            },
        }

        result = parse_chat_log(chat_log)

        assert result is not None
        assert result["org_id"] == "acme"
        assert result["user_id"] == "94c8f418-90d1-701c-e93d-a65df61d91d9"
        assert result["team_id"] == "platform-team"
        assert result["model"] == "us.anthropic.claude-haiku-4-5-20251001-v1:0"
        assert result["input_tokens"] == 353
        assert result["output_tokens"] == 32

    def test_parse_chat_log_missing_required_field(self):
        """Test parsing chat log with missing required field."""
        parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

        chat_log = {
            "request_id": "test-123",
            "org_id": "acme",
            # Missing user_id and model
            "response": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is None

    def test_parse_chat_log_missing_usage(self):
        """Test parsing chat log with missing usage data."""
        parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

        chat_log = {
            "org_id": "acme",
            "user_id": "user-123",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {},  # No usage
        }

        result = parse_chat_log(chat_log)
        assert result is None

    def test_parse_chat_log_no_team_id(self):
        """Test parsing chat log without team_id."""
        parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

        chat_log = {
            "org_id": "acme",
            "user_id": "user-123",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is not None
        assert result["team_id"] is None

    def test_parse_chat_log_extracts_request_id(self):
        """Issue #1074: Test that request_id is extracted from chat log."""
        parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

        chat_log = {
            "request_id": "abc-123-def",
            "org_id": "acme",
            "user_id": "user-123",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is not None
        assert result["request_id"] == "abc-123-def"

    def test_parse_chat_log_missing_request_id_returns_none(self):
        """Issue #1074: request_id is optional, returns None if absent."""
        parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

        chat_log = {
            "org_id": "acme",
            "user_id": "user-123",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is not None
        assert result["request_id"] is None

    def test_parse_chat_log_extracts_cache_tokens(self):
        """Issue #1486: cache token fields are extracted from usage."""
        parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

        chat_log = {
            "org_id": "acme",
            "user_id": "user-123",
            "model": "us.anthropic.claude-opus-4-6-v1",
            "response": {
                "usage": {
                    "input_tokens": 1,
                    "output_tokens": 4000,
                    "cache_read_input_tokens": 65000,
                    "cache_creation_input_tokens": 5000,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is not None
        assert result["cache_read_input_tokens"] == 65000
        assert result["cache_creation_input_tokens"] == 5000

    def test_parse_chat_log_cache_tokens_default_zero(self):
        """Issue #1486: Missing cache fields default to 0."""
        parse_chat_log = load_handler("budget-usage-tracker").parse_chat_log

        chat_log = {
            "org_id": "acme",
            "user_id": "user-123",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 50,
                }
            },
        }

        result = parse_chat_log(chat_log)
        assert result is not None
        assert result["cache_read_input_tokens"] == 0
        assert result["cache_creation_input_tokens"] == 0


@pytest.mark.skipif(
    not _has_psycopg2(),
    reason="psycopg2 not installed (Lambda-only dependency)",
)
class TestBridgeCostToUsageLogs:
    """Issue #1074: Tests for the bridge_cost_to_usage_logs function."""

    def test_bridge_updates_row(self):
        """Test that bridge updates usage_logs when request_id matches."""
        from unittest.mock import MagicMock

        bridge_cost_to_usage_logs = load_handler("budget-usage-tracker").bridge_cost_to_usage_logs

        # Mock connection and cursor
        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = bridge_cost_to_usage_logs(mock_conn, "req-123", Decimal("0.0105"))

        assert result is True
        mock_cursor.execute.assert_called_once()
        sql_call = mock_cursor.execute.call_args
        assert "UPDATE usage_logs" in sql_call[0][0]
        # Issue #1616: params now include chat_log_s3_key (None when not provided)
        assert sql_call[0][1] == (0.0105, None, "req-123")

    def test_bridge_no_matching_row(self):
        """Test that bridge returns False when no matching row found."""
        from unittest.mock import MagicMock

        bridge_cost_to_usage_logs = load_handler("budget-usage-tracker").bridge_cost_to_usage_logs

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 0
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = bridge_cost_to_usage_logs(mock_conn, "nonexistent-req", Decimal("0.01"))

        assert result is False

    def test_bridge_handles_exception(self):
        """Test that bridge handles exceptions gracefully."""
        from unittest.mock import MagicMock

        bridge_cost_to_usage_logs = load_handler("budget-usage-tracker").bridge_cost_to_usage_logs

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(side_effect=Exception("DB connection lost"))
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = bridge_cost_to_usage_logs(mock_conn, "req-123", Decimal("0.01"))

        assert result is False

    def test_bridge_writes_chat_log_s3_key(self):
        """Issue #1616: Test that chat_log_s3_key is passed in the UPDATE."""
        from unittest.mock import MagicMock

        bridge_cost_to_usage_logs = load_handler("budget-usage-tracker").bridge_cost_to_usage_logs

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = bridge_cost_to_usage_logs(
            mock_conn,
            "req-456",
            Decimal("0.05"),
            chat_log_s3_key="acme/user-1/2026/06/19/req-456.json",
        )

        assert result is True
        sql_call = mock_cursor.execute.call_args
        assert "chat_log_s3_key" in sql_call[0][0]
        assert "COALESCE" in sql_call[0][0]
        # Params: (cost, s3_key, request_id)
        assert sql_call[0][1] == (0.05, "acme/user-1/2026/06/19/req-456.json", "req-456")

    def test_bridge_s3_key_none_when_not_provided(self):
        """Issue #1616: When chat_log_s3_key not provided, passes None."""
        from unittest.mock import MagicMock

        bridge_cost_to_usage_logs = load_handler("budget-usage-tracker").bridge_cost_to_usage_logs

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = 1
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = bridge_cost_to_usage_logs(mock_conn, "req-789", Decimal("0.03"))

        assert result is True
        sql_call = mock_cursor.execute.call_args
        # Params: (cost, None, request_id)
        assert sql_call[0][1] == (0.03, None, "req-789")


@pytest.mark.skipif(
    not _has_psycopg2(),
    reason="psycopg2 not installed (Lambda-only dependency)",
)
class TestTransactionIsolation:
    """Per-record transaction isolation for the shared batch connection.

    Incident 2026-07-08: budget_usage.total_tokens hit the int32 ceiling and
    every upsert raised NumericValueOutOfRange. Because the whole batch shared
    one transaction, the already-executed usage_logs cost bridge was rolled
    back too — Agent Activity showed $0 for every run. These tests pin the
    fix: bridge commits independently, and a failing record rolls back
    without poisoning the connection for the rest of the batch.
    """

    @staticmethod
    def _mock_conn(rowcount: int = 1):
        from unittest.mock import MagicMock

        mock_conn = MagicMock()
        mock_cursor = MagicMock()
        mock_cursor.rowcount = rowcount
        mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return mock_conn, mock_cursor

    @staticmethod
    def _chat_log(request_id: str = "req-1") -> dict:
        return {
            "org_id": "org-1",
            "user_id": "user-1",
            "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
            "response": {"usage": {"input_tokens": 100, "output_tokens": 50}},
            "timestamp": "2026-07-10T12:00:00Z",
            "request_id": request_id,
        }

    def test_bridge_rolls_back_on_exception(self):
        """A failed bridge statement must rollback so the connection isn't poisoned."""
        from unittest.mock import MagicMock

        bridge_cost_to_usage_logs = load_handler("budget-usage-tracker").bridge_cost_to_usage_logs

        mock_conn = MagicMock()
        mock_conn.cursor.return_value.__enter__ = MagicMock(side_effect=Exception("boom"))
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

        result = bridge_cost_to_usage_logs(mock_conn, "req-123", Decimal("0.01"))

        assert result is False
        mock_conn.rollback.assert_called_once()

    def test_process_chat_log_commits_bridge_before_upserts(self):
        """The cost bridge must be committed before any budget_usage upsert runs.

        If the commit happened after the upserts, an upsert failure would
        discard the bridged cost (the exact incident failure mode).
        """
        handler_mod = load_handler("budget-usage-tracker")
        mock_conn, mock_cursor = self._mock_conn()

        calls: list[str] = []
        mock_conn.commit.side_effect = lambda: calls.append("commit")
        original_execute = mock_cursor.execute

        def tracking_execute(sql, *args, **kwargs):
            if "UPDATE usage_logs" in sql:
                calls.append("bridge")
            elif "INSERT INTO budget_usage" in sql:
                calls.append("upsert")
            return original_execute(sql, *args, **kwargs)

        mock_cursor.execute = tracking_execute

        handler_mod.process_chat_log(mock_conn, self._chat_log(), handler_mod.MODEL_PRICING, chat_log_s3_key="k.json")

        assert "bridge" in calls and "upsert" in calls
        # A commit must sit between the bridge and the first upsert.
        assert calls.index("bridge") < calls.index("commit") < calls.index("upsert")

    def test_handler_isolates_failing_record(self):
        """One record failing mid-batch must not abort or roll back the others."""
        import json as json_mod
        from unittest.mock import MagicMock, patch

        handler_mod = load_handler("budget-usage-tracker")
        mock_conn, _ = self._mock_conn()

        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=mock_conn)
        cm.__exit__ = MagicMock(return_value=False)

        bodies = {
            "logs/a.json": json_mod.dumps(self._chat_log("req-a")),
            "logs/b.json": json_mod.dumps(self._chat_log("req-b")),
            "logs/c.json": json_mod.dumps(self._chat_log("req-c")),
        }

        def fake_get_object(Bucket, Key):  # noqa: N803 — boto3 kwarg names
            body = MagicMock()
            body.read.return_value = bodies[Key].encode()
            return {"Body": body}

        process_calls = []

        def fake_process(conn, chat_log, pricing_table, chat_log_s3_key=None):
            process_calls.append(chat_log_s3_key)
            if chat_log_s3_key == "logs/b.json":
                raise Exception("integer out of range")

        event = {"Records": [{"s3": {"bucket": {"name": "bkt"}, "object": {"key": k}}} for k in ["logs/a.json", "logs/b.json", "logs/c.json"]]}

        with (
            patch.object(handler_mod, "get_db_connection", return_value=cm),
            patch.object(handler_mod, "get_pricing_table", return_value=handler_mod.MODEL_PRICING),
            patch.object(handler_mod.s3_client, "get_object", side_effect=fake_get_object),
            patch.object(handler_mod, "process_chat_log", side_effect=fake_process),
        ):
            result = handler_mod.handler(event, None)

        body = json_mod.loads(result["body"])
        # All three attempted; the failure neither stopped the batch nor
        # counted the good records as errors.
        assert process_calls == ["logs/a.json", "logs/b.json", "logs/c.json"]
        assert body == {"processed": 2, "errors": 1}
        # Good records committed individually; the bad one rolled back.
        assert mock_conn.commit.call_count >= 2
        mock_conn.rollback.assert_called_once()


# =============================================================================
# Issue #4300: root-human attribution in the settled ledger
# =============================================================================


class _LedgerCursor:
    """A cursor that accumulates ``budget_usage`` rows the way Postgres would.

    The upsert is ``INSERT ... ON CONFLICT (org_id, entity_type, entity_id,
    period_start, period_type) DO UPDATE SET total_cost_usd = existing +
    EXCLUDED``. Counting ``execute`` calls cannot tell "wrote a third row" from
    "debited an existing row twice" — those are the two outcomes #4300 has to
    keep apart — so this replays the conflict key and sums per row instead.
    """

    def __init__(self):
        # (entity_type, entity_id, period_type) -> {"cost": Decimal, "tokens": int, "requests": int}
        self.rows: dict[tuple[str, str, str], dict] = {}
        self.rowcount = 1

    def execute(self, sql, params=None):
        if "INSERT INTO budget_usage" not in sql or params is None:
            return
        (_id, org_id, entity_type, entity_id, period_start, period_type, cost, tokens) = params
        key = (entity_type, entity_id, period_type)
        row = self.rows.setdefault(key, {"cost": Decimal("0"), "tokens": 0, "requests": 0, "org_id": org_id})
        row["cost"] += Decimal(str(cost))
        row["tokens"] += tokens
        row["requests"] += 1

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _LedgerConn:
    """Connection handing out a single shared :class:`_LedgerCursor`."""

    def __init__(self):
        self.cursor_obj = _LedgerCursor()
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _chat_log_4300(root_human_id=None, **overrides) -> dict:
    """A minimal valid chat log, optionally carrying a root-human attribution."""
    log = {
        "org_id": "org-acme",
        "user_id": "cognito-sub-of-the-agent-service-account",
        "model": "anthropic.claude-3-5-sonnet-20241022-v2:0",
        "response": {"usage": {"input_tokens": 1000, "output_tokens": 500}},
        "timestamp": "2026-08-20T12:00:00Z",
        "request_id": "req-4300",
    }
    if root_human_id is not None:
        log["root_human_id"] = root_human_id
    log.update(overrides)
    return log


def _entity_types(conn: _LedgerConn) -> set[str]:
    return {k[0] for k in conn.cursor_obj.rows}


class TestRootHumanEntityTypeContract:
    """T15: the writer's entity_type string must equal the reader's enum value.

    This is the highest-value test in the #4300 set. The Lambda is a separate
    deploy artifact and cannot import gateway ``src``, so the two halves of the
    settled ledger agree only by convention. If the writer emits one string and
    enforcement reads another, nothing raises and no log line appears — the cap
    simply reads an empty ledger and every request passes. That silent failure
    mode already happened once on the org line (writer ``"organization"`` vs.
    reader ``"org"``, fixed in #4322 — see
    ``TestOrganizationEntityTypeContract`` below), where it means budgets are not
    enforced at all.
    """

    def test_writer_constant_equals_reader_enum_value(self):
        from src.shared.schemas.budget import EntityType

        handler_mod = load_handler("budget-usage-tracker")

        assert handler_mod._ROOT_USER_ENTITY_TYPE == EntityType.ROOT_USER.value

    def test_rows_written_are_readable_by_the_enforcement_enum(self):
        """The end-to-end shape of the contract: the string that lands in the
        table is the string ``_get_entity_hierarchy`` will query with."""
        from src.shared.schemas.budget import EntityType

        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(conn, _chat_log_4300(root_human_id="users-id-alice"), MODEL_PRICING)

        written = {k[0] for k in conn.cursor_obj.rows if k[1] == "users-id-alice"}
        assert written == {EntityType.ROOT_USER.value}


# =============================================================================
# Issue #4322: the org line's writer/reader agreement
# =============================================================================


class TestOrganizationEntityTypeContract:
    """#4322: the org row's ``entity_type`` must be what enforcement queries.

    T15 generalized to the org line — and the org line is where the drift
    actually shipped. This Lambda wrote ``"organization"`` while
    ``_check_entity_budget`` has always filtered on
    ``EntityType.ORGANIZATION.value`` == ``"org"``, so the query matched nothing
    and ``current_spend`` was ``Decimal("0")`` on every request. Nothing raised,
    nothing logged: the org cap simply never enforced against accumulated spend.

    These are the tests that fail on pre-#4322 code.
    """

    def test_writer_constant_equals_reader_enum_value(self):
        """The constant, not the emitted row — this is the whole contract.

        Fails if someone re-spells the constant back to the longer word, even if
        no row is written in the failing test.
        """
        from src.shared.schemas.budget import EntityType

        handler_mod = load_handler("budget-usage-tracker")

        assert handler_mod._ORGANIZATION_ENTITY_TYPE == EntityType.ORGANIZATION.value

    def test_rows_written_are_readable_by_the_enforcement_enum(self):
        """The string that lands in the table is the string enforcement reads."""
        from src.shared.schemas.budget import EntityType

        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(conn, _chat_log_4300(), MODEL_PRICING)

        written = {k[0] for k in conn.cursor_obj.rows if k[1] == "org-acme"}
        assert written == {EntityType.ORGANIZATION.value}

    def test_the_stale_literal_is_never_written(self):
        """No row anywhere carries ``"organization"``.

        Explicit because the failure is one of ABSENCE: a row under the old
        spelling is invisible to the reader, so a test that only asserts the new
        row exists would still pass if both were written — and both being written
        is the double-count the migration exists to prevent.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(
            conn,
            _chat_log_4300(root_human_id="users-id-alice", team_id="team-7", account_type="service", agent_id="agent-9"),
            MODEL_PRICING,
        )

        assert "organization" not in _entity_types(conn)

    def test_org_row_still_holds_the_cost_exactly_once(self):
        """Relabelling must not change the arithmetic — one request, one debit.

        The rename is only correct if the org line still receives exactly
        ``cost``. A fix that emitted both spellings, or appended the org entity
        twice, would double the figure and deny the org at half its real cap.
        """
        from src.shared.schemas.budget import EntityType

        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        log = _chat_log_4300()
        handler_mod.process_chat_log(conn, log, MODEL_PRICING)

        expected = calculate_cost(resolve_model_id(log["model"]), 1000, 500, MODEL_PRICING)
        assert expected > 0  # a zero cost would make the assertion below vacuous
        row = conn.cursor_obj.rows[(EntityType.ORGANIZATION.value, "org-acme", "daily")]
        assert row["cost"] == expected
        assert row["requests"] == 1

    def test_org_row_is_written_for_each_period(self):
        """All three period rows move to the new spelling, not just the daily one."""
        from src.shared.schemas.budget import EntityType

        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(conn, _chat_log_4300(), MODEL_PRICING)

        periods = {k[2] for k in conn.cursor_obj.rows if k[0] == EntityType.ORGANIZATION.value}
        assert periods == {"daily", "weekly", "monthly"}

    def test_other_entity_lines_are_unaffected(self):
        """Regression: ``user``/``team``/``agent``/``root_user`` literals already
        agreed with the reader and must stay byte-identical (#4322 scope guard)."""
        from src.shared.schemas.budget import EntityType

        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(
            conn,
            _chat_log_4300(root_human_id="users-id-alice", team_id="team-7", account_type="service", agent_id="agent-9"),
            MODEL_PRICING,
        )

        assert _entity_types(conn) == {
            "user",
            "team",
            "agent",
            "root_user",
            EntityType.ORGANIZATION.value,
        }


class TestRootHumanLedgerRows:
    """T2: the root-human row is a THIRD row, not a second debit elsewhere."""

    def test_root_human_row_is_written_for_each_period(self):
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(conn, _chat_log_4300(root_human_id="users-id-alice"), MODEL_PRICING)

        periods = {k[2] for k in conn.cursor_obj.rows if k[0] == "root_user"}
        assert periods == {"daily", "weekly", "monthly"}

    def test_root_human_row_holds_the_cost_exactly_once(self):
        """The root-human line must equal one request's cost, not two.

        A naive implementation that reused the ``user`` entity_type — or appended
        the same entity twice — would double the figure and deny the human at
        half their real cap.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        log = _chat_log_4300(root_human_id="users-id-alice")
        handler_mod.process_chat_log(conn, log, MODEL_PRICING)

        expected = calculate_cost(
            resolve_model_id(log["model"]),
            1000,
            500,
            MODEL_PRICING,
        )
        assert expected > 0  # a zero cost would make the assertion below vacuous
        row = conn.cursor_obj.rows[("root_user", "users-id-alice", "daily")]
        assert row["cost"] == expected
        assert row["requests"] == 1

    def test_org_row_is_not_double_debited_by_the_new_entity(self):
        """The org line must be unchanged by #4300 — same cost as before.

        Compared against a run of the *same* chat log with no attribution, so
        this fails if the new entity ever debits an existing row instead of
        adding its own.
        """
        handler_mod = load_handler("budget-usage-tracker")

        without = _LedgerConn()
        handler_mod.process_chat_log(without, _chat_log_4300(), MODEL_PRICING)

        with_root = _LedgerConn()
        handler_mod.process_chat_log(with_root, _chat_log_4300(root_human_id="users-id-alice"), MODEL_PRICING)

        # #4322 relabelled the org line "organization" -> "org"; the entity id it
        # is keyed on is unchanged, and so is the invariant under test here.
        for entity, entity_id in (("org", "org-acme"), ("user", "cognito-sub-of-the-agent-service-account")):
            key = (entity, entity_id, "daily")
            assert with_root.cursor_obj.rows[key]["cost"] == without.cursor_obj.rows[key]["cost"]
            assert with_root.cursor_obj.rows[key]["requests"] == without.cursor_obj.rows[key]["requests"] == 1

    def test_sub_agent_fan_out_accumulates_on_one_human_line(self):
        """Six sub-agents on six service accounts, one shared human.

        This is the ledger half of the feature: each hop writes its own ``user``
        row, but every hop adds to the single ``root_user`` line, so the human's
        settled floor grows with the whole chain.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        for i in range(6):
            handler_mod.process_chat_log(
                conn,
                _chat_log_4300(root_human_id="users-id-alice", user_id=f"cognito-sub-agent-{i}", request_id=f"req-{i}"),
                MODEL_PRICING,
            )

        one = calculate_cost(resolve_model_id(_chat_log_4300()["model"]), 1000, 500, MODEL_PRICING)
        human = conn.cursor_obj.rows[("root_user", "users-id-alice", "daily")]
        assert human["requests"] == 6
        assert human["cost"] == one * 6
        # Each hop's own identity carries only its own hop.
        for i in range(6):
            assert conn.cursor_obj.rows[("user", f"cognito-sub-agent-{i}", "daily")]["requests"] == 1


class TestRootHumanAbsent:
    """T18/T19: absence must be inert, and must never degrade metering."""

    def test_chat_log_without_the_field_still_records_usage(self):
        """Back-compat: every log written before #4300 shipped, and every
        non-human-rooted request, has no ``root_human_id`` at all.

        If the field had joined ``required_fields``, ``parse_chat_log`` would
        return ``None`` for these and the Lambda would stop recording ALL budget
        usage for them — an attribution gap would become a metering outage.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(conn, _chat_log_4300(), MODEL_PRICING)

        # "org", not "organization", since #4322.
        assert _entity_types(conn) == {"user", "org"}

    def test_parse_chat_log_without_the_field_is_still_valid(self):
        handler_mod = load_handler("budget-usage-tracker")

        parsed = handler_mod.parse_chat_log(_chat_log_4300())

        assert parsed is not None
        assert parsed["root_human_id"] is None

    @pytest.mark.parametrize("empty", ["", None])
    def test_empty_attribution_writes_no_row(self, empty):
        """An empty value must add NO row.

        A row keyed on ``""`` would collapse every non-human-rooted request in
        the tenant into one shared bogus ledger line — and the first tenant to
        exceed it would deny everyone.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(conn, _chat_log_4300(root_human_id=empty), MODEL_PRICING)

        assert "root_user" not in _entity_types(conn)
        assert not [k for k in conn.cursor_obj.rows if k[1] == ""]

    def test_attribution_is_independent_of_the_agent_entity(self):
        """A human-rooted IAM agent request writes both lines, not one or the
        other: ``agent`` for the machine, ``root_user`` for the person."""
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(
            conn,
            _chat_log_4300(root_human_id="users-id-alice", account_type="service", agent_id="agent-uuid-7"),
            MODEL_PRICING,
        )

        assert {"agent", "root_user"} <= _entity_types(conn)
        assert conn.cursor_obj.rows[("agent", "agent-uuid-7", "daily")]["requests"] == 1
        assert conn.cursor_obj.rows[("root_user", "users-id-alice", "daily")]["requests"] == 1


# =============================================================================
# Issue #4391: the write path must skip root_user when the root IS the caller
# =============================================================================


_SERVICE_ROOTED_KEY = "sched-key"


class TestRootIsCallerWritesNoRootUserRow:
    """#4391: the equality skip, mirroring `enforcement_service.py:437`.

    Enforcement has always skipped the ROOT_USER entity when the root principal
    is the caller; the tracker was presence-gated only, so the same dollar
    settled on both the ``user`` and the ``root_user`` line — distinct rows under
    ``uq_budget_usage``, x3 period types. Nothing raised: enforcement never read
    the line, so no cap moved, and only the spend dashboard (#4324) summed the
    inflated figure.

    Both tests in this class fail on the pre-#4391 handler.
    """

    def test_service_rooted_run_writes_no_root_user_row(self):
        """The headline case: `user_id="k"`, `root_human_id="service:k"`.

        #4344 qualifies the root id at publication, so the two values are not
        byte-equal and a verbatim comparison would not catch this — the skip has
        to unqualify first.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(
            conn,
            _chat_log_4300(root_human_id=f"service:{_SERVICE_ROOTED_KEY}", user_id=_SERVICE_ROOTED_KEY),
            MODEL_PRICING,
        )

        assert "root_user" not in _entity_types(conn)

    def test_direct_human_caller_writes_no_root_user_row(self):
        """The case the issue title omits: caller is their own root, both bare.

        `enforcement_service.py:420-429` documents this as one of the two cases
        its guard actually fires for, so the write path must cover it too.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(
            conn,
            _chat_log_4300(root_human_id="users-id-alice", user_id="users-id-alice"),
            MODEL_PRICING,
        )

        assert "root_user" not in _entity_types(conn)

    def test_service_rooted_spend_is_settled_exactly_once(self):
        """The defect stated as money: one request, one dollar on one line.

        Asserts the `user` and `org` lines each carry exactly `cost` once, which
        is what the pre-fix double-write inflated when the dashboard summed
        `user` + `root_user` for the same principal.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        log = _chat_log_4300(root_human_id=f"service:{_SERVICE_ROOTED_KEY}", user_id=_SERVICE_ROOTED_KEY)
        handler_mod.process_chat_log(conn, log, MODEL_PRICING)

        expected = calculate_cost(resolve_model_id(log["model"]), 1000, 500, MODEL_PRICING)
        assert expected > 0  # a zero cost would make the assertions below vacuous

        # The principal's total across every row naming it is ONE cost, not two.
        settled = sum(
            row["cost"]
            for key, row in conn.cursor_obj.rows.items()
            if key[2] == "daily" and unqualify_root_principal_id(key[1]) == _SERVICE_ROOTED_KEY
        )
        assert settled == expected
        assert conn.cursor_obj.rows[("org", "org-acme", "daily")]["cost"] == expected

    def test_row_count_is_entities_times_period_types(self):
        """Catches an accidental skip of the WRONG entity.

        Service-rooted with a team: user + org + team = 3 entities, no root_user,
        x3 period types = 9 rows. A fix that dropped the user or org line instead
        would still satisfy the "no root_user" assertions above.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(
            conn,
            _chat_log_4300(
                root_human_id=f"service:{_SERVICE_ROOTED_KEY}",
                user_id=_SERVICE_ROOTED_KEY,
                team_id="team-7",
            ),
            MODEL_PRICING,
        )

        assert _entity_types(conn) == {"user", "org", "team"}
        assert len(conn.cursor_obj.rows) == 3 * 3


class TestRootIsNotCallerStillWritesRootUserRow:
    """#4300 must survive #4391: the hosted agent run keeps its attribution.

    This is the case the whole root_user envelope exists for, and the case where
    the new skip must be inert. If this regresses, per-human budgets silently
    stop accumulating across an agent chain.
    """

    def test_agent_worker_with_distinct_human_root_writes_the_row(self):
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(
            conn,
            _chat_log_4300(root_human_id="users-id-alice", user_id="worker-sa"),
            MODEL_PRICING,
        )

        assert conn.cursor_obj.rows[("root_user", "users-id-alice", "daily")]["requests"] == 1

    def test_row_is_keyed_on_the_qualified_id_not_the_unqualified_one(self):
        """The comparison unqualifies; the KEY must not.

        The qualified id is what enforcement reads and what the ledger has
        settled under since #4344. A fix that wrote the stripped id would move
        every service-rooted-but-distinct-caller row to a new key and desync the
        two sides again — the #4322 failure mode, in the other direction.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(
            conn,
            _chat_log_4300(root_human_id="service:root-key", user_id="a-different-caller"),
            MODEL_PRICING,
        )

        assert ("root_user", "service:root-key", "daily") in conn.cursor_obj.rows
        assert ("root_user", "root-key", "daily") not in conn.cursor_obj.rows

    def test_id_merely_containing_a_colon_is_not_mangled(self):
        """Only an exact leading `service:` is stripped for the comparison.

        An id containing a colon elsewhere must compare as-is, or an unrelated
        principal could be mistaken for the caller and lose its row.
        """
        handler_mod = load_handler("budget-usage-tracker")
        conn = _LedgerConn()

        handler_mod.process_chat_log(
            conn,
            _chat_log_4300(root_human_id="tenant:alice", user_id="alice"),
            MODEL_PRICING,
        )

        assert conn.cursor_obj.rows[("root_user", "tenant:alice", "daily")]["requests"] == 1


class TestRootPrincipalHelperParity:
    """T17: the Lambda's copy of the helper must not drift from `src`.

    Same reasoning as T15/T16: the Lambda is a separate deploy artifact and
    cannot import gateway `src`, so the two sides of the equality skip agree only
    by convention. A one-sided edit has no compile-time consequence and its
    runtime symptom is silent — the ledger quietly returns to double-counting.
    `src` is authoritative; this test makes drift a CI failure.
    """

    def test_prefix_matches_the_authoritative_one(self):
        from src.budget import enforcement_service

        assert SERVICE_PRINCIPAL_PREFIX == enforcement_service._SERVICE_PRINCIPAL_PREFIX

    @pytest.mark.parametrize(
        "entity_id",
        [
            "users-id-alice",  # bare human id
            "service:sched-key",  # service-qualified
            "",  # empty stays empty
            "tenant:alice",  # contains a colon, but not the prefix
            "service:service:doubled",  # only one layer is stripped
            "SERVICE:upper",  # prefix match is case-sensitive
        ],
    )
    def test_unqualify_matches_the_authoritative_implementation(self, entity_id):
        from src.budget import enforcement_service

        assert unqualify_root_principal_id(entity_id) == enforcement_service._unqualify_root_principal_id(entity_id)

    def test_handler_uses_the_shared_helper(self):
        """Pins the sharing mechanism, not just the behaviour.

        A handler that re-implemented the prefix logic inline would pass every
        test above while being exactly the drift risk T17 exists to prevent.
        """
        handler_mod = load_handler("budget-usage-tracker")

        assert handler_mod.unqualify_root_principal_id is unqualify_root_principal_id
