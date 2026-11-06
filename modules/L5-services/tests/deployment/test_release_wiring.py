"""Offline checks of release selection, artifact publication and private rendering."""

import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from l5_services.selector_receipts import MAX_SELECTOR_AGE_SECONDS


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/L5-services"


def workflow(name):
    return yaml.load(
        (ROOT / f".github/workflows/{name}.yml").read_text(), Loader=yaml.BaseLoader
    )


def selected(patterns, path):
    matched = False
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern.lstrip("!")):
            matched = not pattern.startswith("!")
    return matched


@pytest.mark.parametrize(
    "path",
    [
        "modules/L5-services/src/l5_services/workers/execution.py",
        "modules/L5-services/pyproject.toml",
        "modules/L5-services/requirements-security.lock",
        "modules/L5-services/scripts/build-package.sh",
        "modules/L5-services/scripts/stage-gateway.sh",
        "modules/gateway/scripts/stage-l5-wheel.sh",
        "modules/gateway/scripts/stage-common-wheel.sh",
        "libs/python/adp-common/src/adp_platform_common/l5_contributions/http.py",
        "docs/l5-services/contracts/v1/capabilities.tsv",
        "docs/l5-services/contracts/v1/schemas.json",
    ],
)
def test_l5_runtime_changes_schedule_gateway_checks_and_backend_deploy(path):
    ci = workflow("gateway-ci")["on"]
    deploy = workflow("gateway-deploy")["on"]
    assert selected(ci["pull_request"]["paths"], path)
    assert selected(ci["push"]["paths"], path)
    assert selected(deploy["push"]["paths"], path)
    selection = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
    backend_line = next(
        line for line in selection.splitlines() if "modules/L5-services/(src/" in line
    )
    expression = backend_line.split("grep -qE '", 1)[1].split("'", 1)[0]
    assert re.search(expression, path)


def test_finops_release_is_selectable_but_gateway_admissions_default_off():
    release = workflow("l5-services-release")
    assert "finops" in release["on"]["workflow_dispatch"]["inputs"]["service"]["options"]
    template = (ROOT / "modules/gateway/k8s/deployment.yaml").read_text()
    assert 'value: "__L5_FINOPS_ADMISSION_ENABLED__"' in template
    for path in ("platform/scripts/deploy-all.sh", ".github/workflows/gateway-deploy.yml"):
        renderer = (ROOT / path).read_text()
        assert 'l5-finops-admission-enabled" "false"' in renderer
        assert '[[ "$L5_FINOPS_ADMISSION_ENABLED" == "true" ]] || L5_FINOPS_ADMISSION_ENABLED="false"' in renderer
        assert 's|__L5_FINOPS_ADMISSION_ENABLED__|${L5_FINOPS_ADMISSION_ENABLED}|g' in renderer


def test_console_policy_precedes_publication_credentials_and_browser_setup():
    release = workflow("l5-services-release")
    assert not any("console" in name for name in release["on"]["workflow_dispatch"]["inputs"])
    for name in ("publish", "qualify-selected"):
        job = release["jobs"][name]
        assert job["environment"] == "adp-gateway-deploy-${{ inputs.environment }}"
        assert job["env"]["L5_AWS_CONSOLE_AUTOMATION"] == "${{ vars.ADP_L5_AWS_CONSOLE_AUTOMATION || 'disabled' }}"
        steps = job["steps"]
        check = next(i for i, step in enumerate(steps) if "check-console-policy" in step.get("run", ""))
        for i, step in enumerate(steps):
            if (step.get("uses") == "./.github/actions/trusted-deployment"
                    or any(action in step.get("run", "") for action in (
                        "publish-workers.sh", "playwright install", "worker-lifecycle.py resume"))):
                assert check < i


