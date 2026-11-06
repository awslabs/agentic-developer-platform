"""Fail-closed checks for the reviewed GitLab runner deployment path."""

import argparse
import json
import re
import sys


REQUIRED_TYPES = {"aws_autoscaling_group", "aws_launch_template", "helm_release"}


def resources(module):
    yield from module.get("resources", [])
    for child in module.get("module_calls", {}).values():
        yield from resources(child.get("module", {}))


def check_plan(plan, operation):
    changes = plan.get("resource_changes", [])
    noop = (
        operation == "apply"
        and plan.get("applyable") is False
        and all(item["change"]["actions"] == ["no-op"] for item in changes)
        and all(item["actions"] == ["no-op"] for item in plan.get("output_changes", {}).values())
    )
    # Terraform can save a partial plan alongside a failed provider validation.
    # Resource additions in that JSON do not make the plan executable.
    if (
        plan.get("complete") is not True
        or (plan.get("applyable") is not True and not noop)
        or plan.get("errored") is not False
        or plan.get("deferred_changes")
    ):
        raise ValueError("saved plan must be complete, executable and error-free")
    configured = {item["type"] for item in resources(plan["configuration"]["root_module"])}
    missing = REQUIRED_TYPES - configured
    if missing:
        raise ValueError("runner capacity and manager implementation not present: " + ", ".join(sorted(missing)))
    if operation == "apply" and any("delete" in item["change"]["actions"] for item in changes):
        raise ValueError("apply cannot destroy runner resources; use the guarded destroy workflow")
    if operation == "destroy" and any("create" in item["change"]["actions"] for item in changes):
        raise ValueError("destroy cannot create runner resources")


def check_identity(identity, expected_role):
    match = re.fullmatch(r"arn:aws:iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]+)", expected_role)
    if not match:
        raise ValueError("expected deployment role must be an exact IAM role ARN")
    account, role = match.groups()
    if identity.get("Account") != account or not re.fullmatch(
        rf"arn:aws:sts::{account}:assumed-role/{re.escape(role.split('/')[-1])}/[^/]+",
        identity.get("Arn", ""),
    ):
        raise ValueError("deployment account or assumed role does not match protected environment")


def check_backend(metadata, expected_bucket):
    backend = metadata["backend"]
    config = backend["config"]
    if backend["type"] != "s3" or not expected_bucket or config["bucket"] != expected_bucket or config["key"] != "dev/modules/gitlab-runners/terraform.tfstate" or config["encrypt"] is not True:
        raise ValueError("runner backend must be the protected, encrypted dev runners state")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("identity", "backend", "apply", "destroy"))
    parser.add_argument("--expected-role")
    parser.add_argument("--expected-bucket")
    args = parser.parse_args()
    try:
        payload = json.load(sys.stdin)
        if args.operation == "identity":
            check_identity(payload, args.expected_role or "")
        elif args.operation == "backend":
            check_backend(payload, args.expected_bucket or "")
        else:
            check_plan(payload, args.operation)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"Runner deployment guard refused {args.operation}: {error}\n")


if __name__ == "__main__":
    main()
