"""SCN-8065-03 checks revision conflicts and live-target removal with local stores."""

import json

import pytest

from tests.e2e.tools import access_integration, runner
from tests.e2e.tools import test_access_ac01 as fresh_access
from tests.e2e.tools.test_access_ac01 import GATEWAY_IMAGE, IMAGE, SHA, IdleClient

gateway_fixture = fresh_access.gateway_fixture


def test_concurrent_edits_and_removed_target_fail_closed(gateway_fixture):
    driver, observer = gateway_fixture
    evidence = access_integration.access_ac03(driver, observer)
    assert evidence["status"] == "pass", evidence
    assert evidence["conflicts"] == ["stale_policy", "stale_entitlement", "stale_consent", "stale_reconcile"]
    assert evidence["removed_target"] == "browser.start"
    assert evidence["provider_call_count"] == driver.recorder.call_count == 1
    assert [call[0] for call in driver.provider.read_calls()] == ["browser___start"]
    assert len(evidence["audit_operation_ids"]) == 10
    assert evidence["execution_id"]
    assert evidence["cleanup"] == {"status": "confirmed"}
    assert observer.verify_cleanup(attempt_id=evidence["attempt_id"])


def test_reconciliation_selector_requires_isolation_and_oracle(gateway_fixture):
    driver, observer = gateway_fixture
    driver.ready_targets = False
    with pytest.raises(access_integration.FixtureUnavailable):
        access_integration.access_ac03(driver, observer)
    driver.ready_targets = True
    with pytest.raises(access_integration.FixtureUnavailable):
        access_integration.access_ac03(driver, None)
    assert driver.actions == [] and driver.provider.read_calls() == []


def test_reconciliation_rejects_false_independent_grant_readback(gateway_fixture):
    driver, observer = gateway_fixture

    class IncorrectObserver:
        def observe_reconciliation(self, **kwargs):
            return {**observer.observe_reconciliation(**kwargs), "team_grants": []}

        def verify_cleanup(self, **kwargs):
            return observer.verify_cleanup(**kwargs)

    evidence = access_integration.access_ac03(driver, IncorrectObserver())
    assert evidence["status"] == "blocked" and evidence["reason"] == "ValueError"
    assert evidence["cleanup"] == {"status": "confirmed"}
    assert driver.provider.read_calls() == []


def test_runner_selects_scn03_without_claiming_missing_fixture(tmp_path, monkeypatch, gateway_fixture):
    driver, observer = gateway_fixture
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    arguments = ("SCN-8065-03", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE)
    assert runner.evaluate(*arguments, command=["adp", "mcp"], client_factory=IdleClient) is False
    monkeypatch.setenv("GITHUB_RUN_ID", "local-scn03")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "1")
    assert runner.evaluate(*arguments, command=["adp", "mcp"], client_factory=IdleClient,
                           integration_driver_factory=lambda *_: driver, integration_observer=observer) is True
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert (report["github_run_id"], report["github_run_attempt"]) == ("local-scn03", "1")
    row, = report["scenarios"]
    assert row["scenario_id"] == "SCN-8065-03" and row["status"] == "pass"
    assert row["attempt_id"] and row["cleanup"] == {"status": "confirmed"}
    assert row["provider_call_count"] == 1 and len(row["conflicts"]) == 4
