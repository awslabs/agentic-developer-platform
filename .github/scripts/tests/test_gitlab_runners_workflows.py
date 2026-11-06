"""Cross-workflow authorization, secret handling and saved-plan regression checks."""
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ROOT / ".github/workflows"


def workflow(name):
    text = (WORKFLOWS / f"gitlab-runners-{name}.yml").read_text()
    # BaseLoader preserves GitHub's `on` key instead of treating it as a bool.
    return yaml.load(text, Loader=yaml.BaseLoader), text


@pytest.mark.parametrize("workflow_name,checks", [
    ("gitlab-infra-apply", {"source", "identity", "config", "tools", "plan", "safety", "apply"}),
    ("gitlab-runners-apply", {"source", "inputs", "identity", "config", "apply"}),
])
def test_apply_workflows_publish_generic_correlated_delivery_after_success(workflow_name, checks):
    document = yaml.load((WORKFLOWS / f"{workflow_name}.yml").read_text(), Loader=yaml.BaseLoader)
    inputs = document["on"]["workflow_dispatch"]["inputs"]
    for name in ("adp_correlation", "adp_source_revision", "adp_definition_revision", "adp_contract_digest"):
        assert inputs[name]["required"] == "true"
    steps = document["jobs"]["apply"]["steps"]
    publisher = next(step for step in steps if step.get("name") == "Publish verified operational delivery")
    assert all(step.get("if") == "always()" for step in steps[steps.index(publisher) + 1:])
    assert "publish-deployment-evidence.py delivery delivery-evidence" in publisher["run"]
    assert "delivery_evidence.py" in publisher["run"]
    assert publisher["env"]["ADP_WORKFLOW_REVISION"] == "${{ github.workflow_sha }}"
    outcomes = publisher["env"]["ADP_DELIVERY_STEP_OUTCOMES"]
    assert set(json.loads(re.sub(r"\$\{\{ steps\.\w+\.outcome \}\}", "success", outcomes))) == checks
    for check in checks:
        assert f'"{check}":"${{{{ steps.{check}.outcome }}}}"' in outcomes
        assert any(step.get("id") == check for step in steps[:-1])



def test_pull_request_checks_have_no_mutation_authority():
    check, source = workflow("check")
    assert "pull_request" in check["on"]
    assert check["permissions"] == {"contents": "read"}
    for job in check["jobs"].values():
        assert "environment" not in job
        assert "permissions" not in job
    for forbidden in ("secrets.", "id-token: write", "configure-aws-credentials", "terraform apply", "terraform destroy"):
        assert forbidden not in source
    for suite in ("test_gitlab_runners_workflows.py", "test_runner_stack_guards.py", "test_verify.py",
                  "test_probe.py", "test_poll_cleanup.py", "test_runbook.py"):
        assert suite in source


def test_runner_check_invokes_native_pinned_terraform_without_stdout_wrapper():
    check, source = workflow("check")
    setup = next(step for step in check["jobs"]["check"]["steps"]
                 if step.get("uses") == "hashicorp/setup-terraform@v3")
    assert setup["with"] == {"terraform_version": "1.14.6", "terraform_wrapper": "false"}
    assert "terraform -chdir=modules/source-control/gitlab/runners test" in source
    assert "test_rendered_runner.py" in source


def test_gitlab_delivery_jobs_use_dedicated_environment():
    for name in ("gitlab-infra-apply", "gitlab-runners-apply", "gitlab-runners-qualify"):
        document = yaml.load((WORKFLOWS / f"{name}.yml").read_text(), Loader=yaml.BaseLoader)
        for job in document["jobs"].values():
            assert job["environment"].startswith("adp-gitlab-deploy-")
            assert "github.ref == 'refs/heads/main'" in job["if"]


