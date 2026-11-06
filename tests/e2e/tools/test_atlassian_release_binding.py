"""Controlled release execution through the actual runner and semantic reader."""

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tests.e2e.tools import atlassian_flow_producer as producer
from tests.e2e.tools import atlassian_flow_summary as flow
from tests.e2e.tools import atlassian_release_binding as binding
from tests.e2e.tools import atlassian_release_scenario as release
from tests.e2e.tools import evidence_reader, runner
from tests.e2e.tools import test_atlassian_release_scenario as fixtures


def environment(tmp_path):
    flow._write_private(tmp_path / "release.json", {})
    return {
        "ADP_TOOLS_RELEASE_MANIFEST_FILE": str(tmp_path / "release.json"),
        "ADP_TOOLS_ATLASSIAN_RELEASE_JSON": json.dumps(fixtures.config()),
        "RUNNER_TEMP": str(tmp_path), "SOURCE_REVISION": "a" * 40,
        "GITHUB_SHA": "e" * 40, "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "1",
        "EXPECTED_ACCOUNT": fixtures.ACCOUNT, "AWS_REGION": fixtures.REGION,
        "EXPECTED_ROLE": "arn:aws:iam::123456789012:role/adp-dev-tools-trusted-deployment",
        "GATEWAY_URL": "https://gateway.example.test", "EKS_CLUSTER": "test-cluster",
    }


def test_materialized_release_uses_bound_read_identity_and_no_mutation_entrypoint(tmp_path, monkeypatch):
    env = environment(tmp_path)
    path = binding.materialize(env)
    assert path.stat().st_mode & 0o777 == 0o600
    env["ADP_TOOLS_ATLASSIAN_RELEASE_FILE"] = str(path)
    campaign = binding.ReleaseCampaign(env)
    captured = {}

    def collect(path, **kwargs):
        captured.update(kwargs)
        assert flow._read_private(path) == fixtures.config()
        return {"observations": {}}

    monkeypatch.setattr(binding, "collect_release", collect)
    monkeypatch.setattr(release, "rehearse_release", lambda *args, **kwargs: pytest.fail("evaluation attempted mutation"))
    assert campaign.collect() == {"observations": {}}
    assert captured["environment"]["ADP_TOOLS_DEPLOY_ROLE_ARN"] == env["EXPECTED_ROLE"]
    assert captured["binding"] == binding.release_binding(env)
    assert captured["journal_path"] is None


@pytest.mark.parametrize("fault", ["source", "gateway", "raw_pass", "account"])
def test_protected_release_binding_rejects_foreign_or_asserted_configuration(tmp_path, fault):
    env = environment(tmp_path)
    doc = fixtures.config()
    if fault == "source":
        doc["handoff"]["plan"]["after"]["release"]["source_revision"] = "b" * 40
    elif fault == "gateway":
        doc["gateway_url"] = "https://foreign.example.test"
    elif fault == "account":
        doc["account"] = "999999999999"
    else:
        doc["qualified_scenarios"] = list(flow.FLOW_SCENARIOS)
    env["ADP_TOOLS_ATLASSIAN_RELEASE_JSON"] = json.dumps(doc)
    with pytest.raises(ValueError):
        binding.materialize(env)
    assert not list(tmp_path.glob("atlassian-release-*"))


@pytest.fixture
def evaluated(tmp_path, monkeypatch):
    parts = fixtures.setup(tmp_path, monkeypatch)
    fixtures.execute(parts)
    observed = fixtures.collect(parts)
    target = {"gateway": "a" * 40, "gateway_image": "gateway@sha256:" + "b" * 64,
              "facade_image": "facade@sha256:" + "c" * 64}
    context = {"target": target, "release_manifest": parts[2], "release_binding": fixtures.BINDING}
    campaign = SimpleNamespace(manifest=parts[2], binding=fixtures.BINDING, collect=lambda: deepcopy(observed))
    for key, value in {"GITHUB_SHA": "e" * 40, "GITHUB_RUN_ID": "1234", "GITHUB_RUN_ATTEMPT": "1"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: None)
    output = tmp_path / "evidence"
    assert not runner.evaluate(
        "SCN-8454-06", output, "https://gateway.example.test", target["gateway"],
        target["gateway_image"], target["facade_image"], command=["never-called"],
        client_factory=lambda *_: pytest.fail("unbound client started"), live=True,
        atlassian_flow_binding=fixtures.BINDING, atlassian_release_campaign=campaign,
        atlassian_context_factory=lambda: dict(context),
    )
    files = {path.name: path.read_bytes() for path in output.glob("*.json")}
    report = flow._decode(files["evidence.json"])
    return report, files, context


