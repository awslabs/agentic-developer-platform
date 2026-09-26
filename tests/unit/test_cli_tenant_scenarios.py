"""E23/E27 grading: no invisible tenant, mutation or failed read is a pass."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def scenario(monkeypatch):
    remote = Path(__file__).parents[1] / "e2e/cli_uplift/remote"
    monkeypatch.syspath_prepend(str(remote))
    spec = importlib.util.spec_from_file_location(
        "tenant_scenario", remote / "tenant_isolation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Client:
    def __init__(self, wrong=False):
        self.calls = []
        self.wrong = wrong

    def json(self, args):
        self.calls.append(args)
        if args[:2] == ["tenant", "list"]:
            data = {"items": [{"org_id": "a"}, {"org_id": "b"}]}
        elif args[:2] == ["tenant", "use"]:
            data = {"tenant_id": args[2]}
        elif "capabilities" in args:
            data = {"tenant": {"org_id": "foreign" if self.wrong else args[1]}}
        else:
            data = {"tenant_id": args[1], "selection_source": "flag"}
        return {"status": "ok", "detail": data}

    def run(self, args, **kwargs):
        self.calls.append(args)
        if args == ["refresh"]:
            return 0, None
        return 4, {"status": "failed", "error": {"code": "tenant_selection_required"}}


def test_smoke_runs_without_default_writes_or_inference(scenario):
    cli, evidence = Client(), {}
    scenario.smoke(cli, evidence)
    assert evidence["tenant_id"] == "a"
    assert all(
        "use" not in call and "refresh" not in call and "select" not in call
        for call in cli.calls
    )


def test_two_tenant_fixture_requires_visible_distinct_memberships(scenario):
    for fixture in [{}, {"tenant_ids": ["a", "a"]}, {"tenant_ids": ["a", "foreign"]}]:
        cli = Client()
        with pytest.raises(scenario.common.RemoteError):
            scenario.isolated_reads(cli, fixture, {})
        assert all("use" not in call and "refresh" not in call for call in cli.calls)


def test_crossed_tenant_read_cannot_pass(scenario):
    with pytest.raises(scenario.common.RemoteError, match="crossed"):
        scenario.isolated_reads(Client(wrong=True), {"tenant_ids": ["a", "b"]}, {})


def test_two_tenant_scenario_refreshes_but_does_not_claim_inference(scenario):
    cli, evidence = Client(), {}
    scenario.isolated_reads(cli, {"tenant_ids": ["a", "b"]}, evidence)
    assert evidence["refreshed"] is True
    assert ["refresh"] in cli.calls
    assert "remain separate acceptance" in evidence["qualification"]
    assert not any("workspaces/select" in call or "task" in call for call in cli.calls)