@pytest.mark.parametrize("requested,protected,accepted", [
    ("us-east-1", "us-east-1", True),
    ("us-west-2", "us-east-1", False),
    ("us-east-1", "", False),
    ("", "", False),
])
def test_registration_region_is_checked_before_aws_authentication(requested, protected, accepted):
    document = yaml.load((WORKFLOWS / "gitlab-infra-apply.yml").read_text(), Loader=yaml.BaseLoader)
    assert document["on"]["workflow_dispatch"]["inputs"]["region"]["required"] == "true"
    steps = document["jobs"]["apply"]["steps"]
    guard = next(step for step in steps if step.get("id") == "source")
    identity = next(step for step in steps if step.get("id") == "identity")
    assert steps.index(guard) < steps.index(identity)
    # Run the real pre-authentication region guard without fetching GitHub history.
    script = guard["run"].split('[[ "$ACCEPTED_REVISION"')[0]
    result = subprocess.run(["bash", "-c", script], capture_output=True, timeout=5,
                            env={"PATH": os.environ["PATH"], "REQUESTED_REGION": requested,
                                 "PROTECTED_REGION": protected})
    assert (result.returncode == 0) is accepted


def test_registration_run_is_discoverable_and_publishes_context_before_apply():
    document = yaml.load((WORKFLOWS / "gitlab-infra-apply.yml").read_text(), Loader=yaml.BaseLoader)
    assert document["run-name"] == "ADP deployment ${{ inputs.adp_correlation }}"
    assert document["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert document["env"]["ADP_DELIVERY_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert document["env"]["ADP_SOURCE_REVISION"] == "${{ inputs.adp_source_revision }}"
    steps = document["jobs"]["apply"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ inputs.adp_source_revision }}"
    assert checkout["with"]["fetch-depth"] == "0"
    assert checkout["with"]["persist-credentials"] == "false"
    source = next(step for step in steps if step.get("id") == "source")
    assert 'git merge-base --is-ancestor "$ACCEPTED_REVISION" refs/remotes/origin/main' in source["run"]
    context = next(step for step in steps if step.get("name") == "Publish authenticated deployment context")
    assert "delivery_evidence.py --context" in context["run"]
    assert "publish-deployment-evidence.py context gitlab-infra-apply.yml" in context["run"]
    assert context["env"]["ADP_DELIVERY_INPUTS_JSON"] == "${{ toJSON(inputs) }}"
    assert steps.index(context) < next(i for i, step in enumerate(steps) if step.get("id") == "apply")


@pytest.mark.parametrize("name", ["apply", "destroy", "build", "ami"])
def test_mutations_require_manual_reviewed_main_and_protected_environment(name):
    document, source = workflow(name)
    assert set(document["on"]) == {"workflow_dispatch"}
    assert document["permissions"] == {"contents": "read"}
    assert document["concurrency"]["cancel-in-progress"] == "false"
    for job in document["jobs"].values():
        assert "github.ref == 'refs/heads/main'" in job["if"]
        assert job["environment"] == ("adp-build-dev" if name in {"build", "ami"} else "adp-gitlab-deploy-dev" if name == "apply" else "adp-deploy-dev")
        assert job["permissions"] == {"id-token": "write", "contents": "read"}
        checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout@"))
        assert checkout["with"]["persist-credentials"] == "false"
    assert "deployment_guard.py identity" in source
    assert "set -x" not in source and "upload-artifact" not in source


def test_apply_and_destroy_share_the_same_account_environment_lock():
    apply, _ = workflow("apply")
    destroy, _ = workflow("destroy")
    assert apply["concurrency"] == destroy["concurrency"]
    assert "inputs.account_id" in apply["concurrency"]["group"]
    assert "inputs.environment" in apply["concurrency"]["group"]
    assert "inputs.confirm == 'gitlab-runners'" in destroy["jobs"]["destroy"]["if"]


