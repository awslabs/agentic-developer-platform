"""Offline chat rollout contract: no cluster or validation service is required."""

import hashlib
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml
from fastapi import FastAPI

from src.agentauth import chat_data_routes
from src.agentauth.chat_tls import HOST, SandboxTransport
from src.orchestration.deployment_manifest import WorkflowRef
from src.orchestration.deployment_runtime_contract import CHAT_MANIFEST_KEYS, ReleaseArtifact
from src.orchestration.deployment_workflow_provider import WorkflowContext
from tests.agentauth.test_chat_tls_handshake import certificate_fixture
from tests.orchestration import test_deployment_workflow_provider as provider_tests

provider = provider_tests.provider

ROOT = Path(__file__).resolve().parents[4]
HELPER = ROOT / "modules/agent-factory/agent/k8s/chat-release.py"
SCRIPT = ROOT / "modules/agent-factory/agent/k8s/deploy-chat-scaledjob.sh"
spec = importlib.util.spec_from_file_location("chat_release", HELPER)
release_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_module)
SOURCE = "c" * 40
DEFINITION = "d" * 40
ACCOUNT = "123456789012"
REGION = "us-east-1"
SUPERVISOR = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-chat-agent@sha256:" + "a" * 64
SANDBOX = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-chat-agent@sha256:" + "b" * 64
GATEWAY = "https://gateway.example.test"


@pytest.fixture
def context_env():
    return {
        "ADP_RELEASE_SOURCE": SOURCE,
        "ADP_RELEASE_DEFINITION": DEFINITION,
        "ACCOUNT_ID": ACCOUNT,
        "AWS_REGION": REGION,
        "EKS_CLUSTER": "fixture-cluster",
        "GITHUB_REPOSITORY_ID": "17",
        "GITHUB_RUN_ID": "42",
        "GITHUB_RUN_ATTEMPT": "1",
        "SUPERVISOR_IMAGE": SUPERVISOR,
        "SANDBOX_IMAGE": SANDBOX,
        "ADP_CORRELATION": "e" * 64,
        "GATEWAY_HTTPS_ORIGIN": GATEWAY,
    }


@pytest.fixture
def published_gateway_ca():
    tls_spec = importlib.util.spec_from_file_location("render_chat_tls", ROOT / "platform/scripts/render-chat-tls.py")
    renderer = importlib.util.module_from_spec(tls_spec)
    tls_spec.loader.exec_module(renderer)
    deployment = {
        "kind": "Deployment",
        "metadata": {"name": "bedrockgateway", "namespace": "adp-gateway"},
        "spec": {
            "template": {
                "spec": {
                    "serviceAccountName": "gateway-service",
                    "containers": [{"name": "bedrockgateway", "image": f"registry.example.test/gateway@sha256:{'f' * 64}"}],
                }
            }
        },
    }
    rendered = renderer.render(deployment, *certificate_fixture())
    return next(item for item in rendered["items"] if item["kind"] == "ConfigMap")


def fake_read(command):
    if command[:3] == ["aws", "sts", "get-caller-identity"]:
        return {"Account": ACCOUNT}
    if command[:3] == ["aws", "eks", "describe-cluster"]:
        return {"cluster": {"name": "fixture-cluster", "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/fixture-cluster"}}
    assert command[:2] == ["kubectl", "get"]
    kind, name = command[2:4]
    if kind == "configmap":
        return {"metadata": {"name": name}, "data": {"pod.json": json.dumps({"spec": {"containers": [{"image": SANDBOX}]}})}}
    return {"metadata": {"name": name}, "spec": {"image": SUPERVISOR, "kind": kind}}


def test_release_and_context_are_provider_compatible_and_hash_all_live_objects(context_env):
    context = WorkflowContext.model_validate(release_module.context(context_env, fake_read))
    assert context.correlation == "e" * 64
    assert context.inputs["legacy_dry_run"] == "True"
    assert context.inputs["crew_controls_only"] == "False"
    assert context.resource_id == "fixture-cluster/adp-gateway-agents"
    artifact = ReleaseArtifact.model_validate(release_module.release(context_env, fake_read, "agent-worker"))
    assert artifact.image_digest == SANDBOX.rsplit("@", 1)[1]
    assert artifact.supervisor_image_digest == SUPERVISOR.rsplit("@", 1)[1]
    assert set(artifact.assets) == CHAT_MANIFEST_KEYS
    assert artifact.manifests_sha256 == release_module.sha(artifact.assets)
    assert ReleaseArtifact.model_validate(release_module.release(context_env, fake_read, "chat-worker")).component == "chat-worker"


