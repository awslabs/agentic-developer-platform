"""Tool readiness is a scoped GET and never an implicit tool call."""

import importlib.util
import json
from pathlib import Path

import pytest

CLI = Path(__file__).parents[2] / "cli"
spec = importlib.util.spec_from_file_location("adp_tools_doctor", CLI / "adp-doctor.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_access_status_dispatch_is_opt_in_and_keeps_offline_status():
    script = (CLI / "adp").read_text()
    assert 'status) shift; if [ "${1:-}" = "--access" ]; then' in script
    assert 'exec_python_helper "adp-doctor.py" "doctor" "--checks" "tools-access" "$@"' in script
    assert 'cmd_status "$@"' in script


def test_access_status_uses_only_a_read_and_explains_denial(capsys):
    calls = []

    def read(method, path, **kwargs):
        calls.append((method, path))
        return {
            "schema_version": "1.0",
            "tools": [
                {"name": "browser.start", "ready": False, "reason": "not_consented"},
                {"name": "websearch.search", "ready": True, "reason": None},
            ],
        }

    assert helper.run(helper.parser().parse_args(["doctor", "--checks", "tools-access"]), client=read) == 4
    assert calls == [("GET", "/auth/tools/access")]
    assert "browser.start: not consented" in capsys.readouterr().out
    assert helper.run(helper.parser().parse_args(["doctor", "--checks", "tools-access", "--json"]), client=read) == 4
    assert '"reason": "not_consented"' in capsys.readouterr().out


def test_access_status_rejects_unknown_report_without_reporting_ready():
    with pytest.raises(helper.CliError):
        helper.run(
            helper.parser().parse_args(["doctor", "--checks", "tools-access"]), client=lambda *args, **kwargs: {"schema_version": "2.0", "tools": []}
        )


def test_access_status_rejects_malformed_tool_name_without_output(capsys):
    with pytest.raises(helper.CliError):
        helper.run(
            helper.parser().parse_args(["doctor", "--checks", "tools-access"]),
            client=lambda *args, **kwargs: {
                "schema_version": "1.0",
                "tools": [{"name": "line\nspoof", "ready": True, "reason": None}],
            },
        )
    assert capsys.readouterr().out == ""


def test_access_status_distinguishes_callable_from_discoverable(capsys):
    report = {
        "schema_version": "1.0",
        "tools": [
            {"name": "browser.start", "ready": True, "reason": None, "discovery_reason": "catalogue_stale"},
        ],
    }
    assert helper.run(helper.parser().parse_args(["doctor", "--checks", "tools-access"]), client=lambda *args, **kwargs: report) == 4
    assert "policy ready; hidden from discovery: catalogue stale" in capsys.readouterr().out


@pytest.mark.parametrize("gateway_discovery_reason,exit_code", [(None, 0), ("catalogue_stale", 4)])
def test_chat_only_tools_explain_transport_without_hiding_gateway_discovery_failures(capsys, gateway_discovery_reason, exit_code):
    report = {
        "schema_version": "1.0",
        "tools": [
            {"name": "platform.my_budget", "ready": True, "reason": None, "discovery_reason": None, "transport": "chat"},
            {"name": "browser.start", "ready": True, "reason": None, "discovery_reason": gateway_discovery_reason, "transport": "tools_gateway"},
        ],
    }

    def read(*args, **kwargs):
        return report

    assert helper.run(helper.parser().parse_args(["doctor", "--checks", "tools-access"]), client=read) == exit_code
    output = capsys.readouterr().out
    assert "platform.my_budget: policy ready; ADP chat only" in output
    assert ("hidden from discovery: catalogue stale" in output) is bool(gateway_discovery_reason)
    assert helper.run(helper.parser().parse_args(["doctor", "--checks", "tools-access", "--json"]), client=read) == exit_code
    assert json.loads(capsys.readouterr().out) == report


def test_access_status_refuses_unknown_transport(capsys):
    with pytest.raises(helper.CliError):
        helper.run(
            helper.parser().parse_args(["doctor", "--checks", "tools-access"]),
            client=lambda *args, **kwargs: {
                "schema_version": "1.0",
                "tools": [{"name": "browser.start", "ready": True, "reason": None, "transport": "unverified"}],
            },
        )
    assert capsys.readouterr().out == ""
