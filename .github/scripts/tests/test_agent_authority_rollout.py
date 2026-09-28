"""Issue #5365: enabling protected agent authority must be deliberate and verified.

The guard under test decides whether a deploy run may flip
`agent_authority_enabled`, and refuses to do so on an image identity it has not
confirmed with the registry. These exercise the real script with a stub `aws` on
PATH, so the refusals are the script's own, not a mock's.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/scripts/resolve-agent-authority-rollout.sh"
WORKFLOW = ROOT / ".github/workflows/webhook-ingress-deploy.yml"
REAL = "sha256:" + "a" * 64
OTHER = "sha256:" + "b" * 64


@pytest.fixture
def workspace(tmp_path):
    """A tfvars file plus a stub ECR that knows about exactly two images."""
    tfvars = tmp_path / "terraform.tfvars"
    tfvars.write_text("enable_agent_otel = true\n")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "aws"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# Stub registry: resolves only the digests this fixture published.\n"
        'for arg in "$@"; do\n'
        '  case "$arg" in\n'
        f'    imageDigest={REAL}) echo "{REAL}"; exit 0 ;;\n'
        f'    imageDigest={OTHER}) echo "{OTHER}"; exit 0 ;;\n'
        "  esac\n"
        "done\n"
        'echo "ImageNotFoundException" >&2\n'
        "exit 254\n"
    )
    stub.chmod(0o755)
    return tmp_path


def run(workspace, **env):
    summary = workspace / "summary"
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=workspace,
        env={
            "PATH": f"{workspace / 'bin'}:{os.environ['PATH']}",
            "AWS_REGION": "us-east-1",
            "AGENT_AUTHORITY_TFVARS": "terraform.tfvars",
            "GITHUB_STEP_SUMMARY": str(summary),
            **env,
        },
        capture_output=True,
        text=True,
    )
    return result, summary.read_text() if summary.exists() else ""


def test_a_plain_push_neither_enables_nor_overrides(workspace):
    """The committed value stays authoritative when no rollout is requested."""
    result, summary = run(workspace)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""
    assert "remains disabled" in summary


def test_an_explicit_request_with_a_verified_digest_enables(workspace):
    result, summary = run(
        workspace,
        ENABLE_AGENT_AUTHORITY="true",
        AGENT_AUTHORITY_WORKER_IMAGE_DIGESTS=REAL,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "-var=agent_authority_enabled=true",
        f'-var=agent_authority_worker_image_digests=["{REAL}"]',
    ]
    assert "1 verified worker image digest" in summary


def test_enabling_without_a_digest_is_refused_not_invented(workspace):
    """The operator's constraint: do not invent an image digest.

    An enable request with nothing to admit would deploy an authority that
    admits no pod, so it fails here rather than silently at bootstrap time.
    """
    result, _ = run(workspace, ENABLE_AGENT_AUTHORITY="true")
    assert result.returncode != 0
    assert result.stdout.strip() == ""
    assert "will not invent one" in result.stderr


@pytest.mark.parametrize(
    "digest",
    [
        "latest",
        "adp-agent-runtime:latest",
        "sha256:" + "a" * 63,
        "sha256:" + "A" * 64,
        "sha512:" + "a" * 64,
    ],
)
def test_a_mutable_or_malformed_identity_is_refused(workspace, digest):
    """A tag is not an admission identity: it can change without a deploy."""
    result, _ = run(
        workspace,
        ENABLE_AGENT_AUTHORITY="true",
        AGENT_AUTHORITY_WORKER_IMAGE_DIGESTS=digest,
    )
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_a_well_formed_digest_that_is_not_a_real_image_is_refused(workspace):
    """The check Terraform cannot make.

    `sha256:c...` satisfies the variable's own regex, so Terraform would accept
    it and produce an authority that admits nothing. Only the registry knows.
    """
    absent = "sha256:" + "c" * 64
    result, _ = run(
        workspace,
        ENABLE_AGENT_AUTHORITY="true",
        AGENT_AUTHORITY_WORKER_IMAGE_DIGESTS=absent,
    )
    assert result.returncode != 0
    assert "Refusing to enable agent authority" in result.stderr


def test_one_unverifiable_digest_refuses_the_whole_set(workspace):
    """Partial verification must not enable a partially-trusted admission list."""
    result, _ = run(
        workspace,
        ENABLE_AGENT_AUTHORITY="true",
        AGENT_AUTHORITY_WORKER_IMAGE_DIGESTS=f"{REAL},sha256:{'d' * 64}",
    )
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_multiple_digests_are_sorted_and_deduplicated(workspace):
    """Matches the `set(string)` the variable declares, so replays do not churn."""
    result, _ = run(
        workspace,
        ENABLE_AGENT_AUTHORITY="true",
        AGENT_AUTHORITY_WORKER_IMAGE_DIGESTS=f" {OTHER}, {REAL} ,{OTHER}",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == (
        f'-var=agent_authority_worker_image_digests=["{REAL}","{OTHER}"]'
    )


def test_an_explicit_rollback_disables_a_committed_enable(workspace):
    """A deliberate way back off the feature, without editing tfvars under pressure."""
    (workspace / "terraform.tfvars").write_text("agent_authority_enabled = true\n")
    result, summary = run(workspace, ENABLE_AGENT_AUTHORITY="false")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "-var=agent_authority_enabled=false"
    assert "explicitly disabled" in summary


def test_a_committed_enable_still_reverifies_its_digests(workspace):
    """Enabling in tfvars does not exempt a run from proving the image exists."""
    (workspace / "terraform.tfvars").write_text("agent_authority_enabled = true\n")
    result, _ = run(workspace)
    assert result.returncode != 0
    assert "will not invent one" in result.stderr


@pytest.mark.parametrize("requested", ["yes", "TRUE", "1", "maybe"])
def test_an_unparseable_request_fails_closed(workspace, requested):
    result, _ = run(
        workspace,
        ENABLE_AGENT_AUTHORITY=requested,
        AGENT_AUTHORITY_WORKER_IMAGE_DIGESTS=REAL,
    )
    assert result.returncode != 0
    assert result.stdout.strip() == ""


def test_missing_region_does_not_skip_registry_verification(workspace):
    result, _ = run(
        workspace,
        AWS_REGION="",
        ENABLE_AGENT_AUTHORITY="true",
        AGENT_AUTHORITY_WORKER_IMAGE_DIGESTS=REAL,
    )
    assert result.returncode != 0
    assert "AWS_REGION is required" in result.stderr


def test_dev_is_not_flipped_on_by_the_committed_configuration():
    """The operator's constraint: do not flip dev before its prerequisites.

    This module auto-applies on merge, so a committed `true` here would apply
    ahead of the step that publishes the worker image it depends on.
    """
    tfvars = (
        ROOT / "modules/agent-factory/webhook-ingress/infra/terraform.tfvars"
    ).read_text()
    assert "agent_authority_enabled = true" not in tfvars


def test_the_workflow_runs_the_guard_before_terraform_applies():
    """The guard is only a guard if apply cannot reach Terraform around it."""
    workflow = yaml.safe_load(WORKFLOW.read_text())
    # YAML 1.1 reads the `on:` key as the boolean True.
    triggers = workflow.get("on", workflow[True])
    dispatch = triggers["workflow_dispatch"]["inputs"]
    assert dispatch["enable_agent_authority"]["default"] == ""
    assert dispatch["enable_agent_authority"]["required"] is False
    assert "agent_authority_worker_image_digests" in dispatch

    steps = workflow["jobs"]["deploy-infra"]["steps"]
    names = [step.get("name") for step in steps]
    assert names.index("Resolve agent authority rollout") < names.index("Terraform Apply")
    guard = next(step for step in steps if step.get("id") == "authority")
    assert "resolve-agent-authority-rollout.sh" in guard["run"]
    assert guard["env"]["ENABLE_AGENT_AUTHORITY"] == (
        "${{ inputs.enable_agent_authority }}"
    )
    apply = next(step for step in steps if step.get("name") == "Terraform Apply")
    assert "${AUTHORITY_VARS}" in apply["run"]
    assert apply["env"]["AUTHORITY_VARS"] == "${{ steps.authority.outputs.vars }}"
