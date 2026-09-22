"""Verify selected image coverage and the real Superplane staging contract."""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "codebuild"))
import scan_security_images as runner
from security_image_targets import SUPERPLANE, discover


@pytest.fixture
def source(tmp_path):
    module = tmp_path / SUPERPLANE
    (module / "releases").mkdir(parents=True)
    shutil.copyfile(ROOT / SUPERPLANE / "releases/superplane.lock.yaml",
                    module / "releases/superplane.lock.yaml")
    for component in ("superplane-api", "superplane-controller", "superplane-platform-monitor"):
        context = module / "src" / component
        context.mkdir(parents=True)
        shutil.copyfile(ROOT / SUPERPLANE / "src" / component / "Dockerfile", context / "Dockerfile")
    for package in ("auth", "contracts"):
        shutil.copytree(ROOT / SUPERPLANE / package, module / package)
    shutil.copytree(ROOT / SUPERPLANE / "src/superplane-api/scripts",
                    module / "src/superplane-api/scripts")
    return tmp_path


def test_inventory_covers_all_source_components_and_pinned_runtime(source):
    targets = discover(source, "superplane")
    assert len(targets) == 4
    assert all(target["required"] for target in targets)
    external = [target for target in targets if target["dockerfile"] == "-"]
    lock = yaml.safe_load((source / SUPERPLANE / "releases/superplane.lock.yaml").read_text())
    assert external[0]["image"] == "registry-1.docker.io/berkeleyskypilot/skypilot@" + lock["images"]["skypilot-api"]
    legacy = source / "modules/agent-factory/gateway"
    legacy.mkdir(parents=True)
    (legacy / "Dockerfile").write_text("FROM scratch\n")
    assert len(discover(source, "superplane")) == 4
    assert next(t for t in discover(source) if t["dockerfile"].endswith("gateway/Dockerfile"))["context"] == "modules/agent-factory"


def test_all_scope_inventory_uses_production_contexts_and_preparation():
    targets = {target["dockerfile"]: target for target in discover(ROOT)}
    expected = {
        "modules/agent-context/images/context-mcp/Dockerfile": (
            "modules/agent-context/images/context-mcp",
            {"door/", "personal_context/"},
        ),
        "modules/agent-context/images/ingestion/Dockerfile": (
            "modules/agent-context/images/ingestion",
            {"pipeline/", "alembic/"},
        ),
        "modules/gateway/Dockerfile": ("modules/gateway", {"contracts/"}),
        "modules/research/gbrain/docker/Dockerfile": (
            "modules/research/gbrain",
            {"config/", "docker/entrypoint.sh"},
        ),
    }
    for dockerfile, (context, copy_inputs) in expected.items():
        target = targets[dockerfile]
        assert target["context"] == context
        dockerfile_text = (ROOT / dockerfile).read_text()
        assert all(f"COPY {source}" in dockerfile_text for source in copy_inputs)
        for step in target["prepare"]:
            if step[0] == "copy-tree":
                assert (ROOT / step[1]).is_dir()
                assert (ROOT / step[2]).parent == ROOT / context
                assert Path(step[2]).name + "/" in copy_inputs
            else:
                assert step[:2] == ["run", "bash"]
                assert (ROOT / step[-1]).is_file()
        if not target["prepare"]:
            assert all((ROOT / context / source).exists() for source in copy_inputs)

    assert targets["modules/gateway/Dockerfile"]["prepare"] == [
        ["run", "bash", "modules/gateway/scripts/stage-contracts.sh"]
    ]


def test_copy_tree_preparation_replaces_stale_build_inputs(tmp_path):
    source = tmp_path / "module/source"
    destination = tmp_path / "module/image/staged"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "current.py").write_text("current")
    (destination / "stale.py").write_text("stale")
    target = {
        "name": "prepared-image",
        "prepare": [["copy-tree", "module/source", "module/image/staged"]],
    }

    runner.prepare(target, tmp_path)

    assert (destination / "current.py").read_text() == "current"
    assert not (destination / "stale.py").exists()


