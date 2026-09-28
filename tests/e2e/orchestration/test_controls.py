"""Non-live service control assertions and unsupported capability handling."""

from copy import deepcopy
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.scenarios import stop
from tests.e2e.orchestration.scenarios.controls import assert_control_outcome
from tests.e2e.orchestration.scenarios.http import Unsupported


@pytest.mark.parametrize("name", ["fanout-budget", "repair-budget"])
@pytest.mark.parametrize(
    "attack", [None, "admitted", "degraded", "binding", "unaccounted", "unreconciled"]
)
def test_shared_allowance_refusal_requires_native_accounting(name, attack):
    after = dict(
        executions=[],
        node_ids=["one", "two", "three"],
        settled_usd="0",
        reserved_usd="50",
        after_usd="50",
        limit="50",
        allowance_id="flow:one",
        cancellation={"remaining_usd": "0"},
    )
    after.update(
        fanout={"admitted": False, "degraded": False, "allowance_id": "flow:one"},
        repair={"admitted": False, "degraded": False, "allowance_id": "flow:one"},
    )
    decision = after["fanout" if name == "fanout-budget" else "repair"]
    if attack in {"admitted", "degraded"}:
        decision[attack] = True
    elif attack == "binding":
        decision["allowance_id"] = "fresh-allowance"
    elif attack == "unaccounted":
        after["reserved_usd"] = "0"
    elif attack == "unreconciled":
        after["cancellation"]["remaining_usd"] = "25"
    if attack:
        with pytest.raises(AssertionError):
            assert_control_outcome(name, {"after": after})
    else:
        assert_control_outcome(name, {"after": after})


@pytest.mark.parametrize(
    "reason", ["work_not_owned", "budget_unavailable", "membership_revoked"]
)
def test_unrelated_refusal_cannot_prove_accepted_policy_withdrawal(reason):
    before = {
        "policy": {"policy_id": "one"},
        "refusal": None,
        "flow_id": "one",
        "plan_version": 1,
        "executions": [],
    }
    after = {
        **deepcopy(before),
        "policy": None,
        "plan_version": 2,
        "previous_policy_versions": [1],
        "refusal": {"permitted": False, "reason": "stale_policy_version"},
    }
    assert_control_outcome("revocation", {"before": before, "after": after})
    after["refusal"]["reason"] = reason
    with pytest.raises(AssertionError):
        assert_control_outcome("revocation", {"before": before, "after": after})


def test_unsupported_abort_is_observed_before_creating_paid_fixtures(monkeypatch):
    session = NS(
        manifest=NS(halt_stop=True, runtime={"engine": "target"}),
        client=Mock(),
        config=NS(versions={"engine": "a" * 40}),
        runtime_observations={},
        evidence=Mock(),
    )
    monkeypatch.setattr(stop, "read_runtime", lambda *_: {"actual_revision": "a" * 40})
    probe = Mock(return_value={"supported_actions": ["pause", "resume"]})
    monkeypatch.setattr(stop, "execute_source", probe)
    with pytest.raises(Unsupported, match="abort is unavailable") as error:
        stop.exercise(session)
    assert error.value.evidence is session.evidence.save.return_value
    probe.assert_called_once()
    session.client.request.assert_not_called()
