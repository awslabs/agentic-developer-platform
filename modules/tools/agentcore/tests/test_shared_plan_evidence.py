"""Private protected-plan diagnostics, exercised without cloud operations."""

import hashlib
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[4]
INFRA = ROOT / "modules/tools/agentcore/infra"


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(INFRA))
    retain = runpy.run_path(str(INFRA / "shared_plan_evidence.py"))["retain"]
    backend, variables, saved = [tmp_path / name for name in ("backend.hcl", "private.tfvars", "saved.plan")]
    backend.write_text('bucket = "example-state"\nkey = "dev/tools/agentcore/example.tfstate"\n'
                       'region = "us-east-1"\nencrypt = true\nuse_lockfile = true\n')
    variables.write_text('aws_account_id = "123456789012"\naws_region = "us-east-1"\n'
                         'private_marker = "private-input-value"\n')
    saved.write_bytes(b"fictional saved plan bytes")
    environment = {
        "SOURCE_SHA": "a" * 40, "WORKFLOW_REVISION": "b" * 40, "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2", "EXPECTED_AWS_ACCOUNT_ID": "123456789012", "AWS_REGION": "us-east-1",
        "EXPECTED_DEPLOY_ROLE_ARN": "arn:aws:iam::123456789012:role/fictional",
        "STATE_BUCKET": "example-state", "STATE_KEY": "dev/tools/agentcore/example.tfstate",
        "RELEASE_MODE": "plan", "TARGET_ENVIRONMENT": "dev", "PLAN_OUTCOME": "failure",
        "BACKEND": str(backend), "TFVARS": str(variables), "SAVED_PLAN_FILE": str(saved),
        "RUNNER_TEMP": str(tmp_path), "TF_AWS_DEFAULT_TAGS_TEST": "private-environment-value",
    }
    document = {
        "terraform_version": "1.14.6", "format_version": "1.2",
        "variables": {"aws_account_id": {"value": "123456789012"}, "aws_region": {"value": "us-east-1"}},
        "resource_drift": [{"address": "aws_iam_role.fictional[0]", "change": {
            "actions": ["update"], "before": {"tags": {"marker": "private-state-value"}},
            "after": {"tags": {"marker": "changed"}}}}],
    }
    calls, uploads = [], {}
    options = {"caller_account": "123456789012", "state_version": "state-version", "object_version": "object-version"}

    def read(arguments):
        calls.append(arguments)
        if arguments[:2] == ["git", "rev-parse"]:
            return ("a" * 40 + "\n").encode()
        if arguments[:3] == ["aws", "sts", "get-caller-identity"]:
            return json.dumps({"Account": options["caller_account"], "Arn": "arn:aws:sts::123456789012:assumed-role/fictional/session"}).encode()
        if arguments[:3] == ["terraform", f"-chdir={INFRA.relative_to(ROOT)}", "show"]:
            return json.dumps(document, indent=3).encode() + b"\n"
        if arguments[:3] == ["aws", "s3api", "head-object"]:
            return json.dumps({"VersionId": options["state_version"], "ETag": "fictional-etag"}).encode()
        if arguments[:3] == ["aws", "s3api", "put-object"]:
            key = arguments[arguments.index("--key") + 1]
            path = Path(arguments[arguments.index("--body") + 1])
            uploads[key] = path.read_bytes()
            assert path.stat().st_mode & 0o777 == 0o600
            return json.dumps({"VersionId": options["object_version"]}).encode()
        pytest.fail("Unexpected command: " + repr(arguments))

    return retain, environment, document, calls, uploads, options, read


@pytest.mark.parametrize("mode,outcome", [("plan", "failure"), ("apply", "failure"), ("plan", "success")])
def test_exact_plan_and_provenance_are_private_versioned_non_deployment_evidence(scenario, mode, outcome):
    retain, env, document, calls, uploads, _, read = scenario
    env.update(RELEASE_MODE=mode, PLAN_OUTCOME=outcome)
    assert retain(env, read)
    prefix = "codebuild/artifacts/shared-agentcore-tools/123-2"
    raw = uploads[prefix + ".plan-diagnostic.json"]
    assert raw == json.dumps(document, indent=3).encode() + b"\n"
    evidence = json.loads(uploads[prefix + ".plan-evidence.json"])
    assert evidence["evidence_type"] == "plan-diagnostic"
    assert evidence["deployment_accepted"] is False
    assert (evidence["mode"], evidence["status"]) == (mode, outcome)
    assert (evidence["source_revision"], evidence["workflow_revision"]) == ("a" * 40, "b" * 40)
    assert (evidence["run_id"], evidence["run_attempt"], evidence["account"]) == ("123", "2", "123456789012")
    assert evidence["plan_json"]["sha256"] == hashlib.sha256(raw).hexdigest()
    assert evidence["plan_json"]["version_id"] == "object-version"
    assert evidence["saved_plan_sha256"] == hashlib.sha256(Path(env["SAVED_PLAN_FILE"]).read_bytes()).hexdigest()
    assert evidence["config"]["tfvars_sha256"] == hashlib.sha256(Path(env["TFVARS"]).read_bytes()).hexdigest()
    assert evidence["state_object_observed_after_plan"]["version_id"] == "state-version"
    assert evidence["resource_drift"] == document["resource_drift"]
    assert evidence["provider_environment_variable_names"] == ["TF_AWS_DEFAULT_TAGS_TEST"]
    assert b"private-environment-value" not in uploads[prefix + ".plan-evidence.json"]
    for command in calls:
        assert not {"apply", "init", "plan", "put-image", "start-build", "assume-role"} & set(command)
        if "s3api" in command:
            assert command[command.index("--expected-bucket-owner") + 1] == "123456789012"
        if "put-object" in command:
            assert command[command.index("--if-none-match") + 1] == "*"
            assert command[command.index("--bucket") + 1] == "example-state"


