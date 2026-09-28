"""The pinned backend makes detached AWS resources discoverable from creation."""

import copy
import json

import boto3
import pytest
from botocore.client import BaseClient
from botocore.stub import Stubber

from installation.skypilot_runtime import (
    install,
    prepare_instances,
    verify_region_binding,
)


def parameters():
    return {
        "ImageId": "ami-0123456789abcdef0",
        "InstanceType": "g5.xlarge",
        "MinCount": 1,
        "MaxCount": 1,
        "TagSpecifications": [
            {
                "ResourceType": "instance",
                "Tags": [
                    {"Key": "superplane-capacity", "Value": "sp-" + "a" * 32},
                ],
            }
        ],
    }


def test_guard_tags_volumes_and_interfaces_in_the_same_run_instances_call(monkeypatch):
    monkeypatch.setattr(BaseClient, "_make_api_call", BaseClient._make_api_call)
    install()
    client = boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    expected = parameters()
    prepare_instances(expected)
    assert {s["ResourceType"] for s in expected["TagSpecifications"]} == {
        "instance",
        "volume",
        "network-interface",
    }
    with Stubber(client) as stub:
        stub.add_response("run_instances", {"Instances": []}, expected)
        client.run_instances(**parameters())
        stub.assert_no_pending_responses()


@pytest.mark.parametrize("mutation", ["missing", "foreign"])
def test_guard_refuses_unattributed_and_mixed_allocation_resources(mutation):
    values = copy.deepcopy(parameters())
    if mutation == "missing":
        values["TagSpecifications"] = []
    else:
        values["TagSpecifications"].append(
            {
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "superplane-capacity", "Value": "sp-" + "b" * 32},
                ],
            }
        )
    with pytest.raises(ValueError):
        prepare_instances(values)


@pytest.mark.parametrize("count", [1, 8, None, True])
def test_selected_physical_gpu_count_is_checked_before_run_instances(
    monkeypatch, count
):
    monkeypatch.setattr(BaseClient, "_make_api_call", BaseClient._make_api_call)
    install()
    client = boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    values = parameters()
    values["TagSpecifications"][0]["Tags"].append(
        {"Key": "superplane-max-gpus-per-node", "Value": "1"}
    )
    record = {"InstanceType": values["InstanceType"]}
    if count is not None:
        record["GpuInfo"] = {"Gpus": [{"Count": count}]}
    with Stubber(client) as stub:
        # Model-level bool validation would hide the runtime's malformed-value
        # check, so inject that response at the read boundary in that one case.
        if count is True:
            monkeypatch.setattr(
                client,
                "describe_instance_types",
                lambda **_: {"InstanceTypes": [record]},
            )
        else:
            stub.add_response(
                "describe_instance_types",
                {"InstanceTypes": [record]},
                {"InstanceTypes": [values["InstanceType"]]},
            )
        if type(count) is int and count == 1:
            expected = copy.deepcopy(values)
            prepare_instances(expected)
            stub.add_response("run_instances", {"Instances": []}, expected)
            client.run_instances(**values)
        else:
            with pytest.raises(ValueError):
                client.run_instances(**values)
        stub.assert_no_pending_responses()


def approved_regions():
    return [
        ["us-east-1", "ami-0123456789abcdef0"],
        ["us-west-2", "ami-0123456789abcdef1"],
    ]


def test_region_binding_absent_leaves_single_region_plans_unaffected():
    """#5925 acceptance 2: single-region plans (no approved-regions tag) keep
    their existing contract -- this guard only engages for a regional plan.
    """
    verify_region_binding(
        type(
            "Client", (), {"meta": type("Meta", (), {"region_name": "us-east-1"})()}
        )(),
        parameters(),
    )


@pytest.mark.parametrize(
    "selected_region,selected_image,accepted",
    [
        ("us-east-1", "ami-0123456789abcdef0", True),
        ("us-west-2", "ami-0123456789abcdef1", True),
        ("us-west-2", "ami-0123456789abcdef0", False),  # cross-region AMI reuse
        ("eu-west-1", "ami-0123456789abcdef0", False),  # unapproved region
        ("us-east-1", "ami-deadbeefdeadbeef0", False),  # unapproved image
    ],
)
def test_region_binding_matches_only_the_approved_region_image_pair(
    selected_region, selected_image, accepted
):
    """#5925 acceptance 2: the actually-selected region+image must be one of the
    approved pairs, checked before RunInstances -- regional AMI IDs are never
    reused across regions, so a mismatched pairing is refused even when both
    the region and the image are independently approved for some other pair.
    """
    values = parameters()
    values["ImageId"] = selected_image
    values["TagSpecifications"][0]["Tags"].append(
        {
            "Key": "superplane-approved-regions",
            "Value": json.dumps(approved_regions(), separators=(",", ":")),
        }
    )
    client = type(
        "Client", (), {"meta": type("Meta", (), {"region_name": selected_region})()}
    )()
    if accepted:
        verify_region_binding(client, values)
    else:
        with pytest.raises(ValueError):
            verify_region_binding(client, values)


