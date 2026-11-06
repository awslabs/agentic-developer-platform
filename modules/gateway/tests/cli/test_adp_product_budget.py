"""Recorded Project budget responses through the Product CLI."""

import base64
import importlib.util
import io
import json
import subprocess
import urllib.error
from pathlib import Path
from unittest.mock import Mock, call

import pytest

SCRIPT = Path(__file__).parents[2] / "cli/adp-product.py"
spec = importlib.util.spec_from_file_location("adp_product_budget", SCRIPT)
product = importlib.util.module_from_spec(spec)
spec.loader.exec_module(product)


@pytest.fixture(autouse=True)
def laptop_environment(monkeypatch):
    for key in ("ADP_CREW_TOOLS_URL", "ADP_CREW_TOOLS_TOKEN", "ADP_CREW_RUN_ID", "SIGV4_PROXY_TARGET"):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def budget_response():
    return {
        "product_id": "product-1",
        "project_id": "project-1",
        "project_name": "Example Project",
        "budget_usd": None,
        "spent_usd": "12.35",
        "reserved_usd": "1.50",
        "available_usd": None,
        "accounting_status": "pending",
        "flows": [{"flow_id": "flow-1", "flow_name": "Draft", "spent_usd": "12.35", "run_count": 2}],
    }


def show_args(*extra):
    return product.parser().parse_args(["budget", "show", "--product", "product-1", "--project", "project-1", *extra])


def test_show_json_preserves_null_and_rollup(budget_response, capsys):
    client = Mock()
    client.request.return_value = budget_response
    result = product.execute(show_args("--json"), client)

    assert product.emit_budget(result, True) == 0
    detail = json.loads(capsys.readouterr().out)["detail"]
    assert detail["budget_usd"] is None
    assert detail["cap_status"] == "none_metered"
    assert detail["spent_usd"] == "12.35"
    assert detail["reserved_usd"] == "1.50"
    assert detail["available_usd"] is None
    assert detail["accounting_status"] == "pending"
    assert detail["flows"] == budget_response["flows"]
    client.request.assert_called_once_with("GET", "/v1/projects/project-1/budget")


def test_show_human_explains_uncapped_metering(budget_response, capsys):
    client = Mock()
    client.request.return_value = budget_response
    assert product.emit_budget(product.execute(show_args(), client), False) == 0
    output = capsys.readouterr().out
    assert "no cap, metered" in output
    assert "spent_usd: 12.35" in output
    assert "reserved_usd: 1.50" in output
    assert "accounting_status: pending" in output
    assert "Draft: 12.35 USD (2 runs)" in output


def test_show_capped_headroom(budget_response, capsys):
    client = Mock()
    client.request.return_value = {**budget_response, "budget_usd": "250.00", "available_usd": "236.15"}
    result = product.execute(show_args(), client)
    assert result["detail"]["cap_status"] == "capped"
    product.emit_budget(result, False)
    assert "available_usd: 236.15" in capsys.readouterr().out


def test_show_rejects_wrong_product(budget_response):
    client = Mock()
    client.request.return_value = {**budget_response, "product_id": "other-product"}
    with pytest.raises(product.common.CliError, match="does not match"):
        product.execute(show_args(), client)


def test_set_sends_exact_decimal_body_and_reports_new_cap(budget_response, capsys):
    client = Mock()
    client.request.return_value = {**budget_response, "budget_usd": "250.00", "available_usd": "236.15"}
    args = product.parser().parse_args(
        [
            "budget",
            "set",
            "--product",
            "product-1",
            "--project",
            "project-1",
            "--budget-usd",
            "250.00",
            "--reason",
            "Approved limit",
            "--json",
        ]
    )

    assert product.emit_budget(product.execute(args, client), True) == 0
    assert json.loads(capsys.readouterr().out)["detail"]["budget_usd"] == "250.00"
    assert client.request.call_args_list == [
        call("GET", "/v1/projects/project-1/budget"),
        call(
            "PUT",
            "/v1/projects/project-1/budget",
            {"budget_usd": "250.00"},
            extra_headers={"X-ADP-Budget-Reason-Base64": base64.b64encode(b"Approved limit").decode("ascii")},
        ),
    ]


