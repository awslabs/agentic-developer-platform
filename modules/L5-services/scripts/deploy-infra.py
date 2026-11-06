#!/usr/bin/env python3
"""Plan/apply independent L5 roots; never enable consumers or claim qualification."""

import argparse
import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

INFRA = Path(__file__).resolve().parents[1] / "infra"
SERVICES = {"security", "devops", "transform", "well-architected", "finops", "foundation"}
COMMON = {
    "vpc_id", "private_subnet_ids", "db_security_group_id", "endpoint_security_group_id",
    "db_port", "permissions_boundary_arn", "gateway_api_arn", "rds_connect_arn",
    "agent_registry_table_name",
}
FINOPS_SURFACE_FIELDS = {
    "agent_identity_selector", "conversation_selector", "turn_selector", "question_selector",
    "turn_receipt_attribute", "answer_selector", "turn_status_selector",
    "answer_classification_selector", "history_complete_selector", "surface_revision",
}
SPEC = importlib.util.spec_from_file_location("l5_worker_lifecycle", Path(__file__).with_name("worker-lifecycle.py"))
LIFECYCLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(LIFECYCLE)


def selection(value):
    result = list(dict.fromkeys(item.strip().replace("_", "-") for item in value.split(",")))
    if not result or any(item not in SERVICES for item in result):
        raise ValueError("Select foundation and/or named L5 services")
    return sorted(result, key=lambda item: (item != "foundation", result.index(item)))


def validate_config(config):
    if config.get("schema_version") != 1:
        raise ValueError("Expected version 1 private L5 infrastructure configuration")
    if config.get("environment") not in {"dev", "test", "prod"}:
        raise ValueError("Unsupported protected environment")
    if not re.fullmatch(r"[0-9]{12}", config.get("account_id", "")):
        raise ValueError("Select the target account")
    if not re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9]+", config.get("region", "")):
        raise ValueError("Select the target region")
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", config.get("state_bucket", "")):
        raise ValueError("Select the existing private state bucket")
    if not re.fullmatch(rf"arn:aws:iam::{config['account_id']}:role/[A-Za-z0-9+=,.@_/-]+", config.get("role_arn", "")):
        raise ValueError("Select the exact target deployment role")
    return config


def variables(config, service, artifact):
    values = {"environment": config["environment"], "aws_region": config["region"], "aws_account_id": config["account_id"]}
    if service == "foundation":
        values["gateway_role_name"] = config["gateway_role_name"]
        if "gateway_transport_mode" in config:
            values["gateway_transport_mode"] = config["gateway_transport_mode"]
        return values
    common = config["platform"]
    if set(common) != COMMON:
        raise ValueError("Platform reference manifest differs from the L5 input contract")
    selected = config["services"][service.replace("-", "_")]
    expected = {"worker_factory", "worker_environment"} | ({"native_event_pattern"} if service == "devops" else set())
    expected |= {"executor_image_uri", "executor_surface"} if service == "finops" else set()
    if set(selected) != expected:
        raise ValueError("Each service must specify its installed factory and runtime environment")
    if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9.]*:[A-Za-z_][A-Za-z_0-9]*", selected["worker_factory"]):
        raise ValueError("Select an installed runtime factory")
    reserved = {"L5_SERVICE_ID", "L5_WORKER_FACTORY", "L5_WORK_QUEUE_URL"}
    if reserved & set(selected["worker_environment"]):
        raise ValueError("Service identity and transport are Terraform-owned")
    if service == "finops" and (not isinstance(selected["executor_image_uri"], str) or not re.fullmatch(
        rf"{config['account_id']}\.dkr\.ecr\.{config['region']}\.amazonaws\.com/"
        rf"adp-{config['environment']}-l5-finops-executor@sha256:[0-9a-f]{{64}}",
        selected["executor_image_uri"],
    )):
        raise ValueError("FinOps executor image must use the selected private repository and immutable digest")
    if service == "finops":
        surface = selected["executor_surface"]
        try:
            decoded = json.loads(surface) if isinstance(surface, str) else None
        except ValueError:
            decoded = None
        if (not isinstance(decoded, dict)
                or not FINOPS_SURFACE_FIELDS <= set(decoded)
                or set(decoded) - FINOPS_SURFACE_FIELDS - {"agent_label", "data_role_selector"}
                or any(not isinstance(value, str) or not value for value in decoded.values())):
            raise ValueError("FinOps executor surface must match the selected private console contract")
    values.update(common)
    values.update(selected)
    values.update(artifact)
    return values


