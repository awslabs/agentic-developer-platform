#!/usr/bin/env python3
"""Accept only a complete, creation-only saved plan for the isolated builder."""
import argparse
import json
import re
import sys


OWNED = {"aws_iam_role.builder", "aws_iam_role_policy.builder", "aws_codebuild_project.builder"} | {
    f'aws_ecr_repository.runtime["{name}"]' for name in ("bootstrap", "helper", "job")
}


def check(plan, account, region):
    if (not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]+", region)
            or plan.get("complete") is not True or plan.get("applyable") is not True
            or plan.get("errored") is not False or plan.get("deferred_changes")):
        raise ValueError("builder plan must be complete, executable and target an exact account and region")
    variables = plan["variables"]
    if variables["account_id"]["value"] != account or variables["aws_region"]["value"] != region:
        raise ValueError("builder plan target differs from reviewed account or region")
    changes = plan["resource_changes"]
    if any(item["address"] != "data.aws_iam_policy.existing_boundary" for item in changes if item["mode"] == "data"):
        raise ValueError("builder plan reads an unexpected external resource")
    managed = [item for item in changes if item["mode"] == "managed"]
    if (len(managed) != len(OWNED) or {item["address"] for item in managed} != OWNED
            or any(item["change"]["actions"] != ["create"] for item in managed)):
        raise ValueError("builder plan must create only its exact owned resources")
    prior = plan.get("prior_state") or {}
    root = prior.get("values", {}).get("root_module", {})
    resources = root.get("resources", [])
    if (root.get("child_modules") or len(resources) > 1
            or any(item.get("mode") != "data" or item.get("address") != "data.aws_iam_policy.existing_boundary"
                   for item in resources)):
        raise ValueError("builder must start from its isolated empty state")
    configured = plan["configuration"]["root_module"]["resources"]
    if {item["address"] for item in configured if item["mode"] == "managed"} != {
        "aws_iam_role.builder", "aws_iam_role_policy.builder", "aws_codebuild_project.builder", "aws_ecr_repository.runtime"
    }:
        raise ValueError("builder configuration contains unexpected managed resources")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    args = parser.parse_args()
    try:
        check(json.load(sys.stdin), args.account, args.region)
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
        parser.exit(1, f"Builder saved-plan guard refused: {error}\n")


if __name__ == "__main__":
    main()
