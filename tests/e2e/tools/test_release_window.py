import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from modules.tools.scripts.verify_release import verify_evaluation_receipt

from modules.tools.scripts.release_window import selected_workflows, verify_window
from tests.e2e.tools import runner


SOURCE = "a" * 40
ACCOUNT = "123456789012"
IMAGE = "example/repo@sha256:" + "b" * 64
DIGEST = "sha256:" + "b" * 64
PATHS = [
    "modules/tools/agentcore/agentcore_tools/handler.py",
    "modules/tools/platform/schemas/github.yaml",
    "modules/tools/interceptor/release_evidence.py",
    "modules/tools/facade/src/main.py",
    "modules/gateway/src/admin/tool_routes.py",
    "modules/gateway/cli/adp",
    "modules/gateway/frontend/src/App.tsx",
    "modules/agent-context/scripts/door_release_evidence.py",
    "modules/agent-factory/agent-worker-image/Dockerfile",
    "modules/tools/task-sdk/README.md",
]


def fixture():
    components = {}
    for component, workflow in selected_workflows(PATHS).items():
        expected = {"image": DIGEST}
        if component == "shared Lambda and Browser":
            expected = {"lambda": DIGEST, "browser": DIGEST}
        receipt = {"component": component, "workflow": workflow,
                   "source_revision": SOURCE, "environment": "dev", "account": ACCOUNT,
                   "run_id": 18, "run_attempt": 2, "mode": "apply", "evidence_type": "independent-runtime",
                   "component_digests": expected, "runtime_digests": expected}
        if component == "shared Lambda and Browser":
            receipt.update(state_sha256="c" * 64, workflow_revision="d" * 40,
                           config={key: "e" * 64 for key in
                                   ("backend_sha256", "tfvars_sha256", "state_identity_sha256")},
                           images={name: {"built": IMAGE, "runtime": IMAGE} for name in expected})
        components[component] = {"source_revision": SOURCE, "expected_runtime": expected, "receipt": receipt}
    expected = {name: component["expected_runtime"] for name, component in components.items()}
    return {"source_revision": SOURCE, "environment": "dev", "account": ACCOUNT,
            "paths": PATHS.copy(), "components": components,
            "readbacks": {"before": copy.deepcopy(expected), "after": copy.deepcopy(expected)},
            "provider_request_count": 0}


def test_release_ac04_isolated_selected_set_and_exact_source_run_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: pytest.fail("fixture contacted gateway"))
    manifest = fixture()
    assert {"shared Lambda and Browser", "platform targets", "interceptor", "Door", "worker image",
            "Task SDK client", "gateway-installed CLI", "frontend client", "gateway", "facade and reconciler"} == set(manifest["components"])
    passed = runner.evaluate("SCN-8068-04", tmp_path, "https://example.invalid", SOURCE, IMAGE, IMAGE,
                             command=[], contract_fixture=manifest,
                             client_factory=lambda _: pytest.fail("fixture contacted provider"))
    row, = json.loads((tmp_path / "evidence.json").read_text())["scenarios"]
    assert passed and row["status"] == "pass" and row["evidence_scope"] == "repository-integration"
    assert row["observations"]["provider_request_count"] == 0
    assert row["observations"]["receipts"]["shared Lambda and Browser"] == {
        "source_revision": SOURCE, "run_id": 18, "run_attempt": 2}
    assert json.loads((tmp_path / "evidence.json").read_text())["live_attestation"] is False


@pytest.mark.parametrize("corrupt", [
    lambda data: data["components"].pop("platform targets"),
    lambda data: data["components"]["gateway"]["receipt"].update(mode="plan"),
    lambda data: data["components"]["Door"]["receipt"].update(run_attempt=0),
    lambda data: data["components"]["shared Lambda and Browser"]["receipt"]["images"]["browser"].update(runtime="old"),
    lambda data: data["readbacks"]["after"]["worker image"].update(image="sha256:" + "f" * 64),
    lambda data: data["readbacks"]["before"].pop("interceptor"),
])
def test_release_ac04_rejects_missing_plan_or_changed_runtime(corrupt):
    manifest = fixture()
    corrupt(manifest)
    with pytest.raises(ValueError):
        verify_window(manifest, lambda phase: manifest["readbacks"][phase], source=SOURCE,
                      account=ACCOUNT, gateway_image=IMAGE, facade_image=IMAGE)


