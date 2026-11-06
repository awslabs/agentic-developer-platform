from hashlib import sha256
from uuid import uuid4

import pytest

from tests.e2e.tools import runner, scenarios


class IsolatedDriver:
    isolated = True
    authorized = True

    def __init__(self, *, failure=None):
        self.calls = []
        self.failure = failure

    def start_owned(self, scenario_id, **bounds):
        self.calls.append(("start", scenario_id, bounds))
        return {"execution_id": str(uuid4()), "run_id": str(uuid4())}

    def step(self, action, **binding):
        self.calls.append((action, binding))
        if action == self.failure:
            raise TimeoutError("injected test failure")
        return {"operation_ids": [str(uuid4())],
                "provider_request_observations": [{"request_id": binding["request_id"], "count": 1}],
                "artifact_sha256": [sha256(action.encode()).hexdigest()]}

    def stop_owned(self, execution_id, **bounds):
        self.calls.append(("stop", execution_id, bounds))
        return {"status": "confirmed", "provider_termination": "provider-observed"}


def test_every_lifecycle_case_has_bounded_isolated_fault_recipe():
    fixtures = scenarios.LIFECYCLE_FIXTURES
    assert {case.identifier for case in fixtures} == {f"SCN-TLS-{number:04}" for number in range(50, 60)}
    assert {case.identifier for case in fixtures} <= {case.identifier for case in runner.SCENARIOS}
    runner.validate_scenarios(runner.SCENARIOS)
    assert all(case.stories and case.max_provider_calls <= 3 and case.max_seconds <= 900 for case in fixtures)
    assert all("confirm_stop" in case.actions for case in fixtures)
    assert "restart_reconciler" in next(case.actions for case in fixtures if case.identifier == "SCN-TLS-0054")


@pytest.mark.parametrize("fixture", scenarios.LIFECYCLE_FIXTURES, ids=lambda fixture: fixture.identifier)
def test_fault_recipes_execute_owned_steps_and_attempt_bounded_cleanup(fixture):
    driver = IsolatedDriver()
    evidence = scenarios.run_fixture(fixture, driver)
    assert evidence["status"] == "pending-verification"
    assert evidence["request_ids"] and evidence["operation_ids"]
    assert evidence["execution_id"] and evidence["run_id"]
    assert evidence["provider_request_observations"] and evidence["artifact_sha256"]
    assert driver.calls[0][0] == "start" and driver.calls[-1][0] == "stop"
    assert driver.calls[0][2]["max_provider_calls"] <= 3
    assert driver.calls[-1][2]["timeout"] <= 60


def test_missing_isolated_authority_refuses_faults_before_provider_work():
    driver = IsolatedDriver()
    driver.isolated = False
    with pytest.raises(scenarios.FixtureUnavailable):
        scenarios.run_fixture(scenarios.LIFECYCLE_FIXTURES[1], driver)
    assert not driver.calls


def test_fault_failure_still_requests_owned_cleanup_and_retains_unknowns():
    driver = IsolatedDriver(failure="drop_handle")
    evidence = scenarios.run_fixture(scenarios.LIFECYCLE_FIXTURES[6], driver)
    assert evidence["status"] == "blocked"
    assert evidence["reason"] == "TimeoutError"
    assert evidence["cleanup"]["status"] == "confirmed"
    assert driver.calls[-1][0] == "stop"


def test_fault_recipes_drop_unapproved_provider_and_cleanup_fields():
    class RedactingDriver(IsolatedDriver):
        def step(self, action, **binding):
            result = super().step(action, **binding)
            result["provider_request_observations"][0]["authorization"] = "not-for-evidence"
            result["raw_secret"] = "not-for-evidence"
            return result

        def stop_owned(self, execution_id, **bounds):
            result = super().stop_owned(execution_id, **bounds)
            result["raw_secret"] = "not-for-evidence"
            return result

    result = scenarios.run_fixture(scenarios.LIFECYCLE_FIXTURES[1], RedactingDriver())
    assert "not-for-evidence" not in str(result)


@pytest.mark.parametrize("cleanup_status,expected", [("unknown", "blocked"),
                                                      ("pending", "blocked"),
                                                      ("failed", "fail")])
def test_cleanup_outcome_cannot_be_promoted_from_client_or_closed_flag(cleanup_status, expected):
    fixture = scenarios.LIFECYCLE_FIXTURES[6]
    driver = IsolatedDriver()
    evidence = scenarios.run_fixture(fixture, driver)
    evidence["cleanup"] = {"status": cleanup_status, "provider_termination": None,
                           "adp_closed": True, "client_exited": True}
    status, _ = scenarios.lifecycle_verdict(fixture, evidence, live=True)
    assert status == expected and driver.calls[-1][0] == "stop"


def test_simulated_provider_success_does_not_establish_live_acceptance():
    fixture = scenarios.LIFECYCLE_FIXTURES[1]
    evidence = scenarios.run_fixture(fixture, IsolatedDriver())
    status, _ = scenarios.lifecycle_verdict(fixture, evidence, live=False)
    assert status == "blocked"


def test_observer_outage_and_mismatched_execution_remain_blocked():
    fixture = scenarios.LIFECYCLE_FIXTURES[1]
    evidence = scenarios.run_fixture(fixture, IsolatedDriver())

    class Observer:
        def observe(self, **binding):
            return {"source": "provider-control-plane", "execution_id": str(uuid4()),
                    "run_id": binding["run_id"]}

    status, _ = scenarios.lifecycle_verdict(fixture, evidence, observer=Observer(), live=True)
    assert status == "blocked"

    class Unavailable:
        def observe(self, **binding):
            raise TimeoutError("unavailable")

    status, _ = scenarios.lifecycle_verdict(fixture, evidence, observer=Unavailable(), live=True)
    assert status == "blocked"


def test_fixture_cleanup_timeout_keeps_unresolved_evidence():
    class TimeoutDriver(IsolatedDriver):
        def stop_owned(self, execution_id, **bounds):
            self.calls.append(("stop", execution_id, bounds))
            raise TimeoutError("provider never confirmed stop")

    driver = TimeoutDriver()
    fixture = scenarios.LIFECYCLE_FIXTURES[6]
    evidence = scenarios.run_fixture(fixture, driver)
    status, _ = scenarios.lifecycle_verdict(fixture, evidence, live=True)
    assert status == "blocked"
    assert evidence["request_ids"] and evidence["operation_ids"]
    assert evidence["cleanup"]["status"] == "unknown"
    assert evidence["cleanup"]["reason"] == "TimeoutError"
    assert driver.calls[-1][2]["timeout"] <= 60


def test_confirmed_provider_overrun_fails_even_with_closed_flag():
    fixture = scenarios.LIFECYCLE_FIXTURES[1]
    evidence = scenarios.run_fixture(fixture, IsolatedDriver())

    class OverrunObserver:
        def observe(self, **binding):
            return {"source": "provider-control-plane", "execution_id": binding["execution_id"],
                    "run_id": binding["run_id"], "provider_request_count": fixture.max_provider_calls + 1}

    status, _ = scenarios.lifecycle_verdict(fixture, evidence, observer=OverrunObserver(), live=True)
    assert status == "fail"


def test_revoke_fixture_targets_selected_stateful_tool():
    fixture = next(case for case in scenarios.LIFECYCLE_FIXTURES if case.identifier == "SCN-TLS-0054")
    assert scenarios.fixture_for_cell(fixture, "browser").actions[0] == "browser"
    assert scenarios.fixture_for_cell(fixture, "interpreter").actions[0] == "interpreter"
