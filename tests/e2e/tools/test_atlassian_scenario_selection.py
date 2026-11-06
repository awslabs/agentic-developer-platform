"""Combined selectors must execute every required protected binding step."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.e2e.tools import atlassian_scenario_selection as selection
from tests.e2e.tools import runner


def test_partial_wildcard_cannot_report_a_complete_original_matrix(tmp_path):
    registered = (runner.Scenario("SCN-8454-01", ("AC-01",), "atlassian_setup"),)
    assert not runner.evaluate(
        "SCN-8454-*",
        tmp_path / "report",
        "https://gateway.example.test",
        "a" * 40,
        "gateway@sha256:" + "b" * 64,
        "facade@sha256:" + "c" * 64,
        command=["unused"],
        scenarios=registered,
    )
    report = json.loads((tmp_path / "report/evidence.json").read_text())
    assert report["unregistered_scenarios"] == [
        f"SCN-8454-{number:02}" for number in range(2, 7)
    ]
    assert report["matrix_complete"] is False


@pytest.mark.parametrize("pattern", ["SCN-8454-0[12]", "SCN-8454-0[124]"])
def test_runner_main_materializes_setup_and_native_bindings(monkeypatch, pattern):
    from tests.e2e.tools import atlassian_native_matrix

    captured = {}
    fixtures, plans, setup = (
        {"private": "fixtures"},
        {"private": "plans"},
        {"private": "setup"},
    )
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_REF", "refs/heads/main")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE", "private-clients")
    monkeypatch.setenv("ADP_TOOLS_ATLASSIAN_NATIVE_PLAN_FILE", "private-plans")
    monkeypatch.setattr(runner, "private_access_bindings", lambda: (None, None))
    monkeypatch.setattr(runner, "private_release_bindings", lambda: (None, None))
    monkeypatch.setattr(
        runner,
        "private_atlassian_setup_bindings",
        lambda *args: (lambda: setup, None, None),
    )
    monkeypatch.setattr(runner, "load_isolated_fixtures", lambda path: fixtures)
    monkeypatch.setattr(
        atlassian_native_matrix, "load_native_plans", lambda path: plans
    )

    def evaluate(*args, **kwargs):
        captured.update(kwargs)
        return False

    monkeypatch.setattr(runner, "evaluate", evaluate)
    assert (
        runner.main(
            [
                "--scenario-filter",
                pattern,
                "--output",
                "private-output",
                "--gateway-url",
                "https://gateway.example.test",
                "--gateway-revision",
                "a" * 40,
                "--gateway-image",
                "gateway@sha256:" + "b" * 64,
                "--facade-image",
                "facade@sha256:" + "c" * 64,
            ]
        )
        == 1
    )
    assert captured["atlassian_setup_fixture"] is setup
    assert captured["atlassian_client_fixtures"] is fixtures
    assert captured["atlassian_native_plans"] is plans


def test_workflow_uses_selection_before_credentials_and_for_cleanup():
    import yaml

    workflow = (
        Path(__file__).resolve().parents[3]
        / ".github/workflows/tools-live-evaluation.yml"
    )
    steps = yaml.safe_load(workflow.read_text())["jobs"]["evaluate"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Require approved definition and source history") < names.index(
        "Resolve selected scenario bindings"
    )
    assert names.index("Resolve selected scenario bindings") < names.index(
        "Install locked Atlassian observer dependencies"
    )
    assert names.index("Resolve selected scenario bindings") < names.index(
        "Preserve runner identity for private CI evidence"
    )
    for step in steps:
        assert "'SCN-8454-" not in step.get("if", "")
    cleanup = next(
        step
        for step in steps
        if step.get("name")
        == "Remove owned evaluation configuration and test-user session"
    )
    assert (
        "steps.scenario-selection.outputs.atlassian_setup"
        in cleanup["env"]["ATLASSIAN_SETUP_SELECTED"]
    )
    assert '"$ATLASSIAN_SETUP_SELECTED"' in cleanup["run"]


def test_reader_main_uses_both_setup_and_runtime_bindings_for_combined_selection(
    monkeypatch,
):
    from tests.e2e.tools import (
        evidence_reader,
        atlassian_setup_live,
        atlassian_recovery_consumer,
    )

    env = {
        "SCENARIO_FILTER": "SCN-8454-0[124]",
        "GATEWAY_URL": "https://gateway.example.test",
        "EXPECTED_ACCOUNT": "000000000000",
        "EXPECTED_ROLE": "arn:aws:iam::000000000000:role/adp-dev-tools-trusted-deployment",
        "ADP_TOOLS_ATLASSIAN_SETUP_FILE": "private-setup",
        "ADP_TOOLS_ATLASSIAN_CLIENT_MANIFEST_FILE": "private-clients",
        "GITHUB_REPOSITORY": "aws-e/adp",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SHA": "a" * 40,
        "GATEWAY_REVISION": "b" * 40,
        "SOURCE_REVISION": "b" * 40,
        "GATEWAY_IMAGE": "gateway@sha256:" + "c" * 64,
        "FACADE_IMAGE": "facade@sha256:" + "d" * 64,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("ADP_TOOLS_ATLASSIAN_FLOW_OUTPUT", raising=False)
    config = {
        "run_id": "original",
        "owner_id": "setup-owner",
        "fixture_kind": "fresh",
        "gateway_url": env["GATEWAY_URL"],
        "account": env["EXPECTED_ACCOUNT"],
    }
    monkeypatch.setattr(
        atlassian_setup_live, "load_setup", lambda *args, **kwargs: ({}, config)
    )
    monkeypatch.setattr(
        atlassian_recovery_consumer,
        "protected_bindings",
        lambda *args, **kwargs: {"bound": True},
    )
    observer = SimpleNamespace(
        account=env["EXPECTED_ACCOUNT"],
        region="us-east-1",
        cluster="fixture-cluster",
        components={},
    )
    monkeypatch.setattr(
        evidence_reader, "observer_from_environment", lambda env: observer
    )
    captured = {}

    def verify(*args, **kwargs):
        captured.update(kwargs)
        return 1

    monkeypatch.setattr(evidence_reader, "verify_retained", verify)
    evidence_reader.main()
    assert captured["setup_binding"]["owner_id"] == "setup-owner"
    assert captured["recovery_bindings"] == {"bound": True}
    assert captured["authorization_binding"]["role"] == env["EXPECTED_ROLE"]


@pytest.fixture
def complete_registry():
    return tuple(
        SimpleNamespace(identifier=identifier) for identifier in selection.SCENARIOS
    ) + (SimpleNamespace(identifier="SCN-8067-02"),)


@pytest.mark.parametrize("pattern", ["SCN-8454-*", "SCN-8454-0?", "SCN-8454-0[1-6]"])
def test_full_selection_binds_setup_and_clients(pattern, complete_registry):
    flags = selection.selection_flags(pattern, scenarios=complete_registry)
    assert flags and all(value is True for value in flags.values())


@pytest.mark.parametrize("pattern", ["SCN-8454-0[24]", "SCN-8454-0[42]"])
def test_combined_matrix_recovery_needs_both_flags(pattern, complete_registry):
    flags = selection.selection_flags(pattern, scenarios=complete_registry)
    assert {name for name, value in flags.items() if value} == {
        "atlassian",
        "atlassian_matrix",
        "atlassian_recovery",
        "atlassian_clients",
        "atlassian_native",
    }


def test_setup_only_does_not_import_a_generic_user_session(complete_registry):
    flags = selection.selection_flags("SCN-8454-01", scenarios=complete_registry)
    assert {name for name, value in flags.items() if value} == {
        "atlassian",
        "atlassian_setup",
    }


@pytest.mark.parametrize("identifier", ["SCN-8454-03", "SCN-8454-05", "SCN-8454-06"])
def test_remaining_scenarios_request_client_bindings(identifier, complete_registry):
    flags = selection.selection_flags(identifier, scenarios=complete_registry)
    assert flags["atlassian"] and flags["atlassian_clients"]
    assert flags["atlassian_" + selection.SCENARIOS[identifier]]
    assert not flags["atlassian_setup"] and not flags["atlassian_full"]


def test_legacy_selection_does_not_request_atlassian_configuration(complete_registry):
    assert not any(
        selection.selection_flags("SCN-8067-02", scenarios=complete_registry).values()
    )


def test_partial_registry_is_not_full_or_automatically_extended():
    cases = tuple(
        SimpleNamespace(identifier=value) for value in ("SCN-8454-01", "SCN-8454-02")
    )
    flags = selection.selection_flags("SCN-8454-*", scenarios=cases)
    assert flags["atlassian_setup"] and flags["atlassian_clients"]
    assert not flags["atlassian_full"] and not flags["atlassian_recovery"]
    with pytest.raises(ValueError):
        selection.selection_flags("SCN-8454-04", scenarios=cases)


@pytest.mark.parametrize("pattern", ["", "SCN-02", "no-matching-case", "x" * 513, None])
def test_invalid_selection_fails_before_configuration(pattern, complete_registry):
    with pytest.raises(ValueError):
        selection.selection_flags(pattern, scenarios=complete_registry)


def test_future_case_cannot_silently_skip_its_binding():
    with pytest.raises(ValueError):
        selection.selection_flags(
            "SCN-8454-*", scenarios=[SimpleNamespace(identifier="SCN-8454-07")]
        )


def test_cli_uses_actual_registry_and_emits_only_boolean_outputs(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "github-output"
    output.write_text("previous=value\n")
    monkeypatch.setenv("SCENARIO_FILTER", "SCN-8454-0[12]")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert selection.main() == 0
    flags = json.loads(capsys.readouterr().out)
    assert flags == selection.selection_flags(
        "SCN-8454-0[12]", scenarios=runner.SCENARIOS
    )
    assert flags["atlassian_setup"] and flags["atlassian_clients"]
    lines = output.read_text().splitlines()
    assert lines[0] == "previous=value"
    assert len(lines) == 1 + len(flags)
    assert all(line.split("=", 1)[1] in {"true", "false"} for line in lines[1:])


def test_cli_invalid_selection_does_not_emit_success_flags(
    tmp_path, monkeypatch, capsys
):
    output = tmp_path / "github-output"
    monkeypatch.setenv("SCENARIO_FILTER", "SCN-8454-99")
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert selection.main() == 2
    assert not output.exists()
    assert not capsys.readouterr().out
