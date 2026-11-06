"""Headless progress remains separate from Security release acceptance."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import security_headless as headless


@pytest.fixture
def selected(tmp_path, monkeypatch):
    tmp_path = tmp_path.resolve()
    tmp_path.chmod(0o700)
    cases = []
    for index in (1, 2):
        home = tmp_path / str(index)
        home.mkdir(mode=0o700)
        token = f"ADP_L5_FIXTURE_TEST_{index}"
        monkeypatch.setenv(token, f"session-{index}")
        cases.append({"api_url": "https://gateway.example/api", "cli_home": str(home),
                      "deployment_id": "fixture", "scope": {"tenant_id": f"tenant-{index}",
                          "product_id": f"product-{index}", "project_id": f"project-{index}"},
                      "configuration_id": f"configuration-{index}", "configuration_revision": 1,
                      "api_token_env": token, "mcp_token_env": token})
    value = {"schema_version": 1, "kind": "security_headless_progress", "deadline_seconds": 60,
             "authorization": {"source_sha": "a" * 40, "owner": "fixture-owner",
                 "actions": headless.ACTIONS, "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
             "cases": cases}
    filename = tmp_path / "fixture.json"
    filename.write_text(json.dumps(value))
    filename.chmod(0o600)
    return value, filename


def test_fixture_needs_no_ui_or_provider_credentials(selected):
    fixture, filename = selected
    assert headless.load_fixture(filename, "a" * 40) == fixture


@pytest.mark.parametrize("change", ["duplicate_tenant", "expired", "wrong_revision", "foreign_origin", "shared_token"])
def test_rejects_unsafe_scope_or_expired_authority(selected, monkeypatch, change):
    value, filename = selected
    if change == "duplicate_tenant":
        value["cases"][1]["scope"]["tenant_id"] = value["cases"][0]["scope"]["tenant_id"]
    elif change == "expired":
        value["authorization"]["expires_at"] = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    elif change == "wrong_revision":
        value["authorization"]["source_sha"] = "b" * 40
    elif change == "foreign_origin":
        value["cases"][1]["api_url"] = "https://foreign.example/api"
    else:
        monkeypatch.setenv(value["cases"][1]["api_token_env"], "session-1")
    filename.write_text(json.dumps(value))
    with pytest.raises(ValueError):
        headless.load_fixture(filename, "a" * 40)


class Driver:
    instances = []
    denial_status = 404
    denial_payload = {"detail": {"code": "not_found"}}
    drift = False

    def __init__(self, case, page):
        assert page is None
        self.case = case
        self.closed = False
        self.path = "/selected"
        self.record = {"configuration_id": case["configuration_id"], "revision": 1,
                       "scope": case["scope"], "settings": {"enabled": False},
                       "state": "disabled", "gaps": ["execution_delegation"], "hash": "digest"}
        self.api = SimpleNamespace(get=self.get)
        self.instances.append(self)

    def remaining(self):
        left = self.deadline - headless.time.monotonic()
        if left <= 0:
            raise AssertionError("expired")
        return min(20, left)

    def get(self, path, **kwargs):
        assert 0 < kwargs["timeout"] <= self.remaining()
        if path == self.path + "/configuration":
            return SimpleNamespace(json=lambda: deepcopy(self.record), raise_for_status=lambda: None)
        return SimpleNamespace(status_code=self.denial_status, json=lambda: self.denial_payload)

    def read_api(self, path):
        assert path.endswith("/configuration")
        return deepcopy(self.record)

    def cli_configuration(self):
        return {**deepcopy(self.record), "etag": '"digest"'}

    def _scope(self):
        return self.case["scope"]

    def mcp_call(self, name, arguments):
        assert name == "l5___get_configuration"
        return {**deepcopy(self.record), "revision": 2 if self.drift else 1}

    def denied_cli_configuration(self, foreign):
        return 5, cli_denial()

    def denied_mcp_configuration(self, foreign):
        return 200, mcp_denial()

    def close(self):
        self.closed = True


def cli_denial():
    return {"status": "failed", "command": "service", "detail": {},
            "error": {"code": "not_found", "http_status": 404, "message": "Resource not found"}}


def mcp_denial():
    structured = {"http_status": 404, "error": {"code": "not_found", "message": "Resource not found",
                                               "retryable": False, "request_id": "opaque-request"}}
    return {"isError": True, "structuredContent": structured,
            "content": [{"type": "text", "text": json.dumps(structured)}]}


def test_partial_receipt_cannot_claim_ui_or_native_release(selected):
    fixture, _ = selected
    receipt = headless.run(fixture, driver_type=Driver)
    assert receipt["release_qualified"] is False
    assert receipt["pending_acceptance"] == headless.PENDING
    assert "selector_node" not in receipt and "result" not in receipt
    assert len(receipt["observations"]) == 2
    assert all(driver.closed for driver in Driver.instances)


@pytest.mark.parametrize("status,payload", [
    (200, {"configuration_id": "foreign"}),
    (404, {"detail": {"code": "not_found", "tenant_id": "foreign"}}),
    (500, {"detail": {"code": "not_found"}}),
])
def test_foreign_disclosure_or_server_failure_does_not_pass(selected, monkeypatch, status, payload):
    monkeypatch.setattr(Driver, "denial_status", status)
    monkeypatch.setattr(Driver, "denial_payload", payload)
    with pytest.raises(AssertionError, match="nondisclosing"):
        headless.run(selected[0], driver_type=Driver)
    assert Driver.instances[-1].closed


def test_mcp_drift_fails_independent_comparison(selected, monkeypatch):
    monkeypatch.setattr(Driver, "drift", True)
    with pytest.raises(AssertionError, match="independent"):
        headless.run(selected[0], driver_type=Driver)


def test_expiry_during_first_case_prevents_second_case(selected, monkeypatch):
    fixture, _ = selected
    fixture["authorization"]["expires_at"] = (datetime.now(UTC) + timedelta(seconds=5)).isoformat()
    tick = [100.0]
    monkeypatch.setattr(headless.time, "monotonic", lambda: tick[0])
    class ExpiringDriver(Driver):
        def close(self):
            super().close()
            tick[0] += 6
    before = len(Driver.instances)
    with pytest.raises(AssertionError, match="expired before"):
        headless.run(fixture, driver_type=ExpiringDriver)
    assert len(Driver.instances) == before + 1


def test_request_timeout_cannot_exceed_authorization(selected, monkeypatch):
    fixture, _ = selected
    fixture["authorization"]["expires_at"] = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    tick = [100.0]
    monkeypatch.setattr(headless.time, "monotonic", lambda: tick[0])
    class SlowDriver(Driver):
        def get(self, path, **kwargs):
            assert 0 < kwargs["timeout"] <= 2
            response = super().get(path, **kwargs)
            tick[0] += 3
            return response
    with pytest.raises(AssertionError, match="expired"):
        headless.run(fixture, driver_type=SlowDriver)


def test_missing_browser_cannot_be_used(selected):
    case = {**selected[0]["cases"][0], "deadline_seconds": 60,
            "journal_file": str(selected[1].parent / "unused.jsonl")}
    driver = headless.LiveSetupDriver(case, None)
    try:
        with pytest.raises(AssertionError, match="not selected"):
            driver.ui_edit("region", "reason")
        with pytest.raises(AssertionError, match="not selected"):
            driver.ui_observations("binding", "run")
    finally:
        driver.close()


def test_fixture_cli_does_not_inherit_operator_tenant(selected, monkeypatch):
    monkeypatch.setenv("ADP_TENANT_ID", "operator-tenant")
    monkeypatch.setenv("ADP_TENANT_SUB", "operator-subject")
    monkeypatch.setenv("ADP_DEPLOYMENT_URL", "https://operator.example/api")
    monkeypatch.setenv("BG_CONFIG_DIR", "/operator/session")
    case = {**selected[0]["cases"][0], "deadline_seconds": 60,
            "journal_file": str(selected[1].parent / "unused.jsonl")}
    driver = headless.LiveSetupDriver(case, None)
    def fake_run(command, **kwargs):
        environment = kwargs["env"]
        assert not any(key.startswith("ADP_TENANT_") for key in environment)
        assert "ADP_DEPLOYMENT_URL" not in environment and "BG_CONFIG_DIR" not in environment
        assert environment["ADP_DEPLOYMENT_ID"] == case["deployment_id"]
        assert environment["ADP_HOME"] == case["cli_home"]
        return SimpleNamespace(returncode=0, stdout='{"status":"ok","detail":{}}')
    monkeypatch.setattr("clients_setup.subprocess.run", fake_run)
    try:
        driver.cli("config", "show")
    finally:
        driver.close()


@pytest.mark.parametrize("surface,fault", [(surface, fault) for surface in ("cli", "mcp")
                                        for fault in ("transport", "disclosure", "success")])
def test_client_denial_must_be_explicit_and_nondisclosing(selected, monkeypatch, surface, fault):
    foreign = selected[0]["cases"][1]
    if surface == "cli":
        response = cli_denial()
        if fault == "transport":
            response["error"]["code"] = "gateway_unavailable"
        elif fault == "disclosure":
            response["error"]["message"] = foreign["configuration_id"]
        else:
            response["status"] = "ok"
        monkeypatch.setattr(Driver, "denied_cli_configuration", lambda self, foreign: (5, response))
    else:
        response = mcp_denial()
        if fault == "transport":
            response["structuredContent"]["http_status"] = 503
        elif fault == "disclosure":
            response["structuredContent"]["error"]["message"] = foreign["scope"]["tenant_id"]
        else:
            response["isError"] = False
        monkeypatch.setattr(Driver, "denied_mcp_configuration", lambda self, foreign: (200, response))
    with pytest.raises(AssertionError, match="denial|disclosed"):
        headless.run(selected[0], driver_type=Driver)
