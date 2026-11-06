"""SCN-8065-02 checks denial reasons and no provider work through isolated routes."""

import json

import pytest

from tests.e2e.tools import access_integration, runner
from tests.e2e.tools.test_access_ac01 import GATEWAY_IMAGE, IMAGE, SHA, IdleClient

from tests.e2e.tools import test_access_ac01 as fresh_access

gateway_fixture = fresh_access.gateway_fixture


def test_denials_are_explained_and_never_reach_provider(gateway_fixture):
    driver, observer = gateway_fixture
    evidence = access_integration.access_ac02(driver, observer)
    assert evidence["status"] == "pass", evidence
    assert evidence["cleanup"]["status"] == "confirmed"
    assert len(evidence["audit_operation_ids"]) == 10
    assert evidence["provider_call_count"] == 0
    assert driver.provider.read_calls() == [] and driver.recorder.call_count == 0
    assert evidence["denials"] == {
        "not_consented": {"reason": "not_consented", "call_status": 403, "discovered_tools": []},
        "team_disabled": {"reason": "team_disabled", "call_status": 403,
                          "discovered_tools": ["code_interpreter.start", "websearch.search"]},
        "tenant_disabled": {"reason": "tenant_disabled", "call_status": 403,
                            "discovered_tools": ["websearch.search"]},
        "other_tenant": {"reason": "tenant_boundary", "call_status": 404, "discovered_tools": []},
    }
    assert observer.verify_cleanup(attempt_id=evidence["attempt_id"])


def test_denial_selector_requires_isolation_and_independent_observer(gateway_fixture):
    driver, observer = gateway_fixture
    driver.isolated = False
    with pytest.raises(access_integration.FixtureUnavailable):
        access_integration.access_ac02(driver, observer)
    driver.isolated = True
    with pytest.raises(access_integration.FixtureUnavailable):
        access_integration.access_ac02(driver, None)
    assert driver.actions == []


def test_denial_selector_rejects_false_independent_provider_readback(gateway_fixture):
    driver, observer = gateway_fixture

    class IncorrectObserver:
        def observe_denial(self, **kwargs):
            return {**observer.observe_denial(**kwargs), "provider_call_count": 1}

        def verify_cleanup(self, **kwargs):
            return observer.verify_cleanup(**kwargs)

    evidence = access_integration.access_ac02(driver, IncorrectObserver())
    assert evidence["status"] == "blocked" and evidence["reason"] == "ValueError"
    assert evidence["cleanup"]["status"] == "confirmed"
    assert driver.provider.read_calls() == []


def test_runner_selects_denial_case_without_implicit_fixture(tmp_path, monkeypatch, gateway_fixture):
    driver, observer = gateway_fixture
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    arguments = ("SCN-8065-02", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE)
    assert runner.evaluate(*arguments, command=["adp", "mcp"], client_factory=IdleClient) is False
    monkeypatch.setenv("GITHUB_RUN_ID", "local-scn02")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    assert runner.evaluate(*arguments, command=["adp", "mcp"], client_factory=IdleClient,
                           integration_driver_factory=lambda *_: driver, integration_observer=observer) is True
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert (report["github_run_id"], report["github_run_attempt"]) == ("local-scn02", "1")
    row, = report["scenarios"]
    assert row["scenario_id"] == "SCN-8065-02" and row["status"] == "pass"
    assert row["attempt_id"] and row["cleanup"] == {"status": "confirmed"}
    assert row["provider_call_count"] == 0 and len(row["denials"]) == 4
