"""Server-owned workload profiles produce the controller's exact admitted plan.

No provider discovery, credential delivery, image defaults, or execution identity
is created here. Profiles are installed policy; requests only select a profile,
name the workload, and confirm its reviewed model options.
"""

import hashlib
import json
import re
import uuid
from dataclasses import dataclass

from harness_jobs.identity import OperationRefused, OperationRequest, payload_digest

from .plan import Plan

DEPLOYMENT_NAMESPACE = uuid.UUID("67f38780-b5ca-5d73-99a3-3e91f43540cd")
MODEL_FIELDS = frozenset(
    {
        "model_name",
        "precision",
        "serving_framework",
        "replicas",
        "gpu_per_replica",
        "tensor_parallel_size",
        "max_model_len",
    }
)
BATCH_FIELDS = frozenset({"image", "command", "args", "gpu_count", "cpu", "memory"})
REQUEST_FIELDS = frozenset(
    {
        "controller_plan",
        "controller_certificate_authority",
        "controller_deployment_id",
        "controller_request_sha256",
        "controller_target_sha256",
        "controller_profile_id",
        "controller_profile_sha256",
        "allocation_id",
        "provider",
        "provider_account_id",
        "max_resource_units",
        "max_runtime_seconds",
        "max_cost_micros",
        "execution_steps",
        "credential_id",
        "credential_service",
        "credential_label",
    }
)
PROFILE_FIELDS = frozenset(
    {
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
)
GPU_PROFILE_FIELDS = (PROFILE_FIELDS - {"instance_type"}) | {
    "accelerators",
    "max_gpus_per_node",
    "cpus",
    "memory_gb",
}
# A bounded set of complete regional bindings (account/role stays shared; each
# entry still names its own region-scoped image, VPC, security group and node
# identity). Replaces the single-region fields; nothing else about the profile
# widens -- SkyPilot picks among these, this app never ranks or falls back.
REGIONAL_BINDING_TUPLE = (
    "region",
    "image_id",
    "vpc_name",
    "security_group",
    "instance_profile",
)
REGIONAL_BINDING_FIELDS = frozenset(REGIONAL_BINDING_TUPLE)
REGIONAL_PROFILE_FIELDS = (GPU_PROFILE_FIELDS - REGIONAL_BINDING_FIELDS) | {"regions"}


def compact(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def document_digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def deployment_identity(org_id, workspace_id, request_id):
    request_id = str(uuid.UUID(str(request_id)))
    deployment_id = str(
        uuid.uuid5(
            DEPLOYMENT_NAMESPACE, f"deployment/{org_id}/{workspace_id}/{request_id}"
        )
    )
    allocation_id = str(
        uuid.uuid5(
            DEPLOYMENT_NAMESPACE, f"allocation/{org_id}/{workspace_id}/{request_id}"
        )
    )
    return deployment_id, allocation_id


def execution_steps(
    org_id, workspace_id, allocation_id, action, *, node_bootstrap=False
):
    name = (
        "sp-"
        + hashlib.sha256(
            json.dumps([org_id, workspace_id, allocation_id]).encode()
        ).hexdigest()[:32]
    )
    from .node_command_plan import actions as node_actions

    actions = node_actions(action, node_bootstrap)
    return compact(
        [
            {
                "step_id": str(index),
                "provider": "aws",
                "operation_kind": value,
                "target": name,
            }
            for index, value in enumerate(actions, 1)
        ]
    )


@dataclass(frozen=True)
class DeploymentPreview:
    deployment_id: str
    request: OperationRequest
    deployment_request: dict
    deployment_target: dict

    def public(self, workspace_id):
        return {
            "deployment_id": self.deployment_id,
            "request_id": self.request.idempotency_key,
            "revision": payload_digest(self.request),
            "allocation_id": self.request.parameters["allocation_id"],
            "controller_plan": json.loads(self.request.parameters["controller_plan"]),
            "approval_request": {
                "workspace_id": workspace_id,
                "action": self.request.action,
                "idempotency_key": self.request.idempotency_key,
                "parameters": dict(self.request.parameters),
            },
        }


def build_deployment_preview(
    *,
    org_id,
    workspace_id,
    request_id,
    profile_id,
    profile,
    target,
    name,
    model_options,
    workload_kind="serving",
    batch_options=None,
):
    """Build solely from explicit installed profile and canonical DB destination."""
    try:
        request_id = str(uuid.UUID(str(request_id)))
        profile = json.loads(compact(profile))
        profile_fields = set(profile) - {"network_probe", "node_bootstrap"}
        if profile_fields not in (
            PROFILE_FIELDS,
            GPU_PROFILE_FIELDS,
            REGIONAL_PROFILE_FIELDS,
            REGIONAL_PROFILE_FIELDS | {"network"},
        ) or not re.fullmatch(r"[a-z][a-z0-9-]{0,62}", profile_id):
            raise ValueError("unsupported profile")
        if (
            workload_kind not in {"serving", "batch"}
            or profile["workload"].get("kind") != workload_kind
        ):
            raise ValueError("profile workload kind differs from requested lifecycle")
        if workload_kind == "serving" and (
            set(model_options) != MODEL_FIELDS
            or model_options != profile["model_options"]
            or batch_options is not None
        ):
            raise ValueError("model options differ from reviewed profile")
        # The current controller contract emits one serving replica. Do not
        # approve multiple replicas and silently run fewer than were requested.
        if workload_kind == "serving" and (
            type(model_options["replicas"]) is not int or model_options["replicas"] != 1
        ):
            raise ValueError("controller supports one serving replica")
        if workload_kind == "batch" and (
            model_options != {}
            or profile["model_options"] != {}
            or profile["serving_auth_contract"] is not None
            or not isinstance(batch_options, dict)
            or set(batch_options) != BATCH_FIELDS
            or batch_options != {key: profile["workload"][key] for key in BATCH_FIELDS}
            or not batch_options["command"]
        ):
            raise ValueError("batch invocation differs from reviewed profile")
        for key in (
            "cluster_id",
            "cluster_arn",
            "endpoint",
            "namespace",
            "provider_account_id",
        ):
            if profile[key] != target[key]:
                raise ValueError("canonical profile destination changed")
        if not target["namespace"] or not target["endpoint"].startswith("https://"):
            raise ValueError("canonical destination is incomplete")
        workload = dict(profile["workload"])
        if "name" in workload:
            raise ValueError("profile cannot shadow request name")
        workload["name"] = name
        if workload_kind == "serving" and (
            profile["serving_auth_contract"] != "superplane-token-file-header-v1"
        ):
            raise ValueError("explicit compatible serving authentication required")
        physical_gpus = profile["physical_gpus"]
        if (
            type(physical_gpus) is not int
            or not 1 <= physical_gpus <= 128
            or (
                workload_kind == "serving"
                and (
                    type(model_options["gpu_per_replica"]) is not int
                    or workload["gpu_count"] != model_options["gpu_per_replica"]
                )
            )
            or not 1 <= workload["gpu_count"] <= physical_gpus
            or type(profile["max_runtime_seconds"]) is not int
            or not 1 <= profile["max_runtime_seconds"] <= 86400
            or type(profile["max_cost_micros"]) is not int
            or profile["max_cost_micros"] <= 0
        ):
            raise ValueError("explicit physical capacity and finite envelope required")
        reference = profile["credential_reference"]
        if set(reference) != {
            "credential_id",
            "credential_service",
            "credential_label",
        }:
            raise ValueError("credential reference required")
        if not all(
            isinstance(value, str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}", value)
            for value in reference.values()
        ):
            raise ValueError("credential reference must be opaque")
        deployment_id, allocation_id = deployment_identity(
            org_id, workspace_id, request_id
        )
        if "network_probe" in profile:
            from .network_probe_contract import invocation

            workload["args"] = invocation(
                profile,
                org_id=org_id,
                workspace_id=workspace_id,
                request_id=request_id,
                allocation_id=allocation_id,
                target=target,
            )
        is_regional = "regions" in profile
        shared_fields = (
            "cluster_arn",
            "endpoint",
            "namespace",
            "provider_account_id",
            "node_count",
            "disk_size",
            "service_cidr",
            "certificate_authority",
        ) + (() if is_regional else REGIONAL_BINDING_TUPLE)
        data = {key: profile[key] for key in shared_fields}
        certificate = data.pop("certificate_authority")
        if is_regional:
            data["regions_sha256"] = document_digest(profile["regions"])
        data.update(
            version=4 if is_regional else (3 if "accelerators" in profile else 2),
            workload=workload,
            certificate_authority_sha256=hashlib.sha256(
                certificate.encode()
            ).hexdigest(),
        )
        capacity_fields = (
            ("accelerators", "max_gpus_per_node", "cpus", "memory_gb")
            if data["version"] in (3, 4)
            else ("instance_type",)
        )
        data.update({key: profile[key] for key in capacity_fields})
        document = {"name": name, "profile_id": profile_id, **model_options}
        if workload_kind == "batch":
            document = {
                "name": name,
                "profile_id": profile_id,
                "kind": "batch",
                **batch_options,
            }
        destination = {**target, "controller_plan": data}
        parameters = {
            "controller_plan": compact(data),
            "controller_certificate_authority": certificate,
            "controller_deployment_id": deployment_id,
            "controller_request_sha256": document_digest(document),
            "controller_target_sha256": document_digest(destination),
            "controller_profile_id": profile_id,
            "controller_profile_sha256": document_digest(profile),
            "allocation_id": allocation_id,
            "provider": "aws",
            "provider_account_id": profile["provider_account_id"],
            "max_resource_units": str(physical_gpus),
            "max_runtime_seconds": str(profile["max_runtime_seconds"]),
            "max_cost_micros": str(profile["max_cost_micros"]),
            "execution_steps": execution_steps(
                org_id,
                workspace_id,
                allocation_id,
                "provision",
                node_bootstrap="node_bootstrap" in profile,
            ),
            **reference,
        }
        if "node_bootstrap" in profile:
            from .node_command_plan import PARAMETER, validate

            parameters[PARAMETER] = compact(validate(profile["node_bootstrap"]))
        if is_regional:
            parameters["controller_regions"] = compact(profile["regions"])
        if "network" in profile:
            if set(profile["network"]) != {"cluster", "regions"}:
                raise ValueError("closed network profile required")
            parameters["controller_network_cluster"] = compact(
                profile["network"]["cluster"]
            )
            parameters["controller_network_regions"] = compact(
                profile["network"]["regions"]
            )
        request = OperationRequest(
            action="provision", idempotency_key=str(request_id), parameters=parameters
        )
        validate_request(request, target, org_id=org_id, workspace_id=workspace_id)
        return DeploymentPreview(deployment_id, request, document, destination)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise OperationRefused(
            "controller deployment profile or request is invalid or unsupported"
        ) from None


def validate_request(request, target, *, org_id, workspace_id):
    fields = REQUEST_FIELDS | (
        {"controller_source_operation_id"} if request.action == "teardown" else set()
    )
    if json.loads(request.parameters.get("controller_plan", "{} ")).get("version") == 4:
        fields = fields | {"controller_regions"}
    from .network_plan import PARAMETERS

    if PARAMETERS & set(request.parameters):
        fields = fields | PARAMETERS
    from .node_command_plan import PARAMETER

    if PARAMETER in request.parameters:
        fields = fields | {PARAMETER}
    if set(request.parameters) != fields:
        raise OperationRefused(
            "controller deployment request has unsupported parameters"
        )
    return Plan.validate_request(
        request,
        target,
        org_id=org_id,
        workspace_id=workspace_id,
        **{
            key: int(request.parameters[key])
            for key in ("max_resource_units", "max_runtime_seconds", "max_cost_micros")
        },
    )


def teardown_request(source, *, org_id, workspace_id, request_id, source_operation_id):
    """Preserve the original paid allocation and exact workload/provider target."""
    if (
        source.action != "provision"
        or "controller_deployment_id" not in source.parameters
    ):
        raise OperationRefused("teardown requires the original controller deployment")
    parameters = dict(source.parameters)
    parameters["controller_source_operation_id"] = source_operation_id
    parameters["max_resource_units"] = "0"
    parameters["max_cost_micros"] = "0"
    parameters["execution_steps"] = execution_steps(
        org_id, workspace_id, parameters["allocation_id"], "teardown"
    )
    return OperationRequest(
        action="teardown",
        idempotency_key=str(uuid.UUID(str(request_id))),
        parameters=parameters,
    )