def test_registered_selection_executes_collector_but_does_not_promote_infrastructure(evaluated):
    report, _, _ = evaluated
    assert report["selected_scenario_ids"] == ["SCN-8454-06"]
    assert report["matrix_complete"] is True
    assert report["unregistered_scenarios"] == []
    row = report["scenarios"][0]
    assert row["status"] == "blocked" and row["handoff"]["full_ac06"] == "pending"
    assert sum(case["status"] == "pass" for case in row["handoff"]["cases"]) == 6
    assert report["live_attestation"] is False
    assert len(runner.select_scenarios("SCN-8454-*")) == 6


def test_reader_rejects_forged_complete_handoff_without_original_client_scenarios(evaluated):
    report, files, context = evaluated
    row = report["scenarios"][0]
    row.update(status="pass", cleanup={"status": "confirmed"})
    row["handoff"]["full_ac06"] = "pass"
    with pytest.raises(ValueError):
        evidence_reader.validate_atlassian_rows(
            files, report, selected={"SCN-8454-06"}, run_id="1234", attempt="1", revision="e" * 40,
            **context,
        )


def test_summary_producer_and_reader_preserve_all_unverified_predicates(evaluated):
    report, files, context = evaluated
    reports = producer.build_reports(report, files=files, binding=fixtures.BINDING, context=context)
    assert len(reports["live"]["criteria"]) == 42
    assert len(reports["stages"]) == 7
    assert not reports["summary"]["completion_ready"]
    assert all(row["status"] == "unverified" for row in reports["live"]["criteria"])
    files.update({"completion-reports.json": flow.canonical(reports), "live.json": flow.canonical(reports["live"]),
                  **{f"stage-{stage}.json": flow.canonical(value) for stage, value in reports["stages"].items()}})
    validation = {**context, **context["target"]}
    assert evidence_reader.read_flow_bundle(files, binding=fixtures.BINDING, validation=validation) == reports["summary"]
    reports["summary"]["completion_ready"] = True
    files["completion-reports.json"] = flow.canonical(reports)
    with pytest.raises(ValueError):
        evidence_reader.read_flow_bundle(files, binding=fixtures.BINDING, validation=validation)


def test_other_scenarios_cannot_establish_transition_parity(evaluated):
    report, _, context = evaluated
    dependencies = {f"SCN-8454-{number:02}" for number in range(2, 6)}
    result = release.qualify_handoff(report["scenarios"][0]["observations"], manifest=context["release_manifest"],
                                     binding=fixtures.BINDING, qualified_scenarios=dependencies)
    assert result["full_ac06"] == "pending"
    assert all(row["status"] == "pending" for row in result["cases"]
               if row["case"] in {"claude-code-parity", "codex-parity", "provider-grants-no-duplicate"})
    for dependency in dependencies:
        result = release.qualify_handoff(report["scenarios"][0]["observations"], manifest=context["release_manifest"],
                                         binding=fixtures.BINDING, qualified_scenarios=dependencies - {dependency})
        assert result["full_ac06"] == "pending"


def test_controlled_release_observations_cannot_pass_live_inventory(evaluated):
    report, files, context = evaluated
    report["scenarios"][0]["evidence_scope"] = "repository-integration"
    reports = producer.build_reports(report, files=files, binding=fixtures.BINDING, context=context)
    assert all(case["status"] == "unverified" for case in reports["live"]["cases"])
    assert reports["live"]["final_release_state"] == "unverified"


@pytest.mark.parametrize("case", ["claude-code-parity", "codex-parity", "provider-grants-no-duplicate"])
def test_passing_dependencies_cannot_override_disagreeing_transition_evidence(evaluated, case):
    report, _, context = evaluated
    observed = deepcopy(report["scenarios"][0]["observations"])
    dependencies = {f"SCN-8454-{number:02}" for number in range(2, 6)}
    observed["observations"]["client_provider_transition"] = {
        "original_operation_id": "operation-a", "before_effects": 1, "after_effects": 2,
        "before_grant": "grant-a", "after_grant": "grant-b", "client_parity": False,
    }
    result = release.qualify_handoff(observed, manifest=context["release_manifest"], binding=fixtures.BINDING,
                                     qualified_scenarios=dependencies)
    assert result["full_ac06"] == "pending"
    assert next(row for row in result["cases"] if row["case"] == case)["status"] == "pending"
    next(row for row in observed["cases"] if row["case"] == case)["status"] = "pass"
    with pytest.raises(ValueError):
        release.qualify_handoff(observed, manifest=context["release_manifest"], binding=fixtures.BINDING,
                                qualified_scenarios=dependencies)
