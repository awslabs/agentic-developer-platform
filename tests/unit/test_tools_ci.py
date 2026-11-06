"""Keep the tools PR lane independent of gateway CI."""

import os
import json
import shlex
import subprocess
import textwrap
import importlib.util
import zipfile
from fnmatch import fnmatchcase
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github/workflows"


def _pull_request_paths(workflow: str) -> list[str]:
    source = yaml.load((WORKFLOWS / workflow).read_text(), Loader=yaml.BaseLoader)
    paths = source["on"]["pull_request"]["paths"]
    assert isinstance(paths, list), f"{workflow} must filter pull requests"
    return paths


def _selected(paths: list[str], changed_file: str) -> bool:
    selected = False
    for path in paths:
        if fnmatchcase(changed_file, path.removeprefix("!")):
            selected = not path.startswith("!")
    return selected


def test_adapter_handoff_publishers_receive_the_deployment_environment():
    workflow = yaml.load(
        (WORKFLOWS / "tools-platform-deploy.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    job = workflow["jobs"]["adapter-handoff"]
    publishers = [
        step for step in job["steps"]
        if "publish-deployment-evidence.py\" context " in step.get("run", "")
    ]
    # The signed context has a create-only object key for this workflow run.
    assert len(publishers) == 1
    for step in publishers:
        environment = {
            **workflow.get("env", {}),
            **job.get("env", {}),
            **step.get("env", {}),
        }
        assert environment["ENVIRONMENT"] == environment["ADP_TOOLS_ENVIRONMENT"]
        assert environment["ACCOUNT_ID"] == environment["ADP_TOOLS_ACCOUNT_ID"]


def test_adapter_handoff_shares_the_gateway_release_queue():
    jobs = yaml.safe_load((WORKFLOWS / "tools-platform-deploy.yml").read_text())["jobs"]
    gateway = yaml.safe_load((WORKFLOWS / "gateway-deploy.yml").read_text())
    queue = jobs["adapter-handoff"]["concurrency"]
    assert queue["group"] == gateway["concurrency"]["group"]
    assert queue == jobs["adapter-foundation"]["concurrency"] == jobs["adapter-identity"]["concurrency"]
    assert queue["cancel-in-progress"] is False and queue["queue"] == "max"


@pytest.mark.parametrize("changed", [False, True])
def test_completed_handoff_checks_the_original_signed_context(tmp_path, changed):
    workflow = yaml.load(
        (WORKFLOWS / "tools-platform-deploy.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    steps = workflow["jobs"]["adapter-handoff"]["steps"]
    step = next(s for s in steps if s.get("name") ==
                "Verify completed handoff context matches signed dispatch")
    original = {"run_id": 42, "source_revision": "a" * 40}
    before = tmp_path / "dispatch-context.json"
    before.write_text(json.dumps(original))
    after = tmp_path / "deployment-context.json"
    after.write_text(json.dumps({**original, "run_id": 43} if changed else original, indent=2))
    result = subprocess.run(
        ["bash", "-euc", step["run"]], capture_output=True, text=True,
        env={**os.environ, "ADP_TOOLS_ADAPTER_HANDOFF_CONTEXT": str(before),
             "ADP_TOOLS_ADAPTER_RELEASE_DIR": str(tmp_path)},
    )
    assert result.returncode == (1 if changed else 0), result.stderr
    if changed:
        assert "differs from signed dispatch" in result.stderr
    assert steps.index(step) < next(
        i for i, item in enumerate(steps)
        if item.get("name") == "Publish correlated generic handoff delivery evidence"
    )


@pytest.mark.parametrize(
    "changed_file",
    [
        "modules/tools/facade/pyproject.toml",
        "modules/tools/facade/tools_facade/server.py",
        "modules/tools/facade/tests/test_workload.py",
        "modules/tools/facade/Dockerfile",
        "modules/tools/facade/k8s/deployment.yaml",
    ],
)
def test_facade_only_change_selects_tools_but_not_gateway_ci(changed_file):
    gateway_paths = _pull_request_paths("gateway-ci.yml")
    assert "!modules/tools/facade/**" in gateway_paths
    assert _selected(_pull_request_paths("tools-ci.yml"), changed_file)
    assert not _selected(gateway_paths, changed_file)


def test_tools_ci_runs_real_checks_without_publishing():
    source = (WORKFLOWS / "tools-ci.yml").read_text()
    assert "runs-on: arc-runner-org" in source
    for package in ("facade", "interceptor"):
        assert f"ruff check modules/tools/{package}" in source
        assert f"pytest -q modules/tools/{package}/tests" in source
        assert f"if: steps.packages.outputs.{package} == 'true'" in source
    assert "if: needs.verify.outputs.facade == 'true'" in source
    assert "python -m pytest -q tests/unit/test_tools_ci.py" in source
    assert "docker build" not in source
    assert "docker push" not in source
    assert "needs: verify" in source
    assert "uses: ./.github/actions/codebuild-run" in source
    assert (
        "service_role_arn: arn:aws:iam::${{ steps.identity.outputs.id }}:role/adp-dev-codebuild-gateway-pr"
        in source
    )
    assert "source_namespace: adp-dev-gateway-build-pr" in source
    assert "buildspec: codebuild/bs-tools-facade-smoke.yml" in source
    assert "configure-aws-credentials" not in source
    assert 'python .github/scripts/tools_ci_packages.py --base "$BASE_SHA"' in source
    assert "fetch-depth: 0" in source
    assert (
        "-e 'modules/gateway[dev]' -e modules/tools -e modules/tools/facade" in source
    )
    assert "-e modules/tools -e 'modules/tools/facade[dev]'" in source


def test_tools_ci_installs_check_tools_with_gateway_dev_extra():
    workflow = yaml.load(
        (WORKFLOWS / "tools-ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    steps = workflow["jobs"]["verify"]["steps"]
    dependencies = next(
        step["run"]
        for step in steps
        if step.get("name") == "Install workflow test dependencies"
    )
    assert {
        "pytest==9.0.3",
        "pytest-asyncio==1.4.0",
        "PyYAML==6.0.3",
        "ruff==0.9.6",
    } <= set(shlex.split(dependencies))
    interceptor = next(
        step["run"]
        for step in steps
        if step.get("name") == "Verify interceptor package"
    )
    assert shlex.split(interceptor.splitlines()[0]) == [
        "python",
        "-m",
        "pip",
        "install",
        "-e",
        "modules/tools/interceptor",
    ]


def _selector():
    path = WORKFLOWS.parents[1] / ".github/scripts/tools_ci_packages.py"
    spec = importlib.util.spec_from_file_location("tools_ci_packages", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.packages


def test_pipeline_can_bootstrap_before_application_stories(tmp_path):
    assert _selector()(tmp_path, None) == {
        "facade": False,
        "interceptor": False,
        "adapters": False,
    }


def test_present_package_cannot_skip_missing_prerequisites(tmp_path):
    (tmp_path / "modules/tools/facade").mkdir(parents=True)
    with pytest.raises(ValueError, match="Incomplete Tools package"):
        _selector()(tmp_path, None)


def test_packages_enable_independently(tmp_path):
    path = tmp_path / "modules/tools/interceptor"
    (path / "tests").mkdir(parents=True)
    (path / "pyproject.toml").write_text("[project]\n")
    assert _selector()(tmp_path, None) == {
        "facade": False,
        "interceptor": True,
        "adapters": False,
    }


@pytest.mark.parametrize("package", ["interceptor", "adapters"])
def test_removing_existing_package_cannot_turn_checks_green(tmp_path, package):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    path = tmp_path / "modules/tools" / package
    path.mkdir(parents=True)
    (path / "pyproject.toml").write_text("[project]\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.invalid",
            "commit",
            "-qm",
            "base",
        ],
        cwd=tmp_path,
        check=True,
    )
    base = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, text=True
    ).strip()
    (path / "pyproject.toml").unlink()
    path.rmdir()
    with pytest.raises(ValueError, match="removed"):
        _selector()(tmp_path, base)


def test_unresolved_base_fails_closed(tmp_path):
    with pytest.raises(ValueError, match="immutable"):
        _selector()(tmp_path, "main")


def test_smoke_buildspec_cannot_publish_and_is_selected_with_its_workflow():
    buildspec = (
        WORKFLOWS.parents[1] / "codebuild/bs-tools-facade-smoke.yml"
    ).read_text()
    assert "docker build --pull --file modules/tools/facade/Dockerfile" in buildspec
    assert "docker image inspect tools-facade:pr-smoke" in buildspec
    assert "docker push" not in buildspec
    assert "aws ecr" not in buildspec
    assert "codebuild/bs-tools-facade-smoke.yml" in _pull_request_paths("tools-ci.yml")
    role_policy = (
        WORKFLOWS.parents[1] / "platform/infra/modules/codebuild/main.tf"
    ).read_text()
    assert 'resource "aws_iam_role_policy" "gateway_pr"' in role_policy
    assert 'Sid       = "DenyOtherApis"' in role_policy


def test_smoke_buildspec_runs_without_a_daemon_or_publish_credentials(tmp_path):
    buildspec = (
        WORKFLOWS.parents[1] / "codebuild/bs-tools-facade-smoke.yml"
    ).read_text()
    shell = textwrap.dedent(buildspec.split("      - |\n", 1)[1])
    dockerfile = tmp_path / "modules/tools/facade/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM scratch\n")
    binary = tmp_path / "bin/docker"
    binary.parent.mkdir()
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$DOCKER_CALLS"\n')
    binary.chmod(0o755)
    calls = tmp_path / "docker-calls"
    environment = os.environ | {
        "PATH": str(binary.parent) + os.pathsep + os.environ["PATH"],
        "DOCKER_CALLS": str(calls),
        "ADP_SOURCE_SHA": "a" * 40,
    }
    subprocess.run(["bash", "-c", shell], cwd=tmp_path, env=environment, check=True)
    invoked = calls.read_text().splitlines()
    assert len(invoked) == 2
    assert invoked[0].startswith(
        "build --pull --file modules/tools/facade/Dockerfile --tag tools-facade:pr-smoke ."
    )
    assert invoked[1].startswith("image inspect tools-facade:pr-smoke")
    calls.unlink()
    result = subprocess.run(
        ["bash", "-c", shell],
        cwd=tmp_path,
        env=environment | {"ADP_SOURCE_SHA": "main"},
        check=False,
    )
    assert result.returncode != 0
    assert not calls.exists()


def test_gateway_source_still_runs_gateway_but_not_tools_ci():
    changed_file = "modules/gateway/src/internal/tools_authorize.py"
    assert _selected(_pull_request_paths("gateway-ci.yml"), changed_file)
    assert not _selected(_pull_request_paths("tools-ci.yml"), changed_file)


def test_identity_proof_repair_scope_selects_real_tools_checks():
    root = WORKFLOWS.parents[1]
    candidate = json.loads(
        (root / "docs/tools/atlassian-mcp/repair-scope-8446.json").read_text()
    )
    paths = candidate["repair_paths"]
    assert candidate["status"].startswith("candidate-only")
    assert candidate["workflow_allowlist"] == ["gateway-deploy.yml"]
    assert paths == sorted(set(paths))
    for path in paths:
        assert (root / path).is_file(), path
        assert _selected(_pull_request_paths("tools-ci.yml"), path), path
    source = (WORKFLOWS / "tools-ci.yml").read_text()
    assert "python -m pytest -q tests/e2e/tools" in source
    for path in (
        "modules/gateway/tests/agentauth/test_operations_repair.py",
        "modules/gateway/tests/orchestration/test_deployment_workflow_contract.py",
        "modules/gateway/tests/internal/test_tools_effective_policy.py",
    ):
        assert path in source


@pytest.mark.parametrize(
    "changed_file",
    [
        "modules/tools/adapters/pyproject.toml",
        "modules/tools/adapters/uv.lock",
        "modules/tools/adapters/tool_adapters/server.py",
        "modules/tools/adapters/Dockerfile",
        "modules/tools/adapters/k8s/render.py",
        "modules/tools/adapters/scripts/image_smoke.py",
        "modules/tools/adapters/scripts/emit_image_evidence.py",
        "modules/tools/adapters/infra/main.tf",
        "modules/tools/adapters/infra/tests/publication.tftest.hcl",
        "modules/tools/adapters/foundation-infra/main.tf",
        "modules/tools/adapters/foundation-infra/tests/access.tftest.hcl",
        "codebuild/bs-tool-adapters-smoke.yml",
    ],
)
def test_adapter_checks_are_selected_without_gateway_build(changed_file):
    assert _selected(_pull_request_paths("tools-ci.yml"), changed_file)
    assert not _selected(_pull_request_paths("gateway-ci.yml"), changed_file)


def _adapter_scaffold(root):
    package = root / "modules/tools/adapters"
    (package / "tests").mkdir(parents=True)
    for name in (
        "pyproject.toml",
        "Dockerfile",
        "uv.lock",
        "tool_adapters/server.py",
        "scripts/image_smoke.py",
        "scripts/emit_image_evidence.py",
    ):
        path = package / name
        path.parent.mkdir(exist_ok=True)
        path.write_text("fixture\n")
    return package


def test_adapter_scaffold_enables_its_own_ci(tmp_path):
    _adapter_scaffold(tmp_path)
    assert _selector()(tmp_path, None) == {
        "facade": False,
        "interceptor": False,
        "adapters": True,
    }


@pytest.mark.parametrize(
    "missing",
    [
        "uv.lock",
        "Dockerfile",
        "tool_adapters/server.py",
        "scripts/image_smoke.py",
        "scripts/emit_image_evidence.py",
    ],
)
def test_incomplete_adapter_package_cannot_skip_image_qualification(tmp_path, missing):
    package = _adapter_scaffold(tmp_path)
    (package / missing).unlink()
    with pytest.raises(ValueError, match="Incomplete Tools package"):
        _selector()(tmp_path, None)


def test_adapter_ci_uses_locked_isolated_tests_and_existing_nonpublishing_role():
    workflow = yaml.load(
        (WORKFLOWS / "tools-ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    verify = workflow["jobs"]["verify"]
    package_check = next(
        step
        for step in verify["steps"]
        if step.get("working-directory") == "modules/tools/adapters"
    )
    assert package_check["if"] == "steps.packages.outputs.adapters == 'true'"
    assert "pip install uv==0.13.4" in package_check["run"]
    for command in ("ruff check", "ruff format --check", "pytest -q"):
        assert (
            f"env -u PYTHONPATH uv run --frozen --group dev {command}"
            in package_check["run"]
        )
    smoke = workflow["jobs"]["adapter-smoke-build"]
    assert smoke["needs"] == "verify"
    assert smoke["if"] == "needs.verify.outputs.adapters == 'true'"
    build = next(step for step in smoke["steps"] if step.get("id") == "smoke")
    assert build["uses"] == "./.github/actions/codebuild-run"
    assert build["with"]["buildspec"] == "codebuild/bs-tool-adapters-smoke.yml"
    assert build["with"]["source_namespace"] == "adp-dev-gateway-build-pr"
    assert build["with"]["service_role_arn"].endswith(
        ":role/adp-dev-codebuild-gateway-pr"
    )
    evidence = smoke["steps"][-1]
    assert evidence["if"].startswith("always()")
    assert "steps.smoke.outputs.build_id" in evidence["env"]["ADP_ADAPTER_BUILD_ID"]
    assert "logs.deepLink" in evidence["run"]


def _adapter_terraform_step():
    workflow = yaml.load(
        (WORKFLOWS / "tools-ci.yml").read_text(), Loader=yaml.BaseLoader
    )
    verify = workflow["jobs"]["verify"]
    step = next(
        step for step in verify["steps"]
        if step.get("name") == "Verify adapter Terraform with mocked providers"
    )
    return workflow, verify, step


def test_adapter_terraform_gates_image_qualification_without_deploy_credentials():
    workflow, verify, step = _adapter_terraform_step()
    assert step["if"] == "steps.packages.outputs.adapters == 'true'"
    assert "continue-on-error" not in step
    assert "continue-on-error" not in verify
    assert verify.get("permissions", workflow["permissions"]) == {"contents": "read"}
    assert "environment" not in verify
    for job in ("smoke-build", "adapter-smoke-build"):
        assert workflow["jobs"][job]["needs"] == "verify"
        assert "always()" not in workflow["jobs"][job]["if"]
    setup = next(
        item for item in verify["steps"]
        if item.get("uses", "").startswith("hashicorp/setup-terraform@")
    )
    assert setup["if"] == step["if"]
    assert setup["with"] == {
        "terraform_version": "1.14.6",
        "terraform_wrapper": "false",
    }
    assert verify["steps"].index(setup) < verify["steps"].index(step)
    assert step["env"] == {
        "AWS_ACCESS_KEY_ID": "adapter-ci-no-cloud-access",
        "AWS_SECRET_ACCESS_KEY": "adapter-ci-no-cloud-access",
        "AWS_SESSION_TOKEN": "adapter-ci-no-cloud-access",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_SHARED_CREDENTIALS_FILE": "/dev/null",
        "AWS_CONFIG_FILE": "/dev/null",
        "KUBECONFIG": "/dev/null",
    }
    # Terraform returns success when no tests are discovered. Keep all actual
    # suites present and selected, with all their providers mocked.
    for module, providers in (
        ("infra", ("aws",)),
        ("foundation-infra", ("aws", "kubernetes")),
        ("deployment-infra", ("aws", "kubernetes")),
    ):
        suites = list(
            (WORKFLOWS.parents[1] / "modules/tools/adapters" / module / "tests")
            .glob("*.tftest.hcl")
        )
        assert suites, module
        for suite in suites:
            source = suite.read_text()
            assert 'run "' in source, suite
            for provider in providers:
                assert f'mock_provider "{provider}"' in source, suite
            assert _selected(
                _pull_request_paths("tools-ci.yml"),
                str(suite.relative_to(WORKFLOWS.parents[1])),
            )


@pytest.mark.parametrize("failure_index", [None, *range(12)])
def test_adapter_terraform_executes_all_suites_and_propagates_failures(
    tmp_path, failure_index,
):
    _, _, step = _adapter_terraform_step()
    binary = tmp_path / "terraform"
    binary.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import json
        import os
        import sys
        from pathlib import Path
        log = Path(os.environ["TF_CALLS"])
        calls = log.read_text().splitlines() if log.exists() else []
        with log.open("a") as stream:
            stream.write(json.dumps(sys.argv[1:]) + "\\n")
        if str(len(calls)) == os.environ["TF_FAIL_INDEX"]:
            sys.exit(29)
        """))
    binary.chmod(0o755)
    calls = tmp_path / "calls.jsonl"
    result = subprocess.run(
        ["bash", "-c", step["run"]],
        cwd=tmp_path,
        env=os.environ | step["env"] | {
            "PATH": str(tmp_path) + os.pathsep + os.environ["PATH"],
            "TF_CALLS": str(calls),
            "TF_FAIL_INDEX": str(failure_index),
        },
        capture_output=True,
        text=True,
        check=False,
    )
    expected = [
        [f"-chdir=modules/tools/adapters/{module}", *command]
        for module in ("infra", "foundation-infra", "deployment-infra")
        for command in (
            ["fmt", "-check", "-recursive"],
            ["init", "-backend=false", "-input=false", "-no-color"],
            ["validate", "-no-color"],
            ["test", "-no-color"],
        )
    ]
    if failure_index is not None:
        expected = expected[:failure_index + 1]
    assert result.returncode == (0 if failure_index is None else 29), result.stderr
    assert [json.loads(line) for line in calls.read_text().splitlines()] == expected


def test_adapter_build_preserves_evidence_without_artifact_write_permissions():
    root = WORKFLOWS.parents[1]
    buildspec = yaml.load(
        (root / "codebuild/bs-tool-adapters-smoke.yml").read_text(),
        Loader=yaml.BaseLoader,
    )
    assert "artifacts" not in buildspec
    build = buildspec["phases"]["build"]
    assert "image_smoke.py" in build["commands"][0]
    assert "emit_image_evidence.py" in build["finally"][0]
    assert '--source-revision "$ADP_SOURCE_SHA"' in build["finally"][0]
    assert "aws s3" not in json.dumps(buildspec)
    policy = (root / "platform/infra/modules/codebuild/main.tf").read_text()
    assert 'type = "NO_ARTIFACTS"' in policy
    assert 'Sid       = "DenyOtherApis"' in policy


def test_shared_source_archive_change_selects_tools_and_gateway_checks():
    for workflow in ("tools-ci.yml", "gateway-ci.yml"):
        assert _selected(
            _pull_request_paths(workflow), "platform/scripts/zip-source.sh"
        )


def test_actual_codebuild_archive_retains_adapter_lock_and_existing_exclusions(
    tmp_path,
):
    root = WORKFLOWS.parents[1]
    source = tmp_path / "source"
    for directory in ("platform", "modules", "environments", "libs", "codebuild"):
        (source / directory).mkdir(parents=True)
    required = (
        "modules/tools/adapters/uv.lock",
        "modules/tools/adapters/pyproject.toml",
        "modules/tools/adapters/Dockerfile",
        "codebuild/bs-tool-adapters-smoke.yml",
    )
    for name in required:
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes((root / name).read_bytes())
    excluded = (
        "modules/sample/node_modules/fixture",
        "platform/infra/terraform.tfstate",
    )
    for name in excluded:
        path = source / name
        path.parent.mkdir(parents=True)
        path.write_text("not-for-build\n")
    archive = tmp_path / "source.zip"
    subprocess.run(
        [
            "bash",
            str(root / "platform/scripts/zip-source.sh"),
            str(source),
            str(archive),
        ],
        check=True,
        capture_output=True,
    )
    with zipfile.ZipFile(archive) as package:
        assert set(required) <= set(package.namelist())
        assert not set(excluded) & set(package.namelist())
        assert package.read(required[0]) == (root / required[0]).read_bytes()
