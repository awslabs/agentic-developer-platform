"""Render the real ConfigMap after resolving the existing ACL database settings."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def render_config(tmp_path, *, endpoint="acl.example", aws_exit=0, overrides=None):
    aws = tmp_path / "aws"
    aws.write_text(
        '#!/bin/bash\nprintf "%s\\n" "$*" > "$AWS_CALL_LOG"\n'
        'printf "%s" "$TEST_ENDPOINT"\nexit "$TEST_AWS_EXIT"\n'
    )
    aws.chmod(0o700)
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "AWS_CALL_LOG": str(tmp_path / "aws.log"),
        "TEST_ENDPOINT": endpoint,
        "TEST_AWS_EXIT": str(aws_exit),
        "ENVIRONMENT": "stage",
        "AWS_REGION": "eu-west-2",
        **(overrides or {}),
    }
    result = subprocess.run(
        [
            "bash", "-eu", "-c",
            'source scripts/_common.sh\nresolve_acl_config\n'
            'template_file manifests/agent-context-configmap.yaml',
        ],
        cwd=ROOT, env=env, capture_output=True, text=True, check=False,
    )
    return result, tmp_path / "aws.log"


def test_existing_ssm_database_renders_usable_iam_settings(tmp_path):
    result, calls = render_config(tmp_path)
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load(result.stdout)["data"]
    assert data["DB_HOST"] == "acl.example"
    assert data["DB_NAME"] == "agent_context"
    assert data["DB_USER"] == "agent_context_svc"
    assert data["DB_USE_IAM_AUTH"] == "true"
    assert "/adp/stage/rds/endpoint" in calls.read_text()
    assert "--region eu-west-2" in calls.read_text()


def test_explicit_database_settings_are_preserved_without_aws_call(tmp_path):
    result, calls = render_config(tmp_path, overrides={
        "AC_RDS_HOST": "existing.example", "AC_DB_NAME": "custom_db", "AC_DB_USERNAME": "custom_user"
    })
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load(result.stdout)["data"]
    assert (data["DB_HOST"], data["DB_NAME"], data["DB_USER"]) == (
        "existing.example", "custom_db", "custom_user"
    )
    assert not calls.exists()


@pytest.mark.parametrize(("endpoint", "aws_exit"), [("", 0), ("None", 0), ("null", 0), ("", 1)])
def test_missing_database_refuses_configmap_render(tmp_path, endpoint, aws_exit):
    result, _ = render_config(tmp_path, endpoint=endpoint, aws_exit=aws_exit)
    assert result.returncode != 0
    assert "ERROR:" in result.stderr
    assert result.stdout == ""


def test_both_deployment_entrypoints_resolve_before_rendering():
    workflow = yaml.safe_load((ROOT.parents[1] / ".github/workflows/agent-context-deploy.yml").read_text())
    step = next(step["run"] for step in workflow["jobs"]["deploy"]["steps"] if step.get("name") == "Deploy ConfigMap")
    for script in (step, (ROOT / "deploy.sh").read_text()):
        assert script.index("resolve_acl_config") < script.index('template_file ' + (
            'manifests/agent-context-configmap.yaml' if script == step else
            '"${SCRIPT_DIR}/manifests/agent-context-configmap.yaml"'
        ))
