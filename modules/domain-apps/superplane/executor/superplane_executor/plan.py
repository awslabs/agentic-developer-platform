"""Closed, approval-bound AWS/EKS controller plan; never accept worker arguments."""

import base64
import hashlib
import json
import re
import ssl
from dataclasses import dataclass

from harness_jobs.identity import MAX_PARAMETER_VALUE_LENGTH, OperationRefused


@dataclass(frozen=True)
class Plan:
    data: dict
    cluster_name: str
    steps: tuple

    @classmethod
    def read(cls, operation, target):
        try:
            return cls.validate_request(
                operation.request,
                target,
                org_id=operation.grant.lease.org_id,
                workspace_id=operation.grant.lease.workspace_id,
                max_resource_units=operation.max_resource_units,
                max_runtime_seconds=operation.max_runtime_seconds,
                max_cost_micros=operation.max_cost_micros,
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            raise OperationRefused(
                "approved controller plan is invalid or unsupported"
            ) from None

    @classmethod
    def validate_request(
        cls,
        request,
        target,
        *,
        org_id,
        workspace_id,
        max_resource_units,
        max_runtime_seconds,
        max_cost_micros,
    ):
        """Pure plan validation shared by preview and execution.

        This method neither grants authority nor constructs an execution identity.
        Execution still obtains its request and envelope from VerifiedOperation.
        """
        try:
            if request.action not in {"provision", "teardown"}:
                raise ValueError("unsupported controller action")
            data = json.loads(request.parameters["controller_plan"])
            steps = json.loads(request.parameters["execution_steps"])
            allocation = request.parameters["allocation_id"]
            required = {
                "version",
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
            }
            if data.get("version") in {2, 3}:
                fields = (required - {"certificate_authority"}) | {
                    "certificate_authority_sha256"
                }
                if data["version"] == 3:
                    fields = (fields - {"instance_type"}) | {
                        "accelerators",
                        "max_gpus_per_node",
                    }
                if set(data) != fields:
                    raise ValueError("unsupported versioned plan")
                ca = request.parameters["controller_certificate_authority"]
                if (
                    not isinstance(ca, str)
                    or not 1 <= len(ca) <= MAX_PARAMETER_VALUE_LENGTH
                    or hashlib.sha256(ca.encode()).hexdigest()
                    != data["certificate_authority_sha256"]
                ):
                    raise ValueError("approved certificate binding changed")
                # Public trust material only. Parsing a local in-memory trust
                # store verifies this is a real PEM certificate, with no network
                # discovery or credential construction at preview time.
                pem = base64.b64decode(ca, validate=True).decode("ascii")
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                tls.load_verify_locations(cadata=pem)
                data.pop("certificate_authority_sha256")
                data["certificate_authority"] = ca
            elif (
                set(data) != required
                or data.get("version") != 1
                or "controller_certificate_authority" in request.parameters
            ):
                raise ValueError("unsupported plan")
            if any(
                data[key] != target[key]
                for key in ("cluster_arn", "endpoint", "namespace")
            ):
                raise ValueError("workspace target mismatch")
            if (
                not re.fullmatch(r"\d{12}", data["provider_account_id"])
                or not re.fullmatch(r"[a-z]{2}(?:-gov)?-[a-z]+-\d", data["region"])
                or not re.fullmatch(r"ami-[a-f0-9]{8,17}", data["image_id"])
                or (
                    data["version"] != 3
                    and not re.fullmatch(r"[a-z0-9]+\.[a-z0-9]+", data["instance_type"])
                )
                or not re.fullmatch(
                    r"[A-Za-z0-9+=,.@_-]{1,128}", data["instance_profile"]
                )
                or any(
                    not isinstance(data[key], str) or not 1 <= len(data[key]) <= 255
                    for key in ("vpc_name", "security_group")
                )
                or type(data["node_count"]) is not int
                or not 1 <= data["node_count"] <= 16
                or type(data["disk_size"]) is not int
                or not 20 <= data["disk_size"] <= 2048
            ):
                raise ValueError("unbounded resources")
            control = (
                request.action == "teardown"
                and data["version"] in {2, 3}
                and bool(request.parameters.get("controller_deployment_id"))
                and bool(request.parameters.get("controller_source_operation_id"))
            )
            if (
                type(max_resource_units) is not int
                or type(max_runtime_seconds) is not int
                or max_runtime_seconds <= 0
                or type(max_cost_micros) is not int
                or (control and (max_resource_units != 0 or max_cost_micros != 0))
                or (
                    not control
                    and (
                        data["node_count"] > max_resource_units or max_cost_micros <= 0
                    )
                )
            ):
                raise ValueError("approved finite envelope required")
            if data["cluster_arn"].split(":")[3:5] != [
                data["region"],
                data["provider_account_id"],
            ]:
                raise ValueError("cloud target mismatch")
            if (
                request.parameters.get("provider") != "aws"
                or request.parameters.get("provider_account_id")
                != data["provider_account_id"]
            ):
                raise ValueError("provider identity mismatch")
            name = (
                "sp-"
                + hashlib.sha256(
                    json.dumps(
                        [
                            org_id,
                            workspace_id,
                            allocation,
                        ]
                    ).encode()
                ).hexdigest()[:32]
            )
            # Exact actions use the shared reviewed AWS effect vocabulary. Creating
            # steps remain blocked by allocation seals; delete_cluster cannot create.
            actions = (
                ["launch", "status", "deploy", "status"]
                if request.action == "provision"
                else ["delete_cluster"]
            )
            expected = tuple(
                {
                    "step_id": str(index + 1),
                    "provider": "aws",
                    "operation_kind": action,
                    "target": name,
                }
                for index, action in enumerate(actions)
            )
            if steps != list(expected):
                raise ValueError("plan descriptors mismatch")
            workload = data["workload"]
            if set(workload) != {
                "kind",
                "name",
                "image",
                "command",
                "args",
                "gpu_count",
                "cpu",
                "memory",
                "port",
                "auth_secret",
            }:
                raise ValueError("unsupported workload")
            if workload["kind"] not in ("batch", "serving") or not re.fullmatch(
                r"[a-z][a-z0-9-]{0,50}", workload["name"]
            ):
                raise ValueError("invalid workload")
            if not re.fullmatch(
                r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", workload["image"]
            ):
                raise ValueError("workload image must be immutable")
            for key in ("command", "args"):
                if (
                    not isinstance(workload[key], list)
                    or len(workload[key]) > 32
                    or any(
                        not isinstance(x, str) or len(x) > 1024 for x in workload[key]
                    )
                ):
                    raise ValueError("invalid workload invocation")
            if (
                type(workload["gpu_count"]) is not int
                or not 0 <= workload["gpu_count"] <= 8
            ):
                raise ValueError("invalid GPU request")
            if data["version"] == 3:
                choices = data["accelerators"]
                limit = data["max_gpus_per_node"]
                if (
                    type(limit) is not int
                    or not 1 <= limit <= 8
                    or not isinstance(choices, list)
                    or not 1 <= len(choices) <= 8
                    or any(
                        not isinstance(choice, str)
                        or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,63}:[1-8]", choice)
                        for choice in choices
                    )
                    or len(set(choices)) != len(choices)
                    or any(
                        not max(1, workload["gpu_count"])
                        <= int(choice.rsplit(":", 1)[1])
                        <= limit
                        for choice in choices
                    )
                    or (
                        not control and data["node_count"] * limit != max_resource_units
                    )
                ):
                    raise ValueError(
                        "GPU choices must fit the approved physical capacity"
                    )
            if not re.fullmatch(
                r"[1-9][0-9]{0,4}m?", workload["cpu"]
            ) or not re.fullmatch(r"[1-9][0-9]{0,4}[MG]i", workload["memory"]):
                raise ValueError("invalid workload resources")
            if workload["kind"] == "serving":
                if (
                    type(workload["port"]) is not int
                    or not 1024 <= workload["port"] <= 65535
                    or not re.fullmatch(
                        r"[a-z][a-z0-9-]{0,62}", workload["auth_secret"]
                    )
                ):
                    raise ValueError("serving authentication required")
            elif workload["port"] is not None or workload["auth_secret"] is not None:
                raise ValueError("batch cannot expose a serving endpoint")
            return cls(data, name, expected)
        except (KeyError, TypeError, ValueError, AttributeError, ssl.SSLError):
            raise OperationRefused(
                "approved controller plan is invalid or unsupported"
            ) from None

    @property
    def user_id(self):
        return hashlib.sha256(self.cluster_name.encode()).hexdigest()[:8]

    @property
    def cloud_cluster_name(self):
        # Pinned AWS permits 248 characters; this name needs no truncation.
        return self.cluster_name + "-" + self.user_id

    @property
    def request_environment(self):
        return {
            "SKYPILOT_USER_ID": self.user_id,
            "SKYPILOT_USER": "superplane-executor",
        }

    def task(self, operation):
        data = self.data
        config = {
            "apiVersion": "node.eks.aws/v1alpha1",
            "kind": "NodeConfig",
            "spec": {
                "cluster": {
                    "name": data["cluster_arn"].split("/")[-1],
                    "apiServerEndpoint": data["endpoint"],
                    "certificateAuthority": data["certificate_authority"],
                    "cidr": data["service_cidr"],
                },
                "kubelet": {
                    "flags": [
                        "--node-labels=superplane.ai/capacity="
                        + self.cluster_name
                        + ",superplane.ai/workspace="
                        + operation.grant.lease.workspace_id,
                        "--register-with-taints=superplane.ai/capacity="
                        + self.cluster_name
                        + ":NoSchedule",
                    ]
                },
            },
        }
        # JSON is YAML-compatible. This generated nodeadm configuration contains
        # only public EKS discovery data. Node auth uses the existing instance role;
        # no hybrid activation code or service credential enters SkyPilot task state.
        encoded = base64.b64encode(json.dumps(config).encode()).decode()
        task = {
            "name": self.cluster_name,
            "num_nodes": data["node_count"],
            "resources": {
                "cloud": "aws",
                "region": data["region"],
                "image_id": data["image_id"],
                "use_spot": False,
                "disk_size": data["disk_size"],
                "labels": {"superplane-capacity": self.cluster_name},
            },
            "setup": "set -eu\nprintf '%s' '"
            + encoded
            + "' | base64 -d | sudo tee /etc/eks/superplane-node.json >/dev/null\nsudo nodeadm init --config-source file:///etc/eks/superplane-node.json",
            "run": "remaining=$(("
            + str(int(operation.grant.lease.runtime_deadline.timestamp()))
            + ' - $(date +%s))); if [ "$remaining" -gt 0 ]; then sleep "$remaining"; fi',
        }
        if data["version"] == 3:
            # Alternatives for this allocation. SkyPilot performs selection;
            # every candidate retains the installed AWS identity/network/image.
            task["resources"]["any_of"] = [
                {"accelerators": value} for value in data["accelerators"]
            ]
            task["resources"]["labels"]["superplane-max-gpus-per-node"] = str(
                data["max_gpus_per_node"]
            )
        else:
            task["resources"]["instance_type"] = data["instance_type"]
        return json.dumps(task)
