"""Release receipts are tied to the selected source and observed ECR image."""

import importlib.util
import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = ROOT / ".github/workflows/tools-facade-deploy.yml"
SPEC = importlib.util.spec_from_file_location("facade_receipt", ROOT / "modules/tools/scripts/facade_receipt.py")
RECEIPT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(RECEIPT)


def environment():
    digest = "sha256:" + "a" * 64
    return {
        "ADP_SOURCE_REVISION": "b" * 40,
        "ADP_WORKFLOW_REVISION": "c" * 40,
        "ADP_ACCOUNT_ID": "123456789012",
        "AWS_REGION": "us-east-1",
        "ADP_ENVIRONMENT": "dev",
        "ADP_TOOLS_EKS_CLUSTER_NAME": "adp-dev-eks-cluster",
        "ADP_TOOLS_NAMESPACE": "adp-gateway",
        "DEPLOYED_IMAGE": f"123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tools-facade@{digest}",
        "OBSERVED_ECR_DIGEST": digest,
        "GITHUB_REPOSITORY_ID": "10",
        "GITHUB_RUN_ID": "20",
        "GITHUB_RUN_ATTEMPT": "1",
    }


def test_verified_digest_produces_engine_release_and_context(tmp_path, monkeypatch):
    values = environment()
    context, release = RECEIPT.evidence(values)
    assert release["image_digest"] == values["OBSERVED_ECR_DIGEST"]
    assert release["source_revision"] == context["source_revision"]
    assert release["resource_id"] == context["resource_id"]
    assert "image" not in release
    assert release["assets"] == {}
    assert release["produced_at"].endswith("+00:00")
    monkeypatch.setattr(RECEIPT.os, "environ", values | {"RUNNER_TEMP": str(tmp_path)})
    RECEIPT.main()
    assert (tmp_path / "adp-release-tools-facade/release.json").is_file()
    assert (tmp_path / "adp-deployment-context/deployment-context.json").is_file()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("OBSERVED_ECR_DIGEST", "sha256:" + "0" * 64),
        ("DEPLOYED_IMAGE", "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-tools-facade:latest"),
        ("DEPLOYED_IMAGE", "123456789012.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "a" * 64),
        ("ADP_SOURCE_REVISION", "main"),
        ("GITHUB_RUN_ATTEMPT", "0"),
    ],
)
def test_unverifiable_release_is_rejected(key, value):
    with pytest.raises(ValueError):
        RECEIPT.evidence(environment() | {key: value})


def test_workflow_routes_push_to_dev_and_dispatch_to_protected_environment():
    source = WORKFLOW.read_text()
    assert "branches: [main]" in source
    assert "- 'modules/tools/facade/**'" in source
    assert "- 'modules/tools/adp_tools/**'" in source
    assert "- 'modules/tools/pyproject.toml'" in source
    assert "-e modules/tools -e 'modules/tools/facade[dev]'" in source
    assert "environment: adp-tools-deploy-${{ inputs.environment || 'dev' }}" in source
    assert "git merge-base --is-ancestor" in source
    assert "git show \"$ADP_WORKFLOW_REVISION:.github/workflows/tools-facade-deploy.yml\"" in source
    assert "bash modules/tools/deploy.sh --facade" in source
    assert "aws ecr describe-images --repository-name adp-tools-facade" in source
    assert "ADP_DEPLOYMENT_EVIDENCE_STORE: s3-oidc-v1" in source
    assert "upload-artifact" not in source
    assert 'publish-deployment-evidence.py" context tools-facade-deploy.yml' in source
    assert 'publish-deployment-evidence.py" release tools-facade' in source
    assert "imageDetails[0].imageTags" in source
    assert "kubectl rollout status deployment/tools-facade" in source
    assert "docker build" not in source
    assert "docker push" not in source
    assert "kubectl set image" not in source
    assert "terraform apply" not in source
    assert "python -m pytest -q tests/unit/test_tools_facade_deploy.py" in source


