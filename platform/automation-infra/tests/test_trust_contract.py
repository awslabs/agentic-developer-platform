"""Trust gates and workflow credential ordering; no cloud mutations."""
import copy
import importlib.util
import json
import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
AUTOMATION = ROOT / "platform/automation-infra"
spec = importlib.util.spec_from_file_location("cutover", AUTOMATION / "verify-cutover.py")
cutover = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cutover)

ENVIRONMENT = {
    "can_admins_bypass": False,
    "protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {"id": 1}}], "prevent_self_review": True}],
    "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
}


def test_valid_environment_is_accepted():
    cutover.verify_environment(ENVIRONMENT)


@pytest.mark.parametrize("mutation", ["bypass", "no-review", "self-review", "all-protected-branches"])
def test_weakened_environment_refused(mutation):
    env = copy.deepcopy(ENVIRONMENT)
    if mutation == "bypass":
        env["can_admins_bypass"] = True
    elif mutation == "no-review":
        env["protection_rules"] = []
    elif mutation == "self-review":
        env["protection_rules"][0]["prevent_self_review"] = False
    else:
        env["deployment_branch_policy"] = {"protected_branches": True, "custom_branch_policies": False}
    with pytest.raises(AssertionError):
        cutover.verify_environment(env)


@pytest.mark.parametrize("kind", ["deployment", "build"])
def test_privileged_jobs_have_protected_context_and_early_oidc(kind):
    inventory = json.loads((AUTOMATION / f"{kind}-workflows.json").read_text())
    assert inventory
    for name in inventory:
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        found = 0
        for job in workflow["jobs"].values():
            steps = job.get("steps", [])
            matching = [i for i, s in enumerate(steps) if s.get("uses") == f"./.github/actions/trusted-{kind}"]
            if not matching:
                continue
            found += 1
            assert len(matching) == 1, name
            assert "github.ref == 'refs/heads/main'" in job["if"], name
            assert job["runs-on"] == {"group": "adp-deployment", "labels": "arc-runner-deployment"}, name
            assert job["environment"].startswith("adp-deploy-" if kind == "deployment" else "adp-build-"), name
            assert job["permissions"]["id-token"] == "write", name
            assert any(s.get("name") == "Verify source belongs to reviewed main history" and "git merge-base --is-ancestor HEAD FETCH_HEAD" in s.get("run", "") for s in steps[:matching[0]]), name
            for step in steps:
                assert "sudo " not in step.get("run", ""), name
                assert not re.search(r"\$\{\{\s*(?:inputs\.|github\.event\.inputs\.)", step.get("run", "")), name
            for step in steps[:matching[0]]:
                assert not re.search(r"\b(aws|terraform|kubectl)\s", step.get("run", "")), f"{name}: cloud operation before trusted identity"
                if "uses" in step:
                    assert step["uses"].startswith("actions/checkout@"), name
        assert found, name


def test_runner_has_no_ambient_credential_and_uses_separate_nodes():
    values = yaml.safe_load((AUTOMATION / "runner-values.yaml").read_text())
    pod = values["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert pod["nodeSelector"] == {"adp.aws/trust": "deployment"}
    assert len(pod["containers"]) == 1
    assert pod["containers"][0]["securityContext"]["allowPrivilegeEscalation"] is False
    assert "volumes" not in pod  # no hostPath, credentials, Docker socket or token mounts
    docs = list(yaml.safe_load_all((AUTOMATION / "runner-isolation.yaml").read_text()))
    sa = next(d for d in docs if d["kind"] == "ServiceAccount")
    assert not sa.get("metadata", {}).get("annotations")
    assert sa["automountServiceAccountToken"] is False


@pytest.mark.parametrize("kind", ["deployment", "build", "scan"])
def test_credential_action_cannot_fall_back_to_irsa(kind):
    action = yaml.safe_load((ROOT / f".github/actions/trusted-{kind}/action.yml").read_text())
    config = next(s["with"] for s in action["runs"]["steps"] if s.get("uses", "").startswith("aws-actions/configure-aws-credentials@"))
    assert config["unset-current-credentials"] is True
    assert config["force-skip-oidc"] is False and config["role-chaining"] is False


def test_scan_jobs_keep_scoped_identity_and_no_schedule():
    for name in ["security-scan.yml", "security-agent-nightly.yml"]:
        raw = (ROOT / ".github/workflows" / name).read_text()
        workflow = yaml.safe_load(raw)
        assert "schedule" not in workflow.get("on", workflow.get(True, {}))
        for job in workflow["jobs"].values():
            if str(job.get("environment", "")).startswith("adp-scan-"):
                assert job["permissions"]["id-token"] == "write"
                assert any(s.get("uses") == "aws-e/adp/.github/actions/trusted-scan@main" for s in job["steps"])
        if name == "security-scan.yml":
            assert "adp-dev-agent-runner-role" not in raw
