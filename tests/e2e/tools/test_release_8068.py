import json
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from modules.tools.scripts.release_journal import bindings_sha256, initialize, start
from modules.tools.scripts import reconcile_release
from modules.tools.scripts.reconcile_release import github_json, reconcile
from modules.tools.scripts.release_scope import operator_sequence, verify_shared_receipt
from tests.e2e.tools import runner
from tests.e2e.tools.test_release_window import four_route_fixture as previous_release_fixture


SOURCE = "a" * 40
IMAGE = "example/repo@sha256:" + "b" * 64
EXPECTED = [
    {"component": "protected bootstrap", "workflow": None,
     "trigger": "coordinator prepares scoped build, release role and private state bindings"},
    {"component": "shared Lambda and Browser", "workflow": "shared-agentcore-tools-deploy.yml",
     "trigger": "dispatch apply"},
    {"component": "platform targets", "workflow": "tools-platform-deploy.yml",
     "trigger": "dispatch apply after shared runtime receipt"},
]
CHANGED = ["modules/tools/platform/schemas/github.yaml", "modules/tools/agentcore/agentcore_tools/handler.py"]


def fixture():
    return {"source_revision": SOURCE, "run_id": 18, "run_attempt": 2, "paths": CHANGED,
            "mode": "plan", "claimed_runtime": False, "provider_request_count": 0,
            "expected_steps": EXPECTED}