def test_completion_qualification_waits_for_captured_setup_evidence(tmp_path):
    from tests.e2e.tools.test_atlassian_evidence_reader import data

    files, report, arguments = data(tmp_path, setup=True)
    bound = {"source_revision": arguments["release_source_revision"], "definition_revision": arguments["revision"],
             "run_id": arguments["run_id"], "run_attempt": arguments["attempt"], "release_sha256": "f" * 64}
    context = {"setup_binding": arguments["setup_binding"],
               "target": {key: arguments[key] for key in ("gateway", "gateway_image", "facade_image")}}
    assert producer.qualify_scenarios(report, files={}, binding=bound, context=context) == set()
    assert producer.qualify_scenarios(report, files=files, binding=bound, context=context) == {"SCN-8454-01"}
    expected = producer.build_reports(report, files=files, binding=bound, context=context)
    files.update({"completion-reports.json": flow.canonical(expected), "live.json": flow.canonical(expected["live"]),
                  **{f"stage-{stage}.json": flow.canonical(value) for stage, value in expected["stages"].items()}})
    assert evidence_reader.read_flow_bundle(files, binding=bound,
                                            validation={**arguments, **context}) == expected["summary"]


def test_summary_command_publishes_private_rebuildable_reports(evaluated, tmp_path, monkeypatch):
    report, files, context = evaluated
    directory = tmp_path / "tools-eval-evidence"
    directory.mkdir(mode=0o700)
    flow._write_private(directory / "evidence.json", report)
    monkeypatch.setenv("RUNNER_TEMP", str(tmp_path))
    monkeypatch.setattr(producer, "release_binding", lambda _: fixtures.BINDING)
    monkeypatch.setattr(producer, "protected_context", lambda _: context)
    producer.main()
    output = {path.name: path.read_bytes() for path in directory.glob("*.json")}
    expected = producer.build_reports(report, files=files, binding=fixtures.BINDING, context=context)
    assert flow._decode(output["completion-reports.json"]) == expected
    assert flow._decode(output["atlassian-flow-summary.json"]) == expected["summary"]
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in directory.glob("*.json"))
    assert evidence_reader.read_flow_bundle(output, binding=fixtures.BINDING,
                                            validation={**context, **context["target"]}) == expected["summary"]


def test_runner_command_binds_release_and_executes_selected_collector(evaluated, tmp_path, monkeypatch):
    report, _, context = evaluated
    env = environment(tmp_path)
    env.update(GITHUB_EVENT_NAME="workflow_dispatch", GITHUB_REF="refs/heads/main",
               GATEWAY_REVISION=context["target"]["gateway"], GATEWAY_IMAGE=context["target"]["gateway_image"],
               FACADE_IMAGE=context["target"]["facade_image"])
    path = binding.materialize(env)
    env["ADP_TOOLS_ATLASSIAN_RELEASE_FILE"] = str(path)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runner, "private_access_bindings", lambda: (None, None))
    monkeypatch.setattr(runner, "private_release_bindings", lambda: ({}, None))
    calls = []

    def collect(config_path, **arguments):
        calls.append((config_path, arguments))
        observed = deepcopy(report["scenarios"][0]["observations"])
        observed["binding"] = arguments["binding"]
        return observed

    monkeypatch.setattr(binding, "collect_release", collect)
    output = tmp_path / "command-evidence"
    assert runner.main(["--scenario-filter", "SCN-8454-06", "--output", str(output),
                        "--gateway-url", env["GATEWAY_URL"], "--gateway-revision", env["GATEWAY_REVISION"],
                        "--gateway-image", env["GATEWAY_IMAGE"], "--facade-image", env["FACADE_IMAGE"]]) == 1
    assert len(calls) == 1 and calls[0][0] == str(path)
    produced = flow._decode((output / "evidence.json").read_bytes())
    assert produced["unregistered_scenarios"] == []
    assert produced["scenarios"][0]["handoff"]["full_ac06"] == "pending"
    assert produced["scenarios"][0]["status"] == "blocked"


def test_workflow_binds_collects_produces_then_independently_reads():
    workflow = yaml.safe_load((Path(__file__).resolve().parents[3] / ".github/workflows/tools-live-evaluation.yml").read_text())
    steps = workflow["jobs"]["evaluate"]["steps"]
    positions = {step.get("name"): index for index, step in enumerate(steps)}
    names = ["Bind read-only adapter release observations", "Exercise deployed gateway through the MCP client",
             "Capture non-token setup recovery state",
             "Produce the independently qualified acceptance inventory", "Publish result and failure evidence",
             "Retrieve and validate protected client evidence", "Remove owned evaluation configuration and test-user session"]
    assert [positions[name] for name in names] == sorted(positions[name] for name in names)
    bind = steps[positions[names[0]]]
    assert bind["if"] == "steps.scenario-selection.outputs.atlassian_handoff == 'true'"
    assert bind["run"] == "python3 -m tests.e2e.tools.atlassian_release_binding"
    assert "rehearse_release" not in str(workflow)