@pytest.mark.parametrize("name", ["destroy", "build", "ami"])
@pytest.mark.parametrize("approved,dispatched,accepted", [
    ("a" * 40, "a" * 40, True),
    ("a" * 40, "b" * 40, False),
    ("main", "a" * 40, False),
])
def test_dispatch_ref_race_is_refused_before_any_aws_authentication(name, approved, dispatched, accepted):
    document, _ = workflow(name)
    assert document["on"]["workflow_dispatch"]["inputs"]["source_revision"]["required"] == "true"
    for job in document["jobs"].values():
        guard = job["steps"][0]
        expected = {
            "APPROVED_REVISION": "${{ inputs.source_revision }}",
            "DISPATCH_REVISION": "${{ github.sha }}",
        }
        if name == "apply":
            expected.update({"ACCEPTED_REVISION": "${{ inputs.adp_source_revision }}",
                             "ACCEPTED_DEFINITION": "${{ inputs.adp_definition_revision }}",
                             "WORKFLOW_REVISION": "${{ github.workflow_sha }}",
                             "ACCEPTED_DIGEST": "${{ inputs.adp_contract_digest }}",
                             "PROTECTED_DIGEST": "${{ vars.GITLAB_RUNNERS_DELIVERY_CONTRACT_DIGEST }}"})
        assert guard["env"] == expected
        assert "if" not in guard and "continue-on-error" not in guard
        result = subprocess.run(["bash", "-c", guard["run"]], capture_output=True, text=True,
                                env={**os.environ, "APPROVED_REVISION": approved,
                                     "DISPATCH_REVISION": dispatched, "ACCEPTED_REVISION": approved,
                                     "ACCEPTED_DEFINITION": approved, "WORKFLOW_REVISION": approved,
                                     "ACCEPTED_DIGEST": "b" * 64, "PROTECTED_DIGEST": "b" * 64}, timeout=5)
        assert (result.returncode == 0) is accepted


@pytest.mark.parametrize("name", ["destroy"])
def test_saved_plan_guard_runs_before_that_exact_plan_is_applied(name):
    _, source = workflow(name)
    plan = source.index('aws s3api get-object' if name == "apply" else 'terraform plan ')
    guard = source.index(f'runner-stack-plan-guard.py {name}')
    apply = source.index('terraform apply ')
    assert plan < guard < apply
    if name == "apply":
        assert 'terraform plan ' not in source
        assert '--key "dev/gitlab-runners/plans/$APPROVED_PLAN_SHA256/tfplan" "$RUNNER_TEMP/runners.tfplan"' in source
    else:
        assert '-out="$RUNNER_TEMP/runners.tfplan"' in source
    assert 'terraform show -json "$RUNNER_TEMP/runners.tfplan"' in source
    assert 'terraform apply -input=false -no-color -auto-approve "$RUNNER_TEMP/runners.tfplan"' in source
    assert "terraform destroy " not in source
    assert "::add-mask::" in source and "umask 077" in source
    assert "deployment_guard.py backend" in source




