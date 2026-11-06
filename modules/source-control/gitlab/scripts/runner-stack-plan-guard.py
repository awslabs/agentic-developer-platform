#!/usr/bin/env python3
"""Restrict saved runner plans to the dedicated runner state and resources."""

import argparse
import hashlib
import json
import re
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runners" / "scripts"))
from deployment_guard import check_plan


OWNED_TYPES = {
    "terraform_data", "helm_release",
    "aws_autoscaling_group", "aws_launch_template", "aws_iam_role",
    "aws_eks_access_entry", "aws_eks_access_policy_association",
    "aws_iam_role_policy", "aws_iam_role_policy_attachment", "aws_iam_policy",
    "aws_iam_instance_profile", "aws_security_group", "aws_security_group_rule",
    "aws_vpc_security_group_ingress_rule", "aws_vpc_security_group_egress_rule",
    "aws_s3_bucket", "aws_s3_bucket_policy", "aws_s3_bucket_lifecycle_configuration",
    "aws_s3_bucket_public_access_block", "aws_s3_bucket_server_side_encryption_configuration",
    "aws_s3_bucket_versioning", "aws_ssm_parameter", "aws_vpc_endpoint",
    "aws_kms_key", "aws_kms_alias", "aws_kms_key_policy",
    "kubernetes_namespace", "kubernetes_namespace_v1", "kubernetes_service_account",
    "kubernetes_service_account_v1", "kubernetes_role_v1", "kubernetes_role_binding_v1",
    "kubernetes_config_map_v1", "kubernetes_resource_quota_v1",
}


def check_saved_plan(plan, operation, account, region, bucket, approved_plan_sha256=None, release_lock_sha256=None):
    if approved_plan_sha256 is not None:
        digest = hashlib.sha256(json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", approved_plan_sha256) or digest != approved_plan_sha256:
            raise ValueError("saved plan differs from the reviewed plan")
    if not re.fullmatch(r"[0-9]{12}", account) or not re.fullmatch(r"[a-z]{2}(-[a-z]+)+-[0-9]+", region) or not bucket:
        raise ValueError("protected target account, region and bucket are required")
    variables = plan["variables"]
    if release_lock_sha256 is not None and (
        not re.fullmatch(r"[0-9a-f]{64}", release_lock_sha256)
        or variables.get("release_lock_sha256", {}).get("value") != release_lock_sha256
        or variables.get("release_lock_file", {}).get("value") != ".terraform/runners-release-lock.json"
    ):
        raise ValueError("saved plan must bind the reviewed release lock at its portable path")
    if variables["environment"]["value"] != "dev" or variables["aws_region"]["value"] != region:
        raise ValueError("saved plan does not target the protected dev region")
    platform = variables["platform_state"]["value"]
    if platform["bucket"] != bucket or platform["key"] != "dev/platform/terraform.tfstate" or platform["region"] != region:
        raise ValueError("saved plan references the wrong platform discovery state")
    groups = variables["groups"]["value"]
    if not isinstance(groups, dict) or not groups:
        raise ValueError("saved plan has no approved numeric groups")
    for group_id, group in groups.items():
        if not re.fullmatch(r"[1-9][0-9]*", group_id) or not re.fullmatch(
            rf"arn:aws:secretsmanager:{re.escape(region)}:{account}:secret:adp/dev/gitlab-runner/{group_id}-[A-Za-z0-9]{{6}}",
            group["token_secret_arn"],
        ):
            raise ValueError("saved plan has a group token in the wrong account or region")
    if operation == "apply":
        endpoints = variables["job_endpoints"]["value"]
        if not isinstance(endpoints, dict) or set(endpoints) != set(groups) or any(
            not isinstance(value, dict) or not re.fullmatch(r"eni-[0-9a-f]{8,17}", value.get("gitlab_eni_id", ""))
            for value in endpoints.values()
        ):
            raise ValueError("saved plan endpoints must bind exactly the approved groups")
        if variables.get("runner_chart_archive", {}).get("value", "") != "":
            raise ValueError("saved plan must use the bundled reviewed runner chart")
        namespaces = plan["planned_values"]["outputs"]["group_namespaces"]["value"]
        if not isinstance(namespaces, dict) or namespaces != {
            group_id: {"manager": f"gitlab-runner-{group_id}", "jobs": f"gitlab-jobs-{group_id}"}
            for group_id in groups
        }:
            raise ValueError("saved plan namespaces differ from approved group bindings")
    check_plan(plan, operation)
    for resource in plan.get("resource_changes", []):
        if resource.get("mode", "managed") != "managed":
            continue
        if resource.get("type") not in OWNED_TYPES:
            raise ValueError("runner saved plan contains a non-runner resource type")
        address = resource.get("address", "")
        name = resource.get("name", "")
        if not isinstance(address, str) or not isinstance(name, str) or not name or not re.search(
            rf"(?:^|\.){re.escape(resource['type'])}\.{re.escape(name)}(?:\[[^\]]+\])?$", address
        ):
            raise ValueError("runner saved plan contains an invalid resource address")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("apply", "destroy"))
    parser.add_argument("--account", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--approved-plan-sha256")
    parser.add_argument("--release-lock-sha256")
    args = parser.parse_args()
    try:
        if args.operation == "apply" and not args.approved_plan_sha256:
            raise ValueError("approved saved plan digest required for apply")
        if args.operation == "apply" and not args.release_lock_sha256:
            raise ValueError("reviewed release lock digest required for apply")
        check_saved_plan(json.load(sys.stdin), args.operation, args.account, args.region, args.bucket,
                         args.approved_plan_sha256, args.release_lock_sha256)
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
        parser.exit(1, f"Runner saved-plan guard refused: {error}\n")


if __name__ == "__main__":
    main()
