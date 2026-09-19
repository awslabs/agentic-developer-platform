"""Run deployment helpers with disposable command doubles; no cloud access."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "modules/agent-factory/webhook-ingress/scripts"


@pytest.fixture
def deployment(tmp_path):
    scripts = tmp_path / "modules/agent-factory/webhook-ingress/scripts"
    scripts.mkdir(parents=True)
    (scripts.parent / "infra").mkdir()
    for name in ("terraform-webhook.sh", "rollout-worker-gateway.sh"):
        shutil.copyfile(SCRIPTS / name, scripts / name)
    binary = tmp_path / "bin"
    binary.mkdir()
    terraform = binary / "terraform"
    terraform.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nprint(json.dumps(sys.argv[1:]))\n"
    )
    terraform.chmod(0o700)
    env = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "ADP_ENV": "staging",
        "AWS_REGION": "eu-west-1",
        "STATE_BUCKET": "adp-terraform-state-123456789012",
    }
    env.pop("ADP_STATE_REGION", None)
    return tmp_path, scripts, env


def invoke(deployment, *args):
    _, scripts, env = deployment
    return subprocess.run(
        ["bash", str(scripts / "terraform-webhook.sh"), *args],
        env=env,
        text=True,
        capture_output=True,
    )


def test_backend_uses_selected_environment_and_region(deployment):
    result = invoke(deployment, "init", "-input=false")
    assert result.returncode == 0, result.stderr
    args = json.loads(result.stdout)
    assert (
        "-backend-config=key=staging/modules/webhook-ingress/terraform.tfstate" in args
    )
    assert "-backend-config=region=eu-west-1" in args
    assert "-backend-config=bucket=adp-terraform-state-123456789012" in args
    assert not any("dev/" in arg for arg in args)


@pytest.mark.parametrize("action", ["plan", "apply", "import"])
def test_all_terraform_paths_load_the_same_environment_overlay(deployment, action):
    root, _, _ = deployment
    overlay = root / "environments/staging/modules/webhook-ingress.tfvars"
    overlay.parent.mkdir(parents=True)
    overlay.write_text("agent_authority_prepared = true\n")
    result = invoke(deployment, action)
    assert result.returncode == 0, result.stderr
    args = json.loads(result.stdout)
    assert args[:3] == [action, "-var-file=terraform.tfvars", f"-var-file={overlay}"]
    assert args[-2:] == ["-var=environment=staging", "-var=aws_region=eu-west-1"]


def test_new_environment_uses_module_defaults_without_a_dev_overlay(deployment):
    _, _, env = deployment
    env["ADP_ENV"] = "prod"
    result = invoke(deployment, "plan")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        "plan",
        "-var-file=terraform.tfvars",
        "-var=environment=prod",
        "-var=aws_region=eu-west-1",
    ]


def test_backend_region_can_be_centralized_explicitly(deployment):
    _, _, env = deployment
    env["ADP_STATE_REGION"] = "us-east-1"
    result = invoke(deployment, "init")
    assert result.returncode == 0, result.stderr
    assert "-backend-config=region=us-east-1" in json.loads(result.stdout)


def test_invalid_environment_cannot_select_another_states_path(deployment):
    _, _, env = deployment
    env["ADP_ENV"] = "../dev"
    result = invoke(deployment, "init")
    assert result.returncode != 0
    assert not result.stdout


@pytest.mark.parametrize("missing", ["environment", "region"])
def test_missing_target_refuses_before_terraform(deployment, missing):
    _, _, env = deployment
    for name in (
        ("ADP_ENV", "ENVIRONMENT") if missing == "environment" else ("AWS_REGION",)
    ):
        env.pop(name, None)
    result = invoke(deployment, "init")
    assert result.returncode != 0
    assert not result.stdout


@pytest.mark.parametrize("selected", ["dev", "staging", "prod"])
def test_real_terraform_defaults_do_not_leak_dev_settings(deployment, selected):
    root, scripts, env = deployment
    infra = scripts.parent / "infra"
    shutil.copyfile(
        SCRIPTS.parent / "infra/terraform.tfvars", infra / "terraform.tfvars"
    )
    overlay = ROOT / "environments/dev/modules/webhook-ingress.tfvars"
    if overlay.exists():
        target = root / overlay.relative_to(ROOT)
        target.parent.mkdir(parents=True)
        shutil.copyfile(overlay, target)
    # Evaluate the actual input files through the wrapper with provider-free
    # Terraform. Every declared variable remains production-owned.
    shutil.copyfile(SCRIPTS.parent / "infra/variables.tf", infra / "variables.tf")
    real_terraform = shutil.which("terraform")
    assert real_terraform
    (root / "bin/terraform").write_text(
        '#!/usr/bin/env bash\nshift\nexec "$REAL_TERRAFORM" console -no-color "$@"\n'
    )
    env.update(ADP_ENV=selected, REAL_TERRAFORM=real_terraform)
    result = subprocess.run(
        ["bash", str(scripts / "terraform-webhook.sh"), "plan"],
        env=env,
        input="jsonencode({reserved=var.enable_lambda_reserved_concurrency, adversarial=var.enable_adversarial_e2e, repo=var.eventbridge_security_agent_repo, environment=var.environment})\n",
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(json.loads(result.stdout)) == {
        "reserved": selected != "dev",
        "adversarial": selected == "dev",
        "repo": "aws-e/adp" if selected == "dev" else "",
        "environment": selected,
    }


@pytest.mark.parametrize(
    "enabled,refs,success,restarts,missing,unavailable",
    [
        ("false", "bedrockgateway-config", True, False, "", ""),
        ("false", "adp-worker-authority-config", True, True, "", "missing-secret"),
        ("true", "bedrockgateway-config", False, False, "", ""),
        (
            "true",
            "bedrockgateway-config adp-worker-authority-config",
            True,
            True,
            "",
            "",
        ),
        *[
            ("true", "adp-worker-authority-config", False, False, "", reason)
            for reason in ("missing-secret", "missing-key", "empty-key")
        ],
        *[
            ("true", "adp-worker-authority-config", False, False, key, "")
            for key in (
                "AGENT_RUN_CREDENTIAL_KEY",
                "AGENT_CONTROL_ENVELOPE_SIGNING_KEY",
                "ADP_MARKER_SIGNING_KEY",
                "ADP_DOOR_SERVICE_KEY",
            )
        ],
    ],
)
def test_gateway_rollout_requires_the_terraform_config_before_activation(
    deployment,
    enabled,
    refs,
    success,
    restarts,
    missing,
    unavailable,
):
    root, scripts, env = deployment
    log = root / "commands.jsonl"
    command = """#!/usr/bin/env python3