def test_standalone_install_cannot_reach_native_resume_or_delivery_emission():
    release = workflow("l5-services-release")
    operation = release["on"]["workflow_dispatch"]["inputs"]["operation"]
    assert operation["default"] == "release" and operation["options"] == ["release", "install", "publish"]
    native_jobs = set()
    for name, job in release["jobs"].items():
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        if any(call in commands for call in ("worker-lifecycle.py resume", "publish-release-reports.py",
                                             "emit-security-qualification.py")):
            native_jobs.add(name)
            assert "(inputs.operation == 'release' || inputs.operation == '')" in job["if"]
    assert native_jobs == {"qualify-security", "qualify-selected"}
    install_commands = "\n".join(step.get("run", "") for step in release["jobs"]["install"]["steps"])
    assert "deploy-infra.py apply" in install_commands and "worker-lifecycle.py rollout" in install_commands
    preflight = next(step["run"] for step in release["jobs"]["publish"]["steps"]
                     if "check-release-prerequisites.py" in step.get("run", ""))
    assert '--operation "$L5_RELEASE_OPERATION"' in preflight
    assert 'if [[ "$L5_RELEASE_OPERATION" == release ]]' in preflight


def test_publication_cannot_install_or_qualify_a_service():
    release = workflow("l5-services-release")
    assert "inputs.operation != 'publish'" in release["jobs"]["install"]["if"]
    for name, job in release["jobs"].items():
        commands = "\n".join(step.get("run", "") for step in job["steps"])
        if "deploy-infra.py apply" in commands or "worker-lifecycle.py rollout" in commands:
            assert name == "install"
        if any(command in commands for command in ("worker-lifecycle.py resume", "publish-release-reports.py")):
            assert "(inputs.operation == 'release' || inputs.operation == '')" in job["if"]


def test_dev_box_migration_checks_install_l5_alongside_gateway():
    steps = workflow("dev-box-ci")["jobs"]["module"]["steps"]
    lifecycle = next(
        step for step in steps if "modules/gateway[dev]" in step.get("run", "")
    )
    install = shlex.split(lifecycle["run"].splitlines()[0])
    assert install[:4] == ["python", "-m", "pip", "install"]
    assert {"modules/gateway[dev]", "modules/L5-services[dev]"} <= set(install)


def test_infra_schedules_plan_but_documents_do_not_auto_deploy():
    infra = "modules/L5-services/infra/modules/worker-runtime/queues.tf"
    assert selected(
        workflow("gateway-infra-plan")["on"]["pull_request"]["paths"], infra
    )
    assert selected(workflow("gateway-ci")["on"]["pull_request"]["paths"], infra)
    deploy = workflow("gateway-deploy")["on"]["push"]["paths"]
    assert not selected(deploy, infra)
    assert not selected(deploy, "docs/l5-services/architecture.md")
    assert not selected(deploy, "docs/l5-services/contracts/v1/README.md")
    assert not selected(deploy, "modules/gateway/frontend/src/app.tsx")


def test_image_builds_stage_the_release_and_private_queue_is_rendered():
    image_build = (ROOT / "platform/scripts/publish-shared-image.sh").read_text()
    assert "bash modules/gateway/scripts/stage-l5-wheel.sh" in image_build
    assert (
        "- bash modules/L5-services/scripts/stage-gateway.sh"
        in (ROOT / "codebuild/bs-gateway-smoke.yml").read_text()
    )
    assert '"run", "bash", "modules/gateway/scripts/stage-l5-wheel.sh"' in (
        (ROOT / "codebuild/security_image_targets.py").read_text()
    )
    for path in (
        "platform/scripts/deploy-all.sh",
        ".github/workflows/gateway-deploy.yml",
    ):
        renderer = (ROOT / path).read_text()
        assert "gateway/l5-work-queue-url" in renderer
        assert "s|__L5_WORK_QUEUE_URL__|${L5_WORK_QUEUE_URL}|g" in renderer
        assert '|| L5_WORK_QUEUE_URL=""' in renderer
    assert (
        "__L5_WORK_QUEUE_URL__"
        in (ROOT / "modules/gateway/k8s/deployment.yaml").read_text()
    )
    assert (
        "INCLUDE_DIRS+=(docs/l5-services/contracts/v1/)"
        in (ROOT / "platform/scripts/zip-source.sh").read_text()
    )


