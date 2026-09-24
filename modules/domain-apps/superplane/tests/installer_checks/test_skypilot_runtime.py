"""The pinned backend makes detached AWS resources discoverable from creation."""

import copy

import boto3
import pytest
from botocore.client import BaseClient
from botocore.stub import Stubber

from installation.skypilot_runtime import install, prepare_instances


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