@pytest.mark.parametrize("name,fail_at", [
    (name, fail_at) for name in ("destroy",)
    for fail_at in ("", "terraform:init", "aws:s3api" if name == "apply" else "terraform:plan", "terraform:show",
                    "python3:runner-stack-plan-guard.py", "terraform:apply")
])
def test_private_plan_paths_hide_synthetic_canaries_and_delete_temporary_files(tmp_path, name, fail_at):
    document, _ = workflow(name)
    steps = document["jobs"][name]["steps"]
    load = next(step for step in steps if step.get("id") == "config" or step.get("name") == "Load reviewed private backend and runner variables")
    plan = next(step for step in steps if step.get("id") == "apply" or step.get("name", "").startswith("Plan and "))
    token = "glrt-synthetic-canary-for-workflow-9285"
    ca_line = "synthetic-ca-pem-canary-9285"
    lock = '{"synthetic":"private-lock"}'
    environment = {**os.environ, "RUNNER_TEMP": str(tmp_path), "BACKEND": f'bucket = "{token}"',
                   "VARIABLES": f'ca_pem = "{ca_line}"', "CANARY_TOKEN": token, "CANARY_CA": ca_line,
                   "RELEASE_LOCK": lock, "APPROVED_LOCK_SHA256": hashlib.sha256(lock.encode()).hexdigest(),
                   "APPROVED_PLAN_SHA256": "a" * 64,
                   "AWS_REGION": "us-east-1", "EXPECTED_BUCKET": "reviewed-bucket",
                   "EXPECTED_ACCOUNT": "123456789012", "FAIL_AT": fail_at,
                   "ACTIVATION_OPERATION": "00000000-0000-4000-8000-000000000001" if
                   fail_at.startswith("python3:activate_manager.py") else "",
                   "ACTIVATION_GROUP": "42" if fail_at.startswith("python3:activate_manager.py") else "",
                   "EVENTS": str(tmp_path / "events")}
    loaded = subprocess.run(["bash", "-c", load["run"]], env=environment, text=True,
                            capture_output=True, timeout=5)
    assert loaded.returncode == 0
    assert loaded.stderr == ""
    assert all(line.startswith("::add-mask::") for line in loaded.stdout.splitlines())
    assert token in loaded.stdout
    assert (ca_line in loaded.stdout) is (name == "destroy")
    assert (tmp_path / "runners-backend.tfvars").stat().st_mode & 0o777 == 0o600
    if name == "destroy":
        assert (tmp_path / "runners.tfvars").stat().st_mode & 0o777 == 0o600
    else:
        assert not (tmp_path / "runners.tfvars").exists()
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "terraform.tfstate").write_text("{}")
    executable = tmp_path / "fake-command"
    executable.write_text('''#!/bin/bash
set -euo pipefail
name="${0##*/}:$1"
if [ "$name" = 'python3:../scripts/runner-stack-plan-guard.py' ]; then name='python3:runner-stack-plan-guard.py'; fi
if [ "$1" = scripts/activate_manager.py ]; then name="python3:activate_manager.py:$2"; fi
printf '%s\n' "$name" >> "$EVENTS"
printf '%s\n' "$CANARY_TOKEN" "$CANARY_CA"
printf '%s\n' "$CANARY_TOKEN" "$CANARY_CA" >&2
if [ "$1" = plan ]; then
  for argument in "$@"; do
    case "$argument" in -out=*) printf '%s\n' "$CANARY_TOKEN" > "${argument#-out=}";; esac
  done
fi
if [ "$name" = aws:s3api ]; then printf '%s\n' "$CANARY_TOKEN" > "${@: -1}"; fi
if [ "$1" = show ] || [ "$1" = apply ]; then
  test "$(cat "${@: -1}")" = "$CANARY_TOKEN"
fi
if [ "$1" = show ]; then printf '{"private":"%s"}\n' "$CANARY_CA"; fi
if [ "$name" = "$FAIL_AT" ]; then exit 7; fi
''')
    executable.chmod(0o700)
    for command in ("python3", "terraform", "aws"):
        (tmp_path / command).symlink_to(executable)
    environment["PATH"] = str(tmp_path) + os.pathsep + environment["PATH"]
    result = subprocess.run(["bash", "-c", plan["run"]], env=environment, text=True,
                            capture_output=True, cwd=tmp_path, timeout=5)
    assert (result.returncode == 0) is (not fail_at)
    assert token not in result.stdout + result.stderr
    assert ca_line not in result.stdout + result.stderr
    if fail_at:
        assert "::error::Private runner step failed" in result.stdout
    assert all(not (tmp_path / filename).exists() for filename in
               ("runners-backend.tfvars", "runners.tfvars", "runners.tfplan", "runners-plan.json",
                "runners-release-lock.json", ".terraform/runners-release-lock.json"))
    events = (tmp_path / "events").read_text()
    assert ("terraform:apply" in events) is (fail_at in ("", "terraform:apply", "python3:activate_manager.py:rollout"))


