"""Exercise deployment eligibility with real Git histories and workflow outputs."""

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/detect-webhook-deploy-changes.sh"
HOLD = Path(".github/deployment-holds/webhook-infra.md")
INFRA = "modules/agent-factory/webhook-ingress/infra/scaledjob-iam.tf"
CODE = "modules/agent-factory/webhook-ingress/lambda/github/handler.py"


def git(repo, *args):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def repo(tmp_path):
    git(tmp_path, "init")
    git(tmp_path, "config", "user.name", "Deployment test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "config", "commit.gpgsign", "false")
    (tmp_path / HOLD).parent.mkdir(parents=True)
    (tmp_path / HOLD).write_text("Worker migration pending\n")
    commit(tmp_path)
    return tmp_path


def commit(repo):
    git(repo, "add", ".")
    git(repo, "commit", "--allow-empty", "-m", "Fixture revision")


def change(repo, *paths):
    for path in paths:
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture change\n")
    commit(repo)


def detect(repo, event="push"):
    output = repo / "outputs"
    summary = repo / "summary"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=repo,
        env={
            **os.environ,
            "GITHUB_EVENT_NAME": event,
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
        capture_output=True,
        text=True,
    )
    values = (
        dict(line.split("=", 1) for line in output.read_text().splitlines())
        if output.exists()
        else {}
    )
    return result, values, summary.read_text() if summary.exists() else ""


@pytest.mark.parametrize(
    "paths",
    [
        (INFRA,),
        (INFRA, CODE),
        ("environments/dev/modules/webhook-ingress.tfvars",),
        ("environments/dev/modules/webhook-ingress.tfvars.json", CODE),
    ],
)
def test_hold_blocks_infra_and_mixed_releases_on_later_commits(repo, paths):
    change(repo, *paths)
    result, outputs, summary = detect(repo)
    assert result.returncode == 0, result.stderr
    assert outputs == {"code": "false", "infra": "false", "infra_held": "true"}
    assert "#5195" in summary


def test_manual_dispatch_cannot_bypass_hold(repo):
    result, outputs, _ = detect(repo, "workflow_dispatch")
    assert result.returncode == 0, result.stderr
    assert outputs == {"code": "false", "infra": "false", "infra_held": "true"}


@pytest.mark.parametrize(
    "path",
    [
        CODE,
        "modules/agent-factory/webhook-ingress/scripts/package-lambdas.sh",
        "modules/agent-factory/webhook-ingress/requirements.txt",
    ],
)
def test_code_only_release_remains_eligible(repo, path):
    change(repo, path)
    result, outputs, summary = detect(repo)
    assert result.returncode == 0, result.stderr
    assert outputs == {"code": "true", "infra": "false", "infra_held": "false"}
    assert not summary


def test_docs_do_not_deploy(repo):
    change(repo, "docs/security/example.md")
    result, outputs, _ = detect(repo)
    assert result.returncode == 0, result.stderr
    assert outputs == {"code": "false", "infra": "false", "infra_held": "false"}


def test_removing_hold_alone_does_not_deploy(repo):
    (repo / HOLD).unlink()
    commit(repo)
    result, outputs, _ = detect(repo)
    assert result.returncode == 0, result.stderr
    assert outputs == {"code": "false", "infra": "false", "infra_held": "false"}


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_release_after_reviewed_hold_removal(repo, event):
    (repo / HOLD).unlink()
    change(repo, CODE, INFRA)
    result, outputs, _ = detect(repo, event)
    assert result.returncode == 0, result.stderr
    assert outputs == {"code": "true", "infra": "true", "infra_held": "false"}


@pytest.mark.parametrize("event", ["push", "pull_request"])
def test_unavailable_diff_or_unsupported_event_fails_closed(repo, event):
    result, outputs, _ = detect(repo, event)
    assert result.returncode != 0
    assert outputs == {}


def test_workflow_uses_guard_before_all_mutating_jobs():
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/webhook-ingress-deploy.yml").read_text()
    )
    jobs = workflow["jobs"]
    changes = jobs["changes"]
    assert next(s for s in changes["steps"] if s.get("id") == "filter")["run"] == (
        "bash .github/scripts/detect-webhook-deploy-changes.sh"
    )
    assert changes["outputs"]["infra"] == "${{ steps.filter.outputs.infra }}"
    assert changes["outputs"]["code"] == "${{ steps.filter.outputs.code }}"
    assert jobs["package"]["needs"] == "changes"
    assert jobs["package"]["if"] == (
        "needs.changes.outputs.code == 'true' || needs.changes.outputs.infra == 'true'"
    )
    for job in ("deploy-infra", "update-code"):
        assert "package" in jobs[job]["needs"]
        assert "needs.package.result == 'success'" in jobs[job]["if"]


def test_other_environment_overlay_does_not_trigger_default_dev_deploy(repo):
    change(repo, "environments/prod/modules/webhook-ingress.tfvars")
    result, outputs, _ = detect(repo)
    assert result.returncode == 0, result.stderr
    assert outputs == {"code": "false", "infra": "false", "infra_held": "false"}
