"""Offline checks for shared L5 build identity and gateway dependency isolation."""

import base64
import csv
import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[4]
MODULE = ROOT / "modules/L5-services"


def build_wheel(workspace):
    workspace.mkdir()
    artifacts = workspace / "artifacts"
    subprocess.run(["bash", str(MODULE / "scripts/build-package.sh"), str(artifacts)],
                   check=True, cwd=workspace)
    wheel, = artifacts.glob("adp_l5_services-*.whl")
    return wheel


def test_package_identity(tmp_path):
    wheel = build_wheel(tmp_path / "first")
    second = build_wheel(tmp_path / "second")
    artifacts = wheel.parent
    expected_digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert wheel.read_bytes() == second.read_bytes()
    assert (artifacts / "wheel.sha256").read_text().strip() == f"{expected_digest}  {wheel.name}"
    with zipfile.ZipFile(wheel) as archive:
        contents = set(archive.namelist())
        assert "l5_services/contracts/v1/openapi.json" in contents
        assert "l5_services/contracts/v1/schemas.json" in contents
        for schema in (ROOT / "docs/l5-services/contracts/v1").glob("*.json"):
            assert archive.read(f"l5_services/contracts/v1/{schema.name}") == schema.read_bytes()
        assert "l5_services/workers/execution.py" in contents
        assert "l5_services/workers/recovery.py" in contents
        assert "l5_services/migrations/versions/l5_worker_runtime.py" in contents
    subprocess.run(["bash", str(MODULE / "scripts/build-workers.sh"),
                    str(wheel), str(artifacts / "worker.zip")], check=True, cwd=tmp_path)
    subprocess.run(["bash", str(MODULE / "scripts/build-workers.sh"),
                    str(second), str(second.parent / "worker.zip")], check=True, cwd=tmp_path)
    # Distinct checkouts and install directories must publish the same archive
    # under one immutable source/wheel identity, including on a resumed run.
    assert (artifacts / "worker.zip").read_bytes() == (second.parent / "worker.zip").read_bytes()
    with zipfile.ZipFile(artifacts / "worker.zip") as archive:
        assert archive.read(f"release/{wheel.name}") == wheel.read_bytes()
        assert json.loads(archive.read("release/wheel.json")) == {
            "wheel_filename": wheel.name, "wheel_sha256": expected_digest,
        }
        assert "l5_services/workers/execution.py" in archive.namelist()
        assert "l5_services/workers/recovery.py" in archive.namelist()
        assert "boto3/__init__.py" in archive.namelist()
        assert all(item.date_time == (1980, 1, 1, 0, 0, 0) for item in archive.infolist())
        assert not any(name.endswith(("/direct_url.json", "/uv_cache.json", ".pyc"))
                       or name.startswith("bin/") or name == ".lock" for name in archive.namelist())
        for name in archive.namelist():
            if name.endswith(".dist-info/RECORD"):
                for filename, recorded_hash, size in csv.reader(io.StringIO(archive.read(name).decode())):
                    assert filename in archive.namelist()
                    if recorded_hash:
                        payload = archive.read(filename)
                        digest = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).decode().rstrip("=")
                        assert recorded_hash == "sha256=" + digest
                        assert int(size) == len(payload)