class PlanRejected(ValueError):
    def __init__(self, message, category, changes, next_action):
        super().__init__(message)
        self.diagnosis = {"category": category, "changes": changes, "next_action": next_action}


def review_plan(plan):
    """A normal release may add/update resources; migration/destruction is separate."""
    if plan.get("errored") or plan.get("complete") is False:
        raise PlanRejected("Terraform plan is incomplete", "incomplete_plan", [],
                           "Run deploy-infra.py plan with the same selected service, private configuration and immutable artifact; "
                           "inspect that plan's private Terraform log and resolve the incomplete plan before applying.")
    destructive = [{"address": change["address"], "actions": change["change"]["actions"],
                    "reason": "tainted" if change.get("action_reason") == "replace_because_tainted" else "replacement_or_deletion"}
                   for change in plan.get("resource_changes", [])
                   if "delete" in change.get("change", {}).get("actions", [])]
    if destructive:
        raise PlanRejected("Release plan deletes or replaces resources; use the state migration procedure",
                           "destructive_plan", destructive,
                           "Compare these exact resources with live identities and backed-up state. For tainted entries, "
                           "verify creation completed before a reviewed state repair; otherwise follow the state migration procedure. "
                           "Do not delete resources, remove the guard or blindly retry.")
    return [{"address": change["address"], "actions": change["change"]["actions"]}
            for change in plan.get("resource_changes", []) if change["change"]["actions"] != ["no-op"]]


def failure_diagnosis(output):
    """Describe the failure without copying private provider error messages."""
    actions = sorted(set(re.findall(r"not authorized to perform:\s*([a-z0-9-]+:[A-Za-z0-9]+)", output)))[:8]
    if actions or re.search(r"AccessDenied|UnauthorizedOperation|not authorized", output):
        return {"category": "access_denied", "actions": actions,
                "next_action": "Check the selected deployment role and its permissions boundary for the denied APIs."}
    if "prevent_destroy" in output:
        return {"category": "retained_resource", "actions": [],
                "next_action": "Review the proposed replacement against the retained state; preserve the resource until migration is approved."}
    if "Error acquiring the state lock" in output:
        return {"category": "state_locked", "actions": [],
                "next_action": "Check the active deployment owning the state lock before retrying; do not force-unlock an active run."}
    return {"category": "terraform_failed", "actions": [],
            "next_action": "Inspect the private Terraform log for this service and stage before retrying."}


