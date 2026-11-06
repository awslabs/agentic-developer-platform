import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import time
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"), SCRIPT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preflight = load("runner-stack-preflight")
plan_guard = load("runner-stack-plan-guard")


@pytest.fixture
def plan():
    return {
        "complete": True,
        "applyable": True,
        "errored": False,
        "variables": {
            "environment": {"value": "dev"},
            "aws_region": {"value": "us-east-1"},
            "platform_state": {"value": {"bucket": "private-bucket", "key": "dev/platform/terraform.tfstate", "region": "us-east-1"}},
            "groups": {"value": {"42": {"token_secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:adp/dev/gitlab-runner/42-Ab12Cd"}}},
            "job_endpoints": {"value": {"42": {"gitlab_eni_id": "eni-0123456789abcdef0"}}},
            "runner_chart_archive": {"value": ""},
        },
        "planned_values": {"outputs": {"group_namespaces": {"value": {
            "42": {"manager": "gitlab-runner-42", "jobs": "gitlab-jobs-42"}
        }}}},
        "configuration": {"root_module": {"resources": [
            {"type": "aws_autoscaling_group"}, {"type": "aws_launch_template"}, {"type": "helm_release"},
        ]}},
        "resource_changes": [{"type": "aws_autoscaling_group", "name": "group", "address": 'aws_autoscaling_group.group["42"]',
                              "mode": "managed", "change": {"actions": ["create"]}}],
    }


def check(plan, operation="apply"):
    plan_guard.check_saved_plan(plan, operation, "123456789012", "us-east-1", "private-bucket")


def test_scoped_saved_plan_accepts_approved_runner_resources(plan):
    check(plan)
    plan["resource_changes"][0]["change"]["actions"] = ["delete"]
    check(plan, "destroy")


@pytest.mark.parametrize("change", [None, "path", "digest", "missing"])
def test_saved_plan_release_lock_is_portable_and_matches_protected_input(plan, change):
    lock_digest = "e" * 64
    plan["variables"].update(release_lock_file={"value": ".terraform/runners-release-lock.json"},
                             release_lock_sha256={"value": lock_digest})
    if change == "path":
        plan["variables"]["release_lock_file"]["value"] = "/tmp/previous-run/release-lock.json"
    elif change == "digest":
        plan["variables"]["release_lock_sha256"]["value"] = "f" * 64
    elif change == "missing":
        del plan["variables"]["release_lock_file"]
    approved = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    if change:
        with pytest.raises(ValueError, match="reviewed release lock"):
            plan_guard.check_saved_plan(plan, "apply", "123456789012", "us-east-1", "private-bucket", approved, lock_digest)
    else:
        plan_guard.check_saved_plan(plan, "apply", "123456789012", "us-east-1", "private-bucket", approved, lock_digest)


def test_apply_requires_exact_reviewed_saved_plan_digest(plan):
    digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    plan_guard.check_saved_plan(plan, "apply", "123456789012", "us-east-1", "private-bucket", digest)
    plan["resource_changes"][0]["change"]["actions"] = ["delete"]
    with pytest.raises(ValueError, match="differs from the reviewed plan"):
        plan_guard.check_saved_plan(plan, "apply", "123456789012", "us-east-1", "private-bucket", digest)
    result = subprocess.run(["python3", str(SCRIPT / "runner-stack-plan-guard.py"), "apply",
                             "--account", "123456789012", "--region", "us-east-1", "--bucket", "private-bucket"],
                            input=json.dumps(plan), capture_output=True, text=True)
    assert result.returncode != 0 and "approved saved plan digest required" in result.stderr


