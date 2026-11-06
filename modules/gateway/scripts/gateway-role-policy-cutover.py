#!/usr/bin/env python3
"""Prepare the gateway service role for the consolidated-policy cutover (#7944).

The gateway module's grants on ``adp-<env>-role-gateway-service`` moved from up to
twenty inline policies into three customer managed policies
(``modules/gateway/infra/gateway-role-policies.tf``). An environment that still
carries the inline policies is upgraded in two complete Terraform passes so the
role never loses a permission:

  pass 1  create + attach the managed policies while every live inline policy is
          retained byte-for-byte (``gateway_role_policy_cutover_keep_legacy`` and
          ``gateway_role_policy_cutover_inline_documents``), and the two retired
          managed policies stay with only their live attachments retained
          (``gateway_role_policy_cutover_managed_attachments``);
  pass 2  defaults; the legacy grants are destroyed under the saved-plan gate
          (platform/scripts/upgrade-plan-policy.py), which requires the attached
          replacement to contain every retired statement.

This script inspects the live role and writes the pass-1 tfvars JSON. It prints
one word on stdout for the caller (deploy-all.sh Step 3 or an operator):

  cutover   pass 1 is needed; the tfvars file was written
  retire    the managed policies are attached and legacy grants remain: run pass 2
  complete  nothing to do

It exits 2 without writing when pass 1 cannot succeed because the role is out of
managed-policy slots (quota 20): the output names every attachment so the operator
can free one -- on dev the hand-created duplicate ``adp-dev-tools-catalogue-read``.
It never changes the role.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

GROUPS = ("identity", "data", "agents")
ATTACHED_QUOTA = 20
# Keep in step with local.gateway_role_legacy_inline_names in gateway-role-policies.tf.
LEGACY_SUFFIXES = {
    "cognito-read",
    "cfn-template-read",
    "chat-logs-s3",
    "run-logs-read",
    "comprehend-pii",
    "mantle-bedrock-invoke",
    "xray-tracing",
    "secretsmanager-read",
    "vault-secrets",
    "webhook-secrets-kms",
    "cross-account",
    "rds-iam-auth",
    "elasticache-iam-auth",
    "identity-index",
    "ingestion-sqs-publish",
    "user-identity-index",
    "crew-checkpoints-s3",
    "crew-run-binding",
    "user-oauth",
    "agentcore-memory",
}


def legacy_suffix(environment: str, policy_name: str) -> str | None:
    match = re.fullmatch(rf"bedrockgw-{re.escape(environment)}-(?:policy-)?gateway-([a-z0-9-]+)", policy_name)
    if match and match.group(1) in LEGACY_SUFFIXES:
        return match.group(1)
    return None


def retired_managed_names(environment: str) -> set[str]:
    return {
        f"bedrockgw-{environment}-gateway-tools-catalogue-read",
        f"bedrockgw-{environment}-policy-gateway-cognito-lifecycle",
    }


def inspect_role(iam, environment: str) -> dict:
    role = f"adp-{environment}-role-gateway-service"
    inline_names: list[str] = []
    paginator = iam.get_paginator("list_role_policies")
    for page in paginator.paginate(RoleName=role):
        inline_names.extend(page["PolicyNames"])
    attached: list[str] = []
    for page in iam.get_paginator("list_attached_role_policies").paginate(RoleName=role):
        attached.extend(item["PolicyName"] for item in page["AttachedPolicies"])
    documents: dict[str, str] = {}
    for name in sorted(inline_names):
        suffix = legacy_suffix(environment, name)
        if suffix is None:
            continue
        document = iam.get_role_policy(RoleName=role, PolicyName=name)["PolicyDocument"]
        if isinstance(document, str):
            document = json.loads(document)
        if not isinstance(document, dict) or document.get("Version") != "2012-10-17":
            raise SystemExit(f"Invalid live gateway inline policy {name}")
        documents[suffix] = json.dumps(document, separators=(",", ":"), sort_keys=True)
    groups = {group: f"adp-{environment}-policy-gateway-{group}" for group in GROUPS}
    missing_groups = [name for name in groups.values() if name not in attached]
    retired_attached = sorted(set(attached) & retired_managed_names(environment))
    return {
        "role": role,
        "inline_names": sorted(inline_names),
        "attached": sorted(attached),
        "legacy_documents": documents,
        "missing_groups": missing_groups,
        "retired_attached": retired_attached,
    }


def decide(state: dict) -> str:
    if state["missing_groups"]:
        return "cutover"
    if state["legacy_documents"] or state["retired_attached"]:
        return "retire"
    return "complete"


def pass_one_tfvars(state: dict) -> dict:
    return {
        "gateway_role_policy_cutover_keep_legacy": True,
        "gateway_role_policy_cutover_inline_documents": state["legacy_documents"],
        "gateway_role_policy_cutover_managed_attachments": state["retired_attached"],
    }


def headroom_error(state: dict) -> str | None:
    needed = len(state["attached"]) + len(state["missing_groups"])
    if needed <= ATTACHED_QUOTA:
        return None
    lines = [
        f"{state['role']} has {len(state['attached'])} attached managed policies; pass 1 attaches "
        f"{len(state['missing_groups'])} more ({', '.join(state['missing_groups'])}) and the quota is {ATTACHED_QUOTA}.",
        "Detach a policy this repository does not declare before retrying (on dev: the hand-created "
        "duplicate adp-dev-tools-catalogue-read, whose statements the gateway's bedrockgw-dev-gateway-tools-catalogue-read "
        "already carries). Attached now:",
    ]
    lines.extend(f"  - {name}" for name in state["attached"])
    return "\n".join(lines)


def main(argv: list[str] | None = None, iam=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--environment", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--output", required=True, type=Path, help="pass-1 tfvars JSON to write when a cutover is needed")
    args = parser.parse_args(argv)
    if iam is None:
        import boto3

        iam = boto3.client("iam", region_name=args.region)
    state = inspect_role(iam, args.environment)
    phase = decide(state)
    if phase == "cutover":
        error = headroom_error(state)
        if error:
            print(error, file=sys.stderr)
            return 2
        args.output.write_text(json.dumps(pass_one_tfvars(state), indent=2, sort_keys=True) + "\n")
        print(
            f"pass 1 retains {len(state['legacy_documents'])} inline policies and attaches {len(state['missing_groups'])} managed policies",
            file=sys.stderr,
        )
    elif phase == "retire":
        print(
            f"managed policies attached; {len(state['legacy_documents'])} legacy inline and "
            f"{len(state['retired_attached'])} retired managed policies remain for pass 2",
            file=sys.stderr,
        )
    print(phase)
    return 0


if __name__ == "__main__":
    sys.exit(main())
