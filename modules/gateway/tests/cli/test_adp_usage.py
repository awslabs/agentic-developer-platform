"""Usage CLI serializer boundaries, continuation and safe export."""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

CLI = Path(__file__).parents[2] / "cli"
sys.path.insert(0, str(CLI))
spec = importlib.util.spec_from_file_location("usage_cli", CLI / "adp-usage.py")
usage = importlib.util.module_from_spec(spec)
spec.loader.exec_module(usage)
START, END = "2026-09-25T00:00:00Z", "2026-09-26T00:00:00Z"


def args(*words):
    return usage.parser().parse_args([*words, "--start", START, "--end", END])


def page(*, complete=True, cursor=None, amount="0.123456", status="estimated"):
    return {
        "scope": {"kind": "own", "org_id": "org", "user_id": "owner"},
        "start": START,
        "end": END,
        "items": [{"id": "one", "model": "model", "cost": {"status": status, "currency": "USD", "amount": amount, "settlement": "unknown"}}],
        "complete": complete,
        "next_cursor": cursor,
    }


def test_exact_own_route_and_bounded_continuation():
    client = Mock()
    second = page()
    second["items"][0]["id"] = "two"
    client.request.side_effect = [page(complete=False, cursor="next"), second]
    result = usage.execute(args("usage", "requests", "--max-pages", "2"), client)
    assert len(result["detail"]["items"]) == 2
    assert result["detail"]["complete"] is True
    method, path = client.request.call_args.args
    assert method == "GET" and path.startswith("/usage/me/requests?")
    assert "cursor=next" in path
    assert "org_id" not in path and "user_id" not in path


def test_managed_org_is_encoded_and_response_checked():
    client = Mock()
    data = page()
    data["scope"] = {"kind": "managed", "org_id": "org-a"}
    client.request.return_value = data
    usage.execute(args("admin", "usage", "requests", "--org", "org-a"), client)
    assert client.request.call_args.args[1].startswith("/usage/managed/org-a/requests?")
    data["scope"]["org_id"] = "foreign"
    with pytest.raises(usage.common.CliError, match="scope mismatch"):
        usage.execute(args("admin", "usage", "requests", "--org", "org-a"), client)


def test_unknown_zero_is_preserved():
    client = Mock()
    client.request.return_value = page(amount=None, status="unknown")
    result = usage.execute(args("usage", "summary"), client)
    assert result["detail"]["items"][0]["cost"]["amount"] is None


@pytest.mark.parametrize("amount", [0.123456, "NaN", "Infinity", "-1", []])
def test_malformed_decimal_refused(amount):
    with pytest.raises(usage.common.CliError):
        usage.checked(page(amount=amount))


def test_limited_export_is_pending_with_resumable_cursor(capsys):
    client = Mock()
    client.request.return_value = page(complete=False, cursor="next")
    result = usage.execute(args("logs", "export"), client)
    assert usage.emit_export(result, "ndjson") == 4
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[0]["type"] == "record"
    assert records[-1]["detail"]["next_cursor"] == "next"
    assert records[-1]["detail"]["scope"]["kind"] == "own"


@pytest.mark.parametrize("text", ["=CMD()", "+SUM(A1)", "-1+2", "@formula", " \t=1", "\nvalue"])
def test_csv_formula_injection_escaped(text):
    assert usage.csv_cell(text).startswith("'")


def test_csv_metadata_on_stderr_not_mixed_into_rows(capsys):
    result = usage.common.envelope("ok", "adp logs export", page())
    result["detail"]["items"][0]["model"] = "=CMD()"
    assert usage.emit_export(result, "csv") == 0
    captured = capsys.readouterr()
    assert "'=CMD()" in captured.out
    assert json.loads(captured.err)["detail"]["complete"] is True


def test_missing_and_foreign_request_are_same_unavailable():
    client = Mock()
    data = page()
    data["items"] = []
    client.request.return_value = data
    result = usage.execute(args("usage", "request", "missing-id"), client)
    assert result["status"] == "unavailable"
    assert "inaccessible" in result["next_action"]


@pytest.mark.parametrize("start,end", [("2026-09-25", END), (END, START), (START, "2027-09-26T00:00:00Z")])
def test_invalid_range_rejected_before_api(start, end):
    options = args("usage", "summary")
    options.start, options.end = start, end
    client = Mock()
    with pytest.raises(usage.common.CliError):
        usage.execute(options, client)
    client.request.assert_not_called()


def test_bad_flags_stable_json(capsys):
    assert usage.main(["usage", "summary", "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage_error"


def test_real_usage_response_serializer_roundtrip():
    from datetime import UTC, datetime

    from src.usage.cli_reads import UsageReadResponse, retention_metadata

    value = UsageReadResponse(
        scope={"kind": "own", "org_id": "org", "user_id": "owner"},
        start=datetime(2026, 9, 25, tzinfo=UTC),
        end=datetime(2026, 9, 26, tzinfo=UTC),
        items=[],
        complete=True,
        **retention_metadata(datetime(2026, 9, 25, tzinfo=UTC), datetime(2026, 9, 26, tzinfo=UTC)),
    )
    assert usage.checked(json.loads(value.model_dump_json()))["items"] == []


def test_partial_http_read_fails_without_exporting_empty_success(monkeypatch, capsys):
    import http.client

    client = Mock()
    client.request.side_effect = http.client.IncompleteRead(b'{"items":[')
    monkeypatch.setattr(usage.common, "Api", lambda: client)
    code = usage.main(["logs", "export", "--start", START, "--end", END, "--json"])
    assert code != 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "failed"
    assert "items" not in result["detail"]


def test_duplicate_record_across_pages_is_not_success():
    client = Mock()
    client.request.side_effect = [page(complete=False, cursor="next"), page()]
    with pytest.raises(usage.common.CliError, match="repeated usage record"):
        usage.execute(args("logs", "export", "--max-pages", "2"), client)
