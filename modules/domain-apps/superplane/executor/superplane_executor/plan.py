"""Closed, approval-bound AWS/EKS controller plan; never accept worker arguments."""

import base64
import hashlib
import json
import re
import ssl
from dataclasses import dataclass

from harness_jobs.identity import MAX_PARAMETER_VALUE_LENGTH, OperationRefused

REGIONAL_BINDING_FIELDS = frozenset(
    {
        "region",
        "image_id",
        "vpc_name",
        "security_group",
        "instance_profile",
        "vpc_id",
        "security_group_id",
        "subnet_ids",
    }
)


@dataclass(frozen=True)
class Plan:
    data: dict
    cluster_name: str
    steps: tuple
    network: dict | None = None
    node_bootstrap: dict | None = None

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
            if data.get("version") in {2, 3, 4}:
                fields = (required - {"certificate_authority"}) | {
                    "certificate_authority_sha256"
                }
                if data["version"] in {3, 4}:
                    fields = (fields - {"instance_type"}) | {
                        "accelerators",
                        "max_gpus_per_node",
                        "cpus",
                        "memory_gb",
                    }
                if data["version"] == 4:
                    fields = (fields - REGIONAL_BINDING_FIELDS) | {"regions_sha256"}
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
            if data["version"] == 4:
                encoded = request.parameters["controller_regions"]
                if len(encoded) > MAX_PARAMETER_VALUE_LENGTH or hashlib.sha256(
                    encoded.encode()
                ).hexdigest() != data.pop("regions_sha256"):
                    raise ValueError("regional binding digest mismatch")
                regions = data["regions"] = json.loads(encoded)
                if (
                    not isinstance(regions, list)
                    or not 1 <= len(regions) <= 4
                    or any(set(entry) != REGIONAL_BINDING_FIELDS for entry in regions)
                    or any(
                        not re.fullmatch(
                            r"[a-z]{2}(?:-gov)?-[a-z]+-\d", entry["region"]
                        )
                        or not re.fullmatch(r"ami-[a-f0-9]{8,17}", entry["image_id"])
                        or not re.fullmatch(r"vpc-[a-f0-9]{8,17}", entry["vpc_id"])
                        or not re.fullmatch(
                            r"sg-[a-f0-9]{8,17}", entry["security_group_id"]
                        )
                        or not isinstance(entry["subnet_ids"], list)
                        or not 1 <= len(entry["subnet_ids"]) <= 4
                        or len(set(entry["subnet_ids"])) != len(entry["subnet_ids"])
                        or any(
                            not re.fullmatch(r"subnet-[a-f0-9]{8,17}", subnet)
                            for subnet in entry["subnet_ids"]
                        )
                        or not re.fullmatch(
                            r"[A-Za-z0-9+=,.@_-]{1,128}", entry["instance_profile"]
                        )
                        or any(
                            not isinstance(entry[key], str)
                            or not 1 <= len(entry[key]) <= 255
                            for key in ("vpc_name", "security_group")
                        )
                        for entry in regions
                    )
                    # Regional AMI IDs are not portable across regions: a repeated
                    # image id across two entries is a copy-paste error, not a
                    # legitimate alternative. Repeated regions are ambiguous.
                    or len({entry["region"] for entry in regions}) != len(regions)
                    or len({entry["image_id"] for entry in regions}) != len(regions)
                ):
                    raise ValueError("unbounded regional resources")
                if (
                    not re.fullmatch(r"\d{12}", data["provider_account_id"])
                    or type(data["node_count"]) is not int
                    or not 1 <= data["node_count"] <= 16
                    or type(data["disk_size"]) is not int
                    or not 20 <= data["disk_size"] <= 2048
                ):
                    raise ValueError("unbounded resources")
            elif (
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
                and data["version"] in {2, 3, 4}
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
            # The EKS control plane keeps its own fixed region regardless of which
            # approved compute region SkyPilot selects (#5926 carries the resulting
            # cross-region traffic). Versions 1-3 keep the stricter same-region
            # check: their one compute region must be the cluster's own region.
            if data["version"] == 4:
                if data["cluster_arn"].split(":")[4] != data["provider_account_id"]:
                    raise ValueError("cloud target mismatch")
            elif data["cluster_arn"].split(":")[3:5] != [
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
            from .node_command_plan import (
                actions as node_actions,
                read as read_node_bootstrap,
            )

            node_bootstrap = read_node_bootstrap(request.parameters, data, target)
            actions = node_actions(request.action, node_bootstrap is not None)
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
            if data["version"] in {3, 4}:
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
            if data["version"] in {3, 4}:
                cpu_millis = int(workload["cpu"].removesuffix("m")) * (
                    1 if workload["cpu"].endswith("m") else 1000
                )
                memory_mib = int(workload["memory"][:-2]) * (
                    1024 if workload["memory"].endswith("Gi") else 1
                )
                if (
                    type(data["cpus"]) is not int
                    or not 1 <= data["cpus"] <= 1024
                    or type(data["memory_gb"]) is not int
                    or not 1 <= data["memory_gb"] <= 16384
                    or data["cpus"] * 1000 <= cpu_millis
                    or data["memory_gb"] * 1024 <= memory_mib
                ):
                    raise ValueError(
                        "machine CPU and memory minimums must leave room for node services"
                    )
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
            from .network_plan import read_network

            network = read_network(request.parameters, data)
            if network is not None and network["cluster"]["cluster_id"] != target.get(
                "cluster_id"
            ):
                raise ValueError("approved network cluster identity changed")
            from .network_probe_contract import read as probe_contract

            probe_contract(
                data,
                org_id=org_id,
                workspace_id=workspace_id,
                request_id=request.idempotency_key
                if request.action == "provision"
                else None,
                allocation_id=allocation,
            )
            return cls(data, name, expected, network, node_bootstrap)
        except (KeyError, TypeError, ValueError, AttributeError, ssl.SSLError):
            raise OperationRefused(
                "approved controller plan is invalid or unsupported"
            ) from None

    @property
    def region_bindings(self):
        """Every approved region binding, uniformly, regardless of plan version.

        Versions 1-3 have exactly one binding (their flat single-region fields);
        version 4 has the approved bounded set. Callers that must check or search
        every approved region -- pre-create authorization and inventory discovery
        -- iterate this instead of special-casing `data["regions"]`.
        """
        if self.data["version"] == 4:
            return tuple(self.data["regions"])
        return (
            {
                key: self.data[key]
                for key in (
                    "region",
                    "image_id",
                    "vpc_name",
                    "security_group",
                    "instance_profile",
                )
            },
        )

    def resource_reference(self, kind, reference, region):
        """Use one durable identity in launch receipts, recovery and inventory."""
        if self.data["version"] != 4:
            return reference
        if region not in {binding["region"] for binding in self.region_bindings}:
            raise OperationRefused("resource region is outside approval")
        resource_type = "elastic-ip" if kind == "address" else kind.replace("_", "-")
        return f"arn:aws:ec2:{region}:{self.data['provider_account_id']}:{resource_type}/{reference}"

    @property
    def cluster_region(self):
        """The EKS control plane's own fixed region, independent of compute region."""
        return self.data["cluster_arn"].split(":")[3]

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

    def node_config(self, operation):
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
        return config

    def task(self, operation):
        data = self.data
        config = self.node_config(operation)
        # JSON is YAML-compatible. This generated nodeadm configuration contains
        # only public EKS discovery data. Node auth uses the existing instance role;
        # no hybrid activation code or service credential enters SkyPilot task state.
        encoded = base64.b64encode(json.dumps(config).encode()).decode()
        task = {
            "name": self.cluster_name,
            "num_nodes": data["node_count"],
            "resources": {
                "cloud": "aws",
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
        if self.node_bootstrap is not None:
            # SSM is the sole bootstrap owner for this explicitly approved path.
            # Neither public NodeConfig nor bootstrap invocation enters SkyPilot.
            task["setup"] = "true"
            task["resources"]["labels"].update(
                {
                    "superplane-org": operation.grant.lease.org_id,
                    "superplane-workspace": operation.grant.lease.workspace_id,
                }
            )
        if data["version"] == 4:
            # Eligible region x GPU alternatives for this allocation. SkyPilot
            # performs the actual selection; every candidate retains its own
            # approved region's image, VPC, security group and node identity.
            # No executor-side ranking, instance choice or provider fallback.
            task["resources"]["any_of"] = [
                {
                    "region": region["region"],
                    "image_id": region["image_id"],
                    "accelerators": accelerator,
                    "_cluster_config_overrides": {
                        "aws": {
                            "remote_identity": region["instance_profile"],
                            "vpc_name": region["vpc_name"],
                            "security_group_name": region["security_group"],
                            "disk_encrypted": True,
                        }
                    },
                }
                for region in data["regions"]
                for accelerator in data["accelerators"]
            ]
            task["resources"]["labels"]["superplane-max-gpus-per-node"] = str(
                data["max_gpus_per_node"]
            )
            # The pinned backend guard (installation/skypilot_runtime.py) re-checks
            # this exact approved [region, image_id] set at the moment EC2 would
            # create the instance -- SkyPilot's own selection is re-verified, not
            # trusted, before any resource exists.
            task["resources"]["labels"]["superplane-approved-regions"] = json.dumps(
                [[region["region"], region["image_id"]] for region in data["regions"]],
                separators=(",", ":"),
            )
            for candidate in task["resources"]["any_of"]:
                binding = next(
                    b for b in data["regions"] if b["region"] == candidate["region"]
                )
                candidate["labels"] = {
                    **task["resources"]["labels"],
                    "superplane-binding-version": "1",
                    "superplane-account": data["provider_account_id"],
                    "superplane-region": binding["region"],
                    "superplane-image": binding["image_id"],
                    "superplane-vpc": binding["vpc_id"],
                    "superplane-subnets": ",".join(binding["subnet_ids"]),
                    "superplane-security-group": binding["security_group_id"],
                    "superplane-profile": binding["instance_profile"],
                    "superplane-disk-gb": str(data["disk_size"]),
                    "superplane-node-count": str(data["node_count"]),
                }
            task["resources"]["cpus"] = f"{data['cpus']}+"
            task["resources"]["memory"] = f"{data['memory_gb']}+"
        elif data["version"] == 3:
            # Alternatives for this allocation. SkyPilot performs selection;
            # every candidate retains the installed AWS identity/network/image.
            task["resources"]["region"] = data["region"]
            task["resources"]["image_id"] = data["image_id"]
            task["resources"]["any_of"] = [
                {"accelerators": value} for value in data["accelerators"]
            ]
            task["resources"]["labels"]["superplane-max-gpus-per-node"] = str(
                data["max_gpus_per_node"]
            )
            task["resources"]["cpus"] = f"{data['cpus']}+"
            task["resources"]["memory"] = f"{data['memory_gb']}+"
        else:
            task["resources"]["region"] = data["region"]
            task["resources"]["image_id"] = data["image_id"]
            task["resources"]["instance_type"] = data["instance_type"]
        return json.dumps(task)