def test_approved_plan_survives_later_apply_with_restored_portable_lock(plan, tmp_path):
    reviewed = tmp_path / "reviewed"
    later = tmp_path / "later"
    lock = '{"synthetic":"reviewed-release"}'
    configuration = '''
variable "release_lock_file" { type = string }
output "lock" { value = jsondecode(file(var.release_lock_file)) }
'''
    for directory in (reviewed, later):
        directory.mkdir()
        (directory / ".terraform").mkdir()
        (directory / "main.tf").write_text(configuration)
        (directory / ".terraform/runners-release-lock.json").write_text(lock)
        subprocess.run(["terraform", "init", "-backend=false", "-input=false"], cwd=directory,
                       check=True, capture_output=True, timeout=30)
    subprocess.run(["terraform", "plan", "-input=false", "-out=reviewed.tfplan",
                    "-var=release_lock_file=.terraform/runners-release-lock.json"], cwd=reviewed,
                   check=True, capture_output=True, timeout=30)
    original = subprocess.check_output(["terraform", "show", "-json", "reviewed.tfplan"], cwd=reviewed, timeout=30)
    approved_plan = json.loads(original)
    approved = hashlib.sha256(json.dumps(approved_plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    (later / "downloaded.tfplan").write_bytes((reviewed / "reviewed.tfplan").read_bytes())
    time.sleep(1.1)
    subprocess.run(["terraform", "plan", "-input=false", "-out=regenerated.tfplan",
                    "-var=release_lock_file=.terraform/runners-release-lock.json"], cwd=later,
                   check=True, capture_output=True, timeout=30)
    regenerated = json.loads(subprocess.check_output(["terraform", "show", "-json", "regenerated.tfplan"], cwd=later, timeout=30))
    assert regenerated["timestamp"] != approved_plan["timestamp"]
    assert hashlib.sha256(json.dumps(regenerated, sort_keys=True, separators=(",", ":")).encode()).hexdigest() != approved
    restored = json.loads(subprocess.check_output(["terraform", "show", "-json", "downloaded.tfplan"], cwd=later, timeout=30))
    assert hashlib.sha256(json.dumps(restored, sort_keys=True, separators=(",", ":")).encode()).hexdigest() == approved
    plan["timestamp"] = approved_plan["timestamp"]
    runner_digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    plan_guard.check_saved_plan(plan, "apply", "123456789012", "us-east-1", "private-bucket", runner_digest)
    plan["timestamp"] = regenerated["timestamp"]
    with pytest.raises(ValueError, match="differs from the reviewed plan"):
        plan_guard.check_saved_plan(plan, "apply", "123456789012", "us-east-1", "private-bucket", runner_digest)
    subprocess.run(["terraform", "apply", "-input=false", "downloaded.tfplan"], cwd=later,
                   check=True, capture_output=True, timeout=30)
    outputs = json.loads(subprocess.check_output(["terraform", "output", "-json"], cwd=later, timeout=30))
    assert outputs["lock"]["value"] == json.loads(lock)


def test_saved_plan_accepts_real_rendered_resource_inventory(plan, tmp_path):
    with (tmp_path / "render.jsonl").open("w+") as output:
        rendered = subprocess.run(
            ["terraform", "test", "-filter=tests/rendered_config.tftest.hcl", "-json", "-verbose", "-no-color"],
            cwd=ROOT / "runners", stdout=output, stderr=subprocess.PIPE, text=True, timeout=180,
        )
        assert rendered.returncode == 0, rendered.stderr
        output.seek(0)
        inventory = next(event["test_plan"]["resource_changes"] for line in output
                         if (event := json.loads(line)).get("type") == "test_plan")
    plan["resource_changes"] = inventory
    types = {item["type"] for item in inventory if item["mode"] == "managed"}
    assert {"aws_s3_bucket_versioning", "aws_ssm_parameter", "aws_vpc_endpoint",
            "kubernetes_config_map_v1", "kubernetes_resource_quota_v1"} <= types
    check(plan)
    for resource in inventory:
        resource["change"]["actions"] = ["delete"] if resource["mode"] == "managed" else ["no-op"]
    check(plan, "destroy")


def test_unchanged_secret_arn_plan_is_safe_to_apply_for_rotation(plan, tmp_path):
    (tmp_path / "main.tf").write_text('''
variable "token_secret_arn" { type = string }
resource "terraform_data" "rotation" { input = var.token_secret_arn }
output "token_secret_arn" { value = terraform_data.rotation.output }
''')
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps({
        "token_secret_arn": plan["variables"]["groups"]["value"]["42"]["token_secret_arn"],
    }))
    for args in (("init", "-backend=false", "-input=false"), ("apply", "-auto-approve", "-input=false"),
                 ("plan", "-input=false", "-out=rotation.tfplan")):
        subprocess.run(["terraform", *args], cwd=tmp_path, capture_output=True, check=True, timeout=30)
    rendered = subprocess.run(["terraform", "show", "-json", "rotation.tfplan"], cwd=tmp_path,
                              capture_output=True, text=True, check=True, timeout=30)
    unchanged = json.loads(rendered.stdout)
    for field in ("complete", "applyable", "errored", "resource_changes", "output_changes"):
        plan[field] = unchanged[field]
    assert plan["applyable"] is False
    check(plan)
    applied = subprocess.run(["terraform", "apply", "-input=false", "rotation.tfplan"], cwd=tmp_path,
                             capture_output=True, text=True, check=True, timeout=30)
    assert "0 added, 0 changed, 0 destroyed" in applied.stdout


@pytest.mark.parametrize("field,value", [
    ("complete", False), ("errored", True), ("deferred_changes", [{"reason": "deferred_prereq"}]),
    ("applyable", None), ("applyable", "false"),
])
def test_noop_still_requires_complete_error_free_metadata(plan, field, value):
    plan["resource_changes"][0]["change"]["actions"] = ["no-op"]
    plan["applyable"] = False
    plan[field] = value
    with pytest.raises(ValueError, match="complete, executable and error-free"):
        check(plan)


@pytest.mark.parametrize("actions", [["create"], ["update"], ["delete"], ["read"], []])
def test_nonapplyable_output_changes_are_not_noop(plan, actions):
    plan["resource_changes"][0]["change"]["actions"] = ["no-op"]
    plan["applyable"] = False
    plan["output_changes"] = {"output": {"actions": actions}}
    with pytest.raises(ValueError, match="complete, executable and error-free"):
        check(plan)


@pytest.mark.parametrize("change,reason", [
    (lambda plan: plan["variables"]["job_endpoints"]["value"].clear(), "endpoints"),
    (lambda plan: plan["variables"]["job_endpoints"]["value"].update({"43": {"gitlab_eni_id": "eni-0123456789abcdef1"}}), "endpoints"),
    (lambda plan: plan["variables"]["job_endpoints"]["value"]["42"].update(gitlab_eni_id="eni-invalid"), "endpoints"),
    (lambda plan: plan["variables"]["runner_chart_archive"].update(value="unreviewed.tgz"), "runner chart"),
    (lambda plan: plan["planned_values"]["outputs"]["group_namespaces"]["value"]["42"].update(manager="gitlab-runner-43"), "namespaces"),
    (lambda plan: plan["planned_values"]["outputs"]["group_namespaces"]["value"].clear(), "namespaces"),
])
def test_apply_rejects_private_input_or_output_drift(plan, change, reason):
    change(plan)
    with pytest.raises(ValueError, match=reason):
        check(plan)


def test_destroy_does_not_require_apply_only_endpoint_outputs(plan):
    del plan["planned_values"]
    del plan["variables"]["job_endpoints"]
    plan["resource_changes"][0]["change"]["actions"] = ["delete"]
    check(plan, "destroy")



@pytest.mark.parametrize("field,value", [
    ("complete", False), ("applyable", False), ("errored", True),
    ("complete", "true"), ("deferred_changes", [{"reason": "deferred_prereq"}]),
])
@pytest.mark.parametrize("operation", ["apply", "destroy"])
def test_partial_saved_plan_with_plausible_resources_is_refused(plan, field, value, operation):
    plan[field] = value
    with pytest.raises(ValueError, match="complete, executable and error-free"):
        check(plan, operation)


@pytest.mark.parametrize("field", ["complete", "applyable", "errored"])
def test_missing_plan_execution_metadata_is_refused(plan, field):
    del plan[field]
    with pytest.raises(ValueError, match="complete, executable and error-free"):
        check(plan)


@pytest.mark.parametrize("resource_type", ["aws_instance", "aws_vpc", "aws_eks_cluster", "aws_db_instance", "aws_ssm_document"])
@pytest.mark.parametrize("operation,action", [("apply", "create"), ("destroy", "delete")])
def test_no_collateral_instance_or_platform_mutation(plan, resource_type, operation, action):
    plan["resource_changes"][0].update(type=resource_type, address=f"{resource_type}.group", change={"actions": [action]})
    with pytest.raises(ValueError, match="non-runner resource"):
        check(plan, operation)


@pytest.mark.parametrize("field,value", [("environment", "prod"), ("aws_region", "eu-west-1")])
def test_wrong_environment_or_region_is_rejected(plan, field, value):
    plan["variables"][field]["value"] = value
    with pytest.raises(ValueError, match="protected dev region"):
        check(plan)


def test_wrong_platform_state_or_group_secret_account_is_rejected(plan):
    plan["variables"]["platform_state"]["value"]["key"] = "dev/modules/gitlab/terraform.tfstate"
    with pytest.raises(ValueError, match="platform discovery"):
        check(plan)
    plan["variables"]["platform_state"]["value"]["key"] = "dev/platform/terraform.tfstate"
    plan["variables"]["groups"]["value"]["42"]["token_secret_arn"] = "arn:aws:secretsmanager:us-east-1:999999999999:secret:adp/dev/gitlab-runner/42-Ab12Cd"
    with pytest.raises(ValueError, match="wrong account"):
        check(plan)


def test_wrong_group_or_unsafe_replacement_is_rejected(plan):
    plan["variables"]["groups"]["value"]["042"] = plan["variables"]["groups"]["value"].pop("42")
    with pytest.raises(ValueError, match="wrong account"):
        check(plan)
    plan["variables"]["groups"]["value"]["42"] = plan["variables"]["groups"]["value"].pop("042")
    plan["resource_changes"][0]["change"]["actions"] = ["delete", "create"]
    with pytest.raises(ValueError, match="apply cannot destroy"):
        check(plan)


def test_missing_runner_capacity_and_ambiguous_address_are_rejected(plan):
    plan["configuration"]["root_module"]["resources"] = [{"type": "terraform_data"}]
    with pytest.raises(ValueError, match="implementation not present"):
        check(plan)
    plan["configuration"]["root_module"]["resources"] = [{"type": "aws_autoscaling_group"}, {"type": "aws_launch_template"}, {"type": "helm_release"}]
    plan["resource_changes"][0]["address"] = "aws_autoscaling_group.another"
    with pytest.raises(ValueError, match="invalid resource address"):
        check(plan)


def test_preflight_rejects_missing_lock_and_wrong_host_checksum(tmp_path):
    with pytest.raises(preflight.BuildError, match="missing"):
        preflight.check(tmp_path, "us-east-1", "private-bucket")
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates/job-user-data.sh.tftpl").write_bytes(b"host")
    lock = {"host_ami": {"us-east-1": {"image_id": "ami-12345678", "owner_id": "123456789012", "architecture": "x86_64", "bootstrap_sha256": "a" * 64}},
            "fleeting_plugin": {"s3_key": "gitlab-runners/upstream/pinned-plugin", "sha256": "b" * 64}}
    (tmp_path / "versions.lock.json").write_text(json.dumps(lock))
    with patch.object(preflight, "bootstrap_base", return_value="python:3.12.8-slim@sha256:" + "c" * 64), pytest.raises(preflight.BuildError, match="checksum"):
        preflight.check(tmp_path, "us-east-1", "private-bucket")


def test_preflight_accepts_only_verified_local_inputs(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates/job-user-data.sh.tftpl").write_bytes(b"host")
    lock = {"host_ami": {"us-east-1": {"image_id": "ami-12345678", "owner_id": "123456789012", "architecture": "x86_64", "bootstrap_sha256": hashlib.sha256(b"host").hexdigest()}},
            "fleeting_plugin": {"s3_key": "gitlab-runners/upstream/pinned-plugin", "sha256": "b" * 64}}
    (tmp_path / "versions.lock.json").write_text(json.dumps(lock))
    with patch.object(preflight, "bootstrap_base", return_value="python:3.12.8-slim@sha256:" + "c" * 64):
        preflight.check(tmp_path, "us-east-1", "private-bucket")


def test_private_release_lock_requires_matching_digest_and_private_file(tmp_path):
    (tmp_path / "templates").mkdir()
    (tmp_path / "templates/job-user-data.sh.tftpl").write_bytes(b"host")
    lock = {"host_ami": {"us-east-1": {"image_id": "ami-12345678", "owner_id": "123456789012",
            "architecture": "x86_64", "bootstrap_sha256": hashlib.sha256(b"host").hexdigest()}},
            "fleeting_plugin": {"s3_key": "gitlab-runners/upstream/pinned-plugin", "sha256": "b" * 64}}
    path = tmp_path / "private-lock.json"
    path.write_text(json.dumps(lock))
    path.chmod(0o600)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    with patch.object(preflight, "bootstrap_base", return_value="python:3.12.8-slim@sha256:" + "c" * 64):
        preflight.check(tmp_path, "us-east-1", "private-bucket", str(path), digest)
        with pytest.raises(preflight.BuildError, match="invalid"):
            preflight.check(tmp_path, "us-east-1", "private-bucket", str(path), "a" * 64)
        path.chmod(0o644)
        with pytest.raises(preflight.BuildError, match="invalid"):
            preflight.check(tmp_path, "us-east-1", "private-bucket", str(path), digest)


@pytest.mark.parametrize("case", ["portable", "absolute", "bundled", "wrong_digest", "public_file", "symlink"])
def test_terraform_release_query_reaches_real_private_lock_reader(tmp_path, monkeypatch, capsys, case):
    runner = ROOT / "runners"
    source = (runner / "versions.tf").read_text()
    query_expression = source.split("  query = ", 1)[1].split("\n  }\n", 1)[0] + "\n}"
    lock_path = tmp_path / ".terraform/runners-release-lock.json"
    lock_path.parent.mkdir()
    lock = {"synthetic": "reviewed private release"}
    lock_path.write_text(json.dumps(lock))
    lock_path.chmod(0o600)
    digest = hashlib.sha256(lock_path.read_bytes()).hexdigest()
    variables = {"aws_region": "us-east-1", "host_ami_id": "ami-00000000000000000",
                 "job_image_digest": "example.invalid/job:v1@sha256:" + "d" * 64,
                 "release_lock_file": ".terraform/runners-release-lock.json", "release_lock_sha256": digest}
    if case == "absolute":
        variables["release_lock_file"] = str(lock_path)
    elif case == "bundled":
        variables.update(release_lock_file="", release_lock_sha256="")
        lock = json.loads((runner / "versions.lock.json").read_text())
    elif case == "wrong_digest":
        variables["release_lock_sha256"] = "0" * 64
    elif case == "public_file":
        lock_path.chmod(0o644)
    elif case == "symlink":
        target = lock_path.with_name("original.json")
        lock_path.rename(target)
        lock_path.symlink_to(target)
    (tmp_path / "main.tf").write_text("\n".join(f'variable "{name}" {{ type = string }}' for name in variables)
                                      + f"\nlocals {{\n release_query = {query_expression}\n}}\n")
    (tmp_path / "terraform.tfvars.json").write_text(json.dumps(variables))
    result = subprocess.run(["terraform", "console"], cwd=tmp_path,
                            input="jsonencode(local.release_query)\n", capture_output=True, text=True,
                            check=True, timeout=30)
    query = json.loads(json.loads(result.stdout))
    spec = importlib.util.spec_from_file_location("runner_release_reader", runner / "scripts/release_lock.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    monkeypatch.chdir(tmp_path)
    with patch.object(release.sys, "stdin", io.StringIO(json.dumps(query))), \
            patch.object(release, "check", return_value={}) as check_artifacts, \
            patch.object(release, "verify_ami") as verify_provider:
        release.main()
    accepted = case in {"portable", "absolute", "bundled"}
    assert json.loads(capsys.readouterr().out) == {"ready": "true" if accepted else "false"}
    assert query["lock_path"] == ("" if case == "bundled" else str(lock_path))
    assert query["lock_sha256"] == variables["release_lock_sha256"]
    if accepted:
        check_artifacts.assert_called_once_with(lock, variables["aws_region"], variables["host_ami_id"], variables["job_image_digest"])
        verify_provider.assert_called_once_with({}, variables["aws_region"])
    else:
        check_artifacts.assert_not_called()
        verify_provider.assert_not_called()


def test_workflows_wire_private_preflight_and_same_saved_plan():
    workflows = Path(__file__).resolve().parents[5] / ".github/workflows"
    for operation in ("apply", "destroy"):
        text = (workflows / f"gitlab-runners-{operation}.yml").read_text()
        if operation == "apply":
            assert 'python3 modules/source-control/gitlab/scripts/runner_workflow.py' in text
            assert 'GITLAB_RUNNER_CONFIG_JSON' in text
            assert 'terraform plan ' not in text
        else:
            for required in ("runner-stack-preflight.py", "runner-stack-plan-guard.py destroy",
                             '"$EXPECTED_ACCOUNT"', '"$EXPECTED_BUCKET"', '"$AWS_REGION"',
                             '"$RUNNER_TEMP/runners.tfplan"', "deployment_guard.py backend"):
                assert required in text
        assert "--auto-approve" not in text
        assert "upload-artifact" not in text
