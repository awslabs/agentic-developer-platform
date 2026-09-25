"""Pinned SkyPilot backend guard, mounted as sitecustomize in every child process.

AWS RunInstances otherwise tags only instances in SkyPilot 0.12.0. Propagate the
allocation tag in the same provider call to independently billable attachments,
so an interrupted launch still leaves discoverable volumes and interfaces.
"""

import json
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


def verify_region_binding(client, parameters):
    """Check the actually-selected region and image before EC2 creates capacity.

    SkyPilot chooses which approved region/image alternative to use; that choice
    must be re-verified here, at the moment of creation, rather than trusted from
    whatever the task requested. A region or image outside the approved bounded
    set must never reach RunInstances, even if something upstream misbehaved.
    Regional AMI IDs are not portable across regions, so each pair -- not just
    each region or each image alone -- is checked together.
    """
    bindings = [
        tag["Value"]
        for spec in parameters.get("TagSpecifications", [])
        if spec["ResourceType"] == "instance"
        for tag in spec.get("Tags", [])
        if tag["Key"] == "superplane-approved-regions"
    ]
    if not bindings:
        return  # Single-region plans bind region/image at the server config level.
    if len(bindings) != 1:
        raise ValueError("ambiguous approved regional bindings")
    try:
        approved = json.loads(bindings[0])
    except ValueError:
        raise ValueError("approved regional bindings unavailable") from None
    if (
        not isinstance(approved, list)
        or not 1 <= len(approved) <= 4
        or any(
            not isinstance(entry, list)
            or len(entry) != 2
            or not all(isinstance(v, str) for v in entry)
            for entry in approved
        )
    ):
        raise ValueError("approved regional bindings malformed")
    selected_region = client.meta.region_name
    selected_image = parameters.get("ImageId")
    if (
        not isinstance(selected_image, str)
        or [
            selected_region,
            selected_image,
        ]
        not in approved
    ):
        raise ValueError("selected region and image are outside the approved set")


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


def selected_account(client):
    import boto3

    credentials = client._request_signer._credentials.get_frozen_credentials()
    sts = boto3.client(
        "sts",
        region_name=client.meta.region_name,
        aws_access_key_id=credentials.access_key,
        aws_secret_access_key=credentials.secret_key,
        aws_session_token=credentials.token,
    )
    return sts.get_caller_identity()["Account"]


def verify_selected_binding(client, parameters):
    """Validate the trusted candidate's full binding at the EC2 mutation boundary."""
    pairs = [
        tag
        for spec in parameters.get("TagSpecifications", [])
        if spec["ResourceType"] == "instance"
        for tag in spec.get("Tags", [])
    ]
    labels = {t["Key"]: t["Value"] for t in pairs}
    if "superplane-approved-regions" not in labels:
        return
    required = {
        "binding-version",
        "account",
        "region",
        "image",
        "vpc",
        "subnets",
        "security-group",
        "profile",
        "disk-gb",
        "node-count",
    }
    if any(
        sum(t["Key"] == "superplane-" + key for t in pairs) != 1 for key in required
    ):
        raise ValueError("complete regional binding required")

    def value(key):
        return labels["superplane-" + key]

    if (
        value("binding-version") != "1"
        or client.meta.region_name != value("region")
        or parameters.get("ImageId") != value("image")
        or "LaunchTemplate" in parameters
        or "InstanceRequirements" in parameters
    ):
        raise ValueError("regional launch binding mismatch")
    if selected_account(client) != value("account"):
        raise ValueError("selected provider account mismatch")
    profile = parameters.get("IamInstanceProfile")
    expected_arn = (
        f"arn:aws:iam::{value('account')}:instance-profile/{value('profile')}"
    )
    if profile not in ({"Name": value("profile")}, {"Arn": expected_arn}):
        raise ValueError("node profile not approved")
    interfaces = parameters.get("NetworkInterfaces")
    if interfaces is not None:
        if (
            len(interfaces) != 1
            or interfaces[0].get("DeviceIndex") != 0
            or "NetworkInterfaceId" in interfaces[0]
            or "SubnetId" in parameters
            or "SecurityGroupIds" in parameters
            or "SecurityGroups" in parameters
        ):
            raise ValueError("ambiguous instance networking")
        subnet = interfaces[0].get("SubnetId")
        groups = interfaces[0].get("Groups")
    else:
        subnet = parameters.get("SubnetId")
        groups = parameters.get("SecurityGroupIds")
        if "SecurityGroups" in parameters:
            raise ValueError("security group IDs required")
    if subnet not in value("subnets").split(",") or groups != [value("security-group")]:
        raise ValueError("selected network is outside approval")
    subnets = client.describe_subnets(SubnetIds=[subnet])["Subnets"]
    security = client.describe_security_groups(GroupIds=groups)["SecurityGroups"]
    if (
        len(subnets) != 1
        or subnets[0].get("SubnetId") != subnet
        or subnets[0].get("VpcId") != value("vpc")
        or subnets[0].get("OwnerId") != value("account")
        or len(security) != 1
        or security[0].get("GroupId") != groups[0]
        or security[0].get("VpcId") != value("vpc")
        or security[0].get("OwnerId") != value("account")
    ):
        raise ValueError("selected network ownership mismatch")
    for key in ("MinCount", "MaxCount"):
        if type(parameters.get(key)) is not int or not 1 <= parameters[key] <= int(
            value("node-count")
        ):
            raise ValueError("instance count exceeds approval")
    disks = parameters.get("BlockDeviceMappings", [])
    if (
        len(disks) != 1
        or "Ebs" not in disks[0]
        or disks[0]["Ebs"].get("Encrypted") is not True
        or type(disks[0]["Ebs"].get("VolumeSize")) is not int
        or not 1 <= disks[0]["Ebs"]["VolumeSize"] <= int(value("disk-gb"))
    ):
        raise ValueError("explicit encrypted bounded root disk required")


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
                verify_region_binding(client, api_params)
                verify_selected_binding(client, api_params)
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
    guarded._superplane_regional_binding = True
    BaseClient._make_api_call = guarded


if os.environ.get("SUPERPLANE_SKYPILOT_GOVERNED") == "true":
    install()