@pytest.mark.parametrize(
    "mutation",
    ["not-json", "not-list", "too-many", "wrong-shape", "duplicate-tag"],
)
def test_region_binding_rejects_malformed_approved_set(mutation):
    values = parameters()
    tag_value = json.dumps(approved_regions(), separators=(",", ":"))
    if mutation == "not-json":
        tag_value = "not-json"
    elif mutation == "not-list":
        tag_value = json.dumps({"us-east-1": "ami-0123456789abcdef0"})
    elif mutation == "too-many":
        tag_value = json.dumps(approved_regions() * 3, separators=(",", ":"))
    elif mutation == "wrong-shape":
        tag_value = json.dumps([["us-east-1"]], separators=(",", ":"))
    tags = [{"Key": "superplane-approved-regions", "Value": tag_value}]
    if mutation == "duplicate-tag":
        tags.append(
            {
                "Key": "superplane-approved-regions",
                "Value": json.dumps(approved_regions(), separators=(",", ":")),
            }
        )
    values["TagSpecifications"][0]["Tags"].extend(tags)
    client = type(
        "Client", (), {"meta": type("Meta", (), {"region_name": "us-east-1"})()}
    )()
    with pytest.raises(ValueError):
        verify_region_binding(client, values)


def test_region_binding_runs_before_run_instances_creates_anything(monkeypatch):
    monkeypatch.setattr(BaseClient, "_make_api_call", BaseClient._make_api_call)
    install()
    client = boto3.client(
        "ec2",
        region_name="eu-west-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    values = parameters()
    values["TagSpecifications"][0]["Tags"].append(
        {
            "Key": "superplane-approved-regions",
            "Value": json.dumps(approved_regions(), separators=(",", ":")),
        }
    )
    with Stubber(client) as stub:
        # No response queued: a call reaching run_instances would fail on an
        # empty stub queue, which would mask this guard rejecting it first.
        with pytest.raises(ValueError):
            client.run_instances(**values)
        stub.assert_no_pending_responses()


def test_gpu_inspection_failure_never_falls_through_to_instance_creation(monkeypatch):
    monkeypatch.setattr(BaseClient, "_make_api_call", BaseClient._make_api_call)
    install()
    client = boto3.client(
        "ec2",
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    values = parameters()
    values["TagSpecifications"][0]["Tags"].append(
        {"Key": "superplane-max-gpus-per-node", "Value": "1"}
    )
    from botocore.exceptions import ClientError

    with Stubber(client) as stub:
        stub.add_client_error(
            "describe_instance_types",
            service_error_code="UnauthorizedOperation",
            expected_params={"InstanceTypes": [values["InstanceType"]]},
        )
        with pytest.raises(ClientError):
            client.run_instances(**values)
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "mutation",
    [
        None,
        "account",
        "subnet",
        "group",
        "profile",
        "image",
        "region",
        "count",
        "disk",
        "unencrypted",
        "interface",
        "missing-binding",
    ],
)
def test_actual_run_instances_enforces_complete_regional_binding(monkeypatch, mutation):
    from installation import skypilot_runtime as runtime

    values = parameters()
    binding = {
        "superplane-approved-regions": json.dumps(
            approved_regions(), separators=(",", ":")
        ),
        "superplane-binding-version": "1",
        "superplane-account": "123456789012",
        "superplane-region": "us-east-1",
        "superplane-image": values["ImageId"],
        "superplane-vpc": "vpc-0123456789abcdef0",
        "superplane-subnets": "subnet-0123456789abcdef0",
        "superplane-security-group": "sg-0123456789abcdef0",
        "superplane-profile": "approved",
        "superplane-disk-gb": "100",
        "superplane-node-count": "1",
    }
    if mutation == "missing-binding":
        del binding["superplane-binding-version"]
    values["TagSpecifications"][0]["Tags"].extend(
        {"Key": k, "Value": v} for k, v in binding.items()
    )
    values.update(
        SubnetId="subnet-0123456789abcdef0",
        SecurityGroupIds=["sg-0123456789abcdef0"],
        IamInstanceProfile={"Name": "approved"},
        BlockDeviceMappings=[
            {"DeviceName": "/dev/sda1", "Ebs": {"Encrypted": True, "VolumeSize": 100}}
        ],
    )
    if mutation == "subnet":
        values["SubnetId"] = "subnet-fffffffffffffffff"
    if mutation == "group":
        values["SecurityGroupIds"] = ["sg-fffffffffffffffff"]
    if mutation == "profile":
        values["IamInstanceProfile"] = {"Name": "foreign"}
    if mutation == "image":
        values["ImageId"] = "ami-fffffffffffffffff"
    if mutation == "count":
        values["MaxCount"] = 2
    if mutation == "disk":
        values["BlockDeviceMappings"][0]["Ebs"]["VolumeSize"] = 101
    if mutation == "unencrypted":
        values["BlockDeviceMappings"][0]["Ebs"]["Encrypted"] = False
    if mutation == "interface":
        values["NetworkInterfaces"] = [
            {"NetworkInterfaceId": "eni-fffffffffffffffff", "DeviceIndex": 0}
        ]
    calls = []

    def underlying(client, operation, args):
        if operation == "DescribeSubnets":
            return {
                "Subnets": [
                    {
                        "SubnetId": values["SubnetId"],
                        "VpcId": binding["superplane-vpc"],
                        "OwnerId": "123456789012",
                    }
                ]
            }
        if operation == "DescribeSecurityGroups":
            return {
                "SecurityGroups": [
                    {
                        "GroupId": binding["superplane-security-group"],
                        "VpcId": binding["superplane-vpc"],
                        "OwnerId": "123456789012",
                    }
                ]
            }
        assert operation == "RunInstances"
        calls.append(args)
        return {"Instances": []}

    monkeypatch.setattr(BaseClient, "_make_api_call", underlying)
    monkeypatch.setattr(
        runtime,
        "selected_account",
        lambda _: "000000000000" if mutation == "account" else "123456789012",
    )
    install()
    client = boto3.client(
        "ec2",
        region_name="eu-west-1" if mutation == "region" else "us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    if mutation is None:
        client.run_instances(**values)
        assert len(calls) == 1
    else:
        with pytest.raises(ValueError):
            client.run_instances(**values)
        assert calls == []