def test_worker_archive_contains_devops_handlers_and_provider_model(tmp_path):
    wheel = build_wheel(tmp_path / "devops-worker")
    archive_path = tmp_path / "devops-worker.zip"
    subprocess.run(["bash", str(MODULE / "scripts/build-workers.sh"), str(wheel),
                    str(archive_path)], check=True)
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        assert {
            "l5_services/workers/execution.py",
            "l5_services/workers/recovery.py",
            "l5_services/workers/devops_events.py",
            "l5_services/workers/devops_readback.py",
            "l5_services/adapters/devops/aws_access.py",
            "l5_services/adapters/devops/investigations.py",
            "l5_services/devops/results.py",
            "boto3/__init__.py", "botocore/__init__.py", "sqlalchemy/__init__.py",
            "asyncpg/__init__.py", "httpx/__init__.py",
        } <= names
        models = [name for name in names if name.startswith("botocore/data/devops-agent/")
                  and name.endswith("/service-2.json.gz")]
        assert len(models) == 1
        operations = json.loads(gzip.decompress(archive.read(models[0])))["operations"]
        assert {"GetAgentSpace", "GetBacklogTask", "CreateBacklogTask",
                "ListBacklogTasks", "UpdateBacklogTask", "ListExecutions", "ListJournalRecords",
                "ListRecommendations", "GetRecommendation"} <= set(operations)
        archive.extractall(tmp_path / "isolated-runtime")
    python = subprocess.run(["uv", "python", "find", "3.12"], check=True,
                            capture_output=True, text=True).stdout.strip()
    smoke = """import sys
sys.path.insert(0, sys.argv[1])
from l5_services.workers.execution import lambda_handler as execute
from l5_services.workers.devops_events import lambda_handler as events
from l5_services.workers.devops_readback import lambda_handler as readback
from l5_services.adapters.devops.aws_access import AWSAccessAdapter
from l5_services.adapters.devops.investigations import InvestigationAdapter
import boto3
assert all(handler.__module__.startswith('l5_services.') for handler in (execute, events, readback))
assert AWSAccessAdapter.__module__ == 'l5_services.adapters.devops.aws_access'
assert all(callable(getattr(InvestigationAdapter(), operation)) for operation in ('submit', 'reconcile_submit', 'observe', 'collect', 'cancel'))
assert 'devops-agent' in boto3.Session().get_available_services()
"""
    subprocess.run([python, "-I", "-c", smoke, str(tmp_path / "isolated-runtime")],
                   check=True, cwd=tmp_path)


def test_gateway_imports_without_provider_sdk(tmp_path):
    wheel = build_wheel(tmp_path / "gateway-context")
    site = tmp_path / "gateway"
    subprocess.run([sys.executable, "-m", "pip", "install", "--target", str(site), str(wheel)], check=True)
    code = """import sys
from l5_services.api.router import create_router
from l5_services.workers.execution import lambda_handler
assert not any(key.startswith(('boto3', 'botocore')) for key in sys.modules)
"""
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(site)
    subprocess.run([sys.executable, "-c", code], check=True, env=environment)