def four_route_fixture(status="fenced"):
    manifest = fixture()
    group = {"group": "tool-connectors", "generation": 2, "status": status,
             "routes": {name: {"target_id": name + "-target", "backend": "legacy" if status == "legacy" else "adapter",
                               "digest": DIGEST} for name in ("atlassian", "github", "slack", "jira")}}
    if status == "active":
        manifest["paths"].append("modules/tools/adapters/deploy.py")
        adapter = copy.deepcopy(manifest["components"]["gateway"])
        expected = {"image": DIGEST, "configuration": DIGEST}
        adapter["expected_runtime"] = expected
        adapter["receipt"].update(component="tool-adapters", workflow="tools-platform-deploy.yml",
                                  component_digests=expected, runtime_digests=expected)
        manifest["components"]["tool-adapters"] = adapter
        for phase in ("before", "after"):
            manifest["readbacks"][phase]["tool-adapters"] = copy.deepcopy(expected)
    manifest["route_group"] = group
    for phase in ("before", "after"):
        manifest["readbacks"][phase]["route_group"] = copy.deepcopy(group)
    return manifest


@pytest.mark.parametrize("status", ("legacy", "fenced", "active"))
def test_four_route_manifest_requires_fresh_independent_fence_and_target_readbacks(status):
    manifest = four_route_fixture(status)
    report = verify_window(manifest, lambda phase: manifest["readbacks"][phase], source=SOURCE,
                           account=ACCOUNT, gateway_image=IMAGE, facade_image=IMAGE)
    assert "route_group" not in report["components"]
    assert report["evidence_type"] == "independent-runtime"


@pytest.mark.parametrize("corrupt", [
    lambda manifest: manifest["route_group"]["routes"].pop("jira"),
    lambda manifest: manifest["route_group"]["routes"]["jira"].update(target_id="github-target"),
    lambda manifest: manifest["route_group"]["routes"]["jira"].update(digest="latest"),
    lambda manifest: manifest["route_group"]["routes"]["jira"].update(backend="legacy"),
    lambda manifest: manifest["route_group"].update(generation=0),
    lambda manifest: manifest["route_group"].update(status="unknown"),
    lambda manifest: manifest["readbacks"]["before"]["route_group"].update(status="fenced"),
    lambda manifest: manifest["readbacks"]["before"]["route_group"].update(generation=1),
    lambda manifest: manifest["readbacks"]["after"]["route_group"]["routes"]["jira"].update(digest="sha256:" + "f" * 64),
])
def test_four_route_release_refuses_partial_group_or_changed_readback(corrupt):
    manifest = four_route_fixture("active")
    corrupt(manifest)
    with pytest.raises(ValueError):
        verify_window(manifest, lambda phase: manifest["readbacks"][phase], source=SOURCE,
                      account=ACCOUNT, gateway_image=IMAGE, facade_image=IMAGE)