def test_reconciliation_is_scoped_scheduled_and_has_no_generic_destroy():
    document, source = workflow("reconcile")
    assert document["on"]["schedule"] == [{"cron": "*/5 * * * *"}]
    assert "refs/heads/main" in document["jobs"]["orphan-cleanup"]["if"]
    assert "reconcile_scope.py" in source and "reconcile_instances.py" in source
    assert "GITLAB_RUNNERS_RECONCILE_ROLE_ARN" in source
    for forbidden in ("ADP_DEPLOY_ROLE_ARN", "terraform destroy", "terminate-instances", "set -x"):
        assert forbidden not in source


def test_no_workflow_echoes_authentication_values():
    for path in WORKFLOWS.glob("gitlab-runners-*.yml"):
        text = path.read_text()
        assert not re.search(r"echo .*\$(?:\{)?(?:TOKEN|SECRET|CA_)", text), path
        assert not re.search(r"(?:glrt-|glpat-)[A-Za-z0-9_-]{8}", text), path


@pytest.mark.parametrize("changed_field", [None, "account_id", "region", "source_revision", "expected_role_arn", "operation_id"])
def test_ami_workflow_refuses_private_binding_drift_before_authentication(tmp_path, changed_field):
    document, _ = workflow("ami")
    steps = document["jobs"]["build"]["steps"]
    binding = next(step for step in steps if "AMI_BUILD_CONFIG" in step.get("env", {}))
    auth = next(step for step in steps if step.get("uses") == "./.github/actions/trusted-build")
    assert steps.index(binding) < steps.index(auth)
    config = {"account_id": "123456789012", "region": "us-east-1", "source_revision": "a" * 40,
              "expected_role_arn": "arn:aws:iam::123456789012:role/adp-dev-ami-trusted-build",
              "operation_id": "00000000-0000-0000-0000-000000000001"}
    environment = {**os.environ, "RUNNER_TEMP": str(tmp_path), "EXPECTED_ACCOUNT": config["account_id"],
                   "AWS_REGION": config["region"], "ADP_SOURCE_SHA": config["source_revision"],
                   "EXPECTED_ROLE": config["expected_role_arn"], "OPERATION_ID": config["operation_id"],
                   "SOURCE_BUCKET": "reviewed-build-source", "IMAGES_RECEIPT_SHA256": "b" * 64}
    if changed_field:
        config[changed_field] = "different-reviewed-value"
    environment["AMI_BUILD_CONFIG"] = json.dumps(config)
    result = subprocess.run(["bash", "-c", binding["run"]], env=environment,
                            capture_output=True, text=True, timeout=5)
    output = tmp_path / "gitlab-ami-config.json"
    assert (result.returncode == 0) is (changed_field is None)
    assert output.exists() is (changed_field is None)
    assert config["expected_role_arn"] not in result.stdout + result.stderr
    if output.exists():
        assert output.stat().st_mode & 0o777 == 0o600
        assert json.loads(output.read_text()) == config


def test_ami_workflow_uses_fixed_receipt_and_retains_failure_evidence():
    document, source = workflow("ami")
    build, _ = workflow("build")
    assert document["concurrency"] == build["concurrency"]
    assert "--images-sha256 \"$IMAGES_RECEIPT_SHA256\"" in source
    assert "--expected-bucket-owner \"$EXPECTED_ACCOUNT\"" in source
    assert "gitlab-runners/artifacts/$ADP_SOURCE_SHA/$AWS_REGION/runtime-images.json" in source
    assert "gitlab-runners/ami/$ADP_SOURCE_SHA/$AWS_REGION/$OPERATION_ID/$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT/" in source
    evidence = next(step for step in document["jobs"]["build"]["steps"]
                    if step.get("name") == "Retain private build evidence for success or failure")
    assert evidence["if"] == "always()" and "continue-on-error" not in evidence
    assert "terraform apply" not in source and "--force" not in source