@pytest.fixture(scope="module")
def devops_plan(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("devops-plan")
    module_path = json.dumps(str(MODULE / "infra/modules/legacy-devops-events"))
    (tmp_path / "main.tf").write_text(f'''
terraform {{
  required_providers {{ aws = {{ source = "hashicorp/aws", version = "~> 6.0" }} }}
}}
provider "aws" {{
  region = "us-east-1"
  skip_credentials_validation = true
  skip_requesting_account_id = true
  skip_metadata_api_check = true
  skip_region_validation = true
}}
module "shared_devops" {{
  source = {module_path}
  name_prefix = "offline-shared"
  worker_function_arn = "arn:aws:lambda:us-east-1:111111111111:function:worker"
  repair_function_arn = "arn:aws:lambda:us-east-1:111111111111:function:repair"
  event_ingest_function_arn = "arn:aws:lambda:us-east-1:111111111111:function:ingest"
  readback_function_arn = "arn:aws:lambda:us-east-1:111111111111:function:readback"
  native_event_pattern = jsonencode({{ source = ["aws.example"] }})
  gateway_role_name = "gateway"
  worker_role_name = "worker"
  producer_role_names = ["repair", "ingest", "readback"]
  artifact_bucket_arn = "arn:aws:s3:::fictional-artifacts"
  artifact_kms_key_arn = "arn:aws:kms:us-east-1:111111111111:key/00000000-0000-0000-0000-000000000001"
  secret_reference_arns = ["arn:aws:secretsmanager:us-east-1:111111111111:secret:example"]
}}
''')
    environment = os.environ.copy()
    environment.update(AWS_ACCESS_KEY_ID="testing", AWS_SECRET_ACCESS_KEY="testing",
                       AWS_EC2_METADATA_DISABLED="true", TF_IN_AUTOMATION="true")
    subprocess.run(["terraform", "init", "-backend=false", "-input=false", "-no-color"],
                   check=True, cwd=tmp_path, env=environment, capture_output=True, timeout=180)
    subprocess.run(["terraform", "plan", "-refresh=false", "-input=false", "-no-color",
                    "-out=plan"], check=True, cwd=tmp_path, env=environment,
                   capture_output=True, timeout=180)
    result = subprocess.run(["terraform", "show", "-json", "plan"], check=True,
                            cwd=tmp_path, env=environment, capture_output=True)
    return json.loads(result.stdout)


def test_scoped_devops_events(devops_plan):
    plan = devops_plan
    changes = {change["address"].removeprefix("module.shared_devops."): change["change"]["after"]
               for change in plan["resource_changes"]}
    resources = {resource["address"]: resource["expressions"] for resource in
                 plan["configuration"]["root_module"]["module_calls"]["shared_devops"]["module"]["resources"]}
    assert changes["aws_lambda_event_source_mapping.devops_worker"]["enabled"] is False
    assert changes["aws_lambda_event_source_mapping.devops_worker"]["function_response_types"] == ["ReportBatchItemFailures"]
    assert all(changes[f"aws_cloudwatch_event_rule.{name}"]["state"] == "DISABLED"
               for name in ("repair", "readback", "native"))
    assert changes["aws_cloudwatch_event_rule.repair"]["schedule_expression"] == "rate(1 minute)"
    assert changes["aws_cloudwatch_event_rule.readback"]["schedule_expression"] == "rate(5 minutes)"
    assert changes["aws_cloudwatch_event_target.native"]["arn"].endswith(":function:ingest")
    assert changes["aws_cloudwatch_event_target.readback"]["arn"].endswith(":function:readback")
    assert changes["aws_sqs_queue.devops_work"]["name"] == "offline-shared-devops-work"
    assert "aws_sqs_queue.devops_dead_letters.arn" in resources["aws_sqs_queue.devops_work"]["redrive_policy"]["references"]
    for name in ("repair", "readback", "native"):
        permission = changes[f"aws_lambda_permission.{name}"]
        assert permission["principal"] == "events.amazonaws.com"
        assert f"aws_cloudwatch_event_rule.{name}.arn" in resources[f"aws_lambda_permission.{name}"]["source_arn"]["references"]
    for name in ("gateway_publish", "producer_publish", "worker"):
        assert "aws_sqs_queue.devops_work.arn" in resources[f"aws_iam_role_policy.{name}"]["policy"]["references"]
    worker_sources = resources["aws_iam_role_policy.worker"]["policy"]["references"]
    assert {"var.artifact_bucket_arn", "var.artifact_kms_key_arn", "var.secret_reference_arns"} <= set(worker_sources)


def test_no_public_sso_ingress(devops_plan):
    changes = devops_plan["resource_changes"]
    assert not any(change["type"] in {"aws_lambda_function_url", "aws_cloudfront_distribution",
                                       "aws_lambda_alias"} for change in changes)
    resources = {resource["address"]: resource["expressions"] for resource in
                 devops_plan["configuration"]["root_module"]["module_calls"]["shared_devops"]["module"]["resources"]}
    assert {name for name in resources if name.startswith("aws_lambda_permission.")} == {
        "aws_lambda_permission.repair", "aws_lambda_permission.readback", "aws_lambda_permission.native"}
    for name in ("repair", "readback", "native"):
        assert f"aws_cloudwatch_event_rule.{name}.arn" in resources[f"aws_lambda_permission.{name}"]["source_arn"]["references"]
    source = (MODULE / "infra/modules/legacy-devops-events/main.tf").read_text()
    assert 'resource "aws_lambda_function_url"' not in source
    assert '"lambda:InvokeFunctionUrl"' not in source
    assert 'Resource = "*"' not in source


def test_gateway_wheel_stage(tmp_path):
    staging = tmp_path / "gateway-context"
    subprocess.run(["bash", str(MODULE / "scripts/stage-gateway.sh"), str(staging)],
                   check=True, cwd=tmp_path)
    wheel, = staging.glob("adp_l5_services-*.whl")
    assert (staging / "wheel.sha256").read_text().split()[0] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    release = staging.parent / ".l5-release"
    assert (release / wheel.name).read_bytes() == wheel.read_bytes()
    assert (release / f"{wheel.name}.sha256").read_bytes() == (staging / "wheel.sha256").read_bytes()
    dockerfile = (ROOT / "modules/gateway/Dockerfile").read_text()
    publisher = (ROOT / "platform/scripts/publish-shared-image.sh").read_text()
    assert "COPY l5-wheel/ /opt/l5-wheel/" in dockerfile
    assert "sha256sum -c wheel.sha256" in dockerfile
    assert "pip install --no-deps --no-cache-dir adp_l5_services-*.whl" in dockerfile
    assert "bash modules/L5-services/scripts/stage-gateway.sh" in publisher


@pytest.fixture
def gateway_source_archive(tmp_path):
    source = tmp_path / "source"
    for relative in ("modules/L5-services/src", "modules/L5-services/scripts",
                     "modules/agent-factory/skills",
                     "libs/python/adp-common",
                     "docs/l5-services/contracts/v1"):
        shutil.copytree(ROOT / relative, source / relative)
    for relative in ("modules/L5-services/pyproject.toml", "modules/gateway/Dockerfile",
                     "codebuild/bs-gateway-smoke.yml",
                     "modules/gateway/scripts/build-skills-archive.sh",
                     "modules/gateway/scripts/stage-common-wheel.sh"):
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    contracts_stage = source / "modules/gateway/scripts/stage-contracts.sh"
    contracts_stage.parent.mkdir(exist_ok=True)
    contracts_stage.write_text("#!/usr/bin/env bash\nexit 0\n")
    contracts_stage.chmod(0o755)
    (source / "docs/unrelated.txt").write_text("Not a runtime build input")
    archive = tmp_path / "source.zip"
    subprocess.run(["bash", str(ROOT / "platform/scripts/zip-source.sh"),
                    str(source), str(archive)], check=True, capture_output=True)
    extracted = tmp_path / "extracted"
    subprocess.run(["unzip", "-q", str(archive), "-d", str(extracted)], check=True)
    return extracted


def test_gateway_source_archive_includes_l5_schemas(gateway_source_archive):
    schemas = Path("docs/l5-services/contracts/v1")
    for schema in (ROOT / schemas).glob("*.json"):
        assert (gateway_source_archive / schemas / schema.name).read_bytes() == schema.read_bytes()
    assert not (gateway_source_archive / "docs/unrelated.txt").exists()


def run_gateway_smoke_build(source, tmp_path):
    import yaml

    buildspec = yaml.safe_load((source / "codebuild/bs-gateway-smoke.yml").read_text())
    binaries = tmp_path / "bin"
    binaries.mkdir()
    docker = binaries / "docker"
    docker.write_text('''#!/usr/bin/env bash
set -euo pipefail
printf 'invoked\n' > "$SMOKE_DOCKER_CALL"
[[ "$*" == 'build --pull -t adp-gateway:pr-smoke .' ]]
cd l5-wheel
sha256sum -c wheel.sha256
''')
    docker.chmod(0o755)
    environment = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                   "SMOKE_DOCKER_CALL": str(tmp_path / "docker-called")}
    return subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + "\n".join(buildspec["phases"]["build"]["commands"])],
        cwd=source, env=environment, capture_output=True, text=True, timeout=180,
    )


