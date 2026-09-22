from __future__ import annotations

import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "platform" / "scripts" / "load-deploy-config.sh"
ACTION = REPO_ROOT / ".github" / "actions" / "load-deploy-config" / "action.yml"


def _source(extra_env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "ADP_ACCOUNT_ID": "111122223333",
        "ADP_REGION": "us-east-1",
        "ADP_ENVIRONMENT": "dev",
        "ADP_GITHUB_ORG": "example",
        **extra_env,
    }
    command = f"""
if source {SCRIPT!s}; then
  printf 'target=%s\\n' "$ADP_DEPLOY_TARGET_ACCOUNT"
  exit 0
else
  status=$?
  printf 'target=%s\\n' "${{ADP_DEPLOY_TARGET_ACCOUNT:-}}"
  exit "$status"
fi
"""
    return subprocess.run(
        ["bash", "-c", command], env=env, text=True, capture_output=True, check=False
    )


def test_normal_config_selects_only_the_authenticated_account():
    result = _source({})
    assert result.returncode == 0, result.stderr
    assert "target=111122223333" in result.stdout


def test_customer_account_environment_override_fails_before_target_selection():
    result = _source(
        {
            "ADP_CUSTOMER_ACCOUNT_ID": "999900001111",
            "ADP_SKIP_CROSS_ACCOUNT_ASSUME": "1",
        }
    )
    assert result.returncode != 0
    assert "cross-account customer bootstrap is disabled" in result.stderr
    assert "target=" in result.stdout
    assert "target=999900001111" not in result.stdout


def test_customer_account_config_block_fails_closed(tmp_path: Path):
    config = tmp_path / "deployment.yml"
    config.write_text(
        """account_id: \"111122223333\"
region: us-east-1
environment: dev
customer_account:
  account_id: \"999900001111\"
  aws_label: legacy-admin
  user_id: user-1
"""
    )
    result = _source({"ADP_DEPLOY_CONFIG_FILE": str(config)})
    assert result.returncode != 0
    assert "cross-account customer bootstrap is disabled" in result.stderr
    assert "target=999900001111" not in result.stdout


def test_loader_cannot_invoke_the_legacy_assume_helper():
    text = SCRIPT.read_text()
    assert "assume-customer-creds.py" not in text
    assert 'export ADP_DEPLOY_TARGET_ACCOUNT="$ADP_ACCOUNT_ID"' in text


def test_composite_action_does_not_export_customer_credentials_or_target():
    text = ACTION.read_text()
    assert "Any non-empty value fails the action closed" in text
    assert "AWS_ACCESS_KEY_ID=" not in text
    assert "customer_account_id=${ADP_CUSTOMER_ACCOUNT_ID" not in text


def test_composite_action_clears_inherited_plain_customer_target(tmp_path: Path):
    action = yaml.safe_load(ACTION.read_text())
    github_env = tmp_path / "github-env"
    github_output = tmp_path / "github-output"
    env = {
        **os.environ,
        "GITHUB_WORKSPACE": str(REPO_ROOT),
        "GITHUB_ENV": str(github_env),
        "GITHUB_OUTPUT": str(github_output),
        "ADP_ACCOUNT_ID": "111122223333",
        "ADP_REGION": "us-east-1",
        "ADP_ENVIRONMENT": "dev",
        "ADP_GITHUB_ORG": "example",
        "CUSTOMER_ACCOUNT_ID": "999900001111",
    }

    result = subprocess.run(
        ["bash", "-c", action["runs"]["steps"][0]["run"]],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    exported = dict(line.split("=", 1) for line in github_env.read_text().splitlines())
    assert exported["ACCOUNT_ID"] == "111122223333"
    assert exported["CUSTOMER_ACCOUNT_ID"] == ""


def test_workflows_never_prefer_the_retired_customer_target():
    workflow_text = "\n".join(
        path.read_text()
        for path in sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    )
    assert "CUSTOMER_ACCOUNT_ID:-$ACCOUNT_ID" not in workflow_text
    assert "env.CUSTOMER_ACCOUNT_ID || env.ACCOUNT_ID" not in workflow_text


def test_every_workflow_customer_account_input_reaches_the_fail_closed_action():
    workflows = sorted((REPO_ROOT / ".github" / "workflows").glob("*.yml"))
    customer_workflows = [
        path for path in workflows if "\n      customer_account_id:" in path.read_text()
    ]
    assert len(customer_workflows) == 21
    for path in customer_workflows:
        text = path.read_text()
        assert "uses: ./.github/actions/load-deploy-config" in text, path
        assert "customer_account_id: ${{ inputs.customer_account_id }}" in text, path