@pytest.mark.parametrize(
    ("digest", "event", "accepted"),
    [
        ("", "push", True),
        ("sha256:" + "a" * 64, "workflow_dispatch", True),
        ("sha256:broken", "workflow_dispatch", False),
        ("sha256:" + "a" * 64, "push", False),
        ("sha256:" + "a" * 64 + "$(exit 0)", "workflow_dispatch", False),
    ],
)
def test_rollback_request_is_validated_before_protected_job(digest, event, accepted):
    source = WORKFLOW.read_text()
    section = source.split("      - name: Validate rollback request before protected deployment\n", 1)[1]
    shell = textwrap.dedent(section.split("        run: |\n", 1)[1].split("      - uses: actions/checkout@v4", 1)[0])
    result = subprocess.run(
        ["bash", "-c", shell],
        env=os.environ | {"ROLLBACK_DIGEST": digest, "GITHUB_EVENT_NAME": event},
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is accepted


def test_rollback_routes_to_shared_stage_and_skips_build_tests():
    source = WORKFLOW.read_text()
    assert "rollback_to_digest:" in source
    assert source.count("if: inputs.rollback_to_digest == ''") == 4
    assert "ADP_TOOLS_FACADE_ROLLBACK_IMAGE=" in source
    assert '[[ "$current_image" != "$ADP_TOOLS_FACADE_ROLLBACK_IMAGE" ]]' in source
    assert "bash modules/tools/deploy.sh --facade" in source
    assert 'kubectl rollout status deployment/tools-facade -n "$ADP_TOOLS_NAMESPACE" --timeout=110s' in source
    assert '[[ "$deployed_image" == "$ADP_TOOLS_FACADE_ROLLBACK_IMAGE" ]]' in source
    assert "kubectl set image" not in source


def test_rollback_receipt_names_previous_digest_without_mislabeling_source():
    values = environment()
    values["ADP_TOOLS_FACADE_ROLLBACK_DIGEST"] = values["OBSERVED_ECR_DIGEST"]
    context, release = RECEIPT.evidence(values)
    assert context["inputs"]["rollback_to_digest"] == release["image_digest"]
    with pytest.raises(ValueError, match="requested digest"):
        RECEIPT.evidence(values | {"ADP_TOOLS_FACADE_ROLLBACK_DIGEST": "sha256:" + "0" * 64})

def test_facade_release_requires_bootstrapped_named_objects_before_apply():
    script = (ROOT / "modules/tools/facade/deploy.sh").read_text()
    guard = script.index('kubectl get "$object" -n "$namespace" -o name')
    apply = script.index('kubectl apply -n "$namespace"')
    assert guard < apply
    for named in (
        "deployment/tools-facade",
        "service/tools-facade",
        "serviceaccount/tools-facade",
        "serviceaccount/tools-facade-reconciler",
        "cronjob/tools-facade-reconciler",
    ):
        assert named in script[script.rfind("for object in", 0, guard):guard]


def provider_configuration():
    return {
        "FunctionArn": "arn:aws:lambda:us-east-1:123456789012:function:adp-dev-shared-tools-shared",
        "Role": "arn:aws:iam::123456789012:role/adp-dev-shared-tools-shared-lambda",
        "State": "Active",
        "LastUpdateStatus": "Successful",
        "Environment": {"Variables": {
            "ADP_TOOLS_RECONCILER_KEY_ARN": "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000003",
            "ADP_TOOLS_RECONCILER_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-dev-role-tools-reconciler",
        }},
    }


@pytest.fixture
def facade_release_harness(tmp_path):
    command_dir = tmp_path / "bin"
    command_dir.mkdir()
    command_log = tmp_path / "commands.log"
    source_sha = "b" * 40
    image_digest = "sha256:" + "a" * 64
    executables = {
        "git": '#!/usr/bin/env bash\n[[ "$*" == *"rev-parse HEAD"* ]] && printf "%s\\n" "$FAKE_SOURCE_SHA"\n',
        "aws": '''#!/usr/bin/env bash
printf 'aws %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
case "$1 $2" in
  'eks update-kubeconfig') exit 0 ;;
  'ecr describe-images') printf '%s\\n' "$FAKE_IMAGE_DIGEST" ;;
  'ssm get-parameter')
    case "$*" in
      */tools/gateway-url*) echo https://example.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp ;;
      */tools/assertion-signing-key-arn*) echo arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000001 ;;
      */tools/execution-table-name*) echo adp-tools-dev-executions ;;
      */tools/caller-role-arn*) echo arn:aws:iam::123456789012:role/adp-dev-role-tools-facade ;;
      */tools/reconciler-signing-key-arn*)
        [[ "${FAKE_MISSING_CLEANUP_KEY:-}" != 1 ]] || exit 2
        echo arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000003 ;;
      */tools/shared-tools-lambda-arn*) exit 254 ;;
      */gateway/apigw-invoke-url*) echo https://abc123.execute-api.us-east-1.amazonaws.com/dev ;;
      *) exit 2 ;;
    esac ;;
  'lambda get-function-configuration')
    if [[ "${FAKE_PROVIDER_UNREADY:-}" == 1 ]]; then
      echo '{"State":"Pending"}'
    elif [[ -f "$FAKE_PROVIDER_READ" && -n "${FAKE_PROVIDER_CONFIGURATION_AFTER_BUILD:-}" ]]; then
      printf '%s\\n' "$FAKE_PROVIDER_CONFIGURATION_AFTER_BUILD"
    else
      touch "$FAKE_PROVIDER_READ"
      printf '%s\\n' "$FAKE_PROVIDER_CONFIGURATION"
    fi ;;
  *) exit 2 ;;
esac
''',
        "kubectl": '''#!/usr/bin/env bash
printf 'kubectl %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
if [[ "$1" == get ]]; then
  [[ "$2" != "${FAKE_MISSING_K8S:-}" ]] || exit 1
  printf '%s\\n' "$2"
elif [[ "$1" == apply ]]; then
  cp -R "$5" "$FAKE_RENDERED_MANIFESTS"
fi
''',
    }
    for name, body in executables.items():
        executable = command_dir / name
        executable.write_text(body)
        executable.chmod(0o755)
    environment = os.environ | {
        "PATH": f"{command_dir}:{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path),
        "FAKE_COMMAND_LOG": str(command_log),
        "FAKE_SOURCE_SHA": source_sha,
        "FAKE_IMAGE_DIGEST": image_digest,
        "FAKE_RENDERED_MANIFESTS": str(tmp_path / "rendered"),
        "FAKE_PROVIDER_CONFIGURATION": json.dumps(provider_configuration()),
        "FAKE_PROVIDER_READ": str(tmp_path / "provider-read"),
        "ADP_SOURCE_REVISION": source_sha,
        "ADP_TOOLS_ENVIRONMENT": "dev",
        "ADP_TOOLS_AWS_REGION": "us-east-1",
        "ADP_TOOLS_ACCOUNT_ID": "123456789012",
        "ADP_TOOLS_EKS_CLUSTER_NAME": "adp-dev-eks-cluster",
        "ADP_TOOLS_NAMESPACE": "adp-gateway",
        "ADP_TOOLS_STATE_BUCKET": "example-state",
    }

    def invoke(**overrides):
        result = subprocess.run(
            ["bash", str(ROOT / "modules/tools/facade/deploy.sh")],
            cwd=ROOT, env=environment | overrides, capture_output=True, text=True,
            check=False,
        )
        return result, command_log.read_text() if command_log.exists() else ""

    return invoke


def test_precreated_facade_objects_are_checked_before_build(facade_release_harness):
    result, commands = facade_release_harness(FAKE_MISSING_K8S="service/tools-facade")
    assert result.returncode == 2
    assert "Bootstrap must pre-create service/tools-facade" in result.stderr
    assert "aws eks update-kubeconfig" in commands
    assert "aws ecr" not in commands
    assert "aws s3" not in commands
    assert "aws codebuild" not in commands
    assert "kubectl apply" not in commands


def test_wrong_namespace_is_rejected_before_cloud_calls(facade_release_harness):
    result, commands = facade_release_harness(ADP_TOOLS_NAMESPACE="other-workloads")
    assert result.returncode == 2
    assert commands == ""


def test_existing_facade_rolls_to_immutable_digest(facade_release_harness):
    result, commands = facade_release_harness()
    assert result.returncode == 0, result.stderr
    assert commands.count("kubectl get ") == 5
    assert commands.index("kubectl get deployment/tools-facade") < commands.index("aws ecr describe-images")
    assert commands.count("kubectl apply ") == 1
    assert "kubectl rollout status deployment/tools-facade" in commands
    assert "aws s3" not in commands
    assert "aws codebuild" not in commands
    assert "@sha256:" + "a" * 64 in result.stdout


@pytest.mark.parametrize("missing", ["FAKE_MISSING_CLEANUP_KEY", "FAKE_PROVIDER_UNREADY"])
def test_cleanup_prerequisites_refuse_facade_release_before_apply(facade_release_harness, missing):
    result, commands = facade_release_harness(**{missing: "1"})
    assert result.returncode != 0
    assert "kubectl apply" not in commands
    assert "aws ecr" not in commands
    assert "aws s3" not in commands
    assert "aws codebuild" not in commands


def test_facade_first_release_verifies_provider_without_later_targets_ssm_output(facade_release_harness, tmp_path):
    result, commands = facade_release_harness()
    assert result.returncode == 0, result.stderr
    assert "/tools/shared-tools-lambda-arn" not in commands
    assert "aws lambda get-function-configuration --function-name " + provider_configuration()["FunctionArn"] in commands
    assert commands.index("aws lambda get-function-configuration") < commands.index("aws ecr describe-images")
    assert commands.count("aws lambda get-function-configuration") == 2
    assert commands.index("aws ecr describe-images") < commands.rindex("aws lambda get-function-configuration") < commands.index("kubectl apply")
    job = yaml.safe_load((tmp_path / "rendered/reconciler-cronjob.yaml").read_text())
    values = {item["name"]: item["value"] for item in job["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert values["ADP_TOOLS_CLEANUP_LAMBDA_ARN"] == "arn:aws:lambda:us-east-1:123456789012:function:adp-dev-shared-tools-shared"
    assert values["ADP_TOOLS_ACCOUNT_ID"] == "123456789012"
    # Kubernetes EnvVar.value is a string, including values that look numeric.
    # Validate the rendered documents passed by the real shell stage, since a
    # successful deployment update must not mask a later CronJob type failure.
    for manifest in (tmp_path / "rendered").glob("*.yaml"):
        document = yaml.safe_load(manifest.read_text())
        if document["kind"] == "Deployment":
            pod = document["spec"]["template"]["spec"]
        elif document["kind"] == "CronJob":
            pod = document["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        else:
            continue
        for container in pod["containers"]:
            for entry in container.get("env", []):
                if "value" in entry:
                    assert isinstance(entry["value"], str), (manifest.name, entry["name"])


def test_provider_change_during_image_work_refuses_apply(facade_release_harness):
    configuration = provider_configuration() | {"LastUpdateStatus": "InProgress"}
    result, commands = facade_release_harness(FAKE_PROVIDER_CONFIGURATION_AFTER_BUILD=json.dumps(configuration))
    assert result.returncode != 0
    assert "Cleanup provider release is not ready" in result.stderr
    assert "aws ecr describe-images" in commands
    assert commands.count("aws lambda get-function-configuration") == 2
    assert "kubectl apply" not in commands


@pytest.mark.parametrize("path,value", [
    (("FunctionArn",), "arn:aws:lambda:us-east-1:123456789012:function:other"),
    (("FunctionArn",), "arn:aws:lambda:us-east-1:000000000000:function:adp-dev-shared-tools-shared"),
    (("Role",), "arn:aws:iam::123456789012:role/other"),
    (("State",), "Pending"),
    (("LastUpdateStatus",), "Failed"),
    (("Environment", "Variables", "ADP_TOOLS_RECONCILER_KEY_ARN"), "arn:aws:kms:us-east-1:123456789012:key/00000000-0000-0000-0000-000000000004"),
    (("Environment", "Variables", "ADP_TOOLS_RECONCILER_ROLE_ARN"), "arn:aws:iam::123456789012:role/adp-dev-role-tools-facade"),
    (("Environment", "Variables", "ADP_TOOLS_RECONCILER_ROLE_ARN"), None),
])
def test_unverified_provider_never_builds_or_applies(facade_release_harness, path, value):
    configuration = provider_configuration()
    cursor = configuration
    for key in path[:-1]:
        cursor = cursor[key]
    cursor[path[-1]] = value
    result, commands = facade_release_harness(FAKE_PROVIDER_CONFIGURATION=json.dumps(configuration))
    assert result.returncode != 0
    assert "Cleanup provider release is not ready" in result.stderr
    assert "kubectl apply" not in commands
    assert "aws ecr" not in commands
    assert "aws s3" not in commands
    assert "aws codebuild" not in commands


@pytest.mark.parametrize("environment", [None, {}, {"Variables": None}, {
    "Error": {"ErrorCode": "KMSAccessDeniedException", "Message": "private-error-sentinel"},
}])
def test_unreadable_provider_environment_is_reported_without_details(facade_release_harness, environment):
    configuration = provider_configuration() | {"Environment": environment}
    result, commands = facade_release_harness(FAKE_PROVIDER_CONFIGURATION=json.dumps(configuration))
    assert result.returncode != 0
    assert "environment cannot be read" in result.stderr
    assert "KMS decrypt authorization" in result.stderr
    assert "private-error-sentinel" not in result.stdout + result.stderr
    assert "kubectl apply" not in commands
    assert "aws ecr" not in commands
    assert "aws s3" not in commands
    assert "aws codebuild" not in commands


def test_deployment_context_only_publishes_after_digest_readback():
    workflow = yaml.safe_load(WORKFLOW.read_text())
    steps = workflow["jobs"]["deploy"]["steps"]
    names = [step.get("name") for step in steps]
    confirmation = names.index("Confirm rolled image and existing ECR digest")
    assert confirmation < names.index("Publish deployment context")
    assert confirmation < names.index("Publish tools façade release")
    assert "OBSERVED_ECR_DIGEST=\"$observed_digest\"" in steps[confirmation]["run"]
    for name, kind in (("Publish deployment context", "context tools-facade-deploy.yml"), ("Publish tools façade release", "release tools-facade")):
        step = steps[names.index(name)]
        assert "uses" not in step, "deployment evidence is published to S3, never to GitHub artifacts"
        assert f'publish-deployment-evidence.py" {kind} ' in step["run"]