def test_gateway_smoke_stages_wheel_from_source_archive(gateway_source_archive, tmp_path):
    result = run_gateway_smoke_build(gateway_source_archive, tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "docker-called").is_file()
    skills_manifest = Path("modules/agent-factory/skills/MANIFEST.json")
    staged_manifest = gateway_source_archive / "modules/gateway/skills-source/MANIFEST.json"
    assert staged_manifest.read_bytes() == (gateway_source_archive / skills_manifest).read_bytes()
    wheel, = (gateway_source_archive / "modules/gateway/l5-wheel").glob("*.whl")
    with zipfile.ZipFile(wheel) as archive:
        for schema in (ROOT / "docs/l5-services/contracts/v1").glob("*.json"):
            assert archive.read(f"l5_services/contracts/v1/{schema.name}") == schema.read_bytes()


def test_gateway_smoke_refuses_missing_l5_schemas(gateway_source_archive, tmp_path):
    for schema in (gateway_source_archive / "docs/l5-services/contracts/v1").glob("*.json"):
        schema.unlink()
    result = run_gateway_smoke_build(gateway_source_archive, tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "docker-called").exists()


def test_exact_case_host_filters():
    import fnmatch
    import re
    import yaml

    deploy = (ROOT / ".github/workflows/gateway-deploy.yml").read_text()
    gateway_ci = (ROOT / ".github/workflows/gateway-ci.yml").read_text()
    l5_ci = (ROOT / ".github/workflows/l5-services-ci.yml").read_text()
    frontend = (ROOT / ".github/workflows/gateway-frontend-deploy.yml").read_text()

    def selected(text, event, filename):
        trigger = yaml.load(text, Loader=yaml.BaseLoader)["on"][event] or {}
        patterns = trigger.get("paths")
        if patterns is None:
            return not any(fnmatch.fnmatchcase(filename, pattern)
                           for pattern in trigger.get("paths-ignore", []))
        matched = False
        for pattern in patterns:
            if fnmatch.fnmatchcase(filename, pattern.removeprefix("!")):
                matched = not pattern.startswith("!")
        return matched

    match = re.search(r"if echo \"\$CHANGED\" \| grep -qE '([^']+)'[;] then\s+BACKEND=true", deploy)
    assert match is not None
    backend = re.compile(match.group(1))
    for filename in ("modules/L5-services/src/l5_services/api/router.py",
                     "modules/L5-services/pyproject.toml",
                     "modules/L5-services/scripts/build-package.sh",
                     "docs/l5-services/contracts/v1/schemas.json"):
        assert selected(deploy, "push", filename) and backend.search(filename)
        assert selected(gateway_ci, "pull_request", filename)
        assert selected(gateway_ci, "push", filename)
        assert selected(l5_ci, "pull_request", filename)
    for filename in ("modules/l5-services/src/l5_services/api/router.py", "docs/l5-services/README.md"):
        assert not selected(deploy, "push", filename)
        assert not backend.search(filename)
    worker_only = "modules/L5-services/scripts/build-workers.sh"
    assert selected(l5_ci, "pull_request", worker_only)
    assert not selected(deploy, "push", worker_only) and not backend.search(worker_only)
    for filename in ("platform/scripts/zip-source.sh", "codebuild/bs-gateway-build.yml",
                     "codebuild/bs-gateway-smoke.yml"):
        assert selected(l5_ci, "pull_request", filename)
    for filename in (".github/workflows/l5-services-release.yml",
                     "modules/gateway/src/orchestration/operations_repair.py"):
        assert selected(l5_ci, "pull_request", filename)
    assert selected(frontend, "push", "modules/gateway/frontend/src/App.tsx")
    assert not selected(frontend, "push", "modules/L5-services/src/l5_services/api/router.py")
    assert selected(deploy, "push", "modules/gateway/src/api/router.py")
    assert backend.search("modules/gateway/src/api/router.py")