def test_missing_superplane_source_is_not_silently_omitted(source):
    (source / SUPERPLANE / "src/superplane-api/Dockerfile").unlink()
    with pytest.raises(ValueError, match="Missing maintained"):
        discover(source)


def test_external_image_cannot_fall_back_to_a_tag(source):
    path = source / SUPERPLANE / "releases/superplane.lock.yaml"
    lock = yaml.safe_load(path.read_text())
    lock["images"]["skypilot-api"] = "latest"
    path.write_text(yaml.safe_dump(lock))
    with pytest.raises(ValueError, match="Unpinned"):
        discover(source)


@pytest.mark.parametrize("tool", ["grype", "syft"])
@pytest.mark.parametrize("failure", [None, "build", "invalid_output"])
def test_scan_stages_api_publishes_coverage_and_fails_on_missing_results(source, monkeypatch, tool, failure):
    monkeypatch.chdir(source)
    monkeypatch.setenv("SECURITY_IMAGE_SCOPE", "superplane")
    monkeypatch.setenv("SECURITY_SCAN_DATE", "2026/09/20")
    monkeypatch.setenv("CODEBUILD_BUILD_ID", "scan:test-id")
    monkeypatch.setenv("ADP_SOURCE_SHA", "a" * 40)
    monkeypatch.setenv("SECURITY_SCANS_BUCKET", "private-test-bucket")
    monkeypatch.setattr(sys, "argv", ["scan_security_images.py", tool])
    calls, reports = [], []
    real_run = subprocess.run

    def execute(args, **kwargs):
        calls.append(args)
        if args[0] == "bash":
            return real_run(args, check=True, **kwargs)
        if args[:2] == ["docker", "build"]:
            context = Path(args[-1])
            if context.name == "superplane-api":
                assert (context / "vendor/superplane-auth/superplane_auth/policy.py").is_file()
                assert (context / "vendor/superplane-contracts/superplane_contracts/emission.py").is_file()
            if failure == "build" and context.name == "superplane-controller":
                raise subprocess.CalledProcessError(1, args)
        if args[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(stdout="sha256:" + "d" * 64 + "\n")
        if args[0] == "grype":
            kwargs["stdout"].write(json.dumps({} if failure == "invalid_output" else {"runs": [{"results": []}]}))
        if args[0] == "syft":
            Path(args[-1].split("=", 1)[1]).write_text(json.dumps({} if failure == "invalid_output" else {"bomFormat": "CycloneDX"}))
        if args[:3] == ["aws", "s3", "cp"] and args[3].endswith("coverage.json"):
            reports.append(json.loads(Path(args[3]).read_text()))

    monkeypatch.setattr(runner, "command", execute)
    # Only docker cleanup bypasses the checked command wrapper.
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: None)
    assert runner.main() == (0 if failure is None else 1)
    report = reports[0]
    assert report["expected"] == 4
    assert report["succeeded"] == {None: 4, "build": 3, "invalid_output": 0}[failure]
    assert any(call[:3] == ["docker", "pull", "--platform"] for call in calls)
    uploads = [call for call in calls if call[:3] == ["aws", "s3", "cp"]]
    assert len(uploads) == report["succeeded"] * 3 + 1
    assert all("/2026/09/20/test-id/" in call[4] for call in uploads)
    missing = [item["name"] for item in report["targets"] if item["status"] != "succeeded"]
    assert bool(missing) is (failure is not None)
    # A failed target is recorded by name with its error, so a reader can tell
    # "no findings" apart from "never scanned".
    assert all(item.get("error") for item in report["targets"] if item["status"] != "succeeded")
    assert all(item.get("digest") == "sha256:" + "d" * 64 for item in report["targets"] if item["status"] == "succeeded")
    assert all(item.get("artifact_sha256") for item in report["targets"] if item["status"] == "succeeded")


