"""Offline execution-boundary tests for executor publishing and eval OIDC."""

import datetime
import os
import pathlib
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).parents[2]


def workflow(name="superplane-executor-build.yml"):
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def build_action():
    return yaml.safe_load(
        (ROOT / ".github/actions/trusted-build/action.yml").read_text()
    )


def run_guard(script, **env):
    return subprocess.run(
        ["bash", "-c", script],
        env={"PATH": os.environ["PATH"], **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_executor_uses_existing_narrow_dispatcher_and_environment():
    document = workflow()
    assert set(document.get("on", document.get(True))) == {"workflow_dispatch"}
    job = document["jobs"]["build"]
    assert job["environment"] == "adp-build-dev"
    assert job["permissions"]["id-token"] == "write"
    steps = job["steps"]
    auth_at = next(
        i
        for i, step in enumerate(steps)
        if step.get("uses") == "./.github/actions/trusted-build"
    )
    assert steps[auth_at]["with"] == {
        "role_arn": "${{ vars.ADP_BUILD_ROLE_ARN }}",
        "region": "${{ inputs.region }}",
    }
    checkout_at = next(
        i for i, step in enumerate(steps) if "actions/checkout@" in step.get("uses", "")
    )
    aws_at = next(
        i
        for i, step in enumerate(steps)
        if "aws sts get-caller-identity" in step.get("run", "")
    )
    assert 0 < checkout_at < auth_at < aws_at
    assert steps[checkout_at]["with"]["persist-credentials"] is False
    assert document["concurrency"]["cancel-in-progress"] is False


@pytest.mark.parametrize(
    "ref,allowed",
    [
        ("refs/heads/main", True),
        ("refs/heads/feature", False),
        ("refs/tags/release", False),
        ("refs/pull/1/merge", False),
    ],
)
@pytest.mark.parametrize(
    "name,job",
    [
        ("superplane-executor-build.yml", "build"),
        ("eval-cli-uplift.yml", "evaluate"),
        ("eval-cli-uplift.yml", "recover"),
    ],
)
def test_main_only_guards_execute_before_checkout(name, job, ref, allowed):
    steps = workflow(name)["jobs"][job]["steps"]
    assert steps[0]["name"] == "Refuse an untrusted ref"
    result = run_guard(steps[0]["run"], GITHUB_REF=ref)
    assert (result.returncode == 0) is allowed


@pytest.mark.parametrize(
    "role,allowed",
    [
        ("", False),
        ("arn:aws:iam::123456789012:role/Administrator", False),
        ("arn:aws:iam::123456789012:role/adp-dev-trusted-build", True),
    ],
)
def test_build_action_refuses_missing_or_wrong_role_without_aws_calls(role, allowed):
    steps = build_action()["runs"]["steps"]
    result = run_guard(
        steps[0]["run"],
        DEPLOY_ROLE_ARN=role,
        GITHUB_REF="refs/heads/main",
        GITHUB_EVENT_NAME="workflow_dispatch",
        ACTIONS_ID_TOKEN_REQUEST_URL="fixture",
        ACTIONS_ID_TOKEN_REQUEST_TOKEN="fixture",
    )
    assert (result.returncode == 0) is allowed
    auth = steps[1]["with"]
    assert auth["unset-current-credentials"] is True
    assert auth["role-chaining"] is False
    assert auth["force-skip-oidc"] is False
    assert "aws-access-key-id" not in auth


@pytest.mark.parametrize("job", ["evaluate", "recover"])
@pytest.mark.parametrize(
    "role,allowed",
    [("", False), ("arn:aws:iam::123456789012:role/eval-orchestrator", True)],
)
def test_eval_missing_role_guard_runs_before_credentials(job, role, allowed):
    document = workflow("eval-cli-uplift.yml")
    assert document["jobs"][job]["environment"] == "${{ inputs.environment || 'dev' }}"
    steps = document["jobs"][job]["steps"]
    guard_at = next(
        i
        for i, step in enumerate(steps)
        if step.get("name") == "Require an explicit OIDC role (fail closed)"
    )
    auth_at = next(
        i
        for i, step in enumerate(steps)
        if "configure-aws-credentials@" in step.get("uses", "")
    )
    assert guard_at < auth_at
    assert (
        steps[guard_at]["env"]["ROLE_ARN"] == steps[auth_at]["with"]["role-to-assume"]
    )
    result = run_guard(steps[guard_at]["run"], ROLE_ARN=role)
    assert (result.returncode == 0) is allowed


def test_unknown_expiration_never_assumes_requested_three_hours(tmp_path):
    steps = workflow("eval-cli-uplift.yml")["jobs"]["evaluate"]["steps"]
    script = next(
        step["run"]
        for step in steps
        if step.get("name") == "Publish when these credentials expire"
    )
    output = tmp_path / "github-env"
    # Use this interpreter, without inheriting any AWS credential environment.
    before = datetime.datetime.now(datetime.UTC)
    result = run_guard(
        script,
        GITHUB_ENV=str(output),
        PATH=str(pathlib.Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
    )
    assert result.returncode == 0
    expiry = datetime.datetime.fromisoformat(
        output.read_text().strip().split("=", 1)[1].replace("Z", "+00:00")
    )
    assert 3595 <= (expiry - before).total_seconds() <= 3605
    output.write_text("")
    known = "2026-09-25T12:00:00Z"
    result = run_guard(script, GITHUB_ENV=str(output), AWS_CREDENTIAL_EXPIRATION=known)
    assert result.returncode == 0
    assert output.read_text().strip().endswith("=" + known)


def test_eval_oidc_trust_is_exact_dev_environment_and_sts_audience():
    import json

    policy = json.loads(
        (
            ROOT / "docs/evaluations/cli-uplift/orchestrator-trust-policy.json"
        ).read_text()
    )
    oidc = [
        statement
        for statement in policy["Statement"]
        if "Federated" in statement["Principal"]
    ]
    assert oidc == [
        {
            "Sid": "GithubOidcProtectedDev",
            "Effect": "Allow",
            "Principal": {
                "Federated": "arn:aws:iam::879318057152:oidc-provider/token.actions.githubusercontent.com"
            },
            "Action": "sts:AssumeRoleWithWebIdentity",
            "Condition": {
                "StringEquals": {
                    "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
                    "token.actions.githubusercontent.com:sub": "repo:aws-e/adp:environment:dev",
                }
            },
        }
    ]
