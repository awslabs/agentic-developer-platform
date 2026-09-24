"""Pinned SkyPilot backend guard, mounted as sitecustomize in every child process.

AWS RunInstances otherwise tags only instances in SkyPilot 0.12.0. Propagate the
allocation tag in the same provider call to independently billable attachments,
so an interrupted launch still leaves discoverable volumes and interfaces.
"""

import os
import re


def prepare_instances(parameters):
    specs = parameters.setdefault("TagSpecifications", [])
    instance = next((s for s in specs if s["ResourceType"] == "instance"), {})
    labels = {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}
    capacity = labels.get("superplane-capacity", "")
    if not re.fullmatch(r"sp-[a-f0-9]{32}", capacity):
        raise ValueError("governed allocation tag required")
    for resource in ("volume", "network-interface"):
        matches = [s for s in specs if s["ResourceType"] == resource]
        if len(matches) > 1:
            raise ValueError("ambiguous provider resource tags")
        if not matches:
            matches = [{"ResourceType": resource, "Tags": []}]
            specs.extend(matches)
        tags = matches[0]["Tags"]
        existing = [t for t in tags if t["Key"] == "superplane-capacity"]
        if existing and (len(existing) != 1 or existing[0]["Value"] != capacity):
            raise ValueError("provider allocation tag mismatch")
        if not existing:
            tags.append({"Key": "superplane-capacity", "Value": capacity})


def verify_gpu_limit(client, parameters):
    """Check SkyPilot's selected machine before EC2 creates any capacity.

    SkyPilot accelerator counts are minimum requirements, so selecting one GPU
    can yield an eight-GPU instance. The approved plan reserves a physical upper
    bound; a catalog match alone cannot authorize exceeding it.
    """
    limits = [
        tag["Value"]
        for spec in parameters.get("TagSpecifications", [])
        if spec["ResourceType"] == "instance"
        for tag in spec.get("Tags", [])
        if tag["Key"] == "superplane-max-gpus-per-node"
    ]
    if not limits:
        return  # Original fixed-instance plans retain their existing contract.
    if (
        len(limits) != 1
        or not isinstance(limits[0], str)
        or not re.fullmatch(r"[1-8]", limits[0])
        or not isinstance(parameters.get("InstanceType"), str)
        or "LaunchTemplate" in parameters
        or "InstanceRequirements" in parameters
    ):
        raise ValueError("explicit physical GPU limit and instance type required")
    selected = parameters["InstanceType"]
    records = client.describe_instance_types(InstanceTypes=[selected])["InstanceTypes"]
    if len(records) != 1 or records[0].get("InstanceType") != selected:
        raise ValueError("selected instance GPU capacity unavailable")
    gpus = records[0].get("GpuInfo", {}).get("Gpus", [])
    if (
        not gpus
        or any(type(gpu.get("Count")) is not int or gpu["Count"] <= 0 for gpu in gpus)
        or sum(gpu["Count"] for gpu in gpus) > int(limits[0])
    ):
        raise ValueError(
            "selected machine exceeds approved GPU capacity or is not a GPU instance"
        )


def install():
    from botocore.client import BaseClient

    original = BaseClient._make_api_call
    if getattr(original, "_superplane_guard", False):
        return

    def guarded(client, operation_name, api_params):
        if client.meta.service_model.service_name == "ec2":
            if operation_name == "RunInstances":
                prepare_instances(api_params)
                verify_gpu_limit(client, api_params)
            elif operation_name in {
                "AllocateAddress",
                "CreateVolume",
                "CreateNetworkInterface",
            }:
                # This reviewed backend supports attachments created atomically
                # with instances, not separately created orphanable resources.
                raise ValueError("separate allocation resource creation is unsupported")
        return original(client, operation_name, api_params)

    guarded._superplane_guard = True
    guarded._superplane_gpu_limit = True
    BaseClient._make_api_call = guarded


if os.environ.get("SUPERPLANE_SKYPILOT_GOVERNED") == "true":
    install()
