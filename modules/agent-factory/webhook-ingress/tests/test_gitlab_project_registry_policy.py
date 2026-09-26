"""Render actual GitLab opt-in expressions without providers or live state."""

import importlib.util
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "gitlab_policy_renderer", Path(__file__).with_name("render_engine_command_policies.py")
)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)

INFRA = Path(__file__).resolve().parents[1] / "infra"


@pytest.mark.parametrize(
    "global_policy,project_registry", [(False, False), (True, False), (False, True), (True, True)]
)
def test_explicit_registry_only_enables_existing_scoped_producer_policy(
    tmp_path, global_policy, project_registry
):
    terraform = shutil.which("terraform")
    if terraform is None:
        pytest.skip("Terraform is required to render the actual opt-in expressions")
    source = re.sub(r"^\s*#.*$", "", (INFRA / "pmm-gitlab.tf").read_text(), flags=re.M)
    lambda_source = (INFRA / "gitlab_lambda.tf").read_text()
    declarations, resource = source.split(
        'resource "aws_iam_role_policy" "lambda_gitlab_model_root"', 1
    )
    count = re.search(r"count\s*=\s*([^\n]+)", resource).group(1)
    role = re.search(r"role\s*=\s*([^\n]+)", resource).group(1).strip()
    assert role == "aws_iam_role.lambda_execution.id"
    policy = renderer._policy_expression(
        source, 'resource "aws_iam_role_policy" "lambda_gitlab_model_root"'
    )
    policy = policy.replace(
        "local.work_claim_admission_arn",
        '"arn:aws:execute-api:us-east-1:123456789012:fixture/dev/POST/internal/v1/agent/work/admit"',
    )
    fields = {
        name: re.search(name + r"\s*=\s*([^\n]+)", lambda_source).group(1).strip()
        for name in ("ADP_GITLAB_MODEL_POLICY_ENABLED", "ADP_GITLAB_PROJECT_REGISTRY_ENABLED")
    }
    (tmp_path / "main.tf").write_text(
        declarations
        + "\nlocals {\n"
        + f"count = {count}\npolicy = {policy}\n"
        + "\n".join(f"{key} = {value}" for key, value in fields.items())
        + "\n}\n"
    )
    subprocess.run(
        [terraform, "validate", "-no-color"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    query = "jsonencode({count=local.count,policy=local.policy,global=local.ADP_GITLAB_MODEL_POLICY_ENABLED,registry=local.ADP_GITLAB_PROJECT_REGISTRY_ENABLED})\n"

    def render(flags):
        result = subprocess.run(
            [terraform, "console", "-no-color", *flags],
            input=query,
            cwd=tmp_path,
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(json.loads(result.stdout))

    assert render([])["count"] == 0
    value = render(
        [
            f"-var=gitlab_model_policy_enabled={str(global_policy).lower()}",
            f"-var=gitlab_project_registry_enabled={str(project_registry).lower()}",
        ]
    )
    assert value["count"] == int(global_policy or project_registry)
    assert value["global"] == str(global_policy).lower()
    assert value["registry"] == str(project_registry).lower()
    assert value["policy"] == {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["execute-api:Invoke"],
                "Resource": [
                    "arn:aws:execute-api:us-east-1:123456789012:fixture/dev/POST/internal/v1/agent/roots/admit"
                ],
            }
        ],
    }
