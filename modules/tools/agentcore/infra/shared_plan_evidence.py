"""Retain protected plan diagnostics privately; never claim deployment evidence."""

import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

from validate_private_config import validate


INFRA = "modules/tools/agentcore/infra"


def command(arguments):
    return subprocess.check_output(arguments, stderr=subprocess.PIPE)


def sha256(value):
    return hashlib.sha256(value).hexdigest()


def versioned(value):
    return isinstance(value, str) and bool(value) and value != "null"


def retain(environment, read=command):
    saved_plan = Path(environment["SAVED_PLAN_FILE"])
    if not saved_plan.is_file():
        if environment.get("PLAN_OUTCOME") == "success":
            raise ValueError("Successful plan diagnostics require a saved plan")
        return False
    source, definition = environment["SOURCE_SHA"], environment["WORKFLOW_REVISION"]
    run_id, attempt = environment["GITHUB_RUN_ID"], environment["GITHUB_RUN_ATTEMPT"]
    account, region = environment["EXPECTED_AWS_ACCOUNT_ID"], environment["AWS_REGION"]
    bucket, state_key = environment["STATE_BUCKET"], environment["STATE_KEY"]
    mode, target = environment["RELEASE_MODE"], environment["TARGET_ENVIRONMENT"]
    outcome = environment["PLAN_OUTCOME"]
    if (not all(re.fullmatch(r"[a-f0-9]{40}", value) for value in (source, definition))
            or not all(re.fullmatch(r"[1-9][0-9]*", value) for value in (run_id, attempt))
            or mode not in {"plan", "apply"} or target not in {"dev", "prod"}
            or outcome not in {"success", "failure"} or (outcome == "success" and mode != "plan")):
        raise ValueError("Plan diagnostics require the exact protected workflow identity and outcome")
    backend, variables = Path(environment["BACKEND"]), Path(environment["TFVARS"])
    validate(backend, variables, bucket=bucket, state_key=state_key, account=account, region=region,
             lock_table=environment.get("STATE_LOCK_TABLE", ""))
    if read(["git", "rev-parse", "HEAD"]).decode().strip() != source:
        raise ValueError("Failed-plan source differs from the reviewed checkout")
    caller = json.loads(read(["aws", "sts", "get-caller-identity", "--output", "json"]))
    if caller.get("Account") != account:
        raise ValueError("Failed-plan AWS account differs from the protected target")
    role_arn = environment["EXPECTED_DEPLOY_ROLE_ARN"]
    role = re.fullmatch(rf"arn:aws:iam::{account}:role/(?:[A-Za-z0-9_+=,.@-]+/)*([A-Za-z0-9_+=,.@-]+)", role_arn)
    if not role or not re.fullmatch(
            rf"arn:aws:sts::{account}:assumed-role/{re.escape(role.group(1))}/[^/]+", caller.get("Arn", "")):
        raise ValueError("Plan diagnostic caller differs from the protected deployment role")
    plan_bytes = read(["terraform", f"-chdir={INFRA}", "show", "-json", str(saved_plan)])
    plan = json.loads(plan_bytes)
    bindings = plan.get("variables", {})
    if (bindings.get("aws_account_id", {}).get("value") != account
            or bindings.get("aws_region", {}).get("value") != region):
        raise ValueError("Failed-plan variables differ from the protected target")
    state_object = json.loads(read([
        "aws", "s3api", "head-object", "--bucket", bucket, "--key", state_key,
        "--expected-bucket-owner", account, "--region", region, "--output", "json",
    ]))
    if not versioned(state_object.get("VersionId")):
        raise ValueError("Failed-plan diagnostics require versioned private state")
    output = Path(environment["RUNNER_TEMP"])
    plan_file = output / "shared-tools-plan-diagnostic.json"
    evidence_file = output / "shared-tools-plan-evidence.json"
    prefix = f"codebuild/artifacts/shared-agentcore-tools/{run_id}-{attempt}"

    def put(path, key):
        result = json.loads(read([
            "aws", "s3api", "put-object", "--bucket", bucket, "--key", key, "--body", str(path),
            "--expected-bucket-owner", account, "--if-none-match", "*", "--region", region,
            "--content-type", "application/json", "--output", "json",
        ]))
        if not versioned(result.get("VersionId")):
            raise ValueError("Failed-plan object did not return an immutable version")
        return result["VersionId"]

    plan_file.write_bytes(plan_bytes)
    plan_file.chmod(0o600)
    plan_key = prefix + ".plan-diagnostic.json"
    plan_version = put(plan_file, plan_key)
    evidence = {
        "evidence_type": "plan-diagnostic", "status": outcome, "mode": mode,
        "workflow": "shared-agentcore-tools-deploy.yml",
        "deployment_accepted": False, "source_revision": source, "workflow_revision": definition,
        "run_id": run_id, "run_attempt": attempt, "account": account, "region": region,
        "environment": target, "caller_arn": caller["Arn"], "deployment_role_arn": role_arn,
        "saved_plan_sha256": sha256(saved_plan.read_bytes()),
        "plan_json": {"bucket": bucket, "key": plan_key, "version_id": plan_version,
                      "sha256": sha256(plan_bytes)},
        "config": {"backend_sha256": sha256(backend.read_bytes()),
                   "tfvars_sha256": sha256(variables.read_bytes()),
                   "state_identity_sha256": sha256(f"{bucket}/{state_key}".encode())},
        # This is an observation after planning, not a claim about the plan's input version.
        "state_object_observed_after_plan": {"bucket": bucket, "key": state_key,
                                             "version_id": state_object["VersionId"],
                                             "etag": state_object.get("ETag")},
        "terraform_version": plan.get("terraform_version"),
        "resource_drift": plan.get("resource_drift", []),
        "provider_environment_variable_names": sorted(name for name in environment if name.startswith("TF_AWS_")),
    }
    evidence_file.write_text(json.dumps(evidence, indent=2) + "\n")
    evidence_file.chmod(0o600)
    put(evidence_file, prefix + ".plan-evidence.json")
    return True


if __name__ == "__main__":
    os.umask(0o077)
    try:
        retained = retain(os.environ)
    except (KeyError, OSError, ValueError, subprocess.CalledProcessError):
        raise SystemExit("Private plan diagnostics could not be retained; no deployment is accepted") from None
    print("Plan diagnostics retained in the protected state bucket; this is not deployment evidence" if retained
          else "No completed saved plan was available for private failure diagnostics")
