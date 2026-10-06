"""Factory previews must not claim or perform an infrastructure apply."""

import os
from pathlib import Path
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOW = ROOT / ".github/workflows/agent-factory-infra-apply.yml"


def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def step(name):
    return next(item for item in workflow()["jobs"]["apply"]["steps"] if item.get("name") == name)


def test_preview_retains_deployment_identity_and_blocks_apply():
    config = workflow()
    preview = config[True]["workflow_dispatch"]["inputs"]["plan_only"]
    assert preview["type"] == "boolean"
    assert preview["default"] is False
    assert config["jobs"]["apply"]["environment"] == "adp-deploy-${{ inputs.environment || 'dev' }}"
    assert step("Establish trusted deployment identity")["uses"] == "./.github/actions/trusted-deployment"
    assert step("Terraform Apply")["if"] == "${{ !inputs.plan_only }}"
    assert step("Terraform Apply")["run"] == "terraform apply tfplan"
    assert step("Destroy-safety label gate")["if"] == "steps.plan.outputs.detailed_exitcode == '2' && !inputs.plan_only"


def test_neither_mode_fabricates_github_credentials():
    source = WORKFLOW.read_text()
    assert "create-secret" not in source
    assert "PLACEHOLDER" not in source
    assert "gh-app-" not in source
    assert "Build factory Lambda packages" in source


@pytest.mark.parametrize("preview", [True, False])
@pytest.mark.parametrize("outcome", ["success", "failure"])
def test_summary_reports_observed_outcome_and_keeps_target_as_data(tmp_path, preview, outcome):
    summary = tmp_path / "summary"
    unexpected = tmp_path / "unexpected"
    target = f"module.arc_runner; $(touch {unexpected})"
    result = subprocess.run(
        ["bash", "-c", step("Summary")["run"]],
        env={
            **os.environ,
            "PLAN_ONLY": str(preview).lower(),
            "PLAN_OUTCOME": outcome,
            "APPLY_OUTCOME": "skipped" if preview else outcome,
            "REQUESTED_TARGET": target,
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    text = summary.read_text()
    assert target in text
    assert not unexpected.exists()
    if preview:
        assert f"Plan step: {outcome}" in text
        assert "Terraform Apply was skipped" in text
        assert "No infrastructure convergence is claimed" in text
        assert "fresh plan" in text
    else:
        assert f"Apply step: {outcome}" in text
        assert "Terraform Apply was skipped" not in text
    assert "all resources reconciled" not in text