@pytest.mark.parametrize("event,controls_only", [("push", False), ("workflow_dispatch", False), ("workflow_dispatch", True)])
async def test_generated_chat_context_is_accepted_by_workflow_observation(provider, context_env, monkeypatch, event, controls_only):
    path = ".github/workflows/chat-agent-deploy.yml"
    workflow_yaml = (ROOT / path).read_bytes()
    monkeypatch.setattr(provider_tests, "YAML", workflow_yaml)
    steps = yaml.safe_load(workflow_yaml)["jobs"]["build-and-deploy"]["steps"]
    context_step = next(step for step in steps if step.get("name") == "Record engine deployment context")
    assert context_step["env"]["INPUT_CREW_CONTROLS_ONLY"] == "${{ inputs.crew_controls_only && 'True' || 'False' }}"
    connection = {"customer_account_id": ACCOUNT, "customer_user_id": "fixture-user", "customer_aws_label": "fixture-connection"}
    for name in connection:
        assert context_step["env"]["INPUT_" + name.upper()] == "${{ inputs." + name + " || '' }}"
    inputs = {"crew_controls_only": str(controls_only), **(connection if event == "workflow_dispatch" else {})}
    correlation = "e" * 64 if event == "workflow_dispatch" else ""
    source = provider_tests.SOURCE
    provider.workflow = WorkflowRef(path, provider_tests.APPROVED, {name: frozenset({value}) for name, value in inputs.items()}, "adp_correlation")
    definition = await provider.provider.definition(provider.binding, provider.workflow, source)
    env = {
        **context_env,
        "ADP_RELEASE_SOURCE": source,
        "ADP_RELEASE_DEFINITION": source,
        "ADP_CORRELATION": correlation,
        **{"INPUT_" + name.upper(): value for name, value in inputs.items()},
    }
    provider.document = release_module.context(env, fake_read)
    provider.target.resource_id = "fixture-cluster/adp-gateway-agents"
    provider.run.update(path=path, event=event, display_title="ADP deployment " + correlation)
    # The workflow publishes its context to the signed S3 store (#7624); the
    # observer must read it from there, never from a GitHub artifact.
    assert definition.evidence_transport == "s3-oidc-v1"
    reference = "s3://adp-dev-deployment-evidence-123456789012/deployment-evidence/v1/17/42/1/context/chat-agent-deploy.yml.json?versionId=one"
    reads = []

    def read_evidence(**expected):
        reads.append(expected)
        payload = json.dumps(provider.document).encode()
        return payload, hashlib.sha256(payload).hexdigest(), reference

    provider.provider.evidence_store = SimpleNamespace(read=read_evidence)
    result, incomplete = await provider.provider.observe(
        provider.binding,
        workflow=provider.workflow,
        definition=definition,
        target=provider.target,
        source_revision=source,
        inputs=inputs,
        correlation=correlation or None,
    )
    assert not incomplete
    assert result.run_id == 42 and result.conclusion == "success"
    assert result.context.inputs["crew_controls_only"] == str(controls_only)
    assert result.artifact_id is None and result.artifact_ref == reference
    assert reads[0]["name"] == "chat-agent-deploy.yml" and reads[0]["kind"] == "context"


def test_chat_selected_connection_inputs_reach_deployment_configuration():
    document = yaml.safe_load((ROOT / ".github/workflows/chat-agent-deploy.yml").read_text())
    inputs = document[True]["workflow_dispatch"]["inputs"]
    steps = document["jobs"]["build-and-deploy"]["steps"]
    config = next(step for step in steps if step.get("uses") == "./.github/actions/load-deploy-config")
    for name in ("customer_account_id", "customer_user_id", "customer_aws_label"):
        assert inputs[name]["default"] == ""
        assert name not in config["with"]  # The legacy bootstrap loader stays disabled.
    assert config["with"]["account_id"] == "${{ env.ADP_ACCOUNT_ID }}"
    connected = next(step for step in steps if "adp_cred/deployment.py configure" in step.get("run", ""))
    platform = next(step for step in steps if step.get("uses") == "./.github/actions/trusted-deployment")
    assert connected["if"] == "${{ inputs.adp_correlation != '' }}"
    assert platform["if"] == "${{ inputs.adp_correlation == '' }}"
    assert config["with"]["environment"] == "dev"
    assert document["jobs"]["build-and-deploy"]["environment"] == "adp-chat-deploy-dev"


