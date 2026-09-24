"""Deployment-owned lifecycle settings, bound into each approved request."""

from copy import deepcopy
from ipaddress import IPv4Network
from pathlib import Path
import re
from urllib.parse import urlsplit


class LifecycleRefused(Exception):
    pass


def supported_runtime_modes(config):
    """Pure API capability selection for fully composed execution recipes."""
    modes = {"adopt"}
    if config["workspace_variables"].get("networking_mode", "owned") == "owned":
        modes.add("managed")
    return frozenset(modes)


VARIABLES = frozenset(
    {
        "networking_mode",
        "supplied_vpc_id",
        "supplied_private_subnet_ids",
        "cluster_endpoint_public_access",
        "cluster_endpoint_public_access_cidrs",
        "node_group_desired_size",
        "node_group_min_size",
        "node_group_max_size",
        "node_volume_size",
        "kms_key_arn",
        "workspace_admin_principal_arns",
        "workspace_admin_automation_role_arns",
        "cluster_log_types",
        "log_retention_days",
        "cost_center",
        "node_image_repository_arns",
        "hybrid_networks",
    }
)


def validate_runtime_config(value):
    """No caller-selected commands, target identity overrides or credential bytes."""
    required = {
        "version",
        "backend",
        "environment",
        "workspace_variables",
        "actor_role_names",
        "namespace",
        "enforce_version",
        "management_security_group_id",
        "management_api_origin",
        "bootstrap_credential_reference_id",
        "binaries",
        "controller_image",
        "imds_probe_image",
    }
    if (
        not isinstance(value, dict)
        or set(value) - required - {"new_account"}
        or required - set(value)
    ):
        raise LifecycleRefused("lifecycle runtime configuration is incomplete")
    config = deepcopy(value)
    for key in ("controller_image", "imds_probe_image"):
        if not isinstance(config[key], str) or not re.fullmatch(
            r"[^\s]+@sha256:[a-f0-9]{64}", config[key]
        ):
            raise LifecycleRefused(
                "lifecycle images must be pinned to reviewed digests"
            )
    if type(config["version"]) is not int or config["version"] != 1:
        raise LifecycleRefused("lifecycle runtime version is unsupported")
    backend = config["backend"]
    if not isinstance(backend, dict) or set(backend) != {
        "bucket",
        "region",
        "lock_table",
    }:
        raise LifecycleRefused("an exact Terraform backend is required")
    if any(
        not isinstance(v, str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{1,252}", v)
        for v in backend.values()
    ):
        raise LifecycleRefused("Terraform backend identity is invalid")
    variables = config["workspace_variables"]
    if not isinstance(variables, dict) or set(variables) - VARIABLES:
        raise LifecycleRefused(
            "Terraform variables contain a target override or unsupported field"
        )
    if variables.get("hybrid_networks") is not None:
        hybrid = variables["hybrid_networks"]
        try:
            if not isinstance(hybrid, dict) or set(hybrid) != {
                "node_cidr",
                "pod_cidr",
                "service_cidr",
            }:
                raise ValueError("unsupported hybrid network shape")
            ranges = [IPv4Network(value, strict=True) for value in hybrid.values()]
            private = [
                IPv4Network(value)
                for value in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
            ]
            if (
                IPv4Network(hybrid["service_cidr"]).prefixlen > 24
                or any(
                    not 16 <= value.prefixlen <= 28
                    or not any(value.subnet_of(parent) for parent in private)
                    for value in ranges
                )
                or any(
                    value.overlaps(other)
                    for index, value in enumerate(ranges)
                    for other in ranges[index + 1 :]
                )
            ):
                raise ValueError("hybrid ranges must be private, bounded and disjoint")
        except (TypeError, ValueError):
            raise LifecycleRefused(
                "hybrid networks require disjoint canonical RFC1918 IPv4 ranges: node/pod /16 through /28, service /16 through /24"
            ) from None
    if not re.fullmatch(r"[a-z][a-z0-9-]{1,9}", str(config["environment"])):
        raise LifecycleRefused("workspace environment is invalid")
    if not re.fullmatch(
        r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", str(config["namespace"])
    ):
        raise LifecycleRefused("workspace namespace is invalid")
    if not re.fullmatch(r"v1\.[0-9]+", str(config["enforce_version"])):
        raise LifecycleRefused("a pinned Pod Security version is required")
    roles = config["actor_role_names"]
    if not isinstance(roles, dict) or set(roles) != {
        "registrar",
        "installer",
        "supervisor",
    }:
        raise LifecycleRefused("bootstrap requires three distinct actor roles")
    if any(
        not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9+=,.@_-]{1,64}", name)
        for name in roles.values()
    ):
        raise LifecycleRefused("bootstrap actor role name is invalid")
    if len(set(roles.values())) != 3:
        raise LifecycleRefused("bootstrap requires three distinct actor roles")
    binaries = config["binaries"]
    if not isinstance(binaries, dict) or set(binaries) != {
        "python",
        "terraform",
        "kubectl",
        "aws",
    }:
        raise LifecycleRefused("reviewed lifecycle executables are required")
    if any(
        not isinstance(path, str)
        or not Path(path).is_absolute()
        or ".." in Path(path).parts
        for path in binaries.values()
    ):
        raise LifecycleRefused("lifecycle executable paths must be absolute")
    origin = urlsplit(str(config["management_api_origin"]))
    if (
        origin.scheme != "https"
        or not origin.hostname
        or origin.username
        or origin.password
        or origin.query
        or origin.fragment
        or origin.path not in {"", "/"}
    ):
        raise LifecycleRefused(
            "bootstrap observation requires an explicit HTTPS API origin"
        )
    if not re.fullmatch(r"sg-[a-z0-9]+", str(config["management_security_group_id"])):
        raise LifecycleRefused("management security group is invalid")
    if (
        not isinstance(config["bootstrap_credential_reference_id"], str)
        or not config["bootstrap_credential_reference_id"].strip()
    ):
        raise LifecycleRefused(
            "bootstrap registration credential reference is required"
        )
    if "new_account" in config:
        new = config["new_account"]
        fields = {
            "child_access_role_name",
            "trust_policies",
            "permission_policy_arns",
            "permission_policy_documents",
            "audit_trail_arn",
            "runtime_roles",
        }
        if not isinstance(new, dict) or set(new) != fields:
            raise LifecycleRefused("new-account bootstrap configuration is incomplete")
        if not re.fullmatch(
            r"[A-Za-z0-9+=,.@_-]{1,64}", str(new["child_access_role_name"])
        ):
            raise LifecycleRefused("child-account access role name is invalid")
        for key in (
            "trust_policies",
            "permission_policy_arns",
            "permission_policy_documents",
        ):
            if not isinstance(new[key], dict) or not new[key]:
                raise LifecycleRefused("new-account role policy documents are required")
        runtime_roles = new["runtime_roles"]
        if not isinstance(runtime_roles, dict) or set(runtime_roles) != {
            "provider",
            "registrar",
            "installer",
            "supervisor",
        }:
            raise LifecycleRefused(
                "new-account provider and bootstrap actor roles are required"
            )
        for actor, definition in runtime_roles.items():
            if (
                not isinstance(definition, dict)
                or set(definition)
                != {"role_name", "trust_policy", "policy_arn", "policy_document"}
                or not isinstance(definition["role_name"], str)
                or not re.fullmatch(
                    r"[A-Za-z0-9+=,.@_-]{1,64}", definition["role_name"]
                )
            ):
                raise LifecycleRefused("new-account runtime role definition is invalid")
            if actor != "provider" and definition["role_name"] != roles[actor]:
                raise LifecycleRefused(
                    "new-account actor role differs from bootstrap configuration"
                )
        names = [definition["role_name"] for definition in runtime_roles.values()]
        if len(set(names + [new["child_access_role_name"]])) != 5:
            raise LifecycleRefused(
                "new-account execution, bootstrap and actor roles must be distinct"
            )
    return config
