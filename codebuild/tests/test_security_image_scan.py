"""Verify selected image coverage and the real Superplane staging contract."""

import hashlib
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
def source(tmp_path, monkeypatch):
    monkeypatch.setenv("SECURITY_EXECUTOR_PYTHON_IMAGE", "python:3.12-slim@sha256:" + "b" * 64)
    module = tmp_path / SUPERPLANE
    (module / "releases").mkdir(parents=True)
    shutil.copyfile(ROOT / SUPERPLANE / "releases/superplane.lock.yaml",
                    module / "releases/superplane.lock.yaml")
    for component in ("superplane-api", "superplane-controller", "superplane-platform-monitor"):
        context = module / "src" / component
        context.mkdir(parents=True)
        shutil.copyfile(ROOT / SUPERPLANE / "src" / component / "Dockerfile", context / "Dockerfile")
    # The production stager also vendors the maintained bootstrap/runtime
    # packages. Keep its real filesystem inputs in this offline fixture.
    for package in (
        "auth", "contracts", "infra/account-factory", "infra/account-provisioning", "infra/workspaces",
        "workspace_bootstrap", "workspace_provisioning", "executor",
    ):
        shutil.copytree(ROOT / SUPERPLANE / package, module / package)
    shutil.copyfile(ROOT / SUPERPLANE / "pyproject.toml", module / "pyproject.toml")
    (module / "src/superplane-controller/deploy").mkdir(parents=True)
    shutil.copyfile(ROOT / SUPERPLANE / "src/superplane-controller/deploy/crds.yaml",
                    module / "src/superplane-controller/deploy/crds.yaml")
    shutil.copytree(ROOT / "modules/harness/jobs", tmp_path / "modules/harness/jobs")
    shutil.copytree(ROOT / SUPERPLANE / "src/superplane-api/scripts",
                    module / "src/superplane-api/scripts")
    (tmp_path / "codebuild").mkdir()
    shutil.copyfile(ROOT / "codebuild/filter-sarif-ignores.py", tmp_path / "codebuild/filter-sarif-ignores.py")
    # Exercise raw acquisition and the real scoped filter end to end. These
    # synthetic advisory/package names cannot accidentally match the baseline.
    (tmp_path / ".grype.yaml").write_text(yaml.safe_dump({
        "ignore": [{"vulnerability": "CVE-2099-12345", "package": {"name": "scoped-package", "type": "deb"}}],
        "only-fixed": True,
        "only-notfixed": True,
        "ignore-wontfix": "not-fixed",
    }))
    return tmp_path


def grype_document():
    return {"runs": [{
        "tool": {"driver": {"rules": [
            {"id": "CVE-2099-12345-scoped-package", "help": {"text": "Package: scoped-package\nType: deb"}},
            {"id": "CVE-2099-12345-other-package", "help": {"text": "Package: other-package\nType: deb"}},
        ]}},
        "results": [{"ruleId": "CVE-2099-12345-scoped-package"}, {"ruleId": "CVE-2099-12345-other-package"}],
    }]}


def write_descriptor(args):
    path = Path(next(arg[5:] for arg in args if arg.startswith("json=")))
    db = path.with_suffix(".db")
    db.write_bytes(b"synthetic vulnerability database")
    descriptor = {"name": "grype", "version": "0.119.0", "timestamp": "2026-09-25T15:00:00Z",
                  "configuration": {"match": {"java": {"using-cpes": False}},
                                    "ignore": [{"package": {"name": "kernel-headers"}, "reason": "secret"}],
                                    "registry": {"password": "synthetic-secret"},
                                    "exclude": ["https://user:password@private.example/path?secret=query#token"]},
                  "db": {"status": {"schemaVersion": "v6.1.9", "built": "2026-09-25T06:00:00Z",
                                    "path": str(db), "valid": True},
                         "providers": {"nvd": {"captured": "2026-09-25T00:00:00Z", "input": "xxh64:1234"}}}}
    path.write_text(json.dumps({"descriptor": descriptor}))
    return path


