"""The live adapter must not mutate mappings outside its disposable fixtures."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts/test-cli-routing.py"
spec = importlib.util.spec_from_file_location("cli_routing_adapter", SCRIPT)
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


@pytest.mark.parametrize(
    "scope,destination",
    [("org:other", "dest"), ("team:fixture:other", "dest"), ("user:other", "dest"), ("org:fixture", "other")],
)
def test_adapter_refuses_nonfixture_mapping_before_running_cli(scope, destination, monkeypatch):
    fixture = SimpleNamespace(
        s=SimpleNamespace(data={"resources": {"org": "fixture", "team": "team", "destination_id": "dest"}, "users": {"member1": {"adp_id": "user"}}})
    )

    def forbidden(*args, **kwargs):
        pytest.fail("started a command before checking fixture ownership")

    monkeypatch.setattr(adapter.subprocess, "run", forbidden)
    with pytest.raises(RuntimeError, match="outside the fixture"):
        adapter.cli_command(fixture, Path("unused"), "/admin/bedrock-routing/mappings/" + scope, {"destination_id": destination}, "admin")


def test_resume_keeps_omitted_cloud_failure_in_acceptance_matrix():
    state = SimpleNamespace(data={"matrix": ["ec2", "cloud"], "checks": {"cloud": {"status": "failed"}}}, save=lambda: None)

    def start(state, suites):
        state.data["matrix"] = ["ec2"]
        state.data["checks"]["ec2"] = {"status": "not_run"}

    adapter.resume_matrix(start, state, ["ec2"])
    assert state.data["matrix"] == ["ec2", "cloud"]
    assert state.data["checks"]["cloud"]["status"] == "failed"