@pytest.mark.parametrize("refused_method", ["GET", "PUT"])
def test_set_403_reports_route_message_without_retry(monkeypatch, capsys, budget_response, refused_method):
    client = object.__new__(product.common.Api)
    client.binding = None
    client.base = "https://example.invalid"
    client.opener = Mock()
    client.last_etag = None
    message = "The Product owner or a platform administrator can manage this budget."
    refusal = urllib.error.HTTPError(
        client.base,
        403,
        "Forbidden",
        {},
        io.BytesIO(json.dumps({"detail": {"code": "project_budget_forbidden", "message": message}}).encode()),
    )
    preflight = io.BytesIO(json.dumps(budget_response).encode())
    preflight.headers = {}
    client.opener.open.side_effect = [preflight, refusal] if refused_method == "PUT" else [refusal]
    monkeypatch.setattr(product.common, "Api", lambda: client)
    monkeypatch.setattr(product.common, "access_token", lambda: "fixture-token")

    assert (
        product.main(
            [
                "budget",
                "set",
                "--product",
                "product-1",
                "--project",
                "project-1",
                "--budget-usd",
                "250.00",
                "--reason",
                "Approved limit",
                "--json",
            ]
        )
        == 3
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert error == {"code": "project_budget_forbidden", "message": message}
    assert [invocation.args[0].method for invocation in client.opener.open.call_args_list] == (["GET", "PUT"] if refused_method == "PUT" else ["GET"])


def test_set_still_checks_write_response_selection(budget_response):
    client = Mock()
    client.request.side_effect = [budget_response, {**budget_response, "project_id": "other-project"}]
    args = product.parser().parse_args(
        [
            "budget",
            "set",
            "--product",
            "product-1",
            "--project",
            "project-1",
            "--budget-usd",
            "250.00",
            "--reason",
            "Approved limit",
        ]
    )
    with pytest.raises(product.common.CliError, match="does not match"):
        product.execute(args, client)
    assert [invocation.args[0] for invocation in client.request.call_args_list] == ["GET", "PUT"]


def test_unknown_budget_outcome_names_supported_recovery_command(monkeypatch, budget_response, capsys):
    client = Mock()
    client.request.side_effect = [budget_response, product.common.CliError("Connection lost", "unknown_mutation_outcome", 4)]
    monkeypatch.setattr(product.common, "Api", lambda: client)
    assert (
        product.main(
            [
                "budget",
                "set",
                "--product",
                "product-1",
                "--project",
                "project-1",
                "--budget-usd",
                "250.00",
                "--reason",
                "Approved limit",
                "--json",
            ]
        )
        == 4
    )
    error = json.loads(capsys.readouterr().out)["error"]
    assert error["code"] == "unknown_mutation_outcome"
    assert "adp product budget show" in error["message"]
    assert "--idempotency-key" not in error["message"]
    assert "--revision" not in error["message"]
    assert client.request.call_count == 2


@pytest.mark.parametrize("reason", ["", "   ", "x" * 501])
def test_invalid_reason_never_sends_request(monkeypatch, capsys, reason):
    client = Mock()
    monkeypatch.setattr(product.common, "Api", lambda: client)
    assert (
        product.main(
            [
                "budget",
                "set",
                "--product",
                "product-1",
                "--project",
                "project-1",
                "--budget-usd",
                "250.00",
                "--reason",
                reason,
                "--json",
            ]
        )
        == 2
    )
    assert json.loads(capsys.readouterr().out)["error"]["code"] == "usage_error"
    client.request.assert_not_called()


@pytest.mark.parametrize("binding", ["SIGV4_PROXY_TARGET"])
def test_set_refuses_crew_binding_before_any_request(monkeypatch, capsys, binding):
    """#9640: a Dev Box owner session may set budgets (the owner's authority); an agent run behind the SigV4 proxy may not."""
    client = Mock()
    monkeypatch.setattr(product.common, "Api", lambda: client)
    monkeypatch.setenv(binding, "host-bound")

    assert (
        product.main(
            [
                "budget",
                "set",
                "--product",
                "product-1",
                "--project",
                "project-1",
                "--budget-usd",
                "250.00",
                "--reason",
                "Approved limit",
                "--json",
            ]
        )
        == 2
    )
    assert "not available to agent runs" in json.loads(capsys.readouterr().out)["error"]["message"]
    client.request.assert_not_called()


def test_show_remains_available_with_crew_binding(monkeypatch, budget_response):
    monkeypatch.setenv("ADP_CREW_TOOLS_URL", "host-bound")
    client = Mock()
    client.request.return_value = budget_response
    assert product.execute(show_args(), client)["detail"]["cap_status"] == "none_metered"
    client.request.assert_called_once_with("GET", "/v1/projects/project-1/budget")


@pytest.mark.parametrize("invalid", ["0", "0.00", "0.001", "250.001", "250", "-1.00", "100000000.00", "NaN", "Infinity", "1e2", "1.2"])
def test_invalid_budget_never_sends_request(monkeypatch, capsys, invalid):
    client = Mock()
    monkeypatch.setattr(product.common, "Api", lambda: client)

    assert (
        product.main(
            [
                "budget",
                "set",
                "--product",
                "product-1",
                "--project",
                "project-1",
                "--budget-usd",
                invalid,
                "--reason",
                "Approved limit",
                "--json",
            ]
        )
        == 2
    )
    assert "two decimal places" in json.loads(capsys.readouterr().out)["error"]["message"]
    client.request.assert_not_called()


def test_maximum_allowed_budget_is_not_rounded(budget_response):
    client = Mock()
    client.request.return_value = {**budget_response, "budget_usd": "99999999.99"}
    args = product.parser().parse_args(
        [
            "budget",
            "set",
            "--product",
            "product-1",
            "--project",
            "project-1",
            "--budget-usd",
            "99999999.99",
            "--reason",
            "Approved limit",
        ]
    )
    product.execute(args, client)
    assert client.request.call_args.args[2] == {"budget_usd": "99999999.99"}


def test_launcher_help_advertises_budget_commands():
    result = subprocess.run(["bash", str(SCRIPT.with_name("adp")), "help"], capture_output=True, text=True, check=True)
    assert "product list|create|show|update|budget show|budget set" in result.stdout