def assert_raw_scan_configuration(args, kwargs):
    config = yaml.safe_load(Path(args[args.index("--config") + 1]).read_text())
    assert config["ignore"] == []
    assert config["only-fixed"] is False
    assert config["only-notfixed"] is False
    assert config["ignore-wontfix"] == ""
    assert not any(key.startswith("GRYPE_IGNORE") for key in kwargs["env"])
    assert "GRYPE_ONLY_FIXED" not in kwargs["env"]
    assert "GRYPE_ONLY_NOTFIXED" not in kwargs["env"]


def test_inventory_covers_all_source_components_and_pinned_runtime(source):
    targets = discover(source, "superplane")
    assert len(targets) == 5
    assert {target["dockerfile"] for target in targets} == {
        str(SUPERPLANE / "src/superplane-api/Dockerfile"),
        str(SUPERPLANE / "src/superplane-controller/Dockerfile"),
        str(SUPERPLANE / "src/superplane-platform-monitor/Dockerfile"),
        str(SUPERPLANE / "executor/Dockerfile"),
        "-",
    }
    assert all(target["required"] for target in targets)
    external = [target for target in targets if target["dockerfile"] == "-"]
    lock = yaml.safe_load((source / SUPERPLANE / "releases/superplane.lock.yaml").read_text())
    assert external[0]["image"] == "879318057152.dkr.ecr.us-east-1.amazonaws.com/adp-superplane-skypilot@" + lock["images"]["skypilot-api"]
    legacy = source / "modules/agent-factory/gateway"
    legacy.mkdir(parents=True)
    (legacy / "Dockerfile").write_text("FROM scratch\n")
    assert len(discover(source, "superplane")) == 5
    assert next(t for t in discover(source) if t["dockerfile"].endswith("gateway/Dockerfile"))["context"] == "modules/agent-factory"


