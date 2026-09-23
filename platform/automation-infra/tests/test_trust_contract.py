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


@pytest.mark.parametrize("kind", ["deployment", "build", "scan", "rules"])
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


def test_domain_deployments_and_live_evaluations_are_in_trusted_inventory():
    inventory = set(json.loads((AUTOMATION / "deployment-workflows.json").read_text()))
    assert {
        "_deploy-eks.yml", "cyber-k8s-deploy.yml", "cyber-windows-image-build.yml",
        "superplane-k8s-deploy.yml", "superplane-migrate.yml", "platform-deploy-mgmt-verify.yml",
        "eval-budget-ratelimit.yml", "eval-cli-onboarding.yml", "eval-bedrock-routing.yml",
        "credential-binding-adversarial-e2e.yml",
    } <= inventory


def test_untrusted_rule_compilation_has_only_rules_identity_off_deployment_nodes():
    workflow = yaml.safe_load((ROOT / ".github/workflows/yara-ingest-public.yml").read_text())
    job = workflow["jobs"]["ingest"]
    assert job["environment"] == "adp-rules-dev"
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert not isinstance(job["runs-on"], dict)
    actions = [s.get("uses", "") for s in job["steps"]]
    assert "aws-e/adp/.github/actions/trusted-rules@main" in actions
    assert not any("trusted-deployment" in a or "trusted-build" in a for a in actions)


@pytest.mark.parametrize("username", ["", "eval_operator"])
def test_database_probe_uses_configured_iam_user_without_master_secret(username, tmp_path):
    import os
    import subprocess

    calls = tmp_path / "calls"
    script = '''
source platform/evals/lib/aws.sh
die() { exit 9; }
mask() { :; }
h_aws() { printf '%s\\n' "$*" >> "$CALLS"; printf 'test-token'; }
resolve_db_creds
test "$PGUSER" = "$ADP_DB_USER"
test "$PGSSLMODE" = require
'''
    result = subprocess.run(["bash", "-c", script], cwd=ROOT, env={
        "PATH": os.environ["PATH"], "ADP_DB_USER": username,
        "RDS_HOST": "db.test", "RDS_DB": "test", "AWS_REGION": "us-east-1", "CALLS": str(calls),
    })
    if username:
        assert result.returncode == 0
        assert calls.read_text().startswith("rds generate-db-auth-token ")
        assert "--username eval_operator" in calls.read_text()
        assert "secretsmanager" not in calls.read_text()
    else:
        assert result.returncode == 9
        assert not calls.exists()