@pytest.mark.parametrize("change", ["wrong_account", "wrong_cluster", "unreviewed_image", "identical_images"])
def test_release_refuses_mismatched_identity_or_image(context_env, change):
    env = context_env.copy()
    reader = fake_read
    if change == "wrong_account":
        env["ACCOUNT_ID"] = "000000000000"
    elif change == "wrong_cluster":
        env["EKS_CLUSTER"] = "other-cluster"
    elif change == "unreviewed_image":
        env["SANDBOX_IMAGE"] = "adp-chat-agent:latest"
    else:
        env["SANDBOX_IMAGE"] = env["SUPERVISOR_IMAGE"]
    with pytest.raises(ValueError):
        release_module.release(env, reader, "agent-worker")


def test_workflow_publishes_context_and_both_release_artifacts_after_rollout():
    document = yaml.safe_load((ROOT / ".github/workflows/chat-agent-deploy.yml").read_text())
    steps = document["jobs"]["build-and-deploy"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Guard engine delivery revisions") < names.index("Record engine deployment context") < names.index("Build image via CodeBuild")
    assert (
        names.index("Update chat ScaledJob manifest")
        < names.index("Assert live chat rollout matches SSM and the build")
        < names.index("Record verified release manifests")
        < names.index("Publish engine chat-worker release")
    )
    # The drift step (#8058/#8059) judges the LIVE objects against SSM and the build outputs,
    # independently of what the deploy script resolved, and only when the rollout ran.
    drift = next(step for step in steps if step.get("name") == "Assert live chat rollout matches SSM and the build")
    rollout_step = next(step for step in steps if step.get("name") == "Update chat ScaledJob manifest")
    assert drift["if"] == rollout_step["if"]
    assert drift["env"] == {
        "SUPERVISOR_IMAGE": "${{ steps.build_info.outputs.supervisor_image }}",
        "SANDBOX_IMAGE": "${{ steps.build_info.outputs.sandbox_image }}",
    }
    assert drift["run"].strip() == "bash modules/agent-factory/agent/k8s/verify-chat-rollout.sh"
    # The rollout step passes no Crew value; the script resolves them from SSM (#8058).
    assert "ADP_CREW_" not in json.dumps(rollout_step.get("env", {}))
    assert not any(line.strip().startswith(("ADP_CREW_", "export ADP_CREW_")) for line in rollout_step["run"].splitlines())
    # Deployment evidence is published to the signed S3 store, never to GitHub artifacts (#7624).
    assert not any(step.get("uses", "").startswith("actions/upload-artifact@") for step in steps)
    assert document["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"
    publishes = {step["name"]: step["run"] for step in steps if "publish-deployment-evidence.py" in step.get("run", "")}
    context_target = '"$RUNNER_TEMP/adp-deployment-context/deployment-context.json"'
    assert publishes["Publish engine deployment context"].endswith(f"context chat-agent-deploy.yml {context_target}")
    assert publishes["Publish engine chat-worker release"].endswith('release chat-worker "$RUNNER_TEMP/adp-release-chat-worker/release.json"')
    assert publishes["Publish engine agent-worker release"].endswith('release agent-worker "$RUNNER_TEMP/adp-release-agent-worker/release.json"')
    build = next(step for step in steps if step.get("name") == "Build image via CodeBuild")
    assert build["with"]["source_revision"] == "${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}"
    rollout = next(step for step in steps if step.get("name") == "Update chat ScaledJob manifest")
    assert rollout["env"]["GATEWAY_HTTPS_ORIGIN"] == "${{ vars.GATEWAY_HTTPS_ORIGIN }}"
    assert "publish-chat-sandbox-image.sh" in (ROOT / "codebuild/bs-chat-agent.yml").read_text()
    assert "chat-scaledjob.yaml" not in SCRIPT.read_text()
    assert "chat-crew-pod-admission.yaml" in SCRIPT.read_text()


def test_rollout_fails_closed_before_cluster_mutation_without_model_policy(context_env):
    env = {
        **os.environ,
        **context_env,
        "ENVIRONMENT": "dev",
        "NAMESPACE": "adp-gateway-agents",
        "ADP_CHAT_EXISTING_NAMESPACE_ONLY": "true",
        "ADP_CHAT_MODEL_POLICY_ENABLED": "false",
    }
    result = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "policy plumbing must be enabled" in result.stderr


@pytest.mark.parametrize("untrusted", [False, True])
def test_build_outputs_treat_source_as_data_not_shell_code(tmp_path, context_env, untrusted):
    document = yaml.safe_load((ROOT / ".github/workflows/chat-agent-deploy.yml").read_text())
    step = next(step for step in document["jobs"]["build-and-deploy"]["steps"] if step.get("id") == "build_info")
    assert step["env"]["ADP_SELECTED_SOURCE"] == "${{ inputs.manual_source_revision || inputs.adp_source_revision || github.sha }}"
    marker = tmp_path / "injected"
    source = f"$(touch {marker})" if untrusted else SOURCE
    commands = tmp_path / "commands"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stubs = {
        "aws": '#!/bin/bash\nprintf "%s\\n" "$*" >> "$MOCK_COMMANDS"\nprintf "%s\\n" "${SANDBOX_IMAGE##*@}"\n',
        "python3": '#!/bin/bash\nprintf "%s\\n" "$*" >> "$MOCK_COMMANDS"\n'
        'case "$2" in *@sha256:*) echo "$SANDBOX_IMAGE" ;; *) echo "$SUPERVISOR_IMAGE" ;; esac\n',
    }
    for name, body in stubs.items():
        executable = binaries / name
        executable.write_text(body)
        executable.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["/bin/bash", "-e", "-o", "pipefail", "-c", step["run"]],
        env={
            **os.environ,
            **context_env,
            "PATH": f"{binaries}:{os.environ['PATH']}",
            "ADP_SELECTED_SOURCE": source,
            "ECR_REPO": "adp-chat-agent",
            "GITHUB_OUTPUT": str(output),
            "MOCK_COMMANDS": str(commands),
        },
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert not marker.exists()
    assert f"imageTag=sandbox-{source}" in commands.read_text()
    assert f"adp-chat-agent:{source}" in commands.read_text()
    assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
        "image_tag": source,
        "supervisor_image": SUPERVISOR,
        "sandbox_image": SANDBOX,
        "registry": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com",
    }


def test_deploy_script_and_publisher_are_bash_syntax_valid():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "modules/agent-factory/agent/k8s/verify-chat-rollout.sh")], check=True)
    subprocess.run(["bash", "-n", str(ROOT / "modules/agent-factory/agent/scripts/publish-chat-sandbox-image.sh")], check=True)


