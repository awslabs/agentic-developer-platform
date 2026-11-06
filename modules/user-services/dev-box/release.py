"""Fail-closed, reproducible Dev Box release context for the selected connection."""

import argparse
import hashlib
import json
import os
import re
from pathlib import Path


def _matches(value: object, pattern: str, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(pattern, value) is None:
        raise ValueError(f"invalid {label}")
    return value


def _launch_plan(dispatch: dict, size: str, allowed_sizes: list, subnet: str):
    """Validate the optional capacity-fallback inputs (#8423); each returns None when absent."""
    fallback_sizes = dispatch.get("fallback_sizes")
    if fallback_sizes is not None:
        if (not isinstance(fallback_sizes, list) or not fallback_sizes or fallback_sizes[0] != size
                or len(set(fallback_sizes)) != len(fallback_sizes)
                or any(not isinstance(item, str) or item not in allowed_sizes for item in fallback_sizes)):
            raise ValueError("invalid fallback instance sizes")
        fallback_sizes = list(fallback_sizes)
    rates = dispatch.get("instance_hour_rates_usd")
    if rates is not None:
        if not isinstance(rates, dict) or any(
            not isinstance(key, str) or key not in allowed_sizes or not isinstance(value, str)
            or re.fullmatch(r"[0-9]+(\.[0-9]{1,6})?", value) is None for key, value in rates.items()
        ) or any(item not in rates for item in (fallback_sizes or [size])):
            raise ValueError("invalid instance hour rates")
        rates = {key: rates[key] for key in sorted(rates)}
    subnets = dispatch.get("private_subnets")
    if subnets is not None:
        if (not isinstance(subnets, list) or not subnets or not isinstance(subnets[0], dict) or subnets[0].get("subnet_id") != subnet
                or any(not isinstance(item, dict) or set(item) != {"subnet_id", "availability_zone"}
                       or not isinstance(item["subnet_id"], str) or re.fullmatch(r"subnet-[0-9a-f]{8,17}", item["subnet_id"]) is None
                       or not isinstance(item["availability_zone"], str) or re.fullmatch(r"[a-z]{2}-[a-z]+-[0-9][a-z]", item["availability_zone"]) is None
                       for item in subnets)
                or len({item["subnet_id"] for item in subnets}) != len(subnets)):
            raise ValueError("invalid private subnets")
        subnets = [{"subnet_id": item["subnet_id"], "availability_zone": item["availability_zone"]} for item in subnets]
    return fallback_sizes, rates, subnets


def create_manifest(dispatch: dict, actual_source: str, actual_definition: str) -> dict:
    required = {
        "adp_correlation", "adp_source_revision", "adp_definition_revision",
        "customer_aws_label", "customer_account_id", "environment", "region",
        "image_id", "image_revision", "template_revision", "instance_size",
        "workspace_disk_gb", "allowed_sizes", "max_disk_gb", "infra_sha256", "module_sha256",
        "private_subnet_id", "gateway_security_group_id", "ssm_endpoint_security_group_id", "controller_principal_arn",
        "deployment_id", "kms_key_arn", "dashboard_enabled",
    }
    optional = {"run_table_arn", "run_table_kms_key_arn", "gateway_execute_arn", "crew_profile_revision", "crew_model_id",
                "crew_bootstrap", "crew_bundle_s3_uri", "crew_bundle_sha256", "crew_source_revision",
                # Capacity fallback (#8423): the ordered launch plan and its prices. Absent = one size, one subnet.
                "fallback_sizes", "instance_hour_rates_usd", "private_subnets"}
    if not required.issubset(dispatch) or set(dispatch) - (required | optional):
        raise ValueError("missing or unexpected release context")
    source = _matches(dispatch["adp_source_revision"], r"[0-9a-f]{40}", "source revision")
    definition = _matches(dispatch["adp_definition_revision"], r"[0-9a-f]{40}", "definition revision")
    if source != actual_source or definition != actual_definition:
        raise ValueError("source or workflow definition revision mismatch")
    correlation = _matches(dispatch["adp_correlation"], r"[0-9a-f]{64}", "correlation")
    label = _matches(dispatch["customer_aws_label"], r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", "selected connection")
    account = _matches(dispatch["customer_account_id"], r"[0-9]{12}", "target account")
    environment = _matches(dispatch["environment"], r"[a-z][a-z0-9-]{0,31}", "environment")
    region = _matches(dispatch["region"], r"[a-z]{2}-[a-z]+-[0-9]", "region")
    image = _matches(dispatch["image_id"], r"ami-[0-9a-f]{8,17}", "image")
    image_revision = _matches(dispatch["image_revision"], r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", "image revision")
    template = _matches(dispatch["template_revision"], r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", "template revision")
    size = _matches(dispatch["instance_size"], r"[a-z][a-z0-9-]*\.[a-z0-9]+", "instance size")
    infra = _matches(dispatch["infra_sha256"], r"[0-9a-f]{64}", "infrastructure hash")
    module = _matches(dispatch["module_sha256"], r"[0-9a-f]{64}", "module hash")
    subnet = _matches(dispatch["private_subnet_id"], r"subnet-[0-9a-f]{8,17}", "private subnet")
    gateway_group = _matches(dispatch["gateway_security_group_id"], r"sg-[0-9a-f]{8,17}", "gateway group")
    ssm_group = _matches(dispatch["ssm_endpoint_security_group_id"], r"sg-[0-9a-f]{8,17}", "SSM group")
    principal = _matches(dispatch["controller_principal_arn"], r"arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+", "controller principal")
    deployment = _matches(dispatch["deployment_id"], r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", "deployment")
    kms_key = _matches(dispatch["kms_key_arn"], r"arn:aws:kms:[a-z]{2}-[a-z]+-[0-9]:[0-9]{12}:key/[0-9a-f-]{36}", "KMS key")
    if dispatch["dashboard_enabled"] not in (True, False) or type(dispatch["dashboard_enabled"]) is not bool:
        raise ValueError("invalid dashboard policy")
    profile_revision = dispatch.get("crew_profile_revision")
    if profile_revision:
        profile_revision = _matches(profile_revision, r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", "Crew profile revision")
    elif dispatch["dashboard_enabled"] or profile_revision not in (None, ""):
        raise ValueError("approved Crew profile revision required for dashboard")
    disk = dispatch["workspace_disk_gb"]
    allowed_sizes = dispatch["allowed_sizes"]
    if not isinstance(allowed_sizes, list) or not allowed_sizes or any(
        not isinstance(item, str) or re.fullmatch(r"[a-z][a-z0-9-]*\.[a-z0-9]+", item) is None for item in allowed_sizes
    ):
        raise ValueError("invalid allowed instance sizes")
    max_disk_gb = dispatch["max_disk_gb"]
    if type(max_disk_gb) is not int or not 8 <= max_disk_gb <= 16384:
        raise ValueError("invalid disk policy")
    if type(disk) is not int or not 8 <= disk <= max_disk_gb or size not in allowed_sizes:
        raise ValueError("invalid workspace disk size")
    fallback_sizes, rates, subnets = _launch_plan(dispatch, size, allowed_sizes, subnet)
    manifest = {
        "schema_version": 1,
        "component": "dev-box",
        "correlation": correlation,
        "source_revision": source,
        "definition_revision": definition,
        "selected_connection_sha256": hashlib.sha256(label.encode()).hexdigest(),
        "account_id": account,
        "environment": environment,
        "region": region,
        "image_id": image,
        "image_revision": image_revision,
        "template_revision": template,
        "instance_size": size,
        "workspace_disk_gb": disk,
        "allowed_sizes": sorted(set(allowed_sizes)),
        "max_disk_gb": max_disk_gb,
        "infra_sha256": infra,
        "module_sha256": module,
        "private_subnet_id": subnet,
        "gateway_security_group_id": gateway_group,
        "ssm_endpoint_security_group_id": ssm_group,
        "controller_principal_arn": principal,
        "deployment_id": deployment,
        "kms_key_arn": kms_key,
        "dashboard_enabled": dispatch["dashboard_enabled"],
        "controller_status": "absent",
    }
    if profile_revision:
        manifest["crew_profile_revision"] = profile_revision
    # Capacity fallback (#8423): written only when the release declares a plan beyond
    # the template's own size and subnet, so earlier manifests and their digests are
    # unchanged. Rates are decimal strings keyed by size; subnets carry their zone so a
    # replace can stay in the zone of the retained workspace disk.
    if fallback_sizes is not None:
        manifest["fallback_sizes"] = fallback_sizes
    if rates is not None:
        manifest["instance_hour_rates_usd"] = rates
    if subnets is not None:
        manifest["private_subnets"] = subnets
    # Crew bootstrap mode (#8055). Image mode writes nothing, so existing manifests and
    # their digests are unchanged; user-data mode pins the bundle the guest installs.
    bootstrap = dispatch.get("crew_bootstrap") or "image"
    if bootstrap not in ("image", "user-data"):
        raise ValueError("invalid Crew bootstrap mode")
    bundle_inputs = ("crew_bundle_s3_uri", "crew_bundle_sha256", "crew_source_revision")
    if bootstrap == "user-data":
        if not profile_revision:
            raise ValueError("user-data Crew bootstrap requires an approved Crew profile revision")
        bundle_sha256 = _matches(dispatch.get("crew_bundle_sha256", ""), r"[0-9a-f]{64}", "Crew bundle digest")
        bundle_uri = _matches(dispatch.get("crew_bundle_s3_uri", ""),
                              r"s3://[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]/dev-box/image-inputs/[0-9a-f]{64}/crew\.tar\.gz", "Crew bundle location")
        if f"/dev-box/image-inputs/{bundle_sha256}/" not in bundle_uri:
            raise ValueError("Crew bundle location is not addressed by its digest")
        manifest["crew_bootstrap"] = bootstrap
        manifest["crew_bundle_s3_uri"] = bundle_uri
        manifest["crew_bundle_sha256"] = bundle_sha256
        manifest["crew_source_revision"] = _matches(dispatch.get("crew_source_revision", ""), r"[0-9a-f]{40}", "Crew source revision")
    elif any(dispatch.get(field) for field in bundle_inputs):
        raise ValueError("Crew bundle inputs require user-data bootstrap")
    run_table_arn = dispatch.get("run_table_arn", "")
    if run_table_arn:
        manifest["run_table_arn"] = _matches(
            run_table_arn,
            rf"arn:aws:dynamodb:{region}:{account}:table/[A-Za-z0-9_.-]+",
            "host run table",
        )
    # A run registry encrypted with a customer KMS key (#8139): the controller's
    # owner-bound-host-runs policy then carries a second, DynamoDB-only KMS statement,
    # and runtime.py accepts that statement only when the manifest names the key.
    run_table_kms_key_arn = dispatch.get("run_table_kms_key_arn", "")
    if run_table_kms_key_arn:
        if not run_table_arn:
            raise ValueError("run registry key requires the approved run table")
        manifest["run_table_kms_key_arn"] = _matches(
            run_table_kms_key_arn,
            rf"arn:aws:kms:{region}:{account}:key/[0-9a-f-]{{36}}",
            "run registry key",
        )
    gateway_arn = dispatch.get("gateway_execute_arn", "")
    if gateway_arn:
        if not run_table_arn:
            raise ValueError("gateway route requires the approved run table")
        if not profile_revision:
            raise ValueError("gateway route requires an approved Crew profile")
        manifest["gateway_execute_arn"] = _matches(
            gateway_arn,
            rf"arn:aws:execute-api:{region}:{account}:[a-z0-9]+/[A-Za-z0-9_-]+",
            "approved model gateway",
        )
        manifest["crew_model_id"] = _matches(
            dispatch.get("crew_model_id", ""), r"[A-Za-z0-9_.:-]{1,256}", "approved Crew model",
        )
    elif dispatch.get("crew_model_id"):
        raise ValueError("Crew model requires the approved gateway")
    canonical = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    manifest["manifest_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dispatch", type=Path)
    parser.add_argument("--from-env", action="store_true")
    parser.add_argument("--actual-source", required=True)
    parser.add_argument("--actual-definition", required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.from_env == (arguments.dispatch is not None):
        parser.error("choose exactly one of --dispatch and --from-env")
    fields = ("adp_correlation", "adp_source_revision", "adp_definition_revision", "customer_aws_label",
              "customer_account_id", "environment", "region", "image_id", "image_revision",
              "template_revision", "instance_size", "workspace_disk_gb", "allowed_sizes", "max_disk_gb", "infra_sha256", "module_sha256", "private_subnet_id",
              "gateway_security_group_id", "ssm_endpoint_security_group_id", "controller_principal_arn",
              "deployment_id", "kms_key_arn", "dashboard_enabled", "run_table_arn", "run_table_kms_key_arn", "gateway_execute_arn", "crew_profile_revision", "crew_model_id",
              "crew_bootstrap", "crew_bundle_s3_uri", "crew_bundle_sha256", "crew_source_revision",
              "fallback_sizes", "instance_hour_rates_usd", "private_subnets")
    dispatch = ({field: os.environ.get("DEV_BOX_" + field.upper(), "") for field in fields}
                if arguments.from_env else json.loads(arguments.dispatch.read_text()))
    if arguments.from_env:
        dispatch["workspace_disk_gb"] = int(dispatch["workspace_disk_gb"]) if dispatch["workspace_disk_gb"].isdigit() else None
        dispatch["max_disk_gb"] = int(dispatch["max_disk_gb"]) if dispatch["max_disk_gb"].isdigit() else None
        try:
            dispatch["allowed_sizes"] = json.loads(dispatch["allowed_sizes"])
            dispatch["dashboard_enabled"] = json.loads(dispatch["dashboard_enabled"])
            # Optional capacity-fallback plan (#8423): JSON when set, absent when empty.
            for field in ("fallback_sizes", "instance_hour_rates_usd", "private_subnets"):
                if dispatch.get(field):
                    dispatch[field] = json.loads(dispatch[field])
                else:
                    dispatch.pop(field, None)
        except json.JSONDecodeError as exc:
            raise ValueError("invalid allowed instance sizes") from exc
    manifest = create_manifest(
        dispatch, arguments.actual_source, arguments.actual_definition
    )
    arguments.output.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    main()
