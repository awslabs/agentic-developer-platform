"""Safety and grading checks for the existing CLI harness's terminal diagnostic."""

import importlib.util
from pathlib import Path

import pytest

REMOTE = Path(__file__).resolve().parents[1] / "e2e/cli_uplift/remote"


@pytest.fixture
def scenario(monkeypatch):
    monkeypatch.syspath_prepend(str(REMOTE))
    spec = importlib.util.spec_from_file_location(
        "agent_terminal_scenario", REMOTE / "agent_terminal_controls.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    def __init__(self, states, refusal=None):
        self.states = iter(states)
        self.calls = []
        self.refusal = refusal or (
            4,
            {"status": "unavailable", "detail": {"state": "terminal"}},
        )

    def json(self, args):
        return {"status": "ok", "detail": {"status": next(self.states)}}

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args[1] == "logs":
            return 5, {"status": "failed", "error": {"http_status": 409}}
        return self.refusal


@pytest.mark.parametrize(
    "status", ["running", "pause_requested", "paused", "unknown", None]
)
def test_refuses_nonterminal_fixture_before_any_mutation(scenario, status):
    cli = Client([status])
    with pytest.raises(
        scenario.common.RemoteError, match="refuses an active or unknown run"
    ):
        scenario.exercise(cli, "owned-run", {})
    assert cli.calls == []


def test_transport_failure_cannot_count_as_terminal_refusal(scenario):
    cli = Client(["aborted"], (5, {"status": "failed", "error": {"http_status": 503}}))
    with pytest.raises(
        scenario.common.RemoteError, match="not authoritatively refused"
    ):
        scenario.exercise(cli, "owned-run", {})


def test_state_change_cannot_count_as_success(scenario):
    with pytest.raises(scenario.common.RemoteError, match="status changed"):
        scenario.exercise(Client(["aborted", "complete"]), "owned-run", {})


def test_concurrent_refusals_keep_one_identity_and_do_not_claim_active_acceptance(
    scenario,
):
    cli = Client(["aborted", "aborted"])
    evidence = {}
    scenario.exercise(cli, "owned-run", evidence)
    pauses = [row for row in cli.calls if row[1] == "pause"]
    assert len(pauses) == 2
    assert pauses[0] == pauses[1]
    assert evidence["after_status"] == "aborted"
    assert "acceptance remains open" in evidence["qualification"]


def test_disabled_control_cannot_substitute_for_terminal_refusal(scenario):
    cli = Client(
        ["aborted"], (4, {"status": "unavailable", "detail": {"state": "unknown"}})
    )
    with pytest.raises(
        scenario.common.RemoteError, match="not authoritatively refused"
    ):
        scenario.exercise(cli, "owned-run", {})


def test_accepts_activity_complete_not_task_completed(scenario):
    evidence = {}
    scenario.exercise(Client(["complete", "complete"]), "owned-run", evidence)
    assert evidence["after_status"] == "complete"
    cli = Client(["completed"])
    with pytest.raises(scenario.common.RemoteError, match="unknown run"):
        scenario.exercise(cli, "owned-run", {})
    assert not cli.calls
