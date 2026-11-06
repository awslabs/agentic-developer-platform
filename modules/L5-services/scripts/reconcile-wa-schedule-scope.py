#!/usr/bin/env python3
"""Reconcile only WA schedule admission permissions through one verified saved plan."""

import argparse
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import tempfile

SPEC = importlib.util.spec_from_file_location("wa_deploy", Path(__file__).with_name("deploy-infra.py"))
DEPLOY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPLOY)
LIFECYCLE = DEPLOY.LIFECYCLE
SERVICE = "well-architected"
POLICY = "module.runtime.aws_iam_role_policy.repair"
REGISTRY = "module.registration.aws_dynamodb_table_item.repair"
NEXT_ACTION = (
    "Inspect the private reconciliation receipt and the selected WA root/configuration. "
    "Resolve unrelated drift separately, then generate a fresh verified plan. "
    "Do not force-unlock state, widen the allowlist, enable consumers, or blindly retry an uncertain apply."
)


class ReconciliationRejected(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise ReconciliationRejected(message)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def document(address, value):
    # DynamoDB string sets have no order; do not treat a readback ordering as drift.
    result = copy.deepcopy(value)
    if address == REGISTRY and isinstance(result, dict):
        scopes = result.get("credential_scopes", {}).get("SS")
        if isinstance(scopes, list) and all(isinstance(scope, str) for scope in scopes):
            result["credential_scopes"]["SS"] = sorted(scopes)
    return result


def expected_documents(config, repair_role):
    """Exact reviewed documents, with no operator-supplied permission allowlist."""
    account, region = config["account_id"], config["region"]
    _, repair = LIFECYCLE.functions(config["environment"], "well_architected")
    platform = config["platform"]
    require(re.fullmatch(rf"arn:aws:execute-api:{region}:{account}:[a-z0-9]+/\*/\*/internal/\*", platform["gateway_api_arn"])
            and re.fullmatch(rf"arn:aws:rds-db:{region}:{account}:dbuser:[A-Za-z0-9-]+/[A-Za-z0-9_]+", platform["rds_connect_arn"]),
            "Gateway and database references must belong to the selected account and region")
    function_arn = f"arn:aws:lambda:{region}:{account}:function:{repair}"
    gateway = platform["gateway_api_arn"].removesuffix("/*/*/internal/*")
    routes = [f"{gateway}/*/{method}/internal/v1/{consumer}/l5-wa-contributions{suffix}"
              for consumer in ("context", "insights")
              for method, suffix in (("POST", ""), ("POST", "/*/invalidate"), ("GET", "/*"))]
    network = ["ec2:CreateNetworkInterface", "ec2:DescribeNetworkInterfaces", "ec2:DeleteNetworkInterface",
               "ec2:AssignPrivateIpAddresses", "ec2:UnassignPrivateIpAddresses"]
    policy = {"Version": "2012-10-17", "Statement": [
        {"Effect": "Allow", "Action": ["logs:CreateLogStream", "logs:PutLogEvents"],
         "Resource": f"arn:aws:logs:{region}:{account}:log-group:/aws/lambda/{repair}:*"},
        {"Effect": "Allow", "Action": ["sqs:SendMessage"],
         "Resource": f"arn:aws:sqs:{region}:{account}:{repair.removesuffix('-repair')}-work"},
        {"Effect": "Allow", "Action": ["rds-db:connect"], "Resource": platform["rds_connect_arn"]},
        {"Effect": "Allow", "Action": network, "Resource": "*"},
        {"Effect": "Deny", "Action": network, "Resource": "*",
         "Condition": {"ArnEquals": {"lambda:SourceFunctionArn": function_arn}}},
        {"Effect": "Allow", "Action": ["execute-api:Invoke"], "Resource": routes + [
            f"{gateway}/*/POST/internal/v1/l5/well-architected/schedule-admissions"]},
    ]}
    agent_id = "l5-well-architected-repair"
    item = {name: {"S": value} for name, value in {
        "agent_id": agent_id, "role_arn": f"arn:aws:iam::{account}:role/{repair}",
        "iam_role_id": repair_role["RoleId"], "agent_name": agent_id,
        "org_id": "__platform__", "team_id": "__agents__", "owner": "platform",
        "scope": "internal", "status": "active",
    }.items()}
    item["credential_scopes"] = {"SS": ["l5:contribution-repair", "l5:schedule-admission"]}
    return {POLICY: policy, REGISTRY: item}


def legacy_documents(desired):
    legacy = copy.deepcopy(desired)
    legacy[POLICY]["Statement"][-1]["Resource"].pop()
    legacy[REGISTRY]["credential_scopes"]["SS"].remove("l5:schedule-admission")
    return legacy


def observe(config):
    environment, account, region = config["environment"], config["account_id"], config["region"]
    worker, repair = LIFECYCLE.functions(environment, "well_architected")
    uuid = LIFECYCLE.mapping(worker, environment, account, "well_architected")
    runtime = LIFECYCLE.state(environment, worker, repair, uuid)
    require(runtime["mapping"] == "Disabled" and runtime["rule"] == "DISABLED", "WA consumers must already be paused")
    runtime["mapping_details"] = LIFECYCLE.aws("lambda", "get-event-source-mapping", "--uuid", uuid)
    runtime["rule_details"] = LIFECYCLE.aws("events", "describe-rule", "--name", repair)
    require(runtime["mapping_details"].get("State") == "Disabled"
            and runtime["mapping_details"].get("UUID") == uuid
            and runtime["rule_details"].get("State") == "DISABLED"
            and runtime["rule_details"].get("Name") == repair, "WA consumer readbacks changed")
    for label, function in (("worker", worker), ("repair", repair)):
        observed = runtime[label]
        require(observed.get("FunctionArn") == f"arn:aws:lambda:{region}:{account}:function:{function}"
                and observed.get("Role") == f"arn:aws:iam::{account}:role/{function}", "WA function identity changed")
        require(observed.get("State") == "Active" and observed.get("LastUpdateStatus") == "Successful",
                "WA functions must be healthy before reconciliation")
    role = LIFECYCLE.aws("iam", "get-role", "--role-name", repair)["Role"]
    require(role.get("Arn") == f"arn:aws:iam::{account}:role/{repair}" and role.get("RoleId"), "WA repair role changed")
    policy = LIFECYCLE.aws("iam", "get-role-policy", "--role-name", repair, "--policy-name", repair)["PolicyDocument"]
    item = LIFECYCLE.aws("dynamodb", "get-item", "--table-name", config["platform"]["agent_registry_table_name"],
                         "--key", json.dumps({"agent_id": {"S": "l5-well-architected-repair"}}),
                         "--consistent-read").get("Item")
    return {"runtime": runtime, "documents": {POLICY: policy, REGISTRY: document(REGISTRY, item)},
            "desired": expected_documents(config, role), "mapping_id": uuid}


def has_unknown(value):
    if isinstance(value, dict):
        return any(has_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(has_unknown(item) for item in value)
    return value is True


def review_plan(plan, config, initial):
    """Accept only missing route/scope additions, including a safe partial retry/no-op."""
    require(not plan.get("errored") and plan.get("complete") is True and not plan.get("deferred_changes"),
            "Reconciliation plan must be complete")
    require(all(item.get("actions") == ["no-op"] for item in plan.get("output_changes", {}).values()),
            "Root output changes require separate review")
    desired, before = initial["desired"], initial["documents"]
    legacy = legacy_documents(desired)
    require(all(before[address] in (legacy[address], desired[address]) for address in desired),
            "Installed permissions differ from the reviewed legacy or current scope")
    managed = [item for item in plan.get("resource_changes", []) if item.get("mode") == "managed"]
    resources = {item["address"]: item for item in managed}
    require(len(resources) == len(managed), "Duplicate resource changes are invalid")
    require(set(desired) <= set(resources), "Both existing permission resources must be present in the plan")
    changes = []
    _, repair = LIFECYCLE.functions(config["environment"], "well_architected")
    for address, resource in resources.items():
        change = resource["change"]
        require(not resource.get("previous_address") and not change.get("importing"), "State moves or imports require separate review")
        if address not in desired:
            require(change["actions"] == ["no-op"], "Plan changes a resource outside the two-permission allowlist")
            continue
        field = "policy" if address == POLICY else "item"
        expected_action = ["no-op"] if before[address] == desired[address] else ["update"]
        require(change["actions"] == expected_action and not change.get("replace_paths"),
                "Only an existing permission's missing schedule scope may change")
        require(not has_unknown(change.get("after_unknown", {})), "Permission values must be fully known")
        old, new = change["before"], change["after"]
        require(isinstance(old, dict) and isinstance(new, dict), "Permission resource is not installed")
        require({k: v for k, v in old.items() if k != field} == {k: v for k, v in new.items() if k != field},
                "Permission resource identity or other attributes changed")
        if address == POLICY:
            require(new.get("name") == repair and new.get("role") == repair, "Repair policy target changed")
        else:
            require(new.get("table_name") == config["platform"]["agent_registry_table_name"]
                    and new.get("hash_key") == "agent_id", "Registry target changed")
        require(document(address, json.loads(old[field])) == before[address], "Plan permissions differ from fresh live readback")
        require(document(address, json.loads(new[field])) == desired[address], "Plan grants more than the exact reviewed schedule scope")
        if expected_action == ["update"]:
            changes.append({"address": address, "actions": expected_action})
    return changes


def plan_review_digest(plan, args, values, initial):
    """Bind explicit approval to all managed changes, private inputs and live runtime.

    Exclude Terraform timestamps and the OIDC session's data-source identity, which
    legitimately differ between the plan-only and apply jobs. No private values
    leave this function; its digest is the dispatch handoff.
    """
    return digest({"schema": "wa-schedule-scope-review/v1", "definition_revision": os.environ.get("GITHUB_SHA"),
        "verifier_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha": args.source_sha, "wheel_sha256": args.gateway_wheel_sha256,
        "manifest_version": args.manifest_version, "role_arn": args.role_arn,
        "configuration": plan.get("configuration"), "inputs": values,
        "resources": sorted((item for item in plan.get("resource_changes", []) if item.get("mode") == "managed"),
                            key=lambda item: item["address"]),
        "outputs": plan.get("output_changes", {}), "runtime": initial})


def reconcile(args, config, directory, receipt):
    config = DEPLOY.validate_config(config)
    require((config["environment"], config["region"], config["role_arn"]) ==
            (args.environment, args.region, args.role_arn), "Private configuration differs from the protected target")
    require(args.action == "plan" or re.fullmatch(r"[0-9a-f]{64}", args.reviewed_plan_digest or ""),
            "Apply requires the reviewed plan-only receipt's digest")
    os.environ["AWS_REGION"] = os.environ["AWS_DEFAULT_REGION"] = config["region"]
    receipt["stage"] = "target_identity"
    LIFECYCLE.identity(config["account_id"], config["role_arn"])
    receipt["stage"] = "immutable_artifact"
    key, version, archive_digest = LIFECYCLE.manifest_for(args.bucket, args.source_sha,
                                                        args.gateway_wheel_sha256, args.manifest_version)
    artifact = {"worker_artifact_bucket": args.bucket, "worker_artifact_key": key,
                "worker_artifact_version": version, "worker_artifact_sha256": archive_digest}
    values = DEPLOY.variables(config, SERVICE, artifact)
    receipt["stage"] = "paused_runtime_readback"
    initial = observe(config)
    # Config drift must not be hidden by the two-field permission repair.
    worker_environment = {**values["worker_environment"], "L5_SERVICE_ID": "well_architected",
                          "L5_WORKER_FACTORY": values["worker_factory"], "L5_RDS_CONNECT_ARN": values["rds_connect_arn"],
                          "L5_WORK_QUEUE_URL": initial["runtime"]["worker"]["Environment"]["Variables"].get("L5_WORK_QUEUE_URL")}
    require(values["worker_factory"] == LIFECYCLE.FACTORIES["well_architected"]
            and all(initial["runtime"][label]["Environment"]["Variables"] == worker_environment for label in ("worker", "repair")),
            "Private configuration differs from installed WA configuration")
    backend = directory / "backend.json"
    backend.write_text(json.dumps({"bucket": config["state_bucket"],
        "key": f"l5-services/{config['environment']}/{config['region']}/{SERVICE}/terraform.tfstate",
        "region": config["region"], "encrypt": True, "use_lockfile": True}))
    varfile = directory / "inputs.tfvars.json"
    varfile.write_text(json.dumps(values))
    environment = {key: value for key, value in os.environ.items()
                   if key != "L5_INFRA_CONFIG_JSON" and not key.startswith(("TF_CLI_ARGS", "TF_VAR_"))}
    environment.update(TF_IN_AUTOMATION="true", TF_DATA_DIR=str(directory / ".terraform"), TF_WORKSPACE="default")

    def terraform(arguments):
        return DEPLOY.terraform(SERVICE, arguments, directory, environment)

    receipt["stage"] = "terraform_init"
    terraform(["init", "-input=false", "-reconfigure", f"-backend-config={backend}"])
    saved = directory / "reconciliation.tfplan"
    receipt["stage"] = "terraform_plan"
    terraform(["plan", "-input=false", "-lock-timeout=5m", f"-var-file={varfile}", f"-out={saved}"])
    plan_digest = hashlib.sha256(saved.read_bytes()).hexdigest()
    receipt["stage"] = "exact_plan_review"
    plan = json.loads(terraform(["show", "-json", str(saved)]))
    receipt["requested_changes"] = [{"address": item["address"], "actions": item["change"]["actions"]}
                                    for item in plan.get("resource_changes", [])
                                    if item.get("mode") == "managed" and item["change"]["actions"] != ["no-op"]][:40]
    receipt["changes"] = review_plan(plan, config, initial)
    receipt["reviewed_plan_digest"] = plan_review_digest(plan, args, values, initial)
    receipt["saved_plan_sha256"] = plan_digest
    receipt["documents"] = {address: {"before_sha256": digest(initial["documents"][address]),
                                      "after_sha256": digest(document)} for address, document in initial["desired"].items()}
    receipt["runtime_sha256"] = digest(initial["runtime"])
    receipt["result"] = "planned"
    if args.action == "plan":
        return
    require(args.reviewed_plan_digest == receipt["reviewed_plan_digest"],
            "Plan, inputs, source or runtime differ from the reviewed plan-only receipt; review a new plan")
    receipt["stage"] = "pre_apply_readback"
    require(observe(config) == initial, "WA runtime or permissions changed after planning")
    require(hashlib.sha256(saved.read_bytes()).hexdigest() == plan_digest, "Reviewed saved plan changed")
    if receipt["changes"]:
        receipt["stage"] = "terraform_apply"
        receipt["apply_started"] = True
        # No replan, targeting, lock bypass, direct IAM update, or registry write.
        terraform(["apply", "-input=false", "-lock-timeout=5m", str(saved)])
        receipt["apply_finished"] = True
    receipt["stage"] = "post_apply_readback"
    final = observe(config)
    require(final["runtime"] == initial["runtime"] and final["mapping_id"] == initial["mapping_id"],
            "WA code, configuration or paused consumer changed during reconciliation")
    require(final["documents"] == initial["desired"], "Installed permission readback differs from the verified plan")
    worker, repair = LIFECYCLE.functions(config["environment"], "well_architected")
    LIFECYCLE.scope_readback(config["environment"], config["account_id"], worker, repair, final["runtime"], "well_architected")
    receipt.update(result="reconciled", stage="complete", installed_scope_verified=True, runtime_unchanged=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["plan", "apply"])
    for name in ("environment", "region", "role-arn", "bucket", "source-sha", "gateway-wheel-sha256", "manifest-version"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--reviewed-plan-digest", default="", help="Required for apply; from the reviewed plan-only receipt")
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    args.receipt.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Claim the receipt before any AWS work; an existing path cannot cause a
    # late write failure after a successful or partial apply.
    output = args.receipt.open("x")
    receipt = {"schema_version": 1, "phase": "infrastructure", "action": "reconcile-wa-schedule-scope",
               "mode": args.action, "service": "well_architected", "result": "failed", "stage": "private_config",
               "qualification": "not_run", "apply_started": False, "apply_finished": False,
               "source_sha": args.source_sha, "definition_revision": os.environ.get("GITHUB_SHA"),
               "retained": "No code rollout or consumer change requested; queues and history retained"}
    try:
        config = json.loads(os.environ.pop("L5_INFRA_CONFIG_JSON"))
        with tempfile.TemporaryDirectory(prefix="l5-wa-scope-", dir=os.environ.get("RUNNER_TEMP")) as temporary:
            try:
                reconcile(args, config, Path(temporary), receipt)
            except Exception:
                failure = Path(temporary, "failure-receipt.json")
                if failure.exists():
                    diagnosis = json.loads(failure.read_text())
                    receipt["terraform_diagnosis"] = {name: diagnosis[name] for name in ("category", "actions")}
                raise
    except Exception as error:
        receipt.update(result="failed", category=type(error).__name__, next_action=NEXT_ACTION)
        if isinstance(error, ReconciliationRejected):
            receipt["reason"] = str(error)
        print(f"WA scope reconciliation failed during {receipt['stage']}. {NEXT_ACTION}")
        return 1
    finally:
        # Retain only fixed diagnostics and hashes. Private Terraform inputs,
        # plan and logs never enter workflow logs or the evidence upload.
        try:
            json.dump(receipt, output, indent=2)
            output.write("\n")
        finally:
            output.close()
    print(f"WA scope reconciliation {receipt['result']}; review digest {receipt['reviewed_plan_digest']}. Native qualification was not run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