def test_offline_runner_binary_and_scoped_builder_checks_are_wired():
    check, check_source = workflow("check")
    _, build_source = workflow("build")
    assert "test_prepare_test_runner.py" in check_source
    assert "runners/builder/infra test" in check_source
    assert "runners/builder/infra/tests/test_check_plan.py" in check_source
    assert 'test "$SOURCE_BUCKET" = "adp-terraform-state-$EXPECTED_ACCOUNT"' in build_source
    assert "--output \"$GITLAB_RUNNER_BINARY\"" in check_source


@pytest.mark.parametrize("actions,kind,accepted", [
    (["no-op"], "aws_instance", True), (["delete"], "aws_instance", False),
    (["delete", "create"], "aws_instance", False), (["create"], "aws_secretsmanager_secret", False),
    (["update"], "aws_iam_role_policy", False),
])
def test_registration_plan_gate_refuses_setup_iam_and_destroy_changes(tmp_path, actions, kind, accepted):
    import importlib.util
    import sys
    scripts = ROOT / "modules/source-control/gitlab/scripts"
    sys.path.insert(0, str(scripts))
    spec = importlib.util.spec_from_file_location("registration_workflow_gate", scripts / "registration_workflow.py")
    guard_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard_module)
    plan = {"resource_changes": [{"mode": "managed", "type": kind, "change": {"actions": actions}}]}
    plan_bytes = b"synthetic-reviewed-plan"
    (tmp_path / "tfplan").write_bytes(plan_bytes)
    (tmp_path / "plan.json").write_text(json.dumps(plan))
    config = {"artifacts": {"tfplan": {"sha256": hashlib.sha256(plan_bytes).hexdigest()}},
              "targets": {"reviewed_plan_sha256": guard_module.digest(plan)},
              "instance_id": "i-1234567890abcdef0", "instance_change_approval": None}
    document = yaml.load((WORKFLOWS / "gitlab-infra-apply.yml").read_text(), Loader=yaml.BaseLoader)
    gate = next(step for step in document["jobs"]["apply"]["steps"] if step.get("id") == "safety")
    assert "if" not in gate and "registration_workflow.py guard" in gate["run"]
    if accepted:
        guard_module.guard(config, tmp_path)
    else:
        with pytest.raises(ValueError): guard_module.guard(config, tmp_path)


def test_infra_destroy_gate_refuses_missing_plan_receipt(tmp_path):
    document = yaml.load((WORKFLOWS / "gitlab-infra-apply.yml").read_text(), Loader=yaml.BaseLoader)
    gate = next(step for step in document["jobs"]["apply"]["steps"] if step.get("id") == "safety")
    result = subprocess.run(["bash", "-c", gate["run"].replace("${{ github.sha }}", "a" * 40)],
                            env={**os.environ, "RUNNER_TEMP": str(tmp_path)}, text=True, capture_output=True, timeout=5)
    assert result.returncode != 0