def test_rollout_requires_an_explicit_supervisor_gateway(tmp_path, context_env):
    kubectl = tmp_path / "kubectl"
    kubectl.write_text("#!/bin/sh\necho 'Unexpected cluster access' >&2\nexit 1\n")
    kubectl.chmod(0o755)
    env = {
        **os.environ,
        **context_env,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "ENVIRONMENT": "dev",
        "NAMESPACE": "adp-gateway-agents",
        "ADP_CHAT_EXISTING_NAMESPACE_ONLY": "true",
        "ADP_CHAT_MODEL_POLICY_ENABLED": "true",
    }
    env.pop("GATEWAY_HTTPS_ORIGIN")
    result = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0
    assert "gateway HTTPS URL required" in result.stderr


@pytest.mark.parametrize(
    "scaler_role",
    [
        f"arn:aws:iam::{ACCOUNT}:role/adp-dev-chat-worker-role",
        f"arn:aws:iam::{ACCOUNT}:role/adp-dev-chat-supervisor-role",
        "arn:aws:iam::000000000000:role/adp-dev-chat-worker-role",
        "",
    ],
)
async def test_safe_rollout_applies_only_supervisor_and_crew_with_pinned_images(
    tmp_path, context_env, published_gateway_ca, monkeypatch, scaler_role
):
    commands = tmp_path / "commands"
    binaries = tmp_path / "bin"
    binaries.mkdir()
    kubectl = binaries / "kubectl"
    kubectl.write_text("""#!/usr/bin/env python3
import json, os, sys, yaml
from pathlib import Path
args = sys.argv[1:]
with open(os.environ['MOCK_COMMANDS'], 'a') as log:
    log.write('kubectl ' + ' '.join(args) + '\\n')
if args[:2] == ['get', 'namespace']:
    print('namespace/adp-gateway-agents')
elif args[:2] == ['get', 'service']:
    print('10.10.0.20')
elif args[:2] == ['get', 'configmaps']:
    configmap = json.loads(os.environ['MOCK_CA_CONFIGMAP'])
    namespace = args[args.index('-n') + 1]
    print(json.dumps({'items': [configmap] if namespace == configmap['metadata']['namespace'] else []}))
elif args[:3] == ['get', 'configmap', 'bedrockgateway-config']:
    assert args[args.index('-n') + 1] == 'adp-gateway'
    print(json.dumps({'kind': 'ConfigMap', 'data': json.loads(os.environ['MOCK_GATEWAY_CONFIG'])}))
elif args[:3] == ['patch', 'configmap', 'bedrockgateway-config']:
    assert args[args.index('-n') + 1] == 'adp-gateway'
    Path(os.environ['MOCK_PATCHED']).write_text(args[args.index('--patch') + 1])
elif args[:2] == ['get', 'job']:
    sys.exit(1)
elif args[:2] == ['get', 'scaledjob']:
    print(os.environ['SUPERVISOR_IMAGE'])
elif args[:2] == ['get', 'daemonset']:
    print(f"prepull={os.environ['SUPERVISOR_IMAGE']}\\nprepull-sandbox={os.environ['SANDBOX_IMAGE']}")
elif args[0] == 'create':
    print(json.dumps(yaml.safe_load(Path(args[args.index('-f') + 1]).read_text())))
elif args[0] == 'apply' and args[1] == '-f' and args[2].endswith('objects.json'):
    contents = json.loads(Path(args[2]).read_text())
    Path(os.environ['MOCK_APPLIED']).write_text(json.dumps(contents))
elif args[0] == 'apply' and args[1] == '-f' and args[2].endswith('prepull.json'):
    Path(os.environ['MOCK_PREPULL']).write_text(Path(args[2]).read_text())
elif args[0] not in ('apply', 'wait', 'rollout'):
    sys.exit('unexpected kubectl action')
""")
    stubs = {
        "terraform": """#!/bin/bash
if [[ "$1" == output ]]; then
  case "$3" in
    chat_tasks_fifo_queue_url) echo 'https://sqs.us-east-1.amazonaws.com/123456789012/queue.fifo' ;;
    chat_supervisor_role_arn) echo 'arn:aws:iam::123456789012:role/adp-dev-chat-supervisor-role' ;;
    chat_scaler_role_arn) echo "$MOCK_SCALER_ROLE" ;;
  esac
fi
""",
        # sts get-caller-identity, the SSM-backed Crew values (#8058) and the published chat
        # workload identities (#8086: the two activation values already hold the delegated
        # values, the three digest parameters still hold the previous build's).
        "aws": """#!/bin/bash
if [[ "$1 $2" == "ssm put-parameter" ]]; then
  [[ "$3 $5 $6 $7 $9 ${10} ${11}" == "--name --type String --value --overwrite --region us-east-1" ]] || { echo "unexpected put $*" >&2; exit 9; }
  echo "put $4=$8" >> "$MOCK_SSM_PUTS"
  exit 0
fi
if [[ "$1 $2" == "ssm get-parameter" ]]; then
  case "$*" in
    *crew-dashboard-origin*) echo "$MOCK_CREW_ORIGIN" ;;
    *chat-authority-worker-images*|*chat-sandbox-image-digests*|*chat-crew-image-digests*) echo "sha256:$(printf '0%.0s' {1..64})" ;;
    *chat-worker-service-account*) echo adp-chat-supervisor ;;
    *chat-data-enabled*) echo true ;;
    *) echo "An error occurred (ParameterNotFound) when calling the GetParameter operation: $4" >&2; exit 254 ;;
  esac
  exit 0
fi
echo 123456789012
""",
        "bash": "#!/bin/bash\nexit 0\n",
    }
    for name, source in stubs.items():
        executable = binaries / name
        executable.write_text(source)
        executable.chmod(0o755)
    kubectl.chmod(0o755)
    env = {
        **os.environ,
        **context_env,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "MOCK_COMMANDS": str(commands),
        "MOCK_APPLIED": str(tmp_path / "applied.json"),
        "MOCK_PREPULL": str(tmp_path / "prepull.json"),
        "MOCK_PATCHED": str(tmp_path / "patched.json"),
        "MOCK_SSM_PUTS": str(tmp_path / "ssm-puts"),
        "MOCK_GATEWAY_CONFIG": json.dumps({"ADP_CHAT_WORKER_IMAGE_DIGESTS": "sha256:" + "0" * 64, "BG_OTHER": "kept"}),
        "MOCK_CA_CONFIGMAP": json.dumps(published_gateway_ca),
        "MOCK_SCALER_ROLE": scaler_role,
        "MOCK_CREW_ORIGIN": "https://crew.example.test",
        "ENVIRONMENT": "dev",
        "NAMESPACE": "adp-gateway-agents",
        "ADP_CHAT_EXISTING_NAMESPACE_ONLY": "true",
        "ADP_CHAT_MODEL_POLICY_ENABLED": "true",
    }
    env.pop("ADP_CREW_DASHBOARD_ORIGIN", None)
    env.pop("ADP_CHAT_SANDBOX_START_DEADLINE_MS", None)
    env.pop("ADP_CHAT_PERSISTENT_EXECUTOR", None)
    env.pop("ADP_CHAT_GATEWAY_ROLLOUT_TIMEOUT", None)
    result = subprocess.run(["/bin/bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=20)
    if scaler_role != f"arn:aws:iam::{ACCOUNT}:role/adp-dev-chat-worker-role":
        assert result.returncode != 0
        assert "Unexpected chat queue polling role" in result.stderr
        assert "kubectl apply" not in commands.read_text()
        assert not (tmp_path / "ssm-puts").exists(), "nothing is published before the ScaledJob is applied"
        return
    assert result.returncode == 0, result.stderr
    applied = json.loads((tmp_path / "applied.json").read_text())["items"]
    scaledjob = next(item for item in applied if item["kind"] == "ScaledJob")
    authentication = next(item for item in applied if item["kind"] == "TriggerAuthentication")
    assert scaledjob["spec"]["triggers"][0]["authenticationRef"]["name"] == authentication["metadata"]["name"]
    assert authentication["spec"]["podIdentity"] == {
        "provider": "aws",
        "identityOwner": "keda",
        "roleArn": f"arn:aws:iam::{ACCOUNT}:role/adp-dev-chat-worker-role",
    }
    assert scaledjob["spec"]["triggers"][0]["metadata"]["queueURL"] == f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/queue.fifo"
    # One supervisor per visible message: a delivery held in flight by the running supervisor must
    # not start idle supervisors (#8047).
    assert scaledjob["spec"]["triggers"][0]["metadata"]["queueLength"] == "1"
    assert scaledjob["spec"]["triggers"][0]["metadata"]["scaleOnInFlight"] == "false"
    template = scaledjob["spec"]["jobTargetRef"]["template"]["spec"]
    assert template["serviceAccountName"] == "adp-chat-supervisor"
    assert template["containers"][0]["image"] == SUPERVISOR
    supervisor_env = {item["name"]: item["value"] for item in template["containers"][0]["env"]}
    assert supervisor_env["ADP_CHAT_DATA_URL"] == GATEWAY
    assert supervisor_env["ADP_CHAT_SANDBOX_CA_CONFIGMAP"] == published_gateway_ca["metadata"]["name"]
    # The Crew dashboard origin came from SSM because the caller (like chat-agent-deploy.yml)
    # did not set it (#8058); the sandbox start deadline is explicit on the ScaledJob (#8059).
    assert supervisor_env["ADP_CREW_DASHBOARD_ORIGIN"] == "https://crew.example.test"
    assert supervisor_env["ADP_CHAT_SANDBOX_START_DEADLINE_MS"] == "120000"
    assert "[deploy-chat] ADP_CREW_DASHBOARD_ORIGIN from ssm /adp/dev/gateway/crew-dashboard-origin: https://crew.example.test" in result.stdout
    # The persistent-executor default was resolved from SSM (unpublished here) and rendered as the
    # supervisor's built-in sandbox-loop; the ADP-managed path never publishes it (#8115).
    assert supervisor_env["ADP_CHAT_PERSISTENT_EXECUTOR"] == "sandbox-loop"
    assert (
        "[deploy-chat] ADP_CHAT_PERSISTENT_EXECUTOR from ssm /adp/dev/gateway/chat-persistent-executor (ParameterNotFound): <empty>" in result.stdout
    )
    config = next(item for item in applied if item["kind"] == "ConfigMap")
    assert json.loads(config["data"]["pod.json"])["spec"]["containers"][0]["image"] == SANDBOX
    # Both release digests are pre-pulled on every node, and waited for, before the ScaledJob (#8059).
    prepull = json.loads((tmp_path / "prepull.json").read_text())
    assert prepull["kind"] == "DaemonSet" and prepull["metadata"]["name"] == "chat-agent-image-prepull"
    containers = prepull["spec"]["template"]["spec"]["containers"]
    assert [(c["name"], c["image"]) for c in containers] == [("prepull", SUPERVISOR), ("prepull-sandbox", SANDBOX)]
    assert containers[0]["securityContext"] == containers[1]["securityContext"]
    assert containers[0]["command"] == containers[1]["command"]
    log = commands.read_text()
    assert "chat-scaledjob.yaml" not in log
    assert "chat-sandbox-admission.yaml" in log and "chat-crew-pod-admission.yaml" in log
    assert log.index("chat-sandbox-admission.yaml") < log.index("objects.json")
    applied_at = lambda name: next(m.start() for m in re.finditer(r"kubectl apply -f \S+/" + re.escape(name), log))  # noqa: E731
    assert (
        applied_at("prepull.json")
        < log.index("kubectl rollout status daemonset/chat-agent-image-prepull -n adp-gateway-agents --timeout=600s")
        < log.index("kubectl get daemonset chat-agent-image-prepull")
        < applied_at("supervisor.yaml")
        < applied_at("objects.json")
    )
    assert "get configmaps -n adp-gateway-agents" in log
    # After the ScaledJob apply the script publishes the digests the gateway verifies chat workloads
    # against and converges the live gateway ConfigMap (#8086): only the three digest parameters are
    # put (the activation values already matched and were only confirmed), the ConfigMap patch
    # carries the release keys, and the gateway restart is waited for.
    supervisor_digest, sandbox_digest = SUPERVISOR.rpartition("@")[2], SANDBOX.rpartition("@")[2]
    assert (tmp_path / "ssm-puts").read_text().splitlines() == [
        f"put /adp/dev/gateway/chat-authority-worker-images={supervisor_digest}",
        f"put /adp/dev/gateway/chat-sandbox-image-digests={sandbox_digest}",
        f"put /adp/dev/gateway/chat-crew-image-digests={sandbox_digest}",
    ]
    assert f"[deploy-chat] published /adp/dev/gateway/chat-authority-worker-images={supervisor_digest} (was sha256:{'0' * 64})" in result.stdout
    assert "[deploy-chat] /adp/dev/gateway/chat-worker-service-account already holds adp-chat-supervisor" in result.stdout
    assert "[deploy-chat] /adp/dev/gateway/chat-data-enabled already holds true" in result.stdout
    assert json.loads((tmp_path / "patched.json").read_text()) == {
        "data": {
            "ADP_CHAT_SANDBOX_CA_CONFIGMAP": published_gateway_ca["metadata"]["name"],
            "ADP_CHAT_DATA_URL": "https://chat-sandbox-gateway.adp-gateway.svc:8443",
            "ADP_CHAT_WORKER_IMAGE_DIGESTS": supervisor_digest,
            "ADP_CHAT_WORKER_SERVICE_ACCOUNT": "adp-chat-supervisor",
            "ADP_CHAT_SANDBOX_IMAGE_DIGESTS": sandbox_digest,
            "ADP_CHAT_CREW_IMAGE_DIGESTS": sandbox_digest,
            "ADP_CHAT_DATA_ENABLED": "true",
        }
    }
    assert (
        applied_at("objects.json")
        < log.index("kubectl wait --for=condition=Ready scaledjob/chat-agent-worker")
        < log.index("kubectl get configmap bedrockgateway-config -n adp-gateway -o json")
        < log.index("kubectl patch configmap bedrockgateway-config -n adp-gateway --type merge --patch")
        < log.index("kubectl rollout restart deployment/bedrockgateway -n adp-gateway")
        < log.index("kubectl rollout status deployment/bedrockgateway -n adp-gateway --timeout=600s")
    )
    assert "[deploy-chat] bedrockgateway-config patched" in result.stdout
    assert "[deploy-chat] Release digests published to SSM and the gateway ConfigMap" in result.stdout

    monkeypatch.setenv("ADP_CHAT_DATA_ENABLED", "false")
    gateway = FastAPI()
    gateway.include_router(chat_data_routes.router)
    sandbox_origin = f"https://{HOST}:8443"
    async with httpx.AsyncClient(
        base_url=supervisor_env["ADP_CHAT_DATA_URL"],
        transport=httpx.MockTransport(lambda _: pytest.fail("Unexpected gateway destination")),
        mounts={GATEWAY: httpx.ASGITransport(app=gateway), sandbox_origin: httpx.ASGITransport(app=SandboxTransport(gateway))},
    ) as client:
        for operation in ("admit", "reserve"):
            path = f"/internal/v1/agent/chat/data/{operation}"
            response = await client.post(path, json={})
            assert response.status_code == 503
            assert response.json() == {"detail": {"error": "chat_data_disabled"}}
            restricted = await client.post(sandbox_origin + path, json={})
            assert restricted.status_code == 404


@pytest.mark.parametrize("lookup_succeeds", [True, False])
def test_sandbox_publisher_uses_immutable_variant_and_refuses_registry_errors(tmp_path, lookup_succeeds):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    aws = binaries / "aws"
    aws.write_text(
        "#!/bin/bash\n"
        'printf \'%s\\n\' "$*" >> "$MOCK_AWS_COMMANDS"\n'
        + ("echo 'sha256:" + "a" * 64 + "'\n" if lookup_succeeds else "echo 'AccessDenied' >&2\nexit 1\n")
    )
    aws.chmod(0o755)
    docker = binaries / "docker"
    docker.write_text('#!/bin/bash\necho called > "$MOCK_DOCKER_CALLED"\nexit 1\n')
    docker.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{binaries}:{os.environ['PATH']}",
        "ADP_SOURCE_SHA": SOURCE,
        "REGISTRY": f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com",
        "AWS_REGION": REGION,
        "MOCK_AWS_COMMANDS": str(tmp_path / "aws-commands"),
        "MOCK_DOCKER_CALLED": str(tmp_path / "docker-called"),
    }
    script = ROOT / "modules/agent-factory/agent/scripts/publish-chat-sandbox-image.sh"
    result = subprocess.run(["/bin/bash", str(script)], cwd=ROOT, env=env, capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) is lookup_succeeds, result.stderr
    assert not (tmp_path / "docker-called").exists()
    assert f"imageTag=sandbox-{SOURCE}" in (tmp_path / "aws-commands").read_text()


def test_crew_admission_keeps_ephemeral_deadline_without_rejecting_persistent_sessions():
    manifest_dir = ROOT / "modules/agent-factory/agent/k8s"
    sandbox = list(yaml.safe_load_all((manifest_dir / "chat-sandbox-admission.yaml").read_text()))
    crew = list(yaml.safe_load_all((manifest_dir / "chat-crew-pod-admission.yaml").read_text()))
    assert sandbox[0]["spec"]["failurePolicy"] == crew[0]["spec"]["failurePolicy"] == "Fail"
    assert sandbox[1]["spec"]["validationActions"] == crew[1]["spec"]["validationActions"] == ["Deny"]
    sandbox_rules = " ".join(rule["expression"] for rule in sandbox[0]["spec"]["validations"])
    crew_rules = " ".join(rule["expression"] for rule in crew[0]["spec"]["validations"])
    assert "object.spec.activeDeadlineSeconds == 900" in sandbox_rules
    assert "'adp.io/session-hash' in object.metadata.labels ?" not in sandbox_rules
    assert "!has(object.spec.activeDeadlineSeconds)" in crew_rules
    assert "object.spec.serviceAccountName == 'adp-crew-pod'" in crew_rules
    assert "!('adp.io/chat-sandbox' in object.metadata.labels)" in crew_rules