def test_worker_publication_is_explicit_protected_and_reviewed():
    release = workflow("l5-services-release")
    assert list(release["on"]) == ["workflow_dispatch"]
    job = release["jobs"]["publish"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"] == "adp-gateway-deploy-${{ inputs.environment }}"
    steps = job["steps"]
    checkout = next(
        step for step in steps if step.get("uses", "").startswith("actions/checkout@")
    )
    assert checkout["with"]["ref"] == "${{ inputs.adp_source_revision || inputs.reviewed_source_sha }}"
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["persist-credentials"] == "false"
    assert any(
        "git merge-base --is-ancestor HEAD refs/remotes/origin/main" in step.get("run", "")
        for step in steps
    )
    assert not any("git fetch" in step.get("run", "") for step in steps)
    assert any(
        step.get("uses") == "./.github/actions/trusted-deployment" for step in steps
    )
    publication = next(
        step["run"] for step in steps if "publish-workers.sh" in step.get("run", "")
    )
    assert "build-package.sh" in publication
    assert "build-workers.sh" in publication
    assert "publish-workers.sh" in publication
    script = (MODULE / "scripts/publish-workers.sh").read_text()
    assert "--if-none-match '*'" in script
    assert "s3api get-object" in script
    assert 'cmp --silent "$source" "$stage/readback"' in script


def test_shared_publication_exposes_engine_inputs_without_claiming_qualification():
    release = workflow("l5-services-release")
    inputs = release["on"]["workflow_dispatch"]["inputs"]
    assert inputs["service"]["options"] == ["security", "devops", "finops", "transform", "well_architected", "well-architected"]
    assert inputs["service"]["default"] == "security"
    assert {"adp_correlation", "adp_source_revision", "adp_definition_revision",
            "adp_contract_digest"} <= inputs.keys()
    job = release["jobs"]["publish"]
    steps = job["steps"]
    guard = next(i for i, step in enumerate(steps) if "release_context.py" in step.get("run", ""))
    identity = next(i for i, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-deployment")
    publish = next(i for i, step in enumerate(steps) if "publish-workers.sh" in step.get("run", ""))
    assert guard < identity < publish
    assert steps[publish]["run"].index("--check-aws") < steps[publish]["run"].index("publish-workers.sh")
    assert "release-artifacts.py" in steps[publish]["run"]
    assert release["concurrency"]["group"] == "l5-services-release-${{ inputs.environment }}"
    assert "inputs.service" not in release["concurrency"]["group"]
    assert job.get("name") != "L5 release and live qualification"
    retained = steps[-1]
    assert retained["uses"] == "./.github/actions/upload-ci-evidence-s3"
    assert retained["with"]["name"] == "l5-release-artifacts"


def test_security_qualification_is_gated_on_private_authority_and_pinned_installation():
    jobs = workflow("l5-services-release")["jobs"]
    publish = jobs["publish"]
    qualify = jobs["qualify-security"]
    assert int(qualify["timeout-minutes"]) * 60 == MAX_SELECTOR_AGE_SECONDS
    assert qualify["name"] == "L5 release and live qualification"
    assert qualify["needs"] == ["publish", "install"] and "inputs.service == 'security'" in qualify["if"]
    assert qualify["environment"] == publish["environment"]
    assert qualify["env"]["WHEEL_DIGEST"] == "${{ needs.publish.outputs.wheel_digest }}"
    assert qualify["env"]["MANIFEST_VERSION"] == "${{ needs.publish.outputs.manifest_version }}"
    assert publish["outputs"]["manifest_version"] == "${{ steps.pinned.outputs.manifest_version }}"
    steps = qualify["steps"]
    checkout = next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/checkout@"))
    preflight = next(i for i, step in enumerate(steps) if "run_security_live.py" in step.get("run", "") and "preflight" in step["run"])
    role = next(i for i, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-deployment")
    install = next(i for i, step in enumerate(steps) if "worker-lifecycle.py" in step.get("run", ""))
    selectors = next(i for i, step in enumerate(steps) if "for scenario in setup assessments recovery context" in step.get("run", ""))
    proof = next(i for i, step in enumerate(steps) if step.get("name") == "Read current Product results, provider jobs and installed code independently")
    emission = next(i for i, step in enumerate(steps) if step.get("name") == "Require evaluated delivery evidence before qualification succeeds")
    assert checkout < preflight < role < install < selectors < proof < emission
    assert "git merge-base --is-ancestor HEAD refs/remotes/origin/main" in steps[preflight]["run"]
    assert "PRIVATE_FIXTURE" in steps[preflight]["run"] and "PRIVATE_EVIDENCE_DIR" in steps[preflight]["run"]
    assert "test -x modules/L5-services/scripts/qualify-security-readbacks.py" in steps[preflight]["run"]
    assert "test -x modules/L5-services/scripts/emit-security-qualification.py" in steps[preflight]["run"]
    assert "L5_QUALIFICATION_PREFLIGHT=1 modules/L5-services/scripts/emit-security-qualification.py" in steps[preflight]["run"]
    assert "ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON" not in qualify["env"]
    assert "ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON" in steps[preflight]["env"]
    assert "ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON" in steps[selectors]["env"]
    assert steps[install]["run"].index("release_context.py --check-aws") < steps[install]["run"].index("rollout")
    assert steps[install]["run"].index("status") < steps[install]["run"].index("rollout") < steps[install]["run"].index("resume")
    staging = next(i for i, step in enumerate(steps) if "publish-release-reports.py" in step.get("run", ""))
    uploads = [i for i, step in enumerate(steps) if "upload" in step.get("uses", "")]
    assert uploads and all(emission < staging < index for index in uploads)
    assert all("l5-published-reports/" in steps[index]["with"]["path"] for index in uploads)
    assert "modules/L5-services/scripts/qualify-security-readbacks.py" in steps[proof]["run"]
    assert qualify["permissions"]["actions"] == "read"
    assert qualify["permissions"]["deployments"] == "read"
    assert steps[proof]["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "GH_TOKEN" not in steps[selectors].get("env", {})
    assert "test -x modules/L5-services/scripts/emit-security-qualification.py" in steps[emission]["run"]
    assert steps[preflight]["env"]["PRIVATE_FIXTURE_JSON"] == "${{ secrets.ADP_L5_SECURITY_LIVE_CONFIG_JSON }}"
    assert "qualification-dispatch.py prepare security" in steps[preflight]["run"]
    assert "PRIVATE_FIXTURE" not in steps[emission].get("env", {})
    assert "ADP_L5_SECURITY_LIVE_CREDENTIALS_JSON" not in steps[emission].get("env", {})
    assert qualify["env"]["ARCHIVE_DIGEST"] == "${{ needs.publish.outputs.archive_digest }}"
    assert "L5_FIXTURE_DIGEST=" in steps[preflight]["run"]


def test_only_transform_publication_uses_isolated_hosted_docker():
    jobs = workflow("l5-services-release")["jobs"]
    publish = jobs["publish"]
    assert publish["runs-on"] == "${{ inputs.service == 'transform' && 'ubuntu-24.04' || 'arc-runner-org' }}"
    assert publish["environment"] == "adp-gateway-deploy-${{ inputs.environment }}"
    assert publish["permissions"]["id-token"] == "write"
    assert publish["timeout-minutes"] == "30"
    for name in ("install", "qualify-security", "qualify-selected"):
        assert jobs[name]["runs-on"] == "arc-runner-org"
    steps = publish["steps"]
    probe = next(index for index, step in enumerate(steps) if step.get("name") == "Verify isolated Transform Docker builder")
    role = next(index for index, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-deployment")
    assert probe < role and steps[probe]["if"] == "inputs.service == 'transform'"
    assert "docker info" in steps[probe]["run"] and "::error title=Transform image builder unavailable" in steps[probe]["run"]
    assert "DOCKER_HOST" not in str(publish) and "ssh://" not in str(publish)


@pytest.mark.parametrize("runner", ["", "GitHub Actions 1"])
def test_failed_hosted_assignment_reports_actionable_capacity_repair(tmp_path, runner):
    jobs = workflow("l5-services-release")["jobs"]
    diagnostic = jobs["diagnose-transform-hosted-runner"]
    assert diagnostic["needs"] == "publish"
    assert "inputs.service == 'transform'" in diagnostic["if"]
    assert "needs.publish.result == 'failure'" in diagnostic["if"]
    assert diagnostic["permissions"] == {"actions": "read"}
    run = diagnostic["steps"][0]["run"]
    assert "attempts/$GITHUB_RUN_ATTEMPT/jobs" in run
    script = run.split("python3 - <<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    (tmp_path / "transform-publication-jobs.json").write_text(json.dumps({"jobs": [{
        "name": jobs["publish"]["name"], "runner_name": runner}]}))
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                            env={**os.environ, "RUNNER_TEMP": str(tmp_path)})
    if runner:
        assert result.returncode == 0 and "specific build or registry error" in result.stdout
        assert "billing" not in result.stdout
    else:
        assert result.returncode == 1
        assert "hosted-runner policy" in result.stdout and "billing status" in result.stdout
        assert "original publication job" in result.stdout