@pytest.mark.parametrize("invalid", [None, "source", "definition", "digest", "missing_digest"])
def test_infra_provenance_gate_prevents_authentication_and_mutation(tmp_path, invalid):
    document = yaml.load((WORKFLOWS / "gitlab-infra-apply.yml").read_text(), Loader=yaml.BaseLoader)
    steps = document["jobs"]["apply"]["steps"]
    gate = next(step for step in steps if step.get("id") == "source")
    assert gate["env"] == {
        "ACCEPTED_REVISION": "${{ inputs.adp_source_revision }}",
        "ACCEPTED_DEFINITION": "${{ inputs.adp_definition_revision }}",
        "WORKFLOW_REVISION": "${{ github.workflow_sha }}",
        "ACCEPTED_DIGEST": "${{ inputs.adp_contract_digest }}",
        "PROTECTED_DIGEST": "${{ vars.GITLAB_INFRA_DELIVERY_CONTRACT_DIGEST }}",
        "REQUESTED_REGION": "${{ inputs.region }}",
        "PROTECTED_REGION": "${{ vars.ADP_DEPLOY_REGION }}",
    }
    assert "if" not in gate and "continue-on-error" not in gate
    assert steps.index(gate) < next(index for index, step in enumerate(steps)
                                  if step.get("uses") == "./.github/actions/trusted-deployment")
    assert steps.index(gate) < next(index for index, step in enumerate(steps) if step.get("id") == "apply")
    environment = {**os.environ, "GITHUB_SHA": "a" * 40, "ACCEPTED_REVISION": "a" * 40,
                   "ACCEPTED_DEFINITION": "b" * 40, "WORKFLOW_REVISION": "b" * 40,
                   "ACCEPTED_DIGEST": "c" * 64, "PROTECTED_DIGEST": "c" * 64,
                   "REQUESTED_REGION": "us-east-1", "PROTECTED_REGION": "us-east-1",
                   "MUTATION": str(tmp_path / "mutation")}
    if invalid:
        field = {"source": "ACCEPTED_REVISION", "definition": "ACCEPTED_DEFINITION",
                 "digest": "ACCEPTED_DIGEST", "missing_digest": "PROTECTED_DIGEST"}[invalid]
        environment[field] = "" if invalid == "missing_digest" else "d" * len(environment[field])
    script = 'git() { if [[ \"$1\" == rev-parse ]]; then echo \"$GITHUB_SHA\"; fi; }\n' + gate["run"] + '\ntouch "$MUTATION"'
    result = subprocess.run(["bash", "-c", script], env=environment, capture_output=True, text=True, timeout=5)
    assert (result.returncode == 0) is (invalid is None)
    assert (tmp_path / "mutation").exists() is (invalid is None)