def test_all_scope_inventory_uses_production_contexts_and_preparation():
    targets = {target["dockerfile"]: target for target in discover(ROOT)}
    expected = {
        "modules/agent-context/images/context-mcp/Dockerfile": (
            "modules/agent-context/images/context-mcp",
            {"door/", "personal_context/", "security-stdlib/"},
        ),
        "modules/agent-context/images/ingestion/Dockerfile": (
            "modules/agent-context/images/ingestion",
            {"pipeline/", "alembic/", "personal_context/", "security-build/", "security-stdlib/"},
        ),
        "modules/agent-context/images/codegraph-context/Dockerfile": (
            "modules/agent-context/images/codegraph-context",
            {"security-build/", "security-stdlib/"},
        ),
        "modules/agent-context/images/litellm-proxy/Dockerfile": (
            "modules/agent-context/images/litellm-proxy", {"security-stdlib/"},
        ),
        "modules/agent-context/images/deepwiki/Dockerfile": (
            "modules/agent-context/images/deepwiki", {"security-stdlib/"},
        ),
        "modules/agent-context/images/parser/Dockerfile": (
            "modules/agent-context/images/ingestion", {"security-stdlib/"},
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
@pytest.mark.parametrize("failure", [None, "build", "invalid_output", "executor_base", "missing_metadata"])
def test_scan_stages_api_publishes_coverage_and_fails_on_missing_results(source, monkeypatch, tool, failure):
    if failure == "missing_metadata" and tool == "syft":
        pytest.skip("Grype metadata contract")
    monkeypatch.chdir(source)
    monkeypatch.setenv("SECURITY_IMAGE_SCOPE", "superplane")
    monkeypatch.setenv("SECURITY_SCAN_DATE", "2026/09/20")
    monkeypatch.setenv("CODEBUILD_BUILD_ID", "scan:test-id")
    monkeypatch.setenv("ADP_SOURCE_SHA", "a" * 40)
    monkeypatch.setenv("SECURITY_SCANS_BUCKET", "private-test-bucket")
    monkeypatch.setattr(sys, "argv", ["scan_security_images.py", tool])
    if failure == "executor_base":
        monkeypatch.delenv("SECURITY_EXECUTOR_PYTHON_IMAGE")
    monkeypatch.setenv("GRYPE_ONLY_FIXED", "true")
    monkeypatch.setenv("GRYPE_ONLY_NOTFIXED", "true")
    monkeypatch.setenv("GRYPE_IGNORE_WONTFIX", "not-fixed")
    calls, reports = [], []
    uploaded = {}
    real_run = subprocess.run

    def execute(args, **kwargs):
        calls.append(args)
        if args[:3] == ["aws", "ecr", "get-login-password"]:
            return SimpleNamespace(stdout="synthetic-login-token")
        if args[:2] == ["docker", "login"]:
            assert kwargs["input"] == "synthetic-login-token"
        if args[0] == "bash":
            return real_run(args, check=True, **kwargs)
        if args[0] == "python3" and args[1].endswith("filter-sarif-ignores.py"):
            # Run the real filter with this test interpreter and its dependencies.
            return real_run([sys.executable, *args[1:]], check=True, **kwargs)
        if args[:2] == ["docker", "build"]:
            context = Path(args[-1])
            if args[args.index("-f") + 1] == str(SUPERPLANE / "executor/Dockerfile"):
                assert context == Path(".")
                assert kwargs["cwd"] == source
                assert args[args.index("--build-arg") + 1] == (
                    "PYTHON_IMAGE=python:3.12-slim@sha256:" + "b" * 64
                )
                assert (source / "modules/harness/jobs").is_dir()
                assert (source / SUPERPLANE / "executor/Dockerfile").is_file()
            if context.name == "superplane-api":
                assert (context / "vendor/superplane-auth/superplane_auth/policy.py").is_file()
                assert (context / "vendor/superplane-contracts/superplane_contracts/emission.py").is_file()
                for package, sentinel in (
                    ("harness_jobs", "facade.py"), ("account_factory", "modes.py"),
                    ("account_provisioning", "creation_runner.py"),
                    ("superplane_bootstrap", "workspace.py"),
                    ("workspace_provisioning", "preview.py"),
                    ("superplane_executor", "inventory.py"),
                ):
                    assert (context / "vendor" / package.replace("_", "-") / package / sentinel).is_file()
                runtime_data = context / "vendor/workspace-provisioning/workspace_provisioning/_data"
                for relative in ("workspaces/.terraform.lock.hcl", "workspaces/scripts/apply_workspace_plan.py", "crds.yaml"):
                    assert (runtime_data / relative).is_file()
            if failure == "build" and context.name == "superplane-controller":
                raise subprocess.CalledProcessError(1, args)
        if args[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(stdout="sha256:" + "d" * 64 + "\n")
        if args[0] == "grype":
            assert_raw_scan_configuration(args, kwargs)
            if failure != "missing_metadata":
                write_descriptor(args)
            kwargs["stdout"].write(json.dumps({} if failure == "invalid_output" else grype_document()))
        if args[0] == "syft":
            Path(args[-1].split("=", 1)[1]).write_text(json.dumps({} if failure == "invalid_output" else {"bomFormat": "CycloneDX"}))
        if args[:3] == ["aws", "s3", "cp"]:
            uploaded[args[4]] = Path(args[3]).read_bytes()
            if args[3].endswith("coverage.json"):
                reports.append(json.loads(uploaded[args[4]]))

    monkeypatch.setattr(runner, "command", execute)
    # Only docker cleanup bypasses the checked command wrapper.
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: None)
    assert runner.main() == (0 if failure is None else 1)
    report = reports[0]
    assert report["expected"] == 5
    assert report["succeeded"] == {None: 5, "build": 4, "invalid_output": 0, "executor_base": 4, "missing_metadata": 0}[failure]
    assert any(call[:3] == ["docker", "pull", "--platform"] for call in calls)
    uploads = [call for call in calls if call[:3] == ["aws", "s3", "cp"]]
    assert len(uploads) == report["succeeded"] * (6 if tool == "grype" else 3) + 1
    assert all("/2026/09/20/test-id/" in call[4] for call in uploads)
    missing = [item["name"] for item in report["targets"] if item["status"] != "succeeded"]
    assert bool(missing) is (failure is not None)
    # A failed target is recorded by name with its error, so a reader can tell
    # "no findings" apart from "never scanned".
    assert all(item.get("error") for item in report["targets"] if item["status"] != "succeeded")
    assert all(item.get("digest") == "sha256:" + "d" * 64 for item in report["targets"] if item["status"] == "succeeded")
    assert all(item.get("artifact_sha256") for item in report["targets"] if item["status"] == "succeeded")
    for item in report["targets"]:
        if item["status"] != "succeeded" or tool != "grype":
            continue
        metadata_bytes = uploaded[item["scanner_metadata"]]
        assert item["scanner_metadata_sha256"] == hashlib.sha256(metadata_bytes).hexdigest()
        assert b"synthetic-secret" not in metadata_bytes
        assert b"secret=query" not in metadata_bytes
        assert b"kernel-headers" in metadata_bytes
        raw_bytes = uploaded[item["raw_artifact"]]
        summary_bytes = uploaded[item["suppression_summary"]]
        assert item["raw_artifact_sha256"] == hashlib.sha256(raw_bytes).hexdigest()
        assert item["suppression_summary_sha256"] == hashlib.sha256(summary_bytes).hexdigest()
        assert len(json.loads(raw_bytes)["runs"][0]["results"]) == 2
        assert json.loads(summary_bytes)["total_suppressed"] == 1
        filtered_bytes = uploaded[item["artifact"]]
        assert item["artifact_sha256"] == hashlib.sha256(filtered_bytes).hexdigest()
        provenance = json.loads(uploaded[item["provenance"]])
        for key in ("artifact_sha256", "raw_artifact_sha256", "suppression_summary_sha256", "scanner_metadata_sha256"):
            assert provenance[key] == item[key]
        assert [r["ruleId"] for r in json.loads(filtered_bytes)["runs"][0]["results"]] == ["CVE-2099-12345-other-package"]



def test_any_missing_target_fails_even_above_the_old_half_coverage_threshold(source, monkeypatch):
    """Coverage is all-or-nothing.

    The 2026-09-21 run reported success on 8/17 Grype images because the build
    passed at >=50% coverage. Here 4 of 5 targets succeed -- comfortably over
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
        if args[:3] == ["aws", "ecr", "get-login-password"]:
            return SimpleNamespace(stdout="synthetic-login-token")
        if args[:2] == ["docker", "login"]:
            assert kwargs["input"] == "synthetic-login-token"
        if args[0] == "bash":
            return real_run(args, check=True, **kwargs)
        if args[0] == "python3" and args[1].endswith("filter-sarif-ignores.py"):
            # Run the real filter with this test interpreter and its dependencies.
            return real_run([sys.executable, *args[1:]], check=True, **kwargs)
        # The externally pulled, digest-pinned runtime is the one that fails.
        if args[:2] == ["docker", "pull"]:
            raise subprocess.CalledProcessError(1, args)
        if args[:3] == ["docker", "image", "inspect"]:
            return SimpleNamespace(stdout="sha256:" + "e" * 64 + "\n")
        if args[0] == "grype":
            assert_raw_scan_configuration(args, kwargs)
            write_descriptor(args)
            kwargs["stdout"].write(json.dumps(grype_document()))
        if args[:3] == ["aws", "s3", "cp"] and args[3].endswith("coverage.json"):
            reports.append(json.loads(Path(args[3]).read_text()))

    monkeypatch.setattr(runner, "command", execute)
    monkeypatch.setattr(runner.subprocess, "run", lambda *args, **kwargs: None)
    assert runner.main() == 1
    report = reports[0]
    assert (report["succeeded"], report["expected"]) == (4, 5)
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
        for variable in ("SECURITY_EXECUTOR_PYTHON_IMAGE", "SECURITY_PYTORCH_IMAGE", "SECURITY_RUNNER_IMAGE"):
            assert variable in build["with"]["environment_variables"]
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


@pytest.mark.parametrize("image", ["", "python:3.12-slim", "python@sha256:" + "0" * 64,
                                  "python@sha256:" + "g" * 64])
def test_executor_requires_reviewed_base_before_running_tools(source, monkeypatch, image):
    monkeypatch.setenv("SECURITY_EXECUTOR_PYTHON_IMAGE", image)
    target = next(t for t in discover(source) if t["dockerfile"].endswith("executor/Dockerfile"))
    calls = []
    monkeypatch.setattr(runner, "command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="requires reviewed digest-pinned"):
        runner.scan(target, "grype", source / "result.sarif", source)
    assert not calls


@pytest.mark.parametrize("variable", ["SECURITY_EXECUTOR_PYTHON_IMAGE", "SECURITY_PYTORCH_IMAGE", "SECURITY_RUNNER_IMAGE"])
@pytest.mark.parametrize("image,accepted", [
    ("", True), ("python:3.12-slim@sha256:" + "b" * 64, True),
    ("python:latest", False), ('$(touch SHOULD_NOT_EXIST)', False),
    ('image\" name=OTHER,value=x', False),
])
def test_workflow_validates_base_before_legacy_override_transport(tmp_path, variable, image, accepted):
    workflow = yaml.load((ROOT / ".github/workflows/security-scan.yml").read_text(),
                         Loader=yaml.BaseLoader)
    for tool in ("grype", "syft"):
        steps = workflow["jobs"][tool]["steps"]
        guard_index = next(i for i, step in enumerate(steps)
                           if step.get("name") == "Validate executor base before CodeBuild transport")
        build_index = next(i for i, step in enumerate(steps)
                           if step.get("uses") == "./.github/actions/codebuild-run")
        assert guard_index < build_index
        result = subprocess.run(["bash", "-c", steps[guard_index]["run"]], cwd=tmp_path,
                                env={variable: image}, capture_output=True)
        assert (result.returncode == 0) is accepted
        assert not (tmp_path / "SHOULD_NOT_EXIST").exists()


@pytest.mark.parametrize("failure", [None, "aws", "docker"])
def test_private_registry_authentication_precedes_pull(monkeypatch, tmp_path, capsys, failure):
    registry = "123456789012.dkr.ecr.us-east-1.amazonaws.com"
    target = {"image": registry + "/runtime@sha256:" + "a" * 64}
    calls = []
    token = "synthetic-login-token"

    def execute(args, **kwargs):
        calls.append(args)
        if args[:3] == ["aws", "ecr", "get-login-password"]:
            assert args[-1] == "us-east-1"
            assert kwargs["stdout"] == subprocess.PIPE
            if failure == "aws":
                raise subprocess.CalledProcessError(1, args)
            return SimpleNamespace(stdout=token)
        if args[:2] == ["docker", "login"]:
            assert args[-1] == registry
            assert "--password-stdin" in args
            assert kwargs["input"] == token
            assert token not in args
            if failure == "docker":
                raise subprocess.CalledProcessError(1, args)
        if args[:2] == ["docker", "pull"]:
            raise RuntimeError("pull reached after login")

    monkeypatch.setattr(runner, "command", execute)
    expected = subprocess.CalledProcessError if failure else RuntimeError
    with pytest.raises(expected):
        runner.scan(target, "grype", tmp_path / "scan.sarif", tmp_path)
    assert any(args[:2] == ["docker", "pull"] for args in calls) is (failure is None)
    assert token not in capsys.readouterr().out


def test_public_registry_does_not_request_ecr_credentials(monkeypatch):
    monkeypatch.setattr(runner, "command", lambda *a, **k: pytest.fail("unexpected ECR login"))
    runner.authenticate_registry("docker.io/library/python@sha256:" + "a" * 64)


@pytest.mark.parametrize("dockerfile,context,inputs", [
    ("modules/agent-context/images/parser/Dockerfile", "modules/agent-context/images/ingestion",
     ["isolated_parser.py", "parser_manifest.py", "scip_indexer.py", "scip_proto", "lang_go.py"]),
    ("modules/domain-apps/cyber/browser/Dockerfile", ".",
     ["modules/domain-apps/cyber/browser/requirements.txt", "modules/domain-apps/cyber/agent/skills/url-analysis", "modules/tools/agentcore/agentcore_tools"]),
    ("modules/tools/agentcore/Dockerfile", ".",
     ["modules/tools/adp_tools", "modules/tools/agentcore/agentcore_tools"]),
    ("modules/domain-apps/cyber/workers/Dockerfile", "modules/domain-apps/cyber",
     ["workers/requirements.txt", "workers/isolation.py", "agent/skills/stage-3-static/validate_script.py"]),
])
def test_new_image_contexts_contain_actual_copy_inputs(dockerfile, context, inputs):
    target = next(t for t in discover(ROOT) if t["dockerfile"] == dockerfile)
    assert target["context"] == context
    assert all((ROOT / context / source).exists() for source in inputs)


@pytest.mark.parametrize("dockerfile,arg,variable", [
    ("modules/domain-apps/superplane/tests/acceptance/workloads/Dockerfile", "PYTORCH_IMAGE", "SECURITY_PYTORCH_IMAGE"),
    ("platform/automation-infra/Dockerfile", "RUNNER_IMAGE", "SECURITY_RUNNER_IMAGE"),
])
@pytest.mark.parametrize("image", ["", "example:latest", "example@sha256:" + "0" * 64, "example@sha256:" + "g" * 64])
def test_additional_mandatory_bases_refuse_unreviewed_inputs(monkeypatch, tmp_path, dockerfile, arg, variable, image):
    target = next(t for t in discover(ROOT) if t["dockerfile"] == dockerfile)
    assert target["build_arg_env"] == {arg: variable}
    monkeypatch.setenv(variable, image)
    calls = []
    monkeypatch.setattr(runner, "command", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="requires reviewed digest-pinned"):
        runner.scan(target, "grype", tmp_path / "result.sarif", ROOT)
    assert calls == []


def test_agent_specific_ignore_preserves_only_required_contract_paths():
    patterns = (ROOT / "modules/agent-factory/agent/Dockerfile.dockerignore").read_text().splitlines()
    # The context intentionally starts deny-all. Preserve the exact copied
    # contracts without admitting every sibling rule or repository artifact.
    assert "**" in patterns
    for directory, name in [("agents", "issue-authoring.md"), ("templates", "developer-issue.md")]:
        assert f"!rules/{directory}/" in patterns
        assert f"!rules/{directory}/{name}" in patterns
        assert f"!rules/{directory}/**" not in patterns
        assert (ROOT / "modules/agent-factory/rules" / directory / name).is_file()


def test_private_build_auth_failure_retains_base_provenance(monkeypatch, tmp_path):
    base = "123456789012.dkr.ecr.us-east-1.amazonaws.com/runner@sha256:" + "a" * 64
    monkeypatch.setenv("SECURITY_RUNNER_IMAGE", base)
    target = {"name": "automation", "image": "-", "build_arg_env": {"RUNNER_IMAGE": "SECURITY_RUNNER_IMAGE"}}

    def denied(image):
        assert image == base
        raise subprocess.CalledProcessError(1, ["docker", "login"])

    monkeypatch.setattr(runner, "authenticate_registry", denied)
    with pytest.raises(subprocess.CalledProcessError):
        runner.scan(target, "grype", tmp_path / "result.sarif", tmp_path)
    assert target["build_args"] == {"RUNNER_IMAGE": base}


def test_metadata_is_sanitized_and_hashes_database(tmp_path):
    path = write_descriptor(["json=" + str(tmp_path / "descriptor.json")])
    output = tmp_path / "metadata.json"
    result = runner.scanner_metadata(path, output)
    text = output.read_text()
    assert "password" not in text and "secret" not in text and str(tmp_path) not in text
    assert "https://private.example/path" in text
    assert result["database"]["sha256"] == hashlib.sha256(b"synthetic vulnerability database").hexdigest()
    assert result["effective_matching_configuration"]["ignore"][0]["package"]["name"] == "kernel-headers"


@pytest.mark.parametrize("missing", ["descriptor", "version", "timestamp", "db", "configuration"])
def test_metadata_missing_required_provenance_fails(tmp_path, missing):
    path = write_descriptor(["json=" + str(tmp_path / "descriptor.json")])
    document = json.loads(path.read_text())
    if missing == "descriptor":
        document = {}
    else:
        del document["descriptor"][missing]
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="descriptor"):
        runner.scanner_metadata(path, tmp_path / "metadata.json")