def test_release_ac04_live_cannot_use_isolated_fixture(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: pytest.fail("fixture contacted gateway"))
    manifest = fixture()
    passed = runner.evaluate("SCN-8068-04", tmp_path, "https://example.invalid", SOURCE, IMAGE, IMAGE,
                             command=[], contract_fixture=manifest, live=True,
                             client_factory=lambda _: pytest.fail("fixture contacted provider"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    assert not passed and report["live_attestation"] is False
    assert report["scenarios"][0]["status"] == "blocked"
    assert "Coordinator" in report["scenarios"][0]["reason"]


def test_protected_evaluation_requires_private_bindings_and_no_token_for_readback():
    workflow = yaml.load((Path(__file__).resolve().parents[3] / ".github/workflows/tools-live-evaluation.yml").read_text(),
                         Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["evaluate"]["steps"]
    bound = next(step for step in steps if step.get("name") == "Bind the approved private release window")
    assert bound["if"] == ("inputs.scenario_filter == 'SCN-8068-04' || inputs.scenario_filter == 'repair' || "
                           "startsWith(inputs.scenario_filter, 'SCN-8067-') || steps.scenario-selection.outputs.atlassian_clients == 'true'")
    assert "secrets.ADP_TOOLS_RELEASE_MANIFEST_JSON" in str(bound)
    assert "secrets.ADP_TOOLS_RELEASE_SELECTORS_JSON" in str(bound)
    assert "ADP_TOOLS_RELEASE_OBSERVER_MODULE" not in str(bound)
    token = next(step for step in steps if step.get("name") == "Import the protected test user's refresh session")
    assert token["if"] == ("inputs.scenario_filter != 'SCN-8068-04' && "
                           "inputs.scenario_filter != 'repair' && "
                           "!startsWith(inputs.scenario_filter, 'SCN-8067-') && "
                           "steps.scenario-selection.outputs.atlassian != 'true'")


def test_evaluation_receipt_rejects_local_or_incomplete_evidence(tmp_path):
    path = tmp_path / "fixture.json"
    report = {"live_attestation": True, "release_source_revision": SOURCE, "environment": "dev", "account": ACCOUNT,
              "github_run_id": 51, "github_run_attempt": 1,
              "scenarios": [{"scenario_id": "SCN-8068-04", "status": "pass",
                             "evidence_scope": "deployed-target",
                             "observations": {"evidence_type": "independent-runtime", "account": ACCOUNT,
                                              "components": ["gateway"],
                                              "receipts": {"gateway": {"source_revision": SOURCE,
                                                                      "run_id": 18, "run_attempt": 2}}}}]}
    path.write_text(json.dumps(report))
    assert verify_evaluation_receipt(path, source=SOURCE, environment="dev", account=ACCOUNT) == report
    for change in ({"live_attestation": False},
                   {"scenarios": [{**report["scenarios"][0], "evidence_scope": "repository-integration"}]},
                   {"scenarios": [{**report["scenarios"][0], "observations": {"evidence_type": "isolated-fixture"}}]}):
        path.write_text(json.dumps({**report, **change}))
        with pytest.raises(ValueError, match="protected SCN-8068-04"):
            verify_evaluation_receipt(path, source=SOURCE, environment="dev", account=ACCOUNT)


def test_evaluation_cli_rejects_foreign_or_missing_account(tmp_path):
    path = tmp_path / "evidence.json"
    report = {"live_attestation": True, "release_source_revision": SOURCE, "environment": "dev",
              "account": ACCOUNT, "github_run_id": 51, "github_run_attempt": 1,
              "scenarios": [{"scenario_id": "SCN-8068-04", "status": "pass", "evidence_scope": "deployed-target",
                             "observations": {"evidence_type": "independent-runtime", "account": ACCOUNT,
                                              "components": ["gateway"], "receipts": {"gateway": {
                                                  "source_revision": SOURCE, "run_id": 18, "run_attempt": 2}}}}]}
    path.write_text(json.dumps(report))
    root = Path(__file__).resolve().parents[3]
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)

    def verify(account):
        return subprocess.run([sys.executable, "-m", "modules.tools.scripts.verify_release",
                               "evaluation", str(path), SOURCE, "dev", account], cwd=root,
                              env=environment, text=True, capture_output=True)

    accepted = verify(ACCOUNT)
    assert accepted.returncode == 0, accepted.stderr
    foreign = verify("000000000001")
    assert foreign.returncode != 0 and "protected SCN-8068-04" in foreign.stderr
    del report["account"]
    path.write_text(json.dumps(report))
    assert verify(ACCOUNT).returncode != 0
    report["account"] = ACCOUNT
    del report["scenarios"][0]["observations"]["account"]
    path.write_text(json.dumps(report))
    assert verify(ACCOUNT).returncode != 0

def test_release_live_evidence_binds_validated_manifest_account(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "verify_gateway", lambda _: pytest.fail("release contacted gateway"))
    manifest = fixture()
    passed = runner.evaluate("SCN-8068-04", tmp_path, "https://example.invalid", SOURCE, IMAGE, IMAGE,
                             command=[], live=True, release_manifest=manifest,
                             release_observer=lambda phase: manifest["readbacks"][phase],
                             release_source_revision=SOURCE, release_account=ACCOUNT)
    evidence = json.loads((tmp_path / "evidence.json").read_text())
    assert passed and evidence["account"] == ACCOUNT
    assert evidence["scenarios"][0]["observations"]["account"] == ACCOUNT
