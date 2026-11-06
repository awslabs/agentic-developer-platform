"""Synthetic SCN-8065-04 fixtures prove bounds, not deployed acceptance."""

import json
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from tests.e2e.tools import access_scenarios, runner

SHA = "a" * 40
IMAGE = "example.invalid/adp-tools-facade@sha256:" + "b" * 64
GATEWAY_IMAGE = "example.invalid/adp-gateway@sha256:" + "c" * 64


class IdleClient:
    version = "test"

    def __init__(self, command):
        self.command = command

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


class AccessDriver:
    authorized = True
    isolated = True
    tenant_selected = True
    pricing_approved = True

    def __init__(self, *, cost="0.25", session_count=1, failure=None):
        self.calls = []
        self.cost = cost
        self.session_count = session_count
        self.failure = failure

    def estimate_cost(self, action):
        self.calls.append(("price", action))
        return self.cost if action == "invoke" else "0"

    def start_owned(self, scenario_id, **bounds):
        self.calls.append(("start", bounds))
        assert scenario_id == "SCN-8065-04"
        return {"execution_id": str(uuid4()), "run_id": str(uuid4()),
                "session_count": self.session_count, "ttl_seconds": bounds["ttl_seconds"]}

    def step(self, action, **binding):
        self.calls.append((action, binding))
        if action == self.failure:
            raise TimeoutError("synthetic step timeout")
        return {"ready": access_scenarios.READINESS[action], "provider_request_count": int(action == "invoke"),
                "estimated_cost_usd": self.cost if action == "invoke" else "0",
                "audit_operation_id": str(uuid4())}

    def stop_owned(self, execution_id, **bounds):
        self.calls.append(("stop", execution_id, bounds))
        return {"status": "confirmed", "provider_termination": "provider-observed"}


class Observer:
    def __init__(self, **changes):
        self.changes = changes

    def observe(self, *, execution_id, run_id, request_ids, audit_operation_ids, timeout):
        assert timeout == 600
        return {"provider_source": "provider-control-plane", "audit_source": "gateway-audit",
                "execution_id": execution_id, "run_id": run_id, "request_ids": list(request_ids),
                "audit_operation_ids": list(audit_operation_ids), "session_count": 1,
                "provider_request_count": 1, "audit_attributed": True, "diagnostics_match": True,
                "tenant_verified": True, "owner_verified": True,
                "total_cost_usd": "0.25", "cleanup_elapsed_seconds": 50,
                "termination": "stopped", "termination_observed_at": "2026-01-01T00:00:00Z", **self.changes}


def test_access_fixture_runs_one_bounded_session_and_requires_independent_live_oracle():
    driver = AccessDriver()
    fixture = access_scenarios.ACCESS_LIVE_FIXTURE
    evidence = access_scenarios.run_access_fixture(fixture, driver)
    assert evidence["status"] == "pending-verification"
    assert evidence["provider_request_count"] == 1 and evidence["estimated_cost_usd"] == "0.25"
    assert len(evidence["audit_operation_ids"]) == 6
    assert [action for action, *_ in driver.calls if action != "price"] == ["start", *fixture.actions, "stop"]
    assert [action for action, *_ in driver.calls[:8]] == ["price"] * 8
    assert driver.calls[8][1] == {"max_sessions": 1, "ttl_seconds": 300, "max_cost_usd": Decimal("20")}
    assert driver.calls[-1][-1]["timeout"] <= 600
    assert access_scenarios.access_verdict(fixture, evidence, live=False, observer=Observer())[0] == "blocked"
    assert access_scenarios.access_verdict(fixture, evidence, live=True, observer=Observer())[0] == "pass"


@pytest.mark.parametrize("field", ["authorized", "isolated", "tenant_selected", "pricing_approved"])
def test_access_fixture_refuses_missing_private_authority_before_work(field):
    driver = AccessDriver()
    setattr(driver, field, False)
    with pytest.raises(access_scenarios.FixtureUnavailable):
        access_scenarios.run_access_fixture(access_scenarios.ACCESS_LIVE_FIXTURE, driver)
    assert driver.calls == []


def test_unpriced_or_over_budget_fixture_refuses_before_start():
    driver = AccessDriver(cost="21")
    with pytest.raises(access_scenarios.FixtureUnavailable):
        access_scenarios.run_access_fixture(access_scenarios.ACCESS_LIVE_FIXTURE, driver)
    assert not any(action == "start" for action, *_ in driver.calls)
    driver = AccessDriver(cost="not-priced")
    with pytest.raises(access_scenarios.FixtureUnavailable):
        access_scenarios.run_access_fixture(access_scenarios.ACCESS_LIVE_FIXTURE, driver)
    assert not any(action == "start" for action, *_ in driver.calls)