def test_qualification_workflow_binds_private_final_receipts_to_generic_delivery():
    document, source = workflow("qualify")
    inputs = document["on"]["workflow_dispatch"]["inputs"]
    assert set(document["on"]) == {"workflow_dispatch"}
    assert all(inputs[name]["required"] == "true" for name in (
        "account_id", "region",
        "adp_correlation", "adp_source_revision", "adp_definition_revision", "adp_contract_digest"))
    assert document["permissions"] == {"contents": "read"}
    assert document["concurrency"]["cancel-in-progress"] == "false"
    job = document["jobs"]["qualify"]
    assert job["environment"] == "adp-gitlab-deploy-dev"
    assert job["permissions"] == {"id-token": "write", "contents": "read"}
    assert "github.ref == 'refs/heads/main'" in job["if"]
    steps = job["steps"]
    by_id = {step["id"]: step for step in steps if "id" in step}
    assert [step["id"] for step in steps if "id" in step] == ["source", "inputs", "identity", "bundle", "final"]
    auth = next(index for index, step in enumerate(steps) if step.get("uses") == "./.github/actions/trusted-deployment")
    assert auth > 2 and all(steps[index]["id"] in {"source", "inputs"} for index in (1, 2))
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["persist-credentials"] == "false"
    assert "github.workflow_sha" in by_id["source"]["run"] and "git merge-base" in by_id["source"]["run"]
    assert document["run-name"] == "ADP deployment ${{ inputs.adp_correlation }}"
    assert document["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert "publish-deployment-evidence.py context gitlab-runners-qualify.yml" in source
    assert "deployment_guard.py identity" in source
    assert "qualification_bundle.py" in source and "--stage" not in by_id["final"]["run"]
    assert "--workflow-run-id" in by_id["final"]["run"]
    assert "--key \"dev/gitlab-runners/qualification/$QUALIFICATION_RUN/bundle.tar.gz\"" in by_id["bundle"]["run"]
    publisher = next(step for step in steps if step.get("name") == "Publish verified operational delivery")
    checks = {"source", "inputs", "identity", "bundle", "final"}
    outcomes = publisher["env"]["ADP_DELIVERY_STEP_OUTCOMES"]
    assert set(json.loads(re.sub(r"\$\{\{ steps\.\w+\.outcome \}\}", "success", outcomes))) == checks
    for check in checks:
        assert f'"{check}":"${{{{ steps.{check}.outcome }}}}"' in outcomes
    assert publisher["env"]["ADP_DELIVERY_CLEANUP_RECEIPT"].endswith("/final-verdict.json")
    assert "delivery_evidence.py" in publisher["run"]
    assert "publish-deployment-evidence.py delivery delivery-evidence" in publisher["run"]
    assert steps[-1]["if"] == "always()" and "rm -rf" in steps[-1]["run"]
    assert "terraform apply" not in source and "upload-artifact" not in source


@pytest.mark.parametrize("invalid", ["source", "definition", "digest", "run", "bundle_hash", "lock_hash"])
def test_qualification_refuses_bad_reviewed_input_before_provider_access(invalid):
    document, _ = workflow("qualify")
    by_id = {x["id"]: x for x in document["jobs"]["qualify"]["steps"] if "id" in x}
    first, second = by_id["source"], by_id["inputs"]
    source = "a" * 40
    definition = "b" * 40
    digest = "c" * 64
    run_id = "00000000-0000-4000-8000-000000000001"
    environment = {**os.environ, "GITHUB_SHA": source, "APPROVED_REVISION": source,
                   "ACCEPTED_REVISION": source, "ACCEPTED_DEFINITION": definition,
                   "ACCEPTED_DIGEST": digest, "PROTECTED_DIGEST": digest,
                   "EXPECTED_ACCOUNT": "123456789012", "AWS_REGION": "us-east-1",
                   "QUALIFICATION_RUN": run_id, "BUNDLE_SHA256": digest,
                   "APPROVED_LOCK_SHA256": digest, "BUNDLE_BUCKET": "private-example"}
    if invalid == "source":
        environment["APPROVED_REVISION"] = "d" * 40
    elif invalid == "definition":
        environment["ACCEPTED_DEFINITION"] = "d" * 40
    elif invalid == "digest":
        environment["PROTECTED_DIGEST"] = "d" * 64
    elif invalid == "run":
        environment["QUALIFICATION_RUN"] = "not-a-run"
    elif invalid == "bundle_hash":
        environment["BUNDLE_SHA256"] = "unreviewed"
    else:
        environment["APPROVED_LOCK_SHA256"] = "unreviewed"
    source_step = 'git() { if [[ "$1" == rev-parse ]]; then echo "$GITHUB_SHA"; fi; }\n' + first["run"].replace("${{ github.workflow_sha }}", definition)
    result = subprocess.run(["bash", "-c", source_step + "\n" + second["run"]],
                            env=environment, capture_output=True, text=True, timeout=5)
    assert result.returncode != 0


def test_runner_activation_is_native_and_private_before_semantic_publication():
    document, source = workflow("apply")
    assert document["run-name"] == "ADP deployment ${{ inputs.adp_correlation }}"
    assert document["env"]["ADP_DEPLOYMENT_EVIDENCE_STORE"] == "s3-oidc-v1"
    assert document["env"]["ADP_DELIVERY_EVIDENCE_STORE"] == "s3-oidc-v1"
    steps = document["jobs"]["apply"]["steps"]
    checkout = next(x for x in steps if x.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["ref"] == "${{ inputs.adp_source_revision }}" and checkout["with"]["fetch-depth"] == "0"
    context = next(x for x in steps if x.get("name") == "Publish authenticated deployment context")
    apply = next(x for x in steps if x.get("id") == "apply")
    publisher = next(x for x in steps if x.get("name") == "Publish verified operational delivery")
    assert steps.index(context) < steps.index(apply) < steps.index(publisher)
    assert "publish-deployment-evidence.py context gitlab-runners-apply.yml" in context["run"]
    assert apply["run"] == "python3 modules/source-control/gitlab/scripts/runner_workflow.py"
    assert steps[-1]["if"] == "always()" and "gitlab-runner-activation" in steps[-1]["run"]
    assert "upload-artifact" not in source and "set -x" not in source
