"""Explicit serving policy projection; no workload or credential defaults."""

import json
import re

from .config import digest, require

DIRECTORY = "/etc/superplane/controller-profiles"
FILENAME = "profiles.json"
PATH = DIRECTORY + "/" + FILENAME
MAX_POLICY_BYTES = 65536  # Below Linux's single argv/environment string limit.
# Reject unexpected input before it can enter a rendered manifest or receipt.
# The pinned image remains authoritative for field values and plan semantics.
PROFILE_FIELDS = {
    "cluster_id",
    "cluster_arn",
    "endpoint",
    "namespace",
    "provider_account_id",
    "region",
    "image_id",
    "instance_type",
    "node_count",
    "disk_size",
    "instance_profile",
    "vpc_name",
    "security_group",
    "service_cidr",
    "certificate_authority",
    "workload",
    "model_options",
    "physical_gpus",
    "max_runtime_seconds",
    "max_cost_micros",
    "credential_reference",
    "serving_auth_contract",
}
GPU_PROFILE_FIELDS = (PROFILE_FIELDS - {"instance_type"}) | {
    "accelerators",
    "max_gpus_per_node",
}


def policy(env):
    value = env.get("controller_profiles")
    if value is None:
        return None
    require(
        isinstance(value, dict)
        and set(value) == {"version", "tenants"}
        and type(value["version"]) is int
        and value["version"] == 1
        and isinstance(value["tenants"], dict)
        and set(value["tenants"]) == {env.get("org_id")},
        "controller_profiles must name exactly this installation's domain organization",
    )
    tenant = value["tenants"][env["org_id"]]
    require(
        isinstance(tenant, dict)
        and set(tenant) == {"adp_org_id", "workspaces"}
        and tenant["adp_org_id"] == env.get("adp_org_id")
        and isinstance(tenant["workspaces"], dict)
        and tenant["workspaces"],
        "controller_profiles requires the canonical ADP organization and explicit workspaces",
    )
    for workspace_id, profiles in tenant["workspaces"].items():
        require(
            isinstance(workspace_id, str)
            and re.fullmatch(
                r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}", workspace_id
            )
            and isinstance(profiles, dict)
            and profiles,
            "controller_profiles requires workspace UUIDs and nonempty profile maps",
        )
        for profile_id, profile in profiles.items():
            require(
                isinstance(profile_id, str)
                and re.fullmatch(r"[a-z][a-z0-9-]{0,62}", profile_id)
                and isinstance(profile, dict)
                and set(profile) in (PROFILE_FIELDS, GPU_PROFILE_FIELDS),
                "controller_profiles has an invalid profile identity",
            )
            workload = profile.get("workload", {})
            require(
                isinstance(workload, dict)
                and set(workload)
                == {
                    "kind",
                    "image",
                    "command",
                    "args",
                    "gpu_count",
                    "cpu",
                    "memory",
                    "port",
                    "auth_secret",
                }
                and workload.get("kind") in {"serving", "batch"}
                and isinstance(workload.get("image"), str)
                and re.fullmatch(
                    r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", workload["image"]
                )
                and (
                    (
                        workload["kind"] == "serving"
                        and profile.get("serving_auth_contract")
                        == "superplane-token-file-header-v1"
                        and isinstance(workload.get("auth_secret"), str)
                        and re.fullmatch(
                            r"[a-z][a-z0-9-]{0,62}", workload["auth_secret"]
                        )
                    )
                    or (
                        workload["kind"] == "batch"
                        and workload["auth_secret"] is None
                        and workload["port"] is None
                        and profile["serving_auth_contract"] is None
                    )
                ),
                "Profiles require an immutable image; serving requires workspace authentication and batch cannot expose an endpoint",
            )
            require(
                isinstance(profile.get("credential_reference"), dict)
                and set(profile["credential_reference"])
                == {"credential_id", "credential_service", "credential_label"}
                and isinstance(profile.get("model_options"), dict)
                and set(profile["model_options"])
                == (
                    set()
                    if workload["kind"] == "batch"
                    else {
                        "model_name",
                        "precision",
                        "serving_framework",
                        "replicas",
                        "gpu_per_replica",
                        "tensor_parallel_size",
                        "max_model_len",
                    }
                ),
                "Profiles accept only opaque credential references and documented model options",
            )
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    require(
        len(encoded.encode()) <= MAX_POLICY_BYTES,
        "controller_profiles exceeds the installer transport bound (64 KiB)",
    )
    return encoded


