#!/usr/bin/env python3
"""Resolve the additional EKS capacity subnets a deployment must plan AND apply with (#5830).

Run before `terraform plan` on the platform module. Prints the effective
zone-keyed map for TF_VAR_additional_private_subnet_ids_by_az.

Why this exists rather than passing the repository variable straight through:
the subnet ids are account-specific and live outside the repository, so an unset
or stale variable is indistinguishable from "deliberately none" and resolves to
the empty default. On a cluster whose exhaustion has already been relieved, that
removes the added subnets and re-breaks pod IP assignment for every node launched
afterwards -- without anyone having asked for a change. Passing a TF_VAR through
does not prevent that; deciding against the LIVE cluster does.

So: read the live cluster's subnet set, work out which of its subnets are
additions (by comparing against the networking private subnets in platform state,
not against every Terraform-managed subnet), and combine with what this deployment
was configured with. Unset retains. A configuration that omits a live addition is
REFUSED rather than silently planned as a removal. The single value printed here
is used for both the plan and the apply, so the reviewed plan is the applied plan.

Reads only; performs no mutation. The rules live in capacity_subnets.py, shared
with upgrade discovery and tested there.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
from capacity_subnets import Refused, as_zone_map, effective_additions, live_additions  # noqa: E402


def aws(*args):
    proc = subprocess.run(["aws", *args, "--output", "json"], text=True, capture_output=True)
    if proc.returncode:
        # No CLI input payloads or resource values in diagnostics.
        raise RuntimeError(f"AWS {args[0]} {args[1]} failed: {proc.stderr.strip()}")
    return json.loads(proc.stdout or "{}")


def configured(raw):
    """Parse the operator/CI-supplied map, refusing a shape Terraform would reject later."""
    import re
    raw = (raw or "").strip()
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Refused(f"ADDITIONAL_PRIVATE_SUBNETS_BY_AZ is not valid JSON: {exc.msg}") from None
    if not isinstance(value, dict):
        raise Refused('ADDITIONAL_PRIVATE_SUBNETS_BY_AZ must be a JSON object keyed by availability '
                      'zone, e.g. {"us-east-1a": "subnet-0123456789abcdef0"}')
    for zone, subnet in value.items():
        if not re.fullmatch(r"[a-z0-9-]+", zone) or not isinstance(subnet, str) \
                or not re.fullmatch(r"subnet-[0-9a-f]{8,17}", subnet):
            raise Refused('ADDITIONAL_PRIVATE_SUBNETS_BY_AZ entries must look like '
                          '"us-east-1a": "subnet-0123456789abcdef0"')
    if len(set(value.values())) != len(value):
        raise Refused("ADDITIONAL_PRIVATE_SUBNETS_BY_AZ names the same subnet under two availability zones")
    return value


def platform_state(bucket, environment):
    key = f"{environment}/platform/terraform.tfstate"
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "platform.tfstate"
        aws("s3api", "get-object", "--bucket", bucket, "--key", key, str(path))
        return json.loads(path.read_text())


def resolve(args):
    cluster_name = f"adp-{args.environment}-eks-cluster"
    try:
        cluster = aws("eks", "describe-cluster", "--name", cluster_name)["cluster"]
    except RuntimeError as exc:
        # A cluster that does not exist yet is a FIRST deployment: there is no live
        # set to preserve, so the configured map stands on its own. Any other
        # failure must not be read as "no additions" -- that is the silent-removal
        # path this script exists to close.
        if "ResourceNotFoundException" not in str(exc):
            raise
        return configured(args.configured)

    live = cluster["resourcesVpcConfig"].get("subnetIds", [])
    additions = live_additions(platform_state(args.bucket, args.environment), live)
    zones = {}
    if additions:
        described = aws("ec2", "describe-subnets", "--subnet-ids", *sorted(additions))["Subnets"]
        found = {s["SubnetId"]: s.get("AvailabilityZone") for s in described}
        zones = {s: found.get(s) for s in sorted(additions)}
    return effective_additions(configured(args.configured), as_zone_map(zones),
                               allow_removal=args.allow_removal)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", default="dev")
    parser.add_argument("--bucket", required=True, help="Terraform state bucket")
    parser.add_argument("--configured", default="", help="Configured map (repository variable / TF_VAR_)")
    parser.add_argument("--allow-removal", action="store_true",
                        help="Authorise narrowing the live subnet set (deliberate removal only)")
    args = parser.parse_args()
    try:
        print(json.dumps(resolve(args), sort_keys=True))
    except Refused as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
