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


def install():
    from botocore.client import BaseClient

    original = BaseClient._make_api_call
    if getattr(original, "_superplane_guard", False):
        return

    def guarded(client, operation_name, api_params):
        if client.meta.service_model.service_name == "ec2":
            if operation_name == "RunInstances":
                prepare_instances(api_params)
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
    BaseClient._make_api_call = guarded


if os.environ.get("SUPERPLANE_SKYPILOT_GOVERNED") == "true":
    install()
