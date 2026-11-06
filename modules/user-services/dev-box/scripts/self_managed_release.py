"""Release manifest for the self-managed Dev Box stage (#7998).

The ADP-managed workflow builds the release manifest from workflow_dispatch
inputs and GitHub-environment variables (release.py --from-env). The
self-managed stage has neither: it has the Terraform outputs of the foundation
stack it just applied (`release_context`), the commit it deployed from and the
hashes of what it applied. This helper turns those into the same fail-closed
manifest (release.create_manifest), so controller/worker.py and runtime.py
verify exactly the release that is live, whichever path deployed it.

Self-managed identity fields:
  adp_definition_revision  the deploying commit (no separate workflow definition)
  customer_aws_label       "self-managed/<account>/<environment>"; only its
                           digest enters the manifest
  adp_correlation          sha256 of account, environment, region, deployment
                           and source, so the same release is the same digest
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

MODULE_ROOT = Path(__file__).resolve().parents[1]


def _release_module():
    spec = importlib.util.spec_from_file_location("dev_box_release", MODULE_ROOT / "release.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def output_value(outputs: dict, name: str):
    value = outputs.get(name)
    if isinstance(value, dict) and "value" in value:
        return value["value"]
    return value


def correlation(context: dict, source_revision: str) -> str:
    parts = (context["account_id"], context["environment"], context["region"], context["deployment_id"], source_revision)
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def dispatch_from_outputs(outputs: dict, *, source_revision: str, infra_sha256: str, module_sha256: str) -> dict:
    context = output_value(outputs, "release_context")
    if not isinstance(context, dict):
        raise ValueError("terraform outputs do not contain release_context; apply the foundation stack first")
    dispatch = {
        "adp_correlation": correlation(context, source_revision),
        "adp_source_revision": source_revision,
        "adp_definition_revision": source_revision,
        "customer_aws_label": f"self-managed/{context['account_id']}/{context['environment']}",
        "customer_account_id": context["account_id"],
        "environment": context["environment"],
        "region": context["region"],
        "image_id": context["image_id"],
        "image_revision": context.get("image_revision") or "",
        "template_revision": context["template_revision"],
        "instance_size": context["instance_size"],
        "workspace_disk_gb": int(context["workspace_disk_gb"]),
        "allowed_sizes": list(context["allowed_sizes"]),
        "max_disk_gb": int(context["max_disk_gb"]),
        "infra_sha256": infra_sha256,
        "module_sha256": module_sha256,
        "private_subnet_id": context["private_subnet_id"],
        "gateway_security_group_id": context["gateway_security_group_id"],
        "ssm_endpoint_security_group_id": context["ssm_endpoint_security_group_id"],
        "controller_principal_arn": context["controller_principal_arn"],
        "deployment_id": context["deployment_id"],
        "kms_key_arn": context["kms_key_arn"],
        "dashboard_enabled": bool(context["dashboard_enabled"]),
    }
    if not dispatch["image_revision"]:
        raise ValueError("release_context.image_revision is empty; the image build publishes /adp/<env>/dev-box/image-revision")
    # Terraform applies the gateway route and dashboard only with a pinned Crew
    # profile (self_managed.tf); the manifest carries the same effective values so
    # runtime.py's IAM and ingress checks describe what is live. The run registry's
    # customer KMS key (#8139) is resolved by deploy-dev-box.sh from the table's SSE
    # settings and applied as run_table_kms_key_arn; carrying it here lets runtime.py
    # accept exactly the KMS grant Terraform wrote and nothing more.
    for key in ("run_table_arn", "run_table_kms_key_arn", "gateway_execute_arn", "crew_profile_revision", "crew_model_id"):
        if context.get(key):
            dispatch[key] = context[key]
    # Capacity fallback (#8423): the ordered launch plan Terraform derived from
    # fallback_sizes / private_subnet_ids / instance_hour_rates_usd. Older foundation
    # outputs without these keys produce the one-size, one-subnet manifest as before.
    if context.get("fallback_sizes"):
        dispatch["fallback_sizes"] = list(context["fallback_sizes"])
    if context.get("instance_hour_rates_usd"):
        dispatch["instance_hour_rates_usd"] = dict(context["instance_hour_rates_usd"])
    if context.get("private_subnets"):
        dispatch["private_subnets"] = [
            {"subnet_id": item["subnet_id"], "availability_zone": item["availability_zone"]} for item in context["private_subnets"]
        ]
    # Crew bootstrap (#8055): user-data mode pins the bundle the guest installs at first
    # boot; image mode adds nothing, so the manifest digest is unchanged.
    if context.get("crew_bootstrap") == "user-data":
        for key in ("crew_bootstrap", "crew_bundle_s3_uri", "crew_bundle_sha256", "crew_source_revision"):
            dispatch[key] = context.get(key) or ""
    return dispatch


def build_manifest(outputs: dict, *, source_revision: str, infra_sha256: str, module_sha256: str) -> dict:
    release = _release_module()
    dispatch = dispatch_from_outputs(outputs, source_revision=source_revision, infra_sha256=infra_sha256, module_sha256=module_sha256)
    return release.create_manifest(dispatch, source_revision, source_revision)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outputs", type=Path, required=True, help="terraform output -json of modules/user-services/dev-box/infra")
    parser.add_argument("--source-revision", required=True, help="full commit the stage deployed from")
    parser.add_argument("--infra-sha256", required=True, help="sha256 of infra/main.tf")
    parser.add_argument("--module-sha256", required=True, help="sha256 of the module archive")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        manifest = build_manifest(
            json.loads(args.outputs.read_text()),
            source_revision=args.source_revision,
            infra_sha256=args.infra_sha256,
            module_sha256=args.module_sha256,
        )
    except (ValueError, KeyError, TypeError) as error:
        print(f"self_managed_release.py: {error}", file=sys.stderr)
        return 2
    args.output.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    print(manifest["manifest_sha256"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