def test_release_ac01_uses_isolated_source_run_attempt_without_provider(tmp_path, monkeypatch):
    assert [case.identifier for case in runner.select_scenarios("SCN-8068-01")] == ["SCN-8068-01"]
    monkeypatch.setattr(runner, "verify_gateway", lambda _: pytest.fail("release fixture contacted gateway"))
    passed = runner.evaluate("SCN-8068-01", tmp_path, "https://example.invalid", SOURCE, IMAGE, IMAGE,
                             command=[], contract_fixture=fixture(),
                             client_factory=lambda _: pytest.fail("release fixture contacted provider"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    row = report["scenarios"][0]
    assert passed and report["live_attestation"] is False and row["status"] == "pass"
    assert row["observations"]["steps"] == EXPECTED == operator_sequence(CHANGED)
    assert row["observations"]["evidence_type"] == "isolated-fixture"
    assert (row["observations"]["run_id"], row["observations"]["run_attempt"]) == (18, 2)
    assert row["observations"]["provider_request_count"] == 0


def test_release_ac01_rejects_plan_runtime_claim_and_omitted_schema():
    case = fixture()
    case["claimed_runtime"] = True
    assert runner.release_ac01(case)[0] == "fail"
    case["claimed_runtime"] = False
    case["expected_steps"] = EXPECTED[:2]
    assert runner.release_ac01(case)[0] == "fail"
    case["expected_steps"] = EXPECTED
    case["provider_request_count"] = 1
    assert runner.release_ac01(case)[0] == "blocked"


def test_shared_receipt_refuses_plan_only_and_mixed_runtime(tmp_path):
    path = tmp_path / "shared.json"
    receipt = {"source_revision": SOURCE, "environment": "dev", "account": "123456789012",
               "mode": "apply", "run_id": 18, "run_attempt": 2,
               "images": {name: {"built": IMAGE, "runtime": IMAGE} for name in ("lambda", "browser")}}
    path.write_text(json.dumps(receipt))
    assert verify_shared_receipt(path, source=SOURCE, environment="dev", account="123456789012") == receipt
    for invalid in ({"mode": "plan"}, {"source_revision": "c" * 40},
                    {"images": {"lambda": {"built": IMAGE, "runtime": IMAGE}}},
                    {"images": {**receipt["images"], "browser": {"built": IMAGE, "runtime": "other"}}}):
        path.write_text(json.dumps({**receipt, **invalid}))
        with pytest.raises(ValueError):
            verify_shared_receipt(path, source=SOURCE, environment="dev", account="123456789012")


def test_platform_release_requires_original_direct_dispatch_and_native_inputs_before_role():
    workflow = yaml.load((Path(__file__).resolve().parents[3] / ".github/workflows/tools-platform-deploy.yml").read_text(),
                         Loader=yaml.BaseLoader)
    release = workflow["jobs"]["release"]
    assert release["if"] == (
        "github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' "
        "&& github.run_attempt == 1 && inputs.mode != 'drift' && inputs.stage != 'tool-adapters'"
    )
    steps = release["steps"]
    inputs = next(index for index, step in enumerate(steps)
                  if "native_smoke_inputs download" in step.get("run", ""))
    assume = next(index for index, step in enumerate(steps)
                  if step.get("name") == "Assume environment-scoped deployment role")
    apply = next(index for index, step in enumerate(steps)
                 if step.get("run") == "bash modules/tools/deploy.sh --infra --targets")
    setup = next(index for index, step in enumerate(steps)
                 if step.get("uses", "").startswith("hashicorp/setup-terraform@"))
    assert inputs < assume < setup < apply
    assert steps[inputs]["env"]["ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN"] == (
        "${{ secrets.ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN }}"
    )
    assert "modules/tools/platform/schemas/**" in workflow["on"]["push"]["paths"]
    # Pushes still trigger reviewed-source checks. Both schema-only and mixed
    # releases require dispatch, and mixed changes retain the provider receipt order.
    assert operator_sequence([CHANGED[0]]) == [
        {"component": "platform targets", "workflow": "tools-platform-deploy.yml",
         "trigger": "dispatch apply with native approval"},
    ]
    assert operator_sequence(CHANGED) == EXPECTED


def test_adapter_runbook_triggers_match_protected_workflow_source():
    root = Path(__file__).resolve().parents[3]
    guide = (root / "modules/tools/adapters/README.md").read_text()

    def workflow(name):
        assert f"`{name}`" in guide
        return yaml.load((root / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)

    for name in ("gateway-deploy.yml", "gateway-frontend-deploy.yml",
                 "tools-facade-deploy.yml", "tools-interceptor-deploy.yml"):
        assert {"push", "workflow_dispatch"} <= set(workflow(name)["on"])
    migrations = workflow("run-gateway-migrations.yml")
    assert {"workflow_call", "workflow_dispatch"} <= set(migrations["on"])
    shared = workflow("shared-agentcore-tools-deploy.yml")
    assert set(shared["on"]) == {"workflow_dispatch"}

    adapter = workflow("tools-platform-deploy.yml")
    inputs = adapter["on"]["workflow_dispatch"]["inputs"]
    assert inputs["mode"]["options"] == ["release", "drift", "identity"]
    assert inputs["stage"]["options"] == ["platform", "tool-adapters"]
    for job_name in ("adapter-foundation", "adapter-handoff"):
        condition = adapter["jobs"][job_name]["if"]
        for gate in ("github.ref == 'refs/heads/main'", "github.event_name == 'workflow_dispatch'",
                     "github.run_attempt == 1", "inputs.mode == 'release'", "inputs.environment == 'dev'",
                     "inputs.stage == 'tool-adapters'", "inputs.adp_correlation != ''"):
            assert gate in condition
    handoff = adapter["jobs"]["adapter-handoff"]
    assert handoff["needs"] == "guard"
    assert handoff["environment"] == "adp-tools-handoff-dev"
    for action in ("handoff", "rollback"):
        assert action in inputs["adapter_action"]["options"]
        assert f"inputs.adapter_action == '{action}'" in handoff["if"]
    for name, rollback_input in (("tools-facade-deploy.yml", "rollback_to_digest"),
                                 ("tools-interceptor-deploy.yml", "rollback_to_version"),
                                 ("gateway-deploy.yml", "product_rollback")):
        assert rollback_input in workflow(name)["on"]["workflow_dispatch"]["inputs"]
        assert f"`{rollback_input}`" in guide
    assert "`adapter_action=handoff` or `rollback`" in guide
    assert "original coordinated route plan" in guide


def test_manual_walkthrough_uses_pinned_workflow_source_and_independent_results():
    root = Path(__file__).resolve().parents[3]
    guide = (root / "modules/tools/adapters/README.md").read_text()
    section = guide.split("### Source-pinned role walkthrough (SCN-03)", 1)[1].split(
        "## Controlled foundation qualification", 1
    )[0]
    assert 'git show "$REVIEWED_SOURCE":.github/workflows/tools-platform-deploy.yml' in section
    for marker in ("workflow definition commit", "requirement revision", "manifest",
                   "independent operator", "`blocked`", "`not-run`", "cleanup outcome"):
        assert marker in section
    expectations = {
        "User": ("served UI and installed CLI", "Disconnect only"),
        "Administrator": ("count registrations", "pending recovery jobs"),
        "Operator": ("four independently served routes", "release the fence only after"),
    }
    for role, (observation, cleanup) in expectations.items():
        row = next(line for line in section.splitlines() if line.startswith(f"| {role} |"))
        assert len(row.split("|")) == 6
        assert observation in row and cleanup in row

    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    assert len(revision) == 40
    definition = subprocess.check_output(
        ["git", "show", f"{revision}:.github/workflows/tools-platform-deploy.yml"], cwd=root, text=True
    )
    workflow = yaml.load(definition, Loader=yaml.BaseLoader)
    assert "inputs.mode == 'release'" in workflow["jobs"]["adapter-foundation"]["if"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["stage"]["options"] == [
        "platform", "tool-adapters"
    ]

MANIFEST = {"platform targets": {"targets": "sha256:" + "c" * 64}}
WORKFLOW = "tools-platform-deploy.yml"
NEWER = "d" * 40


def run(*, source=SOURCE, run_id=18, attempt=2, status="completed", conclusion="success",
        workflow=WORKFLOW, event="push"):
    return {"workflow": workflow, "head_sha": source, "id": run_id, "run_attempt": attempt,
            "event": event, "status": status, "conclusion": conclusion}


def receipt(observed, *, manifest=MANIFEST, mode="apply", component="platform targets"):
    return {"component": component, "workflow": observed["workflow"],
            "source_revision": observed["head_sha"], "run_id": observed["id"],
            "run_attempt": observed["run_attempt"], "environment": "dev",
            "account": "123456789012", "mode": mode,
            "component_digests": manifest, "runtime_digests": manifest}


def release_fixture():
    observed = run()
    return {"paths": ["modules/tools/platform/schemas/github.yaml"],
            "source_revision": SOURCE, "main_revision": SOURCE,
            "environment": "dev", "account": "123456789012",
            "manifest": MANIFEST, "runs": [observed], "receipts": [receipt(observed)],
            "provider_request_count": 0, "ancestry": [],
            "expected_decisions": [{"component": "platform targets", "action": "reuse",
                                    "source_revision": SOURCE, "run_id": 18, "run_attempt": 2}]}


def reconcile_fixture(fixture):
    ancestors = {tuple(pair) for pair in fixture["ancestry"]}
    return reconcile(operator_sequence(fixture["paths"]), fixture["source_revision"],
                     fixture["main_revision"], fixture["manifest"], fixture["runs"], fixture["receipts"],
                     lambda older, newer: older == newer or (older, newer) in ancestors,
                     environment=fixture["environment"], account=fixture["account"])["decisions"][0]


def test_release_ac02_reuses_observed_automatic_run_without_dispatch(tmp_path, monkeypatch):
    assert [case.identifier for case in runner.select_scenarios("SCN-8068-02")] == ["SCN-8068-02"]
    monkeypatch.setattr(runner, "verify_gateway", lambda _: pytest.fail("fixture contacted gateway"))
    fixture_data = release_fixture()
    passed = runner.evaluate("SCN-8068-02", tmp_path, "https://example.invalid", SOURCE, IMAGE, IMAGE,
                             command=[], contract_fixture=fixture_data,
                             client_factory=lambda _: pytest.fail("fixture contacted provider"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    observations = report["scenarios"][0]["observations"]
    assert passed and report["live_attestation"] is False
    assert report["scenarios"][0]["status"] == "pass"
    assert observations["decisions"] == fixture_data["expected_decisions"]
    assert observations["provider_request_count"] == 0
    assert observations["evidence_type"] == "isolated-fixture"


def test_release_ac02_reuses_compatible_newer_receipt_without_rollback():
    fixture_data = release_fixture()
    newer = run(source=NEWER, run_id=19, attempt=1)
    fixture_data.update(main_revision=NEWER, ancestry=[[SOURCE, NEWER]])
    fixture_data["runs"].append(newer)
    fixture_data["receipts"].append(receipt(newer))
    decision = reconcile_fixture(fixture_data)
    assert (decision["action"], decision["source_revision"], decision["run_id"], decision["run_attempt"]) == (
        "reuse", NEWER, 19, 1)
    fixture_data["expected_decisions"] = [{"component": "platform targets", "action": "reuse",
                                            "source_revision": NEWER, "run_id": 19, "run_attempt": 1}]
    assert runner.release_ac02(fixture_data)[0] == "pass"


def test_release_ac02_never_downgrades_when_newer_main_is_unverified_or_incompatible():
    fixture_data = release_fixture()
    newer = run(source=NEWER, run_id=19, attempt=1)
    fixture_data.update(main_revision=NEWER, ancestry=[[SOURCE, NEWER]])
    fixture_data["runs"].append(newer)
    for receipts in ([receipt(fixture_data["runs"][0])],
                     [receipt(fixture_data["runs"][0]),
                      receipt(newer, manifest={"platform targets": {"targets": "sha256:" + "e" * 64}})]):
        fixture_data["receipts"] = receipts
        decision = reconcile_fixture(fixture_data)
        assert decision["action"] == "blocked" and decision["source_revision"] == NEWER
    fixture_data["runs"] = [run(status="completed", conclusion="failure")]
    assert reconcile_fixture(fixture_data)["action"] == "blocked"
    fixture_data["runs"] = []
    assert reconcile_fixture(fixture_data)["action"] == "blocked"


def test_release_ac02_waits_for_pending_or_unreceipted_automatic_runs():
    fixture_data = release_fixture()
    observed = fixture_data["runs"][0]
    for change in ({"status": "in_progress"}, {"mode": "plan"}, {"runtime_digests": {}}):
        fixture_data["runs"] = [{**observed, **{key: value for key, value in change.items()
                                                  if key == "status"}}]
        fixture_data["receipts"] = [{**receipt(observed), **{key: value for key, value in change.items()
                                                            if key != "status"}}]
        assert reconcile_fixture(fixture_data)["action"] == "blocked"
    fixture_data["runs"] = [run(status="completed", conclusion="failure")]
    assert reconcile_fixture(fixture_data)["action"] == "protected-apply-required"
    fixture_data["runs"] = [observed, run(attempt=3, status="in_progress", conclusion=None)]
    fixture_data["receipts"] = [receipt(observed)]
    assert reconcile_fixture(fixture_data)["action"] == "blocked"


def test_release_ac02_rejects_missing_components_and_wrong_run_attempt():
    fixture_data = release_fixture()
    fixture_data["receipts"][0]["run_attempt"] = 1
    assert reconcile_fixture(fixture_data)["action"] == "blocked"
    fixture_data["manifest"] = {"shared Lambda and Browser": {"lambda": "sha256:" + "c" * 64}}
    with pytest.raises(ValueError, match="Exact selected component"):
        reconcile_fixture(fixture_data)


def test_release_ac02_reuses_both_provider_and_target_without_duplicate_dispatch():
    provider = run(run_id=17, attempt=1, workflow="shared-agentcore-tools-deploy.yml",
                   event="workflow_dispatch")
    target = run(event="workflow_dispatch")
    manifest = {"shared Lambda and Browser": {"lambda": "sha256:" + "a" * 64,
                                              "browser": "sha256:" + "b" * 64},
                "platform targets": MANIFEST["platform targets"]}
    fixture_data = {"paths": ["modules/tools/agentcore/agentcore_tools/handler.py"],
                    "source_revision": SOURCE, "main_revision": SOURCE,
                    "environment": "dev", "account": "123456789012", "manifest": manifest,
                    "runs": [provider, target],
                    "receipts": [receipt(provider, manifest=manifest, component="shared Lambda and Browser"),
                                 receipt(target, manifest=manifest)],
                    "provider_request_count": 0, "ancestry": [],
                    "expected_decisions": [
                        {"component": "shared Lambda and Browser", "action": "reuse",
                         "source_revision": SOURCE, "run_id": 17, "run_attempt": 1},
                        {"component": "platform targets", "action": "reuse",
                         "source_revision": SOURCE, "run_id": 18, "run_attempt": 2}]}
    status, _, observations = runner.release_ac02(fixture_data)
    assert status == "pass" and observations["decisions"] == fixture_data["expected_decisions"]
    fixture_data["receipts"] = fixture_data["receipts"][1:]
    assert runner.release_ac02(fixture_data)[0] == "fail"


def test_release_ac02_prefers_newer_main_over_later_rerun_of_old_source():
    fixture_data = release_fixture()
    newer = run(source=NEWER, run_id=19, attempt=1)
    fixture_data.update(main_revision=NEWER, ancestry=[[SOURCE, NEWER]])
    fixture_data["runs"] += [newer, run(run_id=30, attempt=1)]
    fixture_data["receipts"].append(receipt(newer))
    decision = reconcile_fixture(fixture_data)
    assert (decision["action"], decision["source_revision"], decision["run_id"]) == (
        "reuse", NEWER, 19)



def test_adapter_reconciliation_ignores_other_stages_and_reuses_only_matching_receipt():
    adapter = run(run_id=17, attempt=1, event="workflow_dispatch")
    adapter["inputs"] = {"mode": "release", "stage": "tool-adapters", "adp_correlation": "orch:12345678-1234-1234-1234-123456789012"}
    platform = run(run_id=18)
    identity = run(run_id=19, attempt=1, event="workflow_dispatch")
    identity["inputs"] = {**adapter["inputs"], "mode": "identity"}
    manifest = {"tool-adapters": {"image": "sha256:" + "b" * 64,
                                   "configuration": "sha256:" + "c" * 64}}
    steps = operator_sequence(["modules/tools/adapters/deploy.py"])

    def decisions(runs, receipts):
        return reconcile(steps, SOURCE, SOURCE, manifest, runs, receipts,
                         lambda older, newer: older == newer,
                         environment="dev", account="123456789012")["decisions"]

    applied = {**receipt(adapter, manifest=manifest, component="tool-adapters"),
               "evidence_type": "independent-runtime"}
    assert decisions([adapter, platform, identity], [applied])[0]["action"] == "reuse"
    assert decisions([adapter, platform, identity], [applied])[0]["run_id"] == 17
    assert decisions([adapter], [{key: value for key, value in applied.items()
                                   if key != "evidence_type"}])[0]["action"] == "blocked"
    assert decisions([platform, identity], [receipt(platform, manifest=manifest,
                                                   component="tool-adapters")])[0]["action"] != "reuse"
    pending = {**adapter, "id": 20, "status": "in_progress", "conclusion": None}
    assert decisions([adapter, platform, pending], [applied])[0]["action"] == "blocked"



def test_failed_adapter_release_requires_complete_prior_group_readback_before_retry():
    adapter = run(run_id=17, attempt=1, event="workflow_dispatch", conclusion="failure")
    adapter["inputs"] = {"mode": "release", "stage": "tool-adapters",
                         "adp_correlation": "orch:12345678-1234-1234-1234-123456789012"}
    stages = operator_sequence(["modules/tools/adapters/deploy.py"])
    manifest = {"tool-adapters": {"image": "sha256:" + "b" * 64,
                                   "configuration": "sha256:" + "c" * 64}}
    previous_manifest = previous_release_fixture("legacy")
    route_digest = {"targets": "sha256:" + "b" * 64}
    target = previous_manifest["components"]["platform targets"]
    target["expected_runtime"] = route_digest
    target["receipt"]["component_digests"] = route_digest
    target["receipt"]["runtime_digests"] = route_digest
    for phase in ("before", "after"):
        previous_manifest["readbacks"][phase]["platform targets"] = route_digest.copy()
    previous = {"manifest": previous_manifest,
                "readback": lambda phase: previous_manifest["readbacks"][phase],
                "gateway_image": IMAGE, "facade_image": IMAGE}

    def recover(prior):
        return reconcile(stages, SOURCE, SOURCE, manifest, [adapter], [],
                         lambda older, newer: older == newer,
                         environment="dev", account="123456789012",
                         previous=prior)["decisions"][0]

    assert recover(None)["action"] == "blocked"
    assert "prior connector group" in recover(None)["reason"].lower()
    assert recover(previous)["action"] == "protected-apply-required"
    previous_manifest["route_group"]["status"] = "fenced"
    assert recover(previous)["action"] == "blocked"
    previous_manifest["route_group"]["status"] = "legacy"
    group = previous_manifest.pop("route_group")
    assert recover(previous)["action"] == "blocked"
    previous_manifest["route_group"] = group
    previous_manifest["readbacks"]["after"]["route_group"]["status"] = "fenced"
    assert recover(previous)["action"] == "blocked"
    previous_manifest["readbacks"]["after"]["route_group"]["status"] = "legacy"
    previous_manifest["readbacks"]["after"]["platform targets"]["targets"] = "sha256:" + "e" * 64
    assert recover(previous)["action"] == "blocked"
    previous_manifest["readbacks"]["after"]["platform targets"]["targets"] = (
        previous_manifest["readbacks"]["before"]["platform targets"]["targets"]
    )
    previous_manifest["components"]["platform targets"]["receipt"]["mode"] = "plan"
    assert recover(previous)["action"] == "blocked"



def test_adapter_controller_loss_requires_observed_terminal_run_before_retry():
    adapter = run(run_id=17, attempt=1, event="workflow_dispatch", status="in_progress", conclusion=None)
    adapter["inputs"] = {"mode": "release", "stage": "tool-adapters",
                         "adp_correlation": "orch:12345678-1234-1234-1234-123456789012"}
    manifest = {"tool-adapters": {"image": "sha256:" + "b" * 64,
                                   "configuration": "sha256:" + "c" * 64}}
    prior = previous_release_fixture("legacy")
    previous = {"manifest": prior, "readback": lambda phase: prior["readbacks"][phase],
                "gateway_image": IMAGE, "facade_image": IMAGE}

    def decide(release):
        result = reconcile(operator_sequence(["modules/tools/adapters/deploy.py"]), SOURCE, SOURCE,
                           manifest, [release], [], lambda older, newer: older == newer,
                           environment="dev", account="123456789012", previous=previous)
        assert result["status"] == "plan-only"
        return result["decisions"][0]["action"]

    assert decide(adapter) == "blocked"
    adapter["status"] = "completed"
    adapter["conclusion"] = "failure"
    assert decide(adapter) == "protected-apply-required"
    adapter["conclusion"] = "success"
    assert decide(adapter) == "blocked"


def test_release_ac02_github_run_observation_uses_read_only_api(monkeypatch):
    calls = []

    def fake_read(arguments, *, text):
        calls.append(arguments)
        return json.dumps({"id": 18, "run_attempt": 2, "head_sha": SOURCE,
                           "status": "completed", "conclusion": "success", "event": "push"}) + "\n"

    monkeypatch.setattr(subprocess, "check_output", fake_read)
    result = github_json("repos/example/tools/actions/workflows/tools-platform-deploy.yml/runs?per_page=100",
                         paginate=True)
    assert result[0]["run_attempt"] == 2 and calls[0][:2] == ["gh", "api"]
    assert "--paginate" in calls[0] and "--jq" in calls[0]

    def inaccessible(*_args, **_kwargs):
        raise subprocess.CalledProcessError(1, "gh api")

    monkeypatch.setattr(subprocess, "check_output", inaccessible)
    with pytest.raises(subprocess.CalledProcessError):
        github_json("repos/example/tools/actions/workflows/tools-platform-deploy.yml/runs", paginate=True)


def test_adapter_recovery_cli_requires_fresh_private_prior_group_readbacks(tmp_path, monkeypatch, capsys):
    release = run(run_id=18, attempt=1, event="workflow_dispatch", conclusion="failure")
    release["inputs"] = {"mode": "release", "stage": "tool-adapters",
                         "adp_correlation": "orch:12345678-1234-1234-1234-123456789012"}
    candidate = {"tool-adapters": {"image": "sha256:" + "b" * 64,
                                   "configuration": "sha256:" + "c" * 64}}
    previous = previous_release_fixture("legacy")
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(candidate))
    receipts_file = tmp_path / "receipts.json"
    receipts_file.write_text("[]")
    previous_file = tmp_path / "previous.json"
    previous_file.write_text(json.dumps(previous))
    previous_file.chmod(0o600)
    readbacks = tmp_path / "readbacks"
    readbacks.mkdir(mode=0o700)
    for phase in ("before", "after"):
        report = {"phase": phase, "observed_at": datetime.now(UTC).isoformat(),
                  "runtime": previous["readbacks"][phase]}
        path = readbacks / f"runtime-{phase}.json"
        path.write_text(json.dumps(report))
        path.chmod(0o600)

    def fake_github(path, *, paginate=False):
        if path == "repos/example/tools":
            return {"default_branch": "main"}
        if path == "repos/example/tools/branches/main":
            return {"commit": {"sha": SOURCE}}
        if "/compare/" in path:
            return {"status": "identical"}
        if paginate:
            return [{key: value for key, value in release.items() if key != "workflow"}]
        pytest.fail("unexpected GitHub read")

    monkeypatch.setattr(reconcile_release, "github_json", fake_github)
    monkeypatch.setattr(reconcile_release.subprocess, "check_output",
                        lambda *_args, **_kwargs: "modules/tools/adapters/deploy.py\n")
    monkeypatch.setattr(sys, "argv", ["reconcile_release.py", "--repo", "example/tools",
                                   "--base", SOURCE, "--source", SOURCE, "--manifest", str(manifest_file),
                                   "--receipts", str(receipts_file), "--environment", "dev",
                                   "--account", "123456789012", "--previous-manifest", str(previous_file),
                                   "--previous-readbacks", str(readbacks), "--previous-gateway-image", IMAGE,
                                   "--previous-facade-image", IMAGE])
    reconcile_release.main()
    assert json.loads(capsys.readouterr().out)["decisions"][0]["action"] == "protected-apply-required"
    readbacks.joinpath("runtime-after.json").chmod(0o644)
    with pytest.raises(ValueError, match="bounded owner-only files"):
        reconcile_release.main()
    after = readbacks / "runtime-after.json"
    after.chmod(0o600)
    report = json.loads(after.read_text())
    report["observed_at"] = (datetime.now(UTC) - timedelta(minutes=6)).isoformat()
    after.write_text(json.dumps(report))
    with pytest.raises(ValueError, match="stale or incomplete"):
        reconcile_release.main()



def test_release_ac02_operator_cli_observes_github_and_rechecks_main(tmp_path, monkeypatch, capsys):
    fixture_data = release_fixture()
    manifest_file = tmp_path / "manifest.json"
    receipts_file = tmp_path / "receipts.json"
    manifest_file.write_text(json.dumps(fixture_data["manifest"]))
    receipts_file.write_text(json.dumps(fixture_data["receipts"]))
    branch_reads = []

    def fake_github(path, *, paginate=False):
        if path == "repos/example/tools":
            return {"default_branch": "main"}
        if path == "repos/example/tools/branches/main":
            branch_reads.append(path)
            return {"commit": {"sha": SOURCE}}
        if "/compare/" in path:
            return {"status": "identical"}
        if paginate and "/runs?branch=main" in path:
            return [{key: value for key, value in fixture_data["runs"][0].items()
                     if key != "workflow"}]
        pytest.fail("unexpected GitHub read")

    def fake_git(arguments, *, text):
        assert arguments[-4:-2] == ["diff", "--name-only"]
        return "modules/tools/platform/schemas/github.yaml\n"

    monkeypatch.setattr(reconcile_release, "github_json", fake_github)
    monkeypatch.setattr(reconcile_release.subprocess, "check_output", fake_git)
    monkeypatch.setattr(sys, "argv", ["reconcile_release.py", "--repo", "example/tools",
                                   "--base", SOURCE, "--source", SOURCE,
                                   "--manifest", str(manifest_file), "--receipts", str(receipts_file),
                                   "--environment", "dev", "--account", "123456789012"])
    reconcile_release.main()
    assert len(branch_reads) == 2
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "plan-only" and output["decisions"][0]["action"] == "reuse"

    def moved_branch(path, *, paginate=False):
        result = fake_github(path, paginate=paginate)
        if path.endswith("/branches/main") and len(branch_reads) > 3:
            return {"commit": {"sha": NEWER}}
        return result

    monkeypatch.setattr(reconcile_release, "github_json", moved_branch)
    with pytest.raises(ValueError, match="branch moved"):
        reconcile_release.main()


def test_release_ac02_bootstrap_only_never_claims_a_deployment():
    steps = operator_sequence(["platform/automation-infra/shared-tools-deployment.tf"])
    result = reconcile(steps, SOURCE, SOURCE, {}, [], [],
                       lambda older, newer: older == newer,
                       environment="dev", account="123456789012")
    assert result["status"] == "plan-only"
    assert result["decisions"] == [{"component": "protected bootstrap", "action": "blocked",
                                    "reason": "Coordinator must review existing state and scoped bootstrap before apply"}]


@pytest.mark.parametrize("scenario,fixture_data", [
    ("SCN-8068-01", fixture()),
    ("SCN-8068-02", release_fixture()),
])
def test_local_release_fixtures_cannot_pass_as_live(tmp_path, monkeypatch, scenario, fixture_data):
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: pytest.fail("must refuse before network"))
    passed = runner.evaluate(scenario, tmp_path, "https://example.invalid", SOURCE, IMAGE, IMAGE,
                             command=[], live=True, contract_fixture=fixture_data,
                             client_factory=lambda *_: pytest.fail("must refuse before client"))
    report = json.loads((tmp_path / "evidence.json").read_text())
    row, = report["scenarios"]
    assert not passed and report["live_attestation"] is False
    assert row["status"] == "blocked" and row["evidence_scope"] == "repository-integration"
    assert "cannot establish deployed acceptance" in row["reason"]
    assert row["issue_number"] == 8068


def test_release_ac03_resumes_only_receipted_stage_after_fixed_interruption(tmp_path, monkeypatch):
    bindings = {"ADP_TOOLS_ACCOUNT_ID": "123456789012", "ADP_TOOLS_AWS_REGION": "us-east-1",
                "ADP_TOOLS_ENVIRONMENT": "dev", "ADP_SOURCE_REVISION": SOURCE,
                "ADP_TOOLS_DEPLOY_ROLE_ARN": "fictional-protected-role",
                "ADP_TOOLS_STATE_BUCKET": "fictional-state",
                "ADP_TOOLS_TFVARS_JSON": json.dumps({"aws_account_id": "123456789012"}),
                "GITHUB_RUN_ID": "18", "GITHUB_RUN_ATTEMPT": "2"}
    journal = tmp_path / "release.json"
    initialize(journal, account="123456789012", region="us-east-1", environment="dev", source=SOURCE,
               bindings=bindings)
    start(journal, "infra", run_id=18, run_attempt=2, bindings=bindings)
    attempted = json.loads(journal.read_text())
    assert attempted["stages"] == {"infra": {"status": "started", "run_id": 18, "run_attempt": 2}}
    receipts = tmp_path / "receipts"
    receipts.mkdir()
    (receipts / "infra.json").write_text(json.dumps({
        "stage": "infra", "source_revision": SOURCE, "account": "123456789012",
        "region": "us-east-1", "environment": "dev", "run_id": 18,
        "bindings_sha256": bindings_sha256(bindings),
        "run_attempt": 2, "mode": "apply", "evidence_type": "independent-runtime",
        "expected_runtime": {"gateway": "sha256:" + "b" * 64},
        "observed_runtime": {"gateway": "sha256:" + "b" * 64}}))
    (receipts / "infra.json").chmod(0o600)
    (receipts / "manifest.json").write_text(json.dumps({
        "source_revision": SOURCE, "account": "123456789012", "region": "us-east-1",
        "environment": "dev", "bindings_sha256": bindings_sha256(bindings),
        "stages": {"infra": {"gateway": "sha256:" + "b" * 64}}}))
    (receipts / "manifest.json").chmod(0o600)
    case = {"journal_path": journal, "source_revision": SOURCE, "expected_attempt": attempted["stages"]["infra"],
            "resume_stage": "infra", "next_stage": "targets", "selected": ["infra", "targets"],
            "receipts_dir": receipts, "bindings": bindings, "provider_request_count": 0}
    missing = {**case, "bindings": {name: value for name, value in bindings.items()
                                     if name != "ADP_TOOLS_STATE_BUCKET"}}
    assert "Coordinator: supply the approved tools state bucket" in runner.release_ac03(missing)[1]
    assert json.loads(journal.read_text()) == attempted
    monkeypatch.setattr(runner, "verify_gateway", lambda *_: pytest.fail("must not contact gateway"))
    passed = runner.evaluate("SCN-8068-03", tmp_path / "output", "https://example.invalid", SOURCE,
                             IMAGE, IMAGE, command=[], contract_fixture=case,
                             client_factory=lambda *_: pytest.fail("must not contact provider"))
    report = json.loads((tmp_path / "output/evidence.json").read_text())
    row, = report["scenarios"]
    assert passed and not report["live_attestation"] and row["status"] == "pass"
    assert row["evidence_scope"] == "repository-integration"
    assert row["observations"]["run_id"] == 18 and row["observations"]["run_attempt"] == 2
    assert row["observations"]["provider_request_count"] == 0
    saved = json.loads(journal.read_text())
    assert saved["stages"]["infra"]["status"] == "receipt-verified"
    assert "targets" not in saved["stages"] and saved["stages"]["infra"]["receipt_sha256"]
    declined = runner.evaluate("SCN-8068-03", tmp_path / "live", "https://example.invalid", SOURCE,
                               IMAGE, IMAGE, command=[], contract_fixture=case, live=True,
                               client_factory=lambda *_: pytest.fail("must not contact provider"))
    live_row, = json.loads((tmp_path / "live/evidence.json").read_text())["scenarios"]
    assert not declined and live_row["status"] == "blocked"
    assert live_row["evidence_scope"] == "repository-integration"


def test_release_ac02_reuses_reviewed_ancestor_from_run_bound_manual_context():
    fixture_data = release_fixture()
    manual = run(source=NEWER, run_id=20, attempt=3, event="workflow_dispatch")
    manual["inputs"] = {"source_sha": SOURCE}
    applied = {**receipt(manual), "source_revision": SOURCE, "workflow_revision": NEWER}
    fixture_data.update(main_revision=NEWER, ancestry=[[SOURCE, NEWER]], runs=[manual], receipts=[applied])
    decision = reconcile_fixture(fixture_data)
    assert (decision["action"], decision["source_revision"], decision["workflow_revision"],
            decision["run_id"], decision["run_attempt"]) == ("reuse", SOURCE, NEWER, 20, 3)
    del manual["inputs"]
    assert reconcile_fixture(fixture_data)["action"] == "reuse"
    del applied["workflow_revision"]
    assert reconcile_fixture(fixture_data)["action"] == "blocked"
    manual["inputs"] = {"manual_source_revision": SOURCE}
    assert reconcile_fixture(fixture_data)["action"] == "reuse"
    manual["inputs"] = {"source_sha": NEWER}
    assert reconcile_fixture(fixture_data)["action"] == "blocked"


def test_release_ac02_manual_ancestor_never_overrides_newer_verified_runtime():
    fixture_data = release_fixture()
    newer = run(source=NEWER, run_id=19, attempt=1)
    manual = run(source=NEWER, run_id=20, attempt=1, event="workflow_dispatch")
    manual["inputs"] = {"source_sha": SOURCE}
    applied = {**receipt(manual), "source_revision": SOURCE, "workflow_revision": NEWER}
    fixture_data.update(main_revision=NEWER, ancestry=[[SOURCE, NEWER]], runs=[newer, manual],
                        receipts=[receipt(newer), applied])
    decision = reconcile_fixture(fixture_data)
    assert decision["action"] == "blocked" and "never downgrade" in decision["reason"]