def invoke_gateway_filter(workspace, filename, script):
    # Preserve distinct casing on case-insensitive development filesystems.
    import hashlib

    repo = workspace / hashlib.sha256(filename.encode()).hexdigest()
    repo.mkdir()

    def git(*arguments):
        subprocess.run(["git", *arguments], cwd=repo, check=True, capture_output=True)

    git("init", "-q")
    git("config", "user.name", "Offline Fixture")
    git("config", "user.email", "fixture@example.invalid")
    git("commit", "--allow-empty", "-qm", "base")
    changed = repo / filename
    changed.parent.mkdir(parents=True)
    changed.write_text("changed\n")
    git("add", filename)
    git("commit", "-qm", "change")
    output = repo / "gateway-output"
    environment = os.environ.copy()
    environment["GITHUB_OUTPUT"] = str(output)
    subprocess.run(["bash", "-e", "-c", script], cwd=repo, env=environment,
                   check=True, capture_output=True)
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


def test_release_wiring(tmp_path, devops_plan):
    import yaml

    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-deploy.yml").read_text())
    steps = workflow["jobs"]["changes"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "filter")
    assert script.count("${{ github.event_name }}") == 1
    script = script.replace("${{ github.event_name }}", "push")

    for filename in ("modules/L5-services/src/l5_services/api/router.py",
                     "docs/l5-services/contracts/v1/schemas.json",
                     "modules/gateway/src/api/router.py"):
        assert invoke_gateway_filter(tmp_path, filename, script)["backend"] == "true"
    for filename in ("modules/l5-services/src/l5_services/api/router.py",
                     "modules/L5-services/scripts/build-workers.sh",
                     "modules/gateway/frontend/src/App.tsx"):
        assert invoke_gateway_filter(tmp_path, filename, script)["backend"] == "false"

    planned = devops_plan["resource_changes"]
    assert {change["change"]["after"]["role"] for change in planned
            if change["address"].startswith("module.shared_devops.aws_iam_role_policy.producer_publish[")} == {
                "ingest", "repair", "readback"}
    event_rules = {"native", "repair", "readback"}
    permissions = {change["address"].rsplit(".", 1)[-1]: change["change"]["after"]
                   for change in planned if change["type"] == "aws_lambda_permission"}
    assert set(permissions) == event_rules
    assert all(permission["principal"] == "events.amazonaws.com" and
               permission["action"] == "lambda:InvokeFunction" and
               permission.get("function_url_auth_type") is None
               for permission in permissions.values())
    assert not any(change["type"] in {"aws_lambda_function_url", "aws_cloudfront_origin_access_control"}
                   for change in planned)