def test_any_missing_target_fails_even_above_the_old_half_coverage_threshold(source, monkeypatch):
    """Coverage is all-or-nothing.

    The 2026-09-21 run reported success on 8/17 Grype images because the build
    passed at >=50% coverage. Here 3 of 4 targets succeed -- comfortably over
    that old threshold -- and the run must still fail and name the missing one.
    """
    monkeypatch.chdir(source)
    monkeypatch.setenv("SECURITY_IMAGE_SCOPE", "superplane")
    monkeypatch.setenv("SECURITY_SCAN_DATE", "2026/09/21")
    monkeypatch.setenv("CODEBUILD_BUILD_ID", "scan:test-id")
    monkeypatch.setenv("ADP_SOURCE_SHA", "b" * 40)
    monkeypatch.setenv("SECURITY_SCANS_BUCKET", "private-test-bucket")
    monkeypatch.setattr(sys, "argv", ["scan_security_images.py", "grype"])
    reports = []
    real_run = subprocess.run

    def execute(args, **kwargs):
        if args[0] == "bash":
            return real_run(args, check=True, **kwargs)
        # The externally pulled, digest-pinned runtime is the one that fails.
        if args[:2] == ["docker", "pull"]:
            raise subprocess.CalledProcessError(1, args)
        if args[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(stdout="sha256:" + "e" * 64 + "\n")
        if args[0] == "grype":
            kwargs["stdout"].write(json.dumps({"runs": [{"results": []}]}))
        if args[:3] == ["aws", "s3", "cp"] and args[3].endswith("coverage.json"):
            reports.append(json.loads(Path(args[3]).read_text()))

    monkeypatch.setattr(runner, "command", execute)
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: None)
    assert runner.main() == 1
    report = reports[0]
    assert (report["succeeded"], report["expected"]) == (3, 4)
    failed = [item for item in report["targets"] if item["status"] != "succeeded"]
    assert [item["name"] for item in failed] == ["superplane-skypilot-api"]


def test_coverage_shortfall_fails_the_scanner_jobs():
    """The workflow must not downgrade a short artifact pull to a warning."""
    workflow = (ROOT / ".github/workflows/security-scan.yml").read_text()
    for kind in ("SARIF", "SBOM"):
        assert f"::error::Grype {kind}" in workflow or f"::error::Syft {kind}" in workflow
    assert "is below the expected image count" in workflow
    assert "is below selected image count" not in workflow
    assert "is below Dockerfile count" not in workflow


def test_dispatch_only_scope_and_shared_buildspecs():
    workflow = yaml.load((ROOT / ".github/workflows/security-scan.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch"}
    assert workflow["on"]["workflow_dispatch"]["inputs"]["scan_scope"]["options"] == ["all", "superplane"]
    for tool in ("grype", "syft"):
        job = workflow["jobs"][tool]
        assert "continue-on-error" not in job
        build = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/codebuild-run")
        assert "SECURITY_IMAGE_SCOPE" in build["with"]["environment_variables"]
        assert "SECURITY_SCAN_DATE" in build["with"]["environment_variables"]
        spec = yaml.safe_load((ROOT / f"codebuild/bs-{tool}-scan.yml").read_text())
        assert spec["phases"]["build"]["commands"] == [f"python3 codebuild/scan_security_images.py {tool}"]


def test_summary_runs_on_dispatch_and_no_automated_baseline_refresh():
    """A manual run must reconcile, and must not blanket-refresh baselines.

    `summary` was gated on `github.event_name == 'pull_request'`, so a dispatch
    could report success while never diffing anything. `update-baseline` was
    gated on `schedule`, which no longer exists: dead code that would have
    overwritten every baseline from one run (S21 #5620 owns dispositions).
    """
    workflow = yaml.load((ROOT / ".github/workflows/security-scan.yml").read_text(), Loader=yaml.BaseLoader)
    assert "update-baseline" not in workflow["jobs"]
    assert "--update-baselines" not in (ROOT / ".github/workflows/security-scan.yml").read_text()
    summary = workflow["jobs"]["summary"]
    assert "pull_request" not in summary["if"]
    assert summary["if"].startswith("always()")
    # The gate still runs, and every scanner feeds the reconciliation.
    steps = " ".join(step.get("run", "") for step in summary["steps"])
    assert "--fail-on critical,high" in steps
    for tool in ("checkov", "semgrep", "detect-secrets", "grype", "bandit", "cfn-nag", "npm-audit", "syft"):
        assert tool in summary["needs"]
    assert workflow["concurrency"]["cancel-in-progress"] == "false"
