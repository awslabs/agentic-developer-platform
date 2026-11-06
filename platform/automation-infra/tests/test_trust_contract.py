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

source_pin_spec = importlib.util.spec_from_file_location(
    "gateway_source_pin", ROOT / ".github/scripts/tests/test_gateway_infra_reviewed_source.py"
)
source_pin = importlib.util.module_from_spec(source_pin_spec)
source_pin_spec.loader.exec_module(source_pin)
# Reuse the real-shell probes and restricted local Git/gh harness. These cases
# remain collected when Automation trust runs without the separate Script Tests.
local_history = source_pin.local_history
test_gateway_pin_missing_or_malformed = source_pin.test_missing_or_malformed_pin_stops_before_any_command
test_gateway_pin_mismatched_sources = source_pin.test_any_source_mismatch_blocks_including_old_ancestor
test_gateway_pin_missing_run_sha = source_pin.test_missing_run_sha_fails_closed
test_gateway_pin_matching_sources = source_pin.test_matching_reviewed_run_and_checkout_reach_deployment

ENVIRONMENT = {
    "can_admins_bypass": False,
    "protection_rules": [{"type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {"id": 1}}], "prevent_self_review": True}],
    "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True},
}


def test_valid_environment_is_accepted():
    cutover.verify_environment(ENVIRONMENT)


# Deploy/apply workflows run on the dedicated ARC pool so a rollout never queues
# behind CI shards; the pin lives in one place and is shared with the release tests.
RUNNER_POOLS = json.loads((ROOT / ".github/runner-pools.json").read_text())
DEPLOY_POOL_WORKFLOWS = frozenset(RUNNER_POOLS["arc-runner-deploy"])
DEPLOY_POOL_JOBS = RUNNER_POOLS["arc-runner-deploy-jobs"]


def assert_adapter_handoff_boundary(job):
    assert job["environment"] == "adp-tools-handoff-dev"
    assert job["if"] == (
        "github.ref == 'refs/heads/main' && github.event_name == 'workflow_dispatch' && "
        "github.run_attempt == 1 && inputs.mode == 'release' && inputs.environment == 'dev' && "
        "inputs.stage == 'tool-adapters' && inputs.adp_correlation != '' && "
        "(inputs.adapter_action == 'handoff' || inputs.adapter_action == 'rollback')"
    )
    assert job["env"]["ADP_TOOLS_DEPLOY_ROLE_ARN"] == "${{ vars.ADP_DEPLOY_ROLE_ARN }}"


def test_all_action_jobs_run_on_arc():
    paths = list((ROOT / ".github/workflows").glob("*.yml")) + list((ROOT / ".github/workflows").glob("*.yaml"))
    paths += [ROOT / relative for relative in (
        "modules/agent-factory/rules/workflows/agent-template.yml",
        "modules/agent-factory/rules/workflows/pm-notify-handler.yml",
        "modules/agent-factory/runner-infra/workflow-example.yml",
        "modules/agent-factory/client-workflows/.github/workflows/pr-review-trigger.yml",
    )]
    for path in paths:
        workflow = yaml.safe_load(path.read_text())
        for name, job in workflow["jobs"].items():
            if path.name == "automation-trust-ci.yml" and name == "public-workflow-contracts":
                assert job["runs-on"] == "${{ github.repository == 'aws-e/adp' && 'arc-runner-org' || 'ubuntu-latest' }}"
                assert job["permissions"] == {"contents": "read"}
                assert "environment" not in job
                assert "secrets." not in yaml.safe_dump(job)
                continue
            if path.name == "l5-services-release.yml" and name == "publish":
                # ARC has no daemon. Only Transform image publication may use
                # the isolated hosted builder; preserve its existing authority.
                assert job["runs-on"] == "${{ inputs.service == 'transform' && 'ubuntu-24.04' || 'arc-runner-org' }}"
                assert job["if"] == "github.ref == 'refs/heads/main'"
                assert job["environment"] == "adp-gateway-deploy-${{ inputs.environment }}"
                assert job["permissions"] == {"contents": "read", "id-token": "write"}
                assert job["timeout-minutes"] == 30
                continue
            if "uses" in job:  # Reusable workflows select their own ARC jobs.
                assert "runs-on" not in job
                continue
            if path.name in DEPLOY_POOL_WORKFLOWS or name in DEPLOY_POOL_JOBS.get(path.name, []):
                assert job["runs-on"] == "arc-runner-deploy", f"{path.name}/{name}: deploy workflows select the dedicated pool"
                continue
            assert job["runs-on"] == "arc-runner-org", f"{path.name}/{name}: every job must select arc-runner-org"


def test_product_recovery_pins_only_protected_switch_jobs_to_deployment_pool():
    name = "gateway-product-recovery.yml"
    workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    switch_jobs = {"quiesce", "baseline", "restore"}
    assert name not in DEPLOY_POOL_WORKFLOWS
    assert set(DEPLOY_POOL_JOBS[name]) == switch_jobs
    assert len(DEPLOY_POOL_JOBS[name]) == len(switch_jobs)
    assert set(workflow.get("on", workflow.get(True))) == {"workflow_call"}
    assert workflow["jobs"]["hold"]["if"] == "github.ref == 'refs/heads/main'"
    assert workflow["concurrency"]["cancel-in-progress"] is False
    assert switch_jobs <= workflow["jobs"].keys()
    for job_name, job in workflow["jobs"].items():
        assert job["permissions"] == {"contents": "read", "id-token": "write"}
        trusted = [step for step in job["steps"] if step.get("uses") == "./.github/actions/trusted-deployment"]
        if job_name in switch_jobs:
            assert job["runs-on"] == "arc-runner-deploy"
            assert job["environment"] == "adp-gateway-deploy-dev"
            assert len(trusted) == 1
            assert trusted[0]["with"]["role_arn"] == "${{ vars.ADP_DEPLOY_ROLE_ARN }}"
        else:
            assert job["runs-on"] == "arc-runner-org"
            assert job["environment"] == "dev"
            assert trusted == []
            evaluator = [step for step in job["steps"] if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@")]
            assert len(evaluator) == 1
            assert evaluator[0]["with"]["role-to-assume"] == "${{ secrets.AWS_CLI_UPLIFT_EVAL_ROLE_ARN }}"


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


def assert_gateway_reviewed_source(workflow, job, authority_index):
    """Gateway infra requires an exact source, stricter than ancestry alone."""
    assert set(workflow["jobs"]) == {"apply"}
    inputs = workflow.get("on", workflow.get(True))["workflow_dispatch"]["inputs"]
    pin = inputs["reviewed_source_sha"]
    assert pin["required"] is True and pin["type"] == "string"
    assert "default" not in pin
    steps = job["steps"]
    assert authority_index == 2
    assert steps[0]["uses"].startswith("actions/checkout@")
    assert "ref" not in steps[0].get("with", {})
    assert steps[3]["uses"] == "./.github/actions/load-deploy-config"
    assert not job.get("continue-on-error", False)
    guard = steps[1]
    assert guard["name"] == "Verify exact reviewed source before deployment"
    assert guard["shell"] == "bash"
    assert guard["env"] == {"REVIEWED_SOURCE_SHA": "${{ inputs.reviewed_source_sha }}"}
    assert "if" not in guard and not guard.get("continue-on-error", False)
    # Bind the placement check to the actual workflow step exercised by the
    # shared behavioral probes above, not a copied implementation snapshot.
    assert guard == source_pin.GUARD
    for step in steps[authority_index:]:
        if any(term in step.get("if", "") for term in ("always(", "failure(", "cancelled(")):
            assert step["name"] == "Summary"


# Workflows using a dedicated (non-deployment/build) trusted action are tested
# individually below rather than in this parametrized loop.
DEDICATED_ACTION_WORKFLOWS = {"webhook-code-deploy.yml"}

# Runner selection is shared; protected contexts and per-job identities remain mandatory.


@pytest.mark.parametrize("kind", ["deployment", "build"])
def test_privileged_jobs_have_protected_context_and_early_oidc(kind):
    inventory = json.loads((AUTOMATION / f"{kind}-workflows.json").read_text())
    assert inventory
    for name in inventory:
        if name in DEDICATED_ACTION_WORKFLOWS:
            continue
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        found = 0
        for job_name, job in workflow["jobs"].items():
            steps = job.get("steps", [])
            matching = [i for i, s in enumerate(steps) if s.get("uses") == f"./.github/actions/trusted-{kind}"]
            if not matching:
                continue
            found += 1
            assert len(matching) == 1, name
            assert "github.ref == 'refs/heads/main'" in job["if"], name
            deploy_pool = name in DEPLOY_POOL_WORKFLOWS or job_name in DEPLOY_POOL_JOBS.get(name, [])
            assert job["runs-on"] == ("arc-runner-deploy" if deploy_pool else "arc-runner-org"), name
            if kind == "deployment" and name == "gateway-deploy.yml" and job_name == "deploy-frontend":
                assert job["environment"].startswith("adp-frontend-deploy-"), name
                assert steps[matching[0]]["with"]["role_arn"] == "${{ vars.ADP_FRONTEND_DEPLOY_ROLE_ARN }}"
            elif kind == "deployment" and name == "superplane-k8s-deploy.yml":
                assert job["environment"].startswith("adp-skypilot-deploy-"), name
            elif kind == "deployment" and name == "agent-context-deploy.yml":
                assert job["environment"].startswith("adp-context-deploy-"), name
            elif kind == "deployment" and name in {"gitlab-infra-apply.yml", "gitlab-runners-apply.yml", "gitlab-runners-qualify.yml"}:
                assert job["environment"].startswith("adp-gitlab-deploy-"), name
            elif kind == "deployment" and name == "cyber-windows-image-build.yml":
                assert job["environment"] == "adp-windows-build-dev", name
            elif kind == "deployment" and name == "agent-worker-image.yml":
                assert job["environment"].startswith("adp-worker-deploy-"), name
            elif kind == "deployment" and name == "chat-agent-deploy.yml":
                assert job["environment"] == "adp-chat-deploy-dev", name
            elif kind == "deployment" and name in {"gateway-deploy.yml", "run-gateway-migrations.yml", "pricing-finalize.yml"}:
                assert job["environment"].startswith("adp-gateway-deploy-"), name
            elif kind == "deployment" and name == "dev-box-controller-deploy.yml":
                # The dev-box-controller is a gateway-namespace workload running the gateway
                # image; it is refreshed under the gateway deployment identity, whose role
                # variable is defined only on that environment.
                assert job["environment"].startswith("adp-gateway-deploy-"), name
            elif kind == "deployment" and (name, job_name) == ("tools-platform-deploy.yml", "adapter-handoff"):
                assert_adapter_handoff_boundary(job)
            elif kind == "deployment" and name in {"tools-platform-deploy.yml", "tools-facade-deploy.yml", "tools-interceptor-deploy.yml"}:
                # #7513: the tools platform releases under its own scoped identity.
                assert "adp-tools-deploy-" in job["environment"], name
            else:
                assert job["environment"].startswith("adp-deploy-" if kind == "deployment" else "adp-build-"), name
            assert job["permissions"]["id-token"] == "write", name
            if name == "gateway-infra-apply.yml":
                assert kind == "deployment"
                assert_gateway_reviewed_source(workflow, job, matching[0])
            elif name == "gitlab-infra-apply.yml":
                before_auth = steps[:matching[0]]
                checkout = [s for s in before_auth if s.get("uses", "").startswith("actions/checkout@")]
                assert len(checkout) == 1
                assert checkout[0]["with"] == {
                    "ref": "${{ inputs.adp_source_revision }}", "fetch-depth": 0, "persist-credentials": False,
                }
                guard = next(s for s in before_auth if s.get("name") == "Verify source belongs to reviewed main history")
                assert guard["env"]["ACCEPTED_REVISION"] == "${{ inputs.adp_source_revision }}"
                assert 'test "$(git rev-parse HEAD)" = "$ACCEPTED_REVISION"' in guard["run"]
                assert 'git merge-base --is-ancestor "$ACCEPTED_REVISION" refs/remotes/origin/main' in guard["run"]
                assert 'test "$WORKFLOW_REVISION" = "$ACCEPTED_DEFINITION"' in guard["run"]
                assert 'test "$ACCEPTED_DIGEST" = "$PROTECTED_DIGEST"' in guard["run"]
            elif name == "tools-interceptor-deploy.yml":
                assert any(s.get("name") == "Verify reviewed immutable source" and
                           'git merge-base --is-ancestor "$ADP_SOURCE" "refs/remotes/origin/$DEFAULT_BRANCH"' in s.get("run", "") and
                           'git show "$ADP_DEFINITION:.github/workflows/tools-interceptor-deploy.yml" | cmp' in s.get("run", "")
                           for s in steps[:matching[0]]), name
            elif (name, job_name) == ("tools-platform-deploy.yml", "adapter-handoff"):
                binding = next(s for s in steps[:matching[0]]
                               if s.get("name") == "Bind handoff implementation to the reviewed workflow")
                checkout = [s for s in steps[:matching[0]] if s.get("uses", "").startswith("actions/checkout@")]
                assert len(checkout) == 1
                assert checkout[0]["with"]["ref"] == "${{ github.workflow_sha }}"
                assert checkout[0]["with"]["persist-credentials"] is False
                assert job["env"]["ADP_DEFINITION_REVISION"] == "${{ github.workflow_sha }}"
                assert 'test "$(git rev-parse HEAD)" = "$ADP_DEFINITION_REVISION"' in binding["run"]
                assert 'git merge-base --is-ancestor "$ADP_SOURCE_REVISION" refs/remotes/origin/main' in binding["run"]
                assert 'git merge-base --is-ancestor "$ADP_SOURCE_REVISION" "$ADP_DEFINITION_REVISION"' in binding["run"]
                assert any(s.get("run") == "python3 modules/tools/adapters/guard_release.py"
                           for s in steps[:matching[0]])
            else:
                assert any(s.get("name") == "Verify source belongs to reviewed main history" and "git merge-base --is-ancestor HEAD FETCH_HEAD" in s.get("run", "") for s in steps[:matching[0]]), name
            for step in steps:
                assert "sudo " not in step.get("run", ""), name
                assert not re.search(r"\$\{\{\s*(?:inputs\.|github\.event\.inputs\.)", step.get("run", "")), name
            for step in steps[:matching[0]]:
                assert not re.search(r"\b(aws|terraform|kubectl)\s", step.get("run", "")), f"{name}: cloud operation before trusted identity"
                if "uses" in step:
                    assert (step["uses"].startswith("actions/checkout@") or
                            (name == "tools-interceptor-deploy.yml" and step["uses"].startswith("actions/setup-python@"))), name
        assert found, name


def test_native_input_runner_delivery_is_exact_and_deployment_setup_follows_oidc():
    workflow = yaml.safe_load((ROOT / ".github/workflows/tools-platform-deploy.yml").read_text())
    job = workflow["jobs"]["release"]
    steps = job["steps"]
    native = next(i for i, step in enumerate(steps)
                  if step.get("name") == "Receive original owner approval and explicit native fixture")
    identity = next(i for i, step in enumerate(steps)
                    if step.get("uses") == "./.github/actions/trusted-deployment")
    setup = next(i for i, step in enumerate(steps)
                 if step.get("uses", "").startswith("hashicorp/setup-terraform@"))
    apply = next(i for i, step in enumerate(steps)
                 if step.get("run") == "bash modules/tools/deploy.sh --infra --targets")
    # This existing-runner read is deliberately before the protected deployment
    # role. It is not a general exception for cloud setup or owner credentials.
    assert steps[native] == {
        "name": "Receive original owner approval and explicit native fixture",
        "if": "env.ADP_TOOLS_RELEASE_QUALIFICATION == 'native'",
        "env": {"ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN": "${{ secrets.ADP_TOOLS_SMOKE_NATIVE_ACCESS_TOKEN }}"},
        "run": "python3 -m modules.tools.platform.scripts.native_smoke_inputs download",
    }
    assert native < identity < setup < apply
    operational = next(i for i, step in enumerate(steps)
                       if step.get("name") == "Validate protected operational policy and dedicated ordinary-user login")
    assert steps[operational] == {
        "name": "Validate protected operational policy and dedicated ordinary-user login",
        "if": "env.ADP_TOOLS_RELEASE_QUALIFICATION == 'operational'",
        "env": {
            "ADP_TOOLS_OPERATIONAL_SMOKE_JSON": "${{ vars.ADP_TOOLS_OPERATIONAL_SMOKE_JSON }}",
            "ADP_TOOLS_SMOKE_OPERATIONAL_ACCESS_TOKEN": "${{ secrets.ADP_TOOLS_SMOKE_OPERATIONAL_ACCESS_TOKEN }}",
        },
        "run": "python3 -m modules.tools.platform.scripts.operational_release",
    }
    assert operational < identity
    assert job["env"]["ADP_TOOLS_RELEASE_QUALIFICATION"] == "${{ vars.ADP_TOOLS_RELEASE_QUALIFICATION || 'native' }}"
    assert any(step.get("name") == "Verify source belongs to reviewed main history"
               for step in steps[:native])


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


@pytest.mark.parametrize("kind", ["deployment", "build", "scan", "rules", "checks", "webhook-code"])
def test_credential_action_cannot_fall_back_to_irsa(kind):
    action = yaml.safe_load((ROOT / f".github/actions/trusted-{kind}/action.yml").read_text())
    config = next(s["with"] for s in action["runs"]["steps"] if s.get("uses", "").startswith("aws-actions/configure-aws-credentials@"))
    assert config["unset-current-credentials"] is True
    assert config["force-skip-oidc"] is False and config["role-chaining"] is False


def test_webhook_code_workflow_uses_dedicated_identity_and_main_guard():
    workflow = yaml.safe_load((ROOT / ".github/workflows/webhook-code-deploy.yml").read_text())
    job = workflow["jobs"]["deploy-code"]
    assert "github.ref == 'refs/heads/main'" in job["if"]
    assert job["runs-on"] == "arc-runner-org"
    assert job["environment"] == "adp-webhook-code-dev"
    assert "github.run_attempt == 1" in job["if"]
    # Permissions may be at workflow level or job level
    permissions = job.get("permissions", workflow.get("permissions", {}))
    assert permissions["id-token"] == "write"
    steps = job["steps"]
    # Verify source ancestry before credentials
    ancestry = [i for i, s in enumerate(steps) if s.get("name") == "Prepare clean committed source"]
    assert len(ancestry) == 1
    assert "prepare-webhook-source.py" in steps[ancestry[0]].get("run", "")
    # Uses dedicated webhook-code action, not the generic deployment action
    oidc = [i for i, s in enumerate(steps) if s.get("uses") == "./.github/actions/trusted-webhook-code"]
    assert len(oidc) == 1
    assert ancestry[0] < oidc[0], "Source verification must precede credential exchange"
    # No cloud operations before the trusted identity step
    for step in steps[:oidc[0]]:
        assert not re.search(r"\b(aws|terraform|kubectl)\s", step.get("run", "")), "Cloud operation before trusted identity"
        if "uses" in step:
            assert step["uses"].startswith(("actions/checkout@", "actions/setup-python@")) or step["uses"] == "./.github/actions/trusted-webhook-code"
    # No sudo or untrusted input interpolation
    for step in steps:
        assert "sudo " not in step.get("run", "")
        assert not re.search(r"\$\{\{\s*(?:inputs\.|github\.event\.inputs\.)", step.get("run", ""))
    # No Terraform or kubectl in the entire workflow — code-only
    for step in steps:
        assert not re.search(r"\b(terraform|kubectl)\s", step.get("run", "")), "Webhook-code workflow must not use Terraform or kubectl"


def assert_frontend_guards_precede_identity(steps):
    identity = next(index for index, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-deployment")
    source = [index for index, step in enumerate(steps) if step.get("name") == "Verify reviewed main source"]
    approval = [index for index, step in enumerate(steps) if step.get("name") == "Guard assistant frontend deployment"]
    assert len(source) == len(approval) == 1
    assert source[0] < approval[0] < identity
    assert "git merge-base --is-ancestor HEAD FETCH_HEAD" in steps[source[0]]["run"]
    assert "set -euo pipefail" in steps[source[0]]["run"]
    assert steps[approval[0]]["run"].strip() == 'bash "$RUNNER_TEMP/adp-frontend-assistant-guard.sh"'
    for index in (source[0], approval[0]):
        assert "if" not in steps[index]
        assert not steps[index].get("continue-on-error", False)


@pytest.mark.parametrize("guard_name", ["Verify reviewed main source", "Guard assistant frontend deployment"])
@pytest.mark.parametrize("mutation", ["missing", "after-identity", "conditional", "ignore-failure"])
def test_frontend_guards_cannot_be_removed_delayed_or_bypassed(guard_name, mutation):
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-frontend-deploy.yml").read_text())
    steps = workflow["jobs"]["publish"]["steps"]
    guard = next(step for step in steps if step.get("name") == guard_name)
    if mutation == "missing":
        steps.remove(guard)
    elif mutation == "after-identity":
        steps.remove(guard)
        steps.append(guard)
    elif mutation == "conditional":
        guard["if"] = "false"
    else:
        guard["continue-on-error"] = True
    with pytest.raises(AssertionError):
        assert_frontend_guards_precede_identity(steps)


def test_frontend_publisher_has_separate_main_only_authority():
    workflow = yaml.safe_load((ROOT / ".github/workflows/gateway-frontend-deploy.yml").read_text())
    triggers = workflow.get("on", workflow.get(True))
    assert triggers["push"]["branches"] == ["main"]
    assert "modules/gateway/frontend/**" in triggers["push"]["paths"]
    job = workflow["jobs"]["publish"]
    assert job["if"] == "github.ref == 'refs/heads/main'"
    assert job["environment"].startswith("adp-frontend-deploy-")
    assert job["permissions"]["id-token"] == "write"
    steps = job["steps"]
    identity = next(i for i, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-deployment")
    assert steps[identity]["with"]["role_arn"] == "${{ vars.ADP_FRONTEND_DEPLOY_ROLE_ARN }}"
    assert_frontend_guards_precede_identity(steps)
    assert not any(re.search(r"\baws\s", step.get("run", "")) for step in steps[:identity])
    assert not any(re.search(r"\b(terraform|kubectl)\s", step.get("run", "")) for step in steps)
    gateway = yaml.safe_load((ROOT / ".github/workflows/gateway-deploy.yml").read_text())
    assert "modules/gateway/frontend/**" not in gateway.get("on", gateway.get(True))["push"]["paths"]
    assert "github.event_name == 'workflow_dispatch'" in gateway["jobs"]["deploy-frontend"]["if"]


def test_gateway_release_uses_namespaced_identity_without_rotating_signing_secrets():
    gateway = yaml.safe_load((ROOT / ".github/workflows/gateway-deploy.yml").read_text())
    backend = gateway["jobs"]["deploy-backend"]
    assert backend["environment"].startswith("adp-gateway-deploy-")
    scripts = "\n".join(step.get("run", "") for step in backend["steps"])
    assert "kubectl get serviceaccount default -n" in scripts
    assert "kubectl get namespace" not in scripts
    assert "aws secretsmanager create-secret" not in scripts
    assert "aws secretsmanager put-secret-value" not in scripts
    assert "ensure-signing-secret.py" not in scripts
    assert scripts.count("aws secretsmanager get-secret-value") == 3


def test_scan_jobs_keep_scoped_identity_and_no_schedule():
    for name in ["security-scan.yml", "security-agent-nightly.yml"]:
        raw = (ROOT / ".github/workflows" / name).read_text()
        workflow = yaml.safe_load(raw)
        assert "schedule" not in workflow.get("on", workflow.get(True, {}))
        for job in workflow["jobs"].values():
            if str(job.get("environment", "")).startswith("adp-scan-"):
                assert job["permissions"]["id-token"] == "write"
                assert any(s.get("uses") == "awslabs/agentic-developer-platform/.github/actions/trusted-scan@1c750b906b46b7806a2363a5e47dcfed92fac918" for s in job["steps"])
        if name == "security-scan.yml":
            assert "adp-dev-agent-runner-role" not in raw


def test_tools_workflows_are_exactly_admitted_and_unlisted_workflow_is_rejected():
    inventory = json.loads((AUTOMATION / "deployment-workflows.json").read_text())
    tools = {"tools-facade-deploy.yml", "tools-platform-deploy.yml", "tools-interceptor-deploy.yml"}
    assert {name for name in inventory if name.startswith("tools-")} == tools
    assert len(inventory) == len(set(inventory))
    assert "tools-unlisted-deploy.yml" not in inventory
    for name in tools:
        workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
        protected = [(job_name, job) for job_name, job in workflow["jobs"].items()
                     if any(step.get("uses") == "./.github/actions/trusted-deployment"
                            for step in job.get("steps", []))]
        assert protected
        for job_name, job in protected:
            if (name, job_name) == ("tools-platform-deploy.yml", "adapter-handoff"):
                assert_adapter_handoff_boundary(job)
            else:
                assert job["environment"] in {
                    "adp-tools-deploy-dev",
                    "adp-tools-deploy-${{ inputs.environment || 'dev' }}",
                    "${{ inputs.environment == 'prod' && 'adp-tools-deploy-prod' || 'adp-tools-deploy-dev' }}",
                }
            assert "github.ref == 'refs/heads/main'" in job["if"]
            trusted = next(step for step in job["steps"] if step.get("uses") == "./.github/actions/trusted-deployment")
            assert trusted["with"]["role_arn"] == "${{ vars.ADP_DEPLOY_ROLE_ARN }}"

    workflow_refs = [f"aws-e/adp/.github/workflows/{name}@refs/heads/main" for name in inventory]
    runner_group = {"visibility": "selected", "restricted_to_workflows": True,
                    "selected_workflows": workflow_refs}
    cutover.verify_group(runner_group, workflow_refs, 1)
    with pytest.raises(AssertionError, match="allowlist differs"):
        cutover.verify_group(runner_group | {
            "selected_workflows": workflow_refs + ["aws-e/adp/.github/workflows/tools-unlisted-deploy.yml@refs/heads/main"]
        }, workflow_refs, 1)


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
    assert "awslabs/agentic-developer-platform/.github/actions/trusted-rules@1c750b906b46b7806a2363a5e47dcfed92fac918" in actions
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


def expanded_steps(steps, seen=()):
    """Inspect app-owned local composites as part of their calling workflow."""
    for step in steps:
        yield step
        use = step.get('uses', '')
        if not use.startswith('./'):
            continue
        path = (ROOT / use.removeprefix('./') / 'action.yml').resolve()
        if not path.exists():
            continue  # Checkouts under an alternate workspace are external input.
        assert ROOT in path.parents and path not in seen, f'Invalid composite graph: {path}'
        action = yaml.safe_load(path.read_text())
        if action.get('runs', {}).get('using') == 'composite':
            yield from expanded_steps(action['runs']['steps'], (*seen, path))


def test_cyber_composites_preserve_trusted_runner_execution_contract():
    for name in ['cyber-infra-apply', 'cyber-infra-plan', 'cyber-windows-image-build', 'cyber-worker-build', 'cyber-k8s-deploy']:
        workflow = yaml.safe_load((ROOT / f'.github/workflows/{name}.yml').read_text())
        for job in workflow['jobs'].values():
            if not str(job.get('environment', '')).startswith(('adp-deploy-', 'adp-build-')):
                continue
            for step in expanded_steps(job['steps']):
                script = step.get('run', '')
                assert not re.search(r'\b(sudo|docker)\s', script), f'{name}/{step.get("name")}: host runtime/privilege'
                assert not re.search(r'\$\{\{\s*(?:inputs\.|github\.event\.inputs\.)', script), f'{name}: interpolated dispatch input'


def test_every_security_ledger_job_assumes_scan_identity_before_aws():
    workflow = yaml.safe_load((ROOT / '.github/workflows/security-agent-nightly.yml').read_text())
    for name in ['code-review', 'scan_gate', 'triage', 'deliver']:
        job = workflow['jobs'][name]
        assert job['environment'].startswith('adp-scan-')
        assert job['permissions']['id-token'] == 'write'
        index = next(i for i, s in enumerate(job['steps']) if s.get('uses') == 'awslabs/agentic-developer-platform/.github/actions/trusted-scan@1c750b906b46b7806a2363a5e47dcfed92fac918')
        assert not any(re.search(r'\baws\s', s.get('run', '')) for s in job['steps'][:index])


def test_post_deploy_and_scheduled_checks_have_independent_credentials():
    checks_action = 'awslabs/agentic-developer-platform/.github/actions/trusted-checks@1c750b906b46b7806a2363a5e47dcfed92fac918'
    smoke = yaml.safe_load((ROOT / '.github/workflows/gateway-smoke.yml').read_text())['jobs']['smoke']
    live = yaml.safe_load((ROOT / '.github/workflows/gateway-live-tests.yml').read_text())['jobs']['live']
    for job in (smoke, live):
        assert job['if'] == "github.ref == 'refs/heads/main'"
        assert job['permissions']['id-token'] == 'write'
        index = next(i for i, step in enumerate(job['steps']) if step.get('uses') == checks_action)
        assert not any(re.search(r'\baws\s', step.get('run', '')) for step in job['steps'][:index])
    assert smoke['environment'].startswith('adp-checks-')
    selected = "inputs.product_cases != '' && inputs.product_cases != 'none'"
    ordinary = "inputs.product_cases == '' || inputs.product_cases == 'none'"
    assert live['environment'] == (
        "${{ (" + selected + ") && 'dev' || "
        "format('adp-checks-{0}', inputs.target_environment || 'dev') }}"
    )
    product_guard = next(step for step in live['steps'] if step.get('name') == 'Require protected Product evaluator configuration')
    assert product_guard['if'] == selected
    assert product_guard['env']['PRODUCT_TARGET_ENV'] == "${{ inputs.target_environment || 'dev' }}"
    assert '[ "$PRODUCT_TARGET_ENV" != dev ]' in product_guard['run']
    assert live['steps'].index(product_guard) < next(
        i for i, step in enumerate(live['steps']) if step.get('name') == 'Assume scoped Product evaluator identity'
    )
    checks_step = next(step for step in live['steps'] if step.get('uses') == checks_action)
    evaluator = next(step for step in live['steps'] if step.get('name') == 'Assume scoped Product evaluator identity')
    assert checks_step['if'] == ordinary
    assert evaluator['if'] == selected
    assert evaluator['with']['role-to-assume'] == '${{ secrets.AWS_CLI_UPLIFT_EVAL_ROLE_ARN }}'
    assert evaluator['with']['unset-current-credentials'] is True
    assert evaluator['with']['role-chaining'] is False
    assert next(step for step in live['steps'] if step.get('id') == 'product')['if'] == selected
    assert next(step for step in live['steps'] if step.get('id') == 'tests')['if'] == ordinary
    assert not any(re.search(r'\baws\s', step.get('run', '')) for step in live['steps'][:live['steps'].index(evaluator)])
    callers = yaml.safe_load((ROOT / '.github/workflows/gateway-deploy.yml').read_text())['jobs']
    for name in ['smoke-test', 'run-migrations']:
        caller = callers[name]
        callee = yaml.safe_load((ROOT / caller['uses']).read_text())
        # GitHub rejects the whole workflow before any job starts when a
        # reusable job requests OIDC beyond its caller's permissions.
        assert any(job.get('permissions', {}).get('id-token') == 'write' for job in callee['jobs'].values())
        assert caller['permissions']['id-token'] == 'write', name
        assert caller['permissions']['contents'] == 'read', name


def test_github_agents_use_repository_tracking_without_shared_beads_or_skypilot():
    names = ['developer', 'pm', 'operations', 'reviewer', 'architect', 'pt-superpower', 'product']
    for name in names:
        workflow = yaml.safe_load((ROOT / f'.github/workflows/agent-{name}.yml').read_text())
        steps = [s for job in workflow['jobs'].values() for s in job.get('steps', [])]
        assert all('setup-beads' not in s.get('uses', '') for s in steps)
        assert any(s.get('env', {}).get('BEADS_ENABLED') == 'false' for s in steps)
        assert not any(re.search(r'\baws (?:ssm|eks)\s|\bsky api login\b', s.get('run', '')) for s in steps)
    skill = (ROOT / '.github/workflows/skill-agent.yml').read_text()
    assert 'sky api login' not in skill and 'aws eks update-kubeconfig' not in skill


def test_shared_gateway_build_and_app_artifact_ownership():
    action = yaml.safe_load((ROOT / 'modules/domain-apps/cyber/ci/cyber-worker-build/action.yml').read_text())
    assert not any('docker run' in s.get('run', '') for s in action['runs']['steps'])
    buildspec = (ROOT / 'modules/domain-apps/cyber/codebuild/bs-cyber-worker.yml').read_text()
    assert 'docker run --rm --network none' in buildspec
    assert 'worker-manifests/by-tag/${IMAGE_TAG}.json' in buildspec
    descriptor = json.loads((ROOT / 'modules/domain-apps/cyber/codebuild/projects.json').read_text())
    assert descriptor['cyber-worker']['artifact_writes'] == [{'bucket_suffix': 'cape-assets', 'prefix': 'worker-manifests'}]
    assert 'artifact_writes' not in descriptor['cyber-browser']


def assert_selected_connection_job(job):
    assert job['if'] == "github.ref == 'refs/heads/main'"
    assert job['environment'] == 'dev-box-${{ inputs.environment }}'
    steps = list(expanded_steps(job['steps']))
    guard = steps[0]
    assert 'if' not in guard and not guard.get('continue-on-error', False)
    for variable, binding in {
        'SELECTED_LABEL': '${{ inputs.customer_aws_label }}',
        'APPROVED_LABEL': '${{ vars.ADP_DEV_BOX_CONNECTION_LABEL }}',
        'TARGET_ACCOUNT': '${{ inputs.customer_account_id }}',
        'APPROVED_ACCOUNT': '${{ vars.ADP_DEV_BOX_ACCOUNT_ID }}',
    }.items():
        assert guard['env'][variable] == binding
    for check in (
        'set -euo pipefail',
        'test -n "$SELECTED_LABEL"',
        'test "$SELECTED_LABEL" = "$APPROVED_LABEL"',
        'test -n "$APPROVED_ACCOUNT"',
        'test "$TARGET_ACCOUNT" = "$APPROVED_ACCOUNT"',
    ):
        assert check in guard['run'].splitlines()
    for step_index, step in enumerate(steps):
        script = step.get('run', '').replace('\\\n', ' ')
        for call in re.finditer(r'\baws\s+[a-z][a-z0-9-]+\s+[a-z][a-z0-9-]+', script):
            assert step_index > 0
            assert step['env']['ADP_CONNECTION_LABEL'] == '${{ inputs.customer_aws_label }}'
            prefix = script[:call.start()].rsplit('\n', 1)[-1].strip()
            assert prefix == 'adp-cred assume --service aws --label "$ADP_CONNECTION_LABEL" --exec'


@pytest.mark.parametrize('mutation', [
    'ambient-upload', 'mixed-upload', 'wrong-label', 'unapproved-label',
    'skipped-guard', 'unprotected', 'non-main',
])
def test_selected_connection_authority_rejects_weakened_binding(mutation):
    workflow = yaml.safe_load((ROOT / '.github/workflows/dev-box-deploy.yml').read_text())
    job = workflow['jobs']['deploy']
    guard = job['steps'][0]
    upload = next(step for step in job['steps'] if step.get('name', '').startswith('Publish immutable'))
    if mutation == 'ambient-upload':
        upload['run'] = upload['run'].replace(
            'adp-cred assume --service aws --label "$ADP_CONNECTION_LABEL" --exec ', '',
        )
    elif mutation == 'mixed-upload':
        upload['run'] += '\naws s3api put-object --bucket example --key example --body example\n'
    elif mutation == 'wrong-label':
        upload['env']['ADP_CONNECTION_LABEL'] = 'unapproved'
    elif mutation == 'unapproved-label':
        guard['run'] = guard['run'].replace('test "$SELECTED_LABEL" = "$APPROVED_LABEL"', 'true')
    elif mutation == 'skipped-guard':
        guard['if'] = 'false'
    elif mutation == 'unprotected':
        job['environment'] = 'unprotected'
    else:
        job['if'] = 'true'
    with pytest.raises(AssertionError):
        assert_selected_connection_job(job)


def ordinary_cloud_inventory():
    result = {}
    for path in sorted((ROOT / '.github/workflows').glob('*.y*ml')):
        workflow = yaml.safe_load(path.read_text())
        for name, job in workflow.get('jobs', {}).items():
            steps = list(expanded_steps(job.get('steps', [])))
            if any('trusted-' in s.get('uses', '') for s in steps):
                continue
            calls = sorted(set(re.findall(r'\baws\s+([a-z][a-z0-9-]+)\s+([a-z][a-z0-9-]+)', '\n'.join(s.get('run', '') for s in steps))))
            if not calls:
                continue
            credential = next((i for i, s in enumerate(steps) if 'configure-aws-credentials' in s.get('uses', '')), None)
            first_cloud = next(i for i, s in enumerate(steps) if re.search(r'\baws\s+[a-z][a-z0-9-]+\s+[a-z][a-z0-9-]+', s.get('run', '')))
            if credential is not None:
                assert credential < first_cloud, f'{path.name}/{name}: AWS operation precedes its operational identity'
            authority = 'existing-oidc' if credential is not None else 'runtime'
            if path.name == 'dev-box-deploy.yml' and name == 'deploy':
                assert credential is None
                assert_selected_connection_job(job)
                authority = 'selected-connection'
            result[f'{path.name}/{name}'] = {
                'authority': authority,
                'operations': [' '.join(call) for call in calls],
            }
    return result


def test_ordinary_cloud_operations_match_reviewed_inventory():
    # New ambient calls require an explicit authority decision; moved app-owned
    # composites are inspected too. Spawn-deploy's quoted examples are retained
    # in the inventory (they do not execute), rather than silently filtered out.
    expected = json.loads((AUTOMATION / 'ordinary-workflow-aws.json').read_text())
    assert ordinary_cloud_inventory() == expected
    for job, record in expected.items():
        if record['authority'] == 'runtime' and job != 'spawn-deploy-instance.yml/spawn':
            assert set(record['operations']) <= {
                'secretsmanager get-secret-value',  # exact retained GitHub transport inputs
                'sts get-caller-identity', 's3 cp',  # isolated gateway PR source
                'codebuild batch-get-builds', 'codebuild batch-get-projects', 'codebuild start-build',
                'codebuild stop-build',  # same exact gateway project; failed dispatch cleanup
            }, job


def test_tools_smoke_build_preserves_nonpublishing_gateway_pr_boundary():
    workflow = yaml.safe_load((ROOT / '.github/workflows/tools-ci.yml').read_text())
    job = workflow['jobs']['smoke-build']
    assert job.get('permissions', workflow['permissions']) == {'contents': 'read'}
    assert 'environment' not in job
    build = next(step for step in job['steps'] if step.get('uses') == './.github/actions/codebuild-run')
    assert build['with'] == {
        'project_name': 'adp-dev-gateway-build',
        'region': 'us-east-1',
        'state_bucket': 'adp-terraform-state-${{ steps.identity.outputs.id }}',
        'source_namespace': 'adp-dev-gateway-build-pr',
        'service_role_arn': 'arn:aws:iam::${{ steps.identity.outputs.id }}:role/adp-dev-codebuild-gateway-pr',
        'buildspec': 'codebuild/bs-tools-facade-smoke.yml',
    }


def test_generated_workflow_job_uses_org_pool():
    source = (ROOT / "modules/agent-factory/runner-infra/scripts/add-auto-fix-to-repo.sh").read_text()
    generated = source.split("AUTO_FIX_JOB=$(cat << 'EOF'\n", 1)[1].split("\nEOF", 1)[0]
    workflow = yaml.safe_load("jobs:\n" + generated.split("    steps:", 1)[0])
    assert workflow["jobs"]["auto-fix-on-failure"]["runs-on"] == "arc-runner-org"
    onboarding = (ROOT / "modules/agent-factory/runner-infra/scripts/onboard-repo.sh").read_text()
    assert 'echo "  runs-on: arc-runner-org"' in onboarding


def test_superplane_automation_definitions_are_app_owned_with_state_moves():
    app = ROOT / 'modules/domain-apps/superplane/infra/automation'
    wrapper = (AUTOMATION / 'skypilot-deployment.tf').read_text()
    resources = {
        'aws_iam_role', 'aws_iam_role_policy',
        'aws_eks_access_entry', 'aws_eks_access_policy_association',
    }
    assert 'resource "' not in wrapper
    assert '../../modules/domain-apps/superplane/infra/automation/skypilot-deployment' in wrapper
    moves = re.findall(r'moved\s*\{\s*from\s*=\s*(\S+)\s+to\s*=\s*(\S+)\s*\}', wrapper)
    assert set(moves) == {
        (f'{kind}.skypilot_deployment',
         f'module.superplane_skypilot_deployment.{kind}.skypilot_deployment')
        for kind in resources
    }
    owned = (app / 'skypilot-deployment/main.tf').read_text()
    assert set(re.findall(r'resource "([^"]+)" "[^"]+"', owned)) == resources
    build = (AUTOMATION / 'build-dispatch.tf').read_text()
    assert 'module.superplane_build_dispatch.policy_statements' in build
    assert 'condition     = module.superplane_build_dispatch.publisher_identity_valid' in build
    assert 'superplane/releases/' not in build
    assert 'Sid      = "Superplane' not in build
    assert 'resource "' not in (app / 'build-dispatch/main.tf').read_text()
    workflow = yaml.safe_load((ROOT / '.github/workflows/automation-trust-ci.yml').read_text())
    triggers = workflow.get('on', workflow.get(True))
    assert 'modules/domain-apps/superplane/infra/automation/**' in triggers['pull_request']['paths']


@pytest.mark.parametrize("event,ref,attempt,accepted", [
    ("push", "refs/heads/main", "1", True),
    ("workflow_dispatch", "refs/heads/main", "1", True),
    ("workflow_call", "refs/heads/main", "1", True),
    ("pull_request", "refs/heads/main", "1", False),
    ("pull_request_target", "refs/heads/main", "1", False),
    ("push", "refs/heads/feature", "1", False),
    ("push", "refs/tags/main", "1", False),
    ("push", "refs/heads/main", "2", False),
])
def test_webhook_context_guard_executes_for_reusable_push(event, ref, attempt, accepted):
    import os
    import subprocess

    action = yaml.safe_load((ROOT / ".github/actions/trusted-webhook-code/action.yml").read_text())
    result = subprocess.run(["bash", "-c", action["runs"]["steps"][0]["run"]], env={
        **os.environ,
        "GITHUB_EVENT_NAME": event, "GITHUB_REF": ref, "GITHUB_RUN_ATTEMPT": attempt,
        "WEBHOOK_CODE_ROLE_ARN": "arn:aws:iam::123456789012:role/adp-test-trusted-webhook-code",
        "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid/token",
        "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "test-placeholder",
    }, capture_output=True, text=True)
    assert (result.returncode == 0) is accepted