def validate_profiles(env, *, control_plane_only):
    encoded = policy(env)
    require(
        control_plane_only or encoded is not None,
        "Full installation requires explicit controller_profiles; management-only may omit serving policy",
    )
    if encoded is not None and not control_plane_only:
        require(
            env["workspace_id"]
            in env["controller_profiles"]["tenants"][env["org_id"]]["workspaces"],
            "controller_profiles must include the selected workspace",
        )
        expected = {
            "cluster_id": env["cluster_id"],
            "cluster_arn": f"arn:aws:eks:{env['region']}:{env['account_id']}:cluster/{env['workspace_cluster']}",
            "namespace": env["workspace_namespace"],
            "provider_account_id": env["account_id"],
            "region": env["region"],
        }
        for profile in selected_profiles(env).values():
            require(
                all(profile[key] == value for key, value in expected.items()),
                "Selected controller profile does not match the installation workspace target",
            )


def selected_profiles(env):
    return env["controller_profiles"]["tenants"][env["org_id"]]["workspaces"][
        env["workspace_id"]
    ]


def verify_cluster_profiles(env, cluster):
    """Bind policy to the independently queried selected EKS cluster before writes."""
    validate_profiles(env, control_plane_only=False)
    for profile in selected_profiles(env).values():
        require(
            profile["cluster_arn"] == cluster.get("arn")
            and profile["endpoint"] == cluster.get("endpoint")
            and profile["certificate_authority"]
            == cluster.get("certificateAuthority", {}).get("data"),
            "Selected controller profile endpoint or public CA differs from the observed workspace cluster",
        )


def projection(env):
    encoded = policy(env)
    if encoded is None:
        return None
    sha = digest(env["controller_profiles"])
    return {
        "name": "superplane-controller-profiles-" + sha[:16],
        "sha256": sha,
        "content": encoded,
    }


# Runs only inside the pinned API image during authorized preflight/verification.
# The maintained builder checks the complete closed profile/plan contract,
# including real CA decoding, resource bounds and the exact serving invocation.
# This is syntax/configuration evidence, not canonical database binding or serving
# readiness, which require an actual approved API request and provider execution.
VERIFY_PROGRAM = """import hashlib,json,os
from pathlib import Path
from superplane_executor.deployment_plan import BATCH_FIELDS,build_deployment_preview
path=os.environ.get("SUPERPLANE_CONTROLLER_PROFILES_FILE")
if path:
    with Path(path).open("rb") as source: raw=source.read(262145)
else: raw=os.environ["SUPERPLANE_INSTALLATION_PROFILES"].encode()
assert len(raw)<=262144
document=json.loads(raw)
assert set(document)=={"version","tenants"} and document["version"]==1
count=0
for org_id,tenant in document["tenants"].items():
    assert set(tenant)=={"adp_org_id","workspaces"} and tenant["adp_org_id"]
    for workspace_id,profiles in tenant["workspaces"].items():
        for profile_id,profile in profiles.items():
            target={key:profile[key] for key in ("cluster_id","cluster_arn","endpoint","namespace","provider_account_id")}
            kind=profile["workload"]["kind"]
            batch={key:profile["workload"][key] for key in BATCH_FIELDS} if kind=="batch" else None
            build_deployment_preview(org_id=org_id,workspace_id=workspace_id,request_id="67f38780-b5ca-5d73-99a3-3e91f43540cd",profile_id=profile_id,profile=profile,target=target,name="installation-validation",model_options=profile["model_options"],workload_kind=kind,batch_options=batch)
            count+=1
assert count>0
encoded=json.dumps(document,sort_keys=True,separators=(",",":"),allow_nan=False).encode()
print(json.dumps({"sha256":hashlib.sha256(encoded).hexdigest(),"profiles":count,"validated":True,"workload_ready":False}))
"""


def verify_result(result, env):
    expected = projection(env)
    require(
        isinstance(result, dict)
        and result.get("validated") is True
        and result.get("sha256") == expected["sha256"]
        and type(result.get("profiles")) is int
        and result["profiles"] > 0
        and result.get("workload_ready") is False,
        "Pinned API image did not validate the exact installed controller profiles",
    )
    return result