def terraform(service, args, private_dir, environment):
    command = ["terraform", f"-chdir={INFRA / service}", *args]
    result = subprocess.run(command, env=environment, text=True, capture_output=True)
    with (private_dir / "terraform.log").open("a") as log:
        log.write(result.stdout + result.stderr)
    if result.returncode:
        diagnosis = failure_diagnosis(result.stdout + result.stderr)
        receipt = {"schema_version": 1, "phase": "infrastructure", "service": service,
                   "stage": args[0], "result": "failed", "qualification": "not_run", **diagnosis}
        (private_dir / "failure-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        denied = ": " + ", ".join(diagnosis["actions"]) if diagnosis["actions"] else ""
        raise RuntimeError(f"Terraform {args[0]} failed for {service} ({diagnosis['category']}{denied}). "
                           f"{diagnosis['next_action']} Private log: {service}/terraform.log")
    return result.stdout


def deploy(args):
    config = validate_config(json.loads(args.config.read_text()))
    services = selection(args.services)
    os.environ["AWS_DEFAULT_REGION"] = config["region"]
    os.environ["AWS_REGION"] = config["region"]
    LIFECYCLE.identity(config["account_id"], config["role_arn"])
    artifact = {}
    if args.action != "status" and any(item != "foundation" for item in services):
        key, version, digest = LIFECYCLE.manifest_for(args.bucket, args.source_sha, args.wheel_sha256, args.manifest_version)
        artifact = {"worker_artifact_bucket": args.bucket, "worker_artifact_key": key,
                    "worker_artifact_version": version, "worker_artifact_sha256": digest}
    # Validate every selected input before the first mutation.
    inputs = {service: variables(config, service, artifact) for service in services} if args.action != "status" else {}
    args.receipt_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    prepared = []
    for service in services:
        directory = args.receipt_dir / service
        directory.mkdir(mode=0o700, exist_ok=True)
        key = f"l5-services/{config['environment']}/{config['region']}/{service}/terraform.tfstate"
        environment = {**os.environ, "TF_IN_AUTOMATION": "true", "TF_DATA_DIR": str(directory / ".terraform")}
        backend = directory / "backend.json"
        backend.write_text(json.dumps({"bucket": config["state_bucket"], "key": key,
                                       "region": config["region"], "encrypt": True, "use_lockfile": True}))
        terraform(service, ["init", "-input=false", "-reconfigure", f"-backend-config={backend}"], directory, environment)
        receipt = {"schema_version": 1, "phase": "infrastructure", "service": service,
                   "environment": config["environment"], "account_id": config["account_id"],
                   "region": config["region"], "state_key": key, "source_sha": args.source_sha,
                   "artifact": artifact, "qualification": "not_run", "result": "pending"}
        if args.action == "status":
            receipt["outputs"] = json.loads(terraform(service, ["output", "-json"], directory, environment))
            receipt["result"] = "observed"
            (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
            continue
        varfile = directory / "inputs.tfvars.json"
        varfile.write_text(json.dumps(inputs[service]))
        planfile = directory / "install.tfplan"
        terraform(service, ["plan", "-input=false", "-lock-timeout=5m", f"-var-file={varfile}", f"-out={planfile}"], directory, environment)
        plan = json.loads(terraform(service, ["show", "-json", str(planfile)], directory, environment))
        try:
            receipt["changes"] = review_plan(plan)
        except PlanRejected as error:
            failure = {**receipt, "stage": "review_plan", "result": "failed", **error.diagnosis}
            failure_path = directory / "failure-receipt.json"
            failure_path.write_text(json.dumps(failure, indent=2) + "\n")
            resources = ", ".join(f"{change['address']} ({change['reason']})" for change in error.diagnosis["changes"][:8])
            raise ValueError(f"{service}: {error}. {resources}. {error.diagnosis['next_action']} "
                             f"Private receipt: {service}/failure-receipt.json") from error
        receipt["result"] = "planned"
        (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
        prepared.append((service, directory, environment, planfile, receipt))
    # Plan the entire group before applying any stack. Each state has its own lock.
    for service, directory, environment, planfile, receipt in prepared:
        if args.action == "apply":
            terraform(service, ["apply", "-input=false", "-lock-timeout=5m", str(planfile)], directory, environment)
            receipt["outputs"] = json.loads(terraform(service, ["output", "-json"], directory, environment))
            receipt["result"] = "applied"
            (directory / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return services


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "apply", "status"])
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--services", required=True)
    parser.add_argument("--bucket")
    parser.add_argument("--source-sha")
    parser.add_argument("--wheel-sha256")
    parser.add_argument("--manifest-version")
    parser.add_argument("--receipt-dir", required=True, type=Path)
    args = parser.parse_args()
    args.config = args.config.resolve()
    args.receipt_dir = args.receipt_dir.resolve()
    os.umask(0o077)
    deploy(args)


if __name__ == "__main__":
    main()
