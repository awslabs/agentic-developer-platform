"""Public action dependencies must resolve without upstream private-repo access.

Credential helpers run before handling potentially untrusted scan source. Keep
them remote and immutable: a local action from that source changes the trust
boundary. Both repositories use the same reviewed public revision.
"""
from pathlib import Path
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
PUBLIC = "awslabs/agentic-developer-platform/.github/actions/"


def action_references(value):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "uses":
                yield child
            else:
                yield from action_references(child)
    elif isinstance(value, list):
        for child in value:
            yield from action_references(child)


def reference_error(reference):
    if not isinstance(reference, str):
        return "action reference must be a literal string"
    if reference.lower().startswith("aws-e/adp/") or reference.lower().startswith("aws-e/adp@"):
        return "public workflows cannot depend on the private upstream repository"
    if reference.startswith(PUBLIC):
        if not re.fullmatch(re.escape(PUBLIC) + r"trusted-(scan|checks|rules)@[0-9a-f]{40}", reference):
            return "remote credential helpers require a reviewed public commit SHA"
    return None


def test_all_workflows_and_composite_actions_are_portable():
    paths = list((ROOT / ".github/workflows").glob("*.y*ml"))
    paths += list((ROOT / ".github/actions").rglob("action.y*ml"))
    errors = []
    for path in paths:
        for reference in action_references(yaml.safe_load(path.read_text())):
            error = reference_error(reference)
            if error:
                errors.append(f"{path.relative_to(ROOT)}: {reference}: {error}")
    assert not errors, "\n".join(errors)


@pytest.mark.parametrize("reference", [
    "aws-e/adp/.github/actions/trusted-scan@main",
    "aws-e/adp/.github/workflows/reusable.yml@main",
    "aws-e/adp@" + "a" * 40,
    PUBLIC + "trusted-scan@main",
    PUBLIC + "trusted-checks@v1",
    PUBLIC + "trusted-rules@1234567",
    PUBLIC + "trusted-scan@${{ github.sha }}",
])
def test_rejects_private_or_mutable_action_references(reference):
    assert reference_error(reference)


def test_finds_reusable_workflows_and_composite_steps():
    document = yaml.safe_load("""
jobs:
  call:
    uses: aws-e/adp/.github/workflows/reusable.yml@main
runs:
  using: composite
  steps:
    - uses: aws-e/adp/.github/actions/trusted-scan@main
""")
    references = list(action_references(document))
    assert len(references) == 2
    assert all(reference_error(reference) for reference in references)


@pytest.mark.parametrize("kind", ["scan", "checks", "rules"])
@pytest.mark.parametrize("valid_ref,has_token,success", [
    (True, True, True), (False, True, False), (True, False, False),
])
def test_credential_guard_still_requires_main_and_oidc(kind, valid_ref, has_token, success):
    action = yaml.safe_load((ROOT / f".github/actions/trusted-{kind}/action.yml").read_text())
    guard = action["runs"]["steps"][0]["run"]
    # Execute only the context guard, never the AWS credential action.
    env = {
        "PATH": "/usr/bin:/bin",
        "GITHUB_REF": "refs/heads/main" if valid_ref else "refs/pull/1/merge",
        "GITHUB_EVENT_NAME": "workflow_dispatch",
        "DEPLOY_ROLE_ARN": f"arn:aws:iam::123456789012:role/adp-test-trusted-{kind}",
    }
    if has_token:
        env.update(ACTIONS_ID_TOKEN_REQUEST_URL="https://example.invalid", ACTIONS_ID_TOKEN_REQUEST_TOKEN="test")
    result = subprocess.run(["bash", "-c", guard], env=env, capture_output=True, text=True)
    assert (result.returncode == 0) is success


def test_portability_gate_runs_without_private_runners_or_credentials():
    workflow = yaml.safe_load((ROOT / ".github/workflows/automation-trust-ci.yml").read_text())
    job = workflow["jobs"]["public-workflow-contracts"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["permissions"] == {"contents": "read"}
    assert "environment" not in job
    assert "secrets." not in yaml.safe_dump(job)
    assert any("test_workflow_portability.py" in step.get("run", "") for step in job["steps"])