@pytest.mark.parametrize("change", [{"session_count": 2}, {"failure": "revoke"}])
def test_access_fixture_does_not_pass_overspend_extra_session_or_partial_write(change):
    driver = AccessDriver(**change)
    evidence = access_scenarios.run_access_fixture(access_scenarios.ACCESS_LIVE_FIXTURE, driver)
    assert evidence["status"] == "blocked" and evidence["cleanup"]["status"] == "confirmed"
    assert driver.calls[-1][0] == "stop"
    assert access_scenarios.access_verdict(access_scenarios.ACCESS_LIVE_FIXTURE, evidence,
                                           live=True, observer=Observer())[0] == "blocked"


def test_access_fixture_attempts_cleanup_after_ttl_and_rejects_late_termination():
    driver = AccessDriver()
    times = iter([0, 301, 310, 610])
    evidence = access_scenarios.run_access_fixture(access_scenarios.ACCESS_LIVE_FIXTURE, driver,
                                                    clock=lambda: next(times))
    assert evidence["status"] == "blocked" and evidence["cleanup"]["status"] == "late"
    assert driver.calls[-1][0] == "stop" and driver.calls[-1][-1]["timeout"] <= 600
    passed = access_scenarios.run_access_fixture(access_scenarios.ACCESS_LIVE_FIXTURE, AccessDriver())
    assert access_scenarios.access_verdict(access_scenarios.ACCESS_LIVE_FIXTURE, passed,
                                           live=True, observer=Observer(cleanup_elapsed_seconds=601))[0] == "blocked"


@pytest.mark.parametrize("invalid", [
    {"provider_source": "fixture"}, {"audit_source": "fixture"}, {"audit_attributed": False},
    {"diagnostics_match": False}, {"provider_request_count": 2}, {"total_cost_usd": "20.01"},
    {"session_count": 2}, {"tenant_verified": False}, {"owner_verified": False},
    {"termination_observed_at": None},
])
def test_independent_observer_rejects_mismatch_or_missing_termination(invalid):
    evidence = access_scenarios.run_access_fixture(access_scenarios.ACCESS_LIVE_FIXTURE, AccessDriver())
    status, _ = access_scenarios.access_verdict(access_scenarios.ACCESS_LIVE_FIXTURE, evidence,
                                                 live=True, observer=Observer(**invalid))
    assert status != "pass"


def test_runner_registers_selector_without_treating_private_fixture_as_present(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda url: None)
    driver = AccessDriver()
    arguments = ("SCN-8065-04", tmp_path, "https://example.invalid", SHA, GATEWAY_IMAGE, IMAGE)
    assert runner.evaluate(*arguments, command=["adp", "mcp"], client_factory=IdleClient,
                           access_driver_factory=lambda *_: driver, access_observer=Observer(), live=False) is False
    assert driver.calls == []
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert report["scenarios"][0]["status"] == "not-run"
    assert report["scenarios"][0]["cleanup"]["status"] == "not-run"
    assert runner.evaluate(*arguments, command=["adp", "mcp"], client_factory=IdleClient, live=True) is False
    assert runner.evaluate(*arguments, command=["adp", "mcp"], client_factory=IdleClient, live=True,
                           access_driver_factory=lambda *_: AccessDriver(), access_observer=Observer()) is True
    assert runner.evaluate("repair", tmp_path, *arguments[2:], command=["adp", "mcp"],
                           client_factory=IdleClient, live=True) is False
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert report["error"] == "ValueError"


def test_private_binding_loader_never_imports_unapproved_module(monkeypatch):
    monkeypatch.setenv("ADP_TOOLS_ACCESS_FIXTURE_MODULE", "untrusted.module")
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: pytest.fail("unapproved import"))
    assert runner.private_access_bindings() == (None, None)
    private = SimpleNamespace(driver_factory=lambda *_: AccessDriver(), observer=Observer())
    monkeypatch.setenv("ADP_TOOLS_ACCESS_FIXTURE_MODULE", "tests.e2e.tools.private_access_test")
    monkeypatch.setattr(runner.importlib, "import_module", lambda name: private)
    factory, observer = runner.private_access_bindings()
    assert isinstance(factory(), AccessDriver) and observer is private.observer
