"""Service allowlist and read-only behavior for partial configuration progress."""

import json
from types import SimpleNamespace

import pytest

import l5_configuration_headless as headless
import test_security_headless as fixtures
from test_security_headless import Driver

selected = fixtures.selected


@pytest.mark.parametrize("service", sorted(headless.SERVICES))
def test_service_progress_retains_unverified_acceptance(selected, service):
    fixture, path = selected
    fixture.update(kind=headless.KIND, service_id=service)
    path.write_text(json.dumps(fixture))
    loaded = headless.load_fixture(path, "a" * 40)
    class ScopedDriver(Driver):
        def __init__(self, case, page):
            super().__init__(case, page)
            self.service_id = case["service_id"]
        def get(self, path, **kwargs):
            if path != self.path + "/configuration":
                assert path.endswith("/services/" + service + "/configuration")
            return super().get(path, **kwargs)
    receipt = headless.run(loaded, driver_type=ScopedDriver)
    assert receipt["service_id"] == service and receipt["kind"] == headless.KIND
    assert receipt["release_qualified"] is False
    assert "native_provider_execution" in receipt["pending_acceptance"]


@pytest.mark.parametrize("service", ["transform", "well_architected", "other", "../security"])
def test_unsupported_or_source_dependent_service_is_not_selected(selected, service):
    fixture, path = selected
    fixture.update(kind=headless.KIND, service_id=service)
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="disabled configuration"):
        headless.load_fixture(path, "a" * 40)
    with pytest.raises(ValueError, match="unsupported"):
        headless.run(fixture)


def test_driver_uses_only_the_selected_service_read_commands(selected, monkeypatch):
    fixture, path = selected
    case = {**fixture["cases"][0], "service_id": "devops", "deadline_seconds": 60,
            "journal_file": str(path.parent / "unused.jsonl")}
    def command(args, **kwargs):
        assert args[2:4] == ["config", "show"]
        assert args[-3:] == ["--service", "devops", "--json"]
        assert kwargs["timeout"] <= 20
        return SimpleNamespace(returncode=0, stdout='{"status":"ok","detail":{},"etag":"version"}')
    monkeypatch.setattr(headless.subprocess, "run", command)
    driver = headless.ConfigurationDriver(case, None)
    try:
        assert driver.path.endswith("/services/devops")
        assert driver._scope()["service_id"] == "devops"
        assert driver.cli_configuration() == {"etag": "version"}
        with pytest.raises(AssertionError, match="only CLI"):
            driver.cli("config", "update")
        with pytest.raises(AssertionError, match="only MCP"):
            driver.mcp_call("l5___submit_run", driver._scope())
        with pytest.raises(AssertionError, match="only MCP"):
            driver.mcp_call("l5___get_configuration", {**driver._scope(), "service_id": "finops"})
    finally:
        driver.close()


def test_security_entrypoint_cannot_consume_generic_fixture(selected):
    fixture, path = selected
    fixture.update(kind=headless.KIND, service_id="finops")
    path.write_text(json.dumps(fixture))
    with pytest.raises(ValueError, match="authorization"):
        headless.security.load_fixture(path, "a" * 40)


def test_denial_probes_keep_own_credentials_and_target_foreign_scope(selected, monkeypatch):
    fixture, path = selected
    case = {**fixture["cases"][0], "service_id": "finops", "deadline_seconds": 60,
            "journal_file": str(path.parent / "unused.jsonl")}
    foreign = fixture["cases"][1]
    driver = headless.ConfigurationDriver(case, None)
    def command(args, **kwargs):
        assert args[args.index("--product") + 1] == foreign["scope"]["product_id"]
        assert args[args.index("--project") + 1] == foreign["scope"]["project_id"]
        assert args[args.index("--service") + 1] == "finops"
        assert kwargs["env"]["ADP_HOME"] == case["cli_home"] != foreign["cli_home"]
        assert kwargs["env"]["ADP_DEPLOYMENT_ID"] == case["deployment_id"]
        assert 0 < kwargs["timeout"] <= 20
        return SimpleNamespace(returncode=5, stdout=json.dumps(fixtures.cli_denial()))
    def post(url, **kwargs):
        assert url == "/v1/l5/mcp/tools/call"
        assert driver.mcp.headers["Authorization"] == "Bearer session-1"
        assert kwargs["json"] == {"tool": "l5___get_configuration", "arguments": {
            "product_id": foreign["scope"]["product_id"], "project_id": foreign["scope"]["project_id"], "service_id": "finops"}}
        assert 0 < kwargs["timeout"] <= 20
        return SimpleNamespace(status_code=200, json=fixtures.mcp_denial)
    monkeypatch.setattr(headless.subprocess, "run", command)
    monkeypatch.setattr(driver.mcp, "post", post)
    try:
        assert driver.denied_cli_configuration(foreign)[0] == 5
        assert driver.denied_mcp_configuration(foreign)[0] == 200
    finally:
        driver.close()