@pytest.mark.parametrize("field,value", [
    ("SOURCE_SHA", "short"), ("WORKFLOW_REVISION", "main"), ("SOURCE_SHA", "c" * 40),
    ("GITHUB_RUN_ID", "0"), ("GITHUB_RUN_ATTEMPT", "0"), ("RELEASE_MODE", "unknown"),
    ("PLAN_OUTCOME", "skipped"), ("TARGET_ENVIRONMENT", "other"),
    ("EXPECTED_AWS_ACCOUNT_ID", "999999999999"), ("AWS_REGION", "eu-west-1"),
    ("EXPECTED_DEPLOY_ROLE_ARN", "arn:aws:iam::123456789012:role/other"),
    ("STATE_BUCKET", "foreign-state"), ("STATE_KEY", "dev/tools/agentcore/other.tfstate"),
])
def test_identity_or_private_binding_mismatch_never_uploads(scenario, field, value):
    retain, env, _, _, uploads, _, read = scenario
    env[field] = value
    with pytest.raises(ValueError):
        retain(env, read)
    assert not uploads


def test_successful_apply_cannot_enter_diagnostic_path(scenario):
    retain, env, _, calls, uploads, _, read = scenario
    env.update(RELEASE_MODE="apply", PLAN_OUTCOME="success")
    with pytest.raises(ValueError, match="outcome"):
        retain(env, read)
    assert not calls and not uploads


@pytest.mark.parametrize("mismatch", ["caller", "plan_account", "plan_region", "unversioned_state"])
def test_foreign_or_unversioned_readback_stops_before_upload(scenario, mismatch):
    retain, env, document, _, uploads, options, read = scenario
    if mismatch == "caller":
        options["caller_account"] = "999999999999"
    elif mismatch == "unversioned_state":
        options["state_version"] = None
    else:
        key = "aws_account_id" if mismatch == "plan_account" else "aws_region"
        document["variables"][key]["value"] = "different"
    with pytest.raises(ValueError):
        retain(env, read)
    assert not uploads


@pytest.mark.parametrize("version", [None, "", "null"])
def test_unversioned_put_never_writes_a_provenance_claim(scenario, version):
    retain, env, _, _, uploads, options, read = scenario
    options["object_version"] = version
    with pytest.raises(ValueError, match="immutable version"):
        retain(env, read)
    assert len(uploads) == 1
    assert next(iter(uploads)).endswith(".plan-diagnostic.json")


def test_no_saved_plan_after_failed_terraform_has_no_cloud_side_effect(scenario):
    retain, env, _, calls, uploads, _, read = scenario
    Path(env["SAVED_PLAN_FILE"]).unlink()
    assert retain(env, read) is False
    assert not calls and not uploads
    env["PLAN_OUTCOME"] = "success"
    with pytest.raises(ValueError, match="saved plan"):
        retain(env, read)


def test_cli_failure_does_not_log_private_provider_stderr(scenario, tmp_path):
    _, env, _, _, _, _, _ = scenario
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "git").write_text("#!/bin/sh\necho " + "a" * 40 + "\n")
    (binary / "aws").write_text("#!/bin/sh\necho private-provider-stderr >&2\nexit 1\n")
    for executable in binary.iterdir():
        executable.chmod(0o755)
    result = subprocess.run([sys.executable, str(INFRA / "shared_plan_evidence.py")],
                            env={**os.environ, **env, "PATH": str(binary) + ":" + os.environ["PATH"]},
                            capture_output=True, text=True)
    assert result.returncode != 0
    assert "Private plan diagnostics could not be retained" in result.stderr
    assert "private-provider-stderr" not in result.stderr + result.stdout
    assert "private-input-value" not in result.stderr + result.stdout


def test_workflow_retains_private_diagnostics_without_changing_apply_fences():
    workflow = yaml.load((ROOT / ".github/workflows/shared-agentcore-tools-deploy.yml").read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["tools"]["steps"]
    planning = next(step for step in steps if step.get("id") == "shared_plan")
    diagnostic = next(step for step in steps if "shared_plan_evidence.py" in step.get("run", ""))
    applying = next(step for step in steps if step.get("name") == "Explicitly apply the reviewed saved plan")
    cleanup = next(step for step in steps if step.get("name") == "Remove temporary deployment files")
    assert diagnostic["if"] == "always() && (steps.shared_plan.outcome == 'failure' || (inputs.mode == 'plan' && steps.shared_plan.outcome == 'success'))"
    assert diagnostic["env"]["PLAN_OUTCOME"] == "${{ steps.shared_plan.outcome }}"
    assert steps.index(planning) < steps.index(diagnostic) < steps.index(applying) < steps.index(cleanup)
    assert applying["if"] == "inputs.mode == 'apply'"  # GitHub also requires preceding success by default.
    assert "continue-on-error" not in planning and "continue-on-error" not in applying
    assert "shared-tools-plan-diagnostic.json" in cleanup["run"]
    assert "shared-tools-plan-evidence.json" in cleanup["run"]
    assert not any("upload-artifact" in step.get("uses", "") for step in steps)