import json,os,sys
with open(os.environ['COMMAND_LOG'], 'a') as output:
    output.write(json.dumps(sys.argv) + '\\n')
if 'get' in sys.argv:
    if 'secret' in sys.argv:
        if os.environ['UNAVAILABLE'] == 'missing-secret': sys.exit(1)
        if os.environ['UNAVAILABLE'] not in ('missing-key', 'empty-key'):
            print('run-credential-key\\nenvelope-signing-key\\nmarker-signing-key\\ninternal-api-key')
    else:
        print(os.environ['SECRET_REFS'] if 'secretKeyRef' in sys.argv[-1] else os.environ['CONFIG_REFS'])
"""
    for name in ("aws", "kubectl"):
        executable = root / "bin" / name
        executable.write_text(command)
        executable.chmod(0o700)
    env.update(
        {
            "ADP_CLUSTER": "adp-staging-eks-cluster",
            "ADP_REGION": "eu-west-1",
            "ADP_NAMESPACE": "adp-gateway",
            "ADP_AUTHORITY_ENABLED": enabled,
            "CONFIG_REFS": refs,
            "SECRET_REFS": "\n".join(
                f"{entry['name']}={ref['name']}/{ref['key']}"
                for container in yaml.safe_load(
                    (ROOT / "modules/gateway/k8s/deployment.yaml").read_text()
                )["spec"]["template"]["spec"]["containers"]
                if container["name"] == "bedrockgateway"
                for entry in container["env"]
                if (ref := entry.get("valueFrom", {}).get("secretKeyRef"))
            ),
            "COMMAND_LOG": str(log),
            "UNAVAILABLE": unavailable,
        }
    )
    if missing:
        env["SECRET_REFS"] = "\n".join(
            line
            for line in env["SECRET_REFS"].splitlines()
            if not line.startswith(missing + "=")
        )
    result = subprocess.run(
        ["bash", str(scripts / "rollout-worker-gateway.sh")],
        env=env,
        text=True,
        capture_output=True,
    )
    assert (result.returncode == 0) == success, result.stderr
    if missing:
        assert f"missing run-service secret reference: {missing}" in result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert any("restart" in call for call in calls) == restarts
    if restarts:
        assert any("status" in call for call in calls)
    if enabled == "false":
        assert not any("secret" in call for call in calls)
    for call in calls:
        if "secret" in call:
            assert (
                call[-1]
                == 'go-template={{range $key, $value := .data}}{{if $value}}{{$key}}{{"\\n"}}{{end}}{{end}}'
            )