def test_sibling_qualification_requires_private_fixture_and_concrete_driver_before_resume():
    release = workflow("l5-services-release")
    jobs = release["jobs"]
    inputs = release["on"]["workflow_dispatch"]["inputs"]
    assert inputs["installation_services"]["required"] == "false"
    install = jobs["install"]
    validate = next(step for step in install["steps"] if "Selected qualification service must be in the installed group" in step.get("run", ""))
    assert '--services "foundation,$L5_INSTALL_SERVICES"' in validate["run"]
    sibling = jobs["qualify-selected"]
    assert sibling["needs"] == ["publish", "install"]
    assert "inputs.service != 'security'" in sibling["if"]
    steps = sibling["steps"]
    selected = next(index for index, step in enumerate(steps) if "qualification-dispatch.py check-driver" in step.get("run", ""))
    native = next(index for index, step in enumerate(steps) if 'preflight "$SOURCE_SHA"' in step.get("run", ""))
    role = next(index for index, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-deployment")
    resume = next(index for index, step in enumerate(steps) if "worker-lifecycle.py resume" in step.get("run", ""))
    execute = next(index for index, step in enumerate(steps) if 'run "$SOURCE_SHA"' in step.get("run", ""))
    renewed_role = max(index for index, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-deployment")
    emit = next(index for index, step in enumerate(steps) if "emit-${service}-qualification.py" in step.get("run", ""))
    assert selected < native < role < resume < execute < renewed_role < emit
    assert "qualification-dispatch.py prepare" in steps[selected]["run"]
    assert "PRIVATE_FIXTURE_JSON" in steps[selected]["env"]
    assert steps[emit]["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "GH_TOKEN" not in steps[execute].get("env", {})
    assert "PRIVATE_FIXTURE_JSON" not in sibling.get("env", {})


def test_finops_qualification_installs_and_checks_browser_before_preflight(tmp_path):
    steps = workflow("l5-services-release")["jobs"]["qualify-selected"]["steps"]
    python_dependencies = next(index for index, step in enumerate(steps) if "python -m playwright install" in step.get("run", ""))
    node = next(index for index, step in enumerate(steps) if step.get("uses") == "actions/setup-node@v7")
    dependencies = next(index for index, step in enumerate(steps) if step.get("run") == "npm ci --include=dev")
    chromium = next(index for index, step in enumerate(steps) if step.get("run") == "npx playwright install chromium --with-deps")
    cache = next(index for index, step in enumerate(steps) if "PLAYWRIGHT_BROWSERS_PATH=" in step.get("run", ""))
    smoke = next(index for index, step in enumerate(steps) if "qualify-finops-release.py check-browser" in step.get("run", ""))
    preflight = next(index for index, step in enumerate(steps) if 'preflight "$SOURCE_SHA"' in step.get("run", ""))
    resume = next(index for index, step in enumerate(steps) if "worker-lifecycle.py resume" in step.get("run", ""))
    assert python_dependencies < node < dependencies < cache < chromium < smoke < preflight < resume
    assert "'playwright==1.58.0'" in steps[python_dependencies]["run"]
    assert "python -m playwright install --with-deps chromium" in steps[python_dependencies]["run"]
    assert "if" not in steps[python_dependencies]
    assert "continue-on-error" not in steps[python_dependencies]
    assert steps[node]["with"]["node-version"] == "22"
    assert steps[node]["with"]["cache-dependency-path"] == "modules/gateway/frontend/package-lock.json"
    for index in (node, dependencies, cache, chromium, smoke):
        assert steps[index]["if"] == "inputs.service == 'finops'"
        assert "continue-on-error" not in steps[index]
    for index in (dependencies, chromium):
        assert steps[index]["working-directory"] == "modules/gateway/frontend"
    assert 'check-browser "$SOURCE_SHA" "$PRIVATE_FIXTURE" "$PRIVATE_EVIDENCE_DIR"' in steps[smoke]["run"]
    github_env = tmp_path / "github-env"
    subprocess.run(["bash", "-eu", "-c", steps[cache]["run"]], check=True,
                   env={"RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(github_env)})
    assert github_env.read_text() == f"PLAYWRIGHT_BROWSERS_PATH={tmp_path}/l5-finops-browsers\n"
    for step in steps[cache + 1:]:
        assert "PLAYWRIGHT_BROWSERS_PATH" not in step.get("env", {})


def test_gateway_staging_discards_old_wheels_and_rejects_bad_digest(tmp_path):
    gateway = tmp_path / "modules/gateway"
    scripts = gateway / "scripts"
    scripts.mkdir(parents=True)
    (gateway.parent / "L5-services").mkdir()
    shutil.copy2(ROOT / "modules/gateway/scripts/stage-l5-wheel.sh", scripts)
    shutil.copy2(ROOT / "modules/gateway/scripts/stage-common-wheel.sh", scripts)
    shutil.copytree(ROOT / "libs/python/adp-common", tmp_path / "libs/python/adp-common",
                    ignore=shutil.ignore_patterns("__pycache__", "dist"))
    wheels = []
    for name in ("old.whl", "current.whl"):
        artifact = tmp_path / name
        artifact.write_bytes(name.encode())
        artifact.with_suffix(".whl.sha256").write_text(
            f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {name}\n"
        )
        wheels.append(artifact)
    for artifact in wheels:
        subprocess.run(
            [str(scripts / "stage-l5-wheel.sh"), str(artifact)],
            check=True,
            capture_output=True,
        )
    assert sorted(path.name for path in (gateway / ".l5-release").iterdir()) == [
        "current.whl",
        "current.whl.sha256",
    ]
    staged = gateway / "l5-wheel"
    assert sorted(path.name for path in staged.iterdir()) == ["current.whl", "wheel.sha256"]
    assert (staged / "current.whl").read_bytes() == wheels[1].read_bytes()
    assert (staged / "wheel.sha256").read_bytes() == wheels[1].with_suffix(".whl.sha256").read_bytes()
    common = gateway / "common-wheel"
    assert len(list(common.glob("adp_common-*.whl"))) == 1
    subprocess.run(["sha256sum", "-c", "wheel.sha256"], cwd=common, check=True, capture_output=True)
    wheels[0].write_bytes(b"changed")
    rejected = subprocess.run(
        [str(scripts / "stage-l5-wheel.sh"), str(wheels[0])], capture_output=True
    )
    assert rejected.returncode != 0


def test_build_from_source_without_git_matches_checked_out_wheel(tmp_path):
    source = tmp_path / "source"
    module = source / "modules/L5-services"
    module.mkdir(parents=True)
    shutil.copytree(MODULE / "src", module / "src")
    shutil.copytree(MODULE / "scripts", module / "scripts")
    shutil.copy2(MODULE / "pyproject.toml", module / "pyproject.toml")
    shutil.copytree(
        ROOT / "docs/l5-services/contracts/v1", source / "docs/l5-services/contracts/v1"
    )
    assert not (source / ".git").exists()
    checkout = subprocess.run(
        [str(MODULE / "scripts/build-package.sh"), str(tmp_path / "checkout")],
        check=True,
        capture_output=True,
        text=True,
    )
    archived = subprocess.run(
        [str(module / "scripts/build-package.sh"), str(tmp_path / "archived")],
        check=True,
        capture_output=True,
        text=True,
    )
    assert (
        Path(checkout.stdout.splitlines()[-1]).read_bytes()
        == Path(archived.stdout.splitlines()[-1]).read_bytes()
    )


@pytest.mark.parametrize("fault", ["", "corrupt", "unversioned"])
def test_publication_requires_versioned_verified_bytes(tmp_path, fault):
    wheel = tmp_path / "wheel.whl"
    archive = tmp_path / "workers.zip"
    for artifact in (wheel, archive):
        artifact.write_bytes(artifact.name.encode())
        artifact.with_name(artifact.name + ".sha256").write_text(
            f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n"
        )
    binary = tmp_path / "bin"
    binary.mkdir()
    aws = binary / "aws"
    aws.write_text(
        "#!/usr/bin/env python3\nimport os, pathlib, sys\n"
        "args = sys.argv\nkey = args[args.index('--key') + 1]\n"
        "object_path = pathlib.Path(os.environ['FAKE_S3']) / key\n"
        "if 'put-object' in args:\n"
        "    object_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    object_path.write_bytes(pathlib.Path(args[args.index('--body') + 1]).read_bytes())\n"
        "    print('None' if os.environ['FAULT'] == 'unversioned' else 'version-1')\n"
        "elif 'get-object' in args:\n"
        "    content = object_path.read_bytes()\n"
        "    pathlib.Path(args[-1]).write_bytes(content + (b'bad' if os.environ['FAULT'] == 'corrupt' else b''))\n"
        "else:\n    sys.exit(2)\n"
    )
    aws.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "FAKE_S3": str(tmp_path / "s3"),
        "FAULT": fault,
    }
    result = subprocess.run(
        [
            str(MODULE / "scripts/publish-workers.sh"),
            str(wheel),
            str(archive),
            "private-artifacts",
            "a" * 40,
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == (fault == "")
    prefix = (
        tmp_path
        / "s3/l5/security"
        / ("a" * 40)
        / hashlib.sha256(wheel.read_bytes()).hexdigest()
    )
    assert (prefix / "manifest.json").exists() == (fault == "")
    if not fault:
        manifest = json.loads((prefix / "manifest.json").read_text())
        assert (
            manifest["archive_sha256"]
            == hashlib.sha256(archive.read_bytes()).hexdigest()
        )


@pytest.mark.parametrize("tampered", [False, True])
def test_publication_resumes_a_lost_attempt_and_rejects_stored_drift(
    tmp_path, tampered
):
    wheel = tmp_path / "wheel.whl"
    archive = tmp_path / "workers.zip"
    for artifact in (wheel, archive):
        artifact.write_bytes(artifact.name.encode())
        artifact.with_name(artifact.name + ".sha256").write_text(
            f"{hashlib.sha256(artifact.read_bytes()).hexdigest()}  {artifact.name}\n"
        )
    binary = tmp_path / "bin"
    binary.mkdir()
    aws = binary / "aws"
    aws.write_text(
        "#!/usr/bin/env python3\nimport os, pathlib, sys\n"
        "args = sys.argv\nkey = args[args.index('--key') + 1]\n"
        "object_path = pathlib.Path(os.environ['FAKE_S3']) / key\n"
        "if 'put-object' in args:\n"
        "    if object_path.exists():\n"
        "        sys.stderr.write('An error occurred (PreconditionFailed) when "
        "calling the PutObject operation: At least one of the pre-conditions you "
        "specified did not hold\\n')\n"
        "        sys.exit(1)\n"
        "    object_path.parent.mkdir(parents=True, exist_ok=True)\n"
        "    object_path.write_bytes("
        "pathlib.Path(args[args.index('--body') + 1]).read_bytes())\n"
        "    print('version-1')\n"
        "elif 'head-object' in args:\n"
        "    object_path.read_bytes()\n"
        "    print('version-1')\n"
        "elif 'get-object' in args:\n"
        "    pathlib.Path(args[-1]).write_bytes(object_path.read_bytes())\n"
        "else:\n    sys.exit(2)\n"
    )
    aws.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{binary}:{os.environ['PATH']}",
        "FAKE_S3": str(tmp_path / "s3"),
    }
    command = [
        str(MODULE / "scripts/publish-workers.sh"),
        str(wheel),
        str(archive),
        "private-artifacts",
        "a" * 40,
    ]
    first = subprocess.run(command, env=env, capture_output=True, text=True)
    assert first.returncode == 0, first.stdout + first.stderr
    prefix = (
        tmp_path
        / "s3/l5/security"
        / ("a" * 40)
        / hashlib.sha256(wheel.read_bytes()).hexdigest()
    )
    published = json.loads((prefix / "manifest.json").read_text())
    coordinates = next(
        line for line in first.stdout.splitlines() if line.startswith("release ")
    )
    if tampered:
        (prefix / "workers.zip").write_bytes(b"replaced in storage")
    resumed = subprocess.run(command, env=env, capture_output=True, text=True)
    assert (resumed.returncode == 0) != tampered, resumed.stdout + resumed.stderr
    if tampered:
        assert "differs from local bytes" in resumed.stderr
    else:
        assert json.loads((prefix / "manifest.json").read_text()) == published
        assert coordinates in resumed.stdout.splitlines()


def test_wa_release_reuses_private_worker_factory_and_existing_gateway_triggers():
    infra = ROOT / "modules/L5-services/infra/modules/worker-runtime"
    worker = (infra / "main.tf").read_text()
    repair = (infra / "repair.tf").read_text()
    for source in (worker, repair):
        assert re.search(r"L5_WORKER_FACTORY\s*= var.worker_factory", source)
        assert "security_group_ids = [aws_security_group.worker.id]" in source
        assert "aws_lambda_function_url" not in source
    for name in ("gateway-ci", "gateway-deploy"):
        paths = workflow(name)["on"]["push"]["paths"]
        assert selected(paths, "modules/L5-services/src/l5_services/adapters/well_architected/generation.py")
        assert selected(paths, "modules/L5-services/src/l5_services/migrations/versions/l5_wa_usage.py")
    assert selected(workflow("gateway-frontend-deploy")["on"]["push"]["paths"],
                    "modules/gateway/frontend/src/app.tsx")


def test_selected_inputs_and_browser_policy_fail_before_publication_or_installation():
    release = workflow("l5-services-release")
    publish = release["jobs"]["publish"]["steps"]
    check = next(i for i, step in enumerate(publish) if "check-release-prerequisites.py" in step.get("run", ""))
    credentials = next(i for i, step in enumerate(publish) if step.get("uses") == "./.github/actions/trusted-deployment")
    assert check < credentials
    fixture = publish[check]["env"]["PRIVATE_FIXTURE_JSON"]
    assert fixture.endswith("|| '' }}")
    assert "(inputs.service == 'security' || inputs.service == '') && secrets.ADP_L5_SECURITY_LIVE_CONFIG_JSON" in fixture
    for name in ("publish", "qualify-security", "qualify-selected"):
        job = release["jobs"][name]
        assert job["env"]["L5_BROWSER_AUTOMATION"] == "${{ vars.ADP_L5_BROWSER_AUTOMATION || 'disabled' }}"
        steps = job["steps"]
        policy = next(i for i, step in enumerate(steps) if "check-browser-policy" in step.get("run", ""))
        for i, step in enumerate(steps):
            if "playwright install" in step.get("run", "") or "worker-lifecycle.py resume" in step.get("run", ""):
                assert policy < i
    selected = release["jobs"]["qualify-selected"]["steps"]
    selected_fixture = next(step["env"]["PRIVATE_FIXTURE_JSON"] for step in selected
                            if "PRIVATE_FIXTURE_JSON" in step.get("env", {}))
    assert selected_fixture.endswith("|| '' }}")
    assert "&& secrets.ADP_L5_WELL_ARCHITECTED_LIVE_CONFIG_JSON" in selected_fixture
