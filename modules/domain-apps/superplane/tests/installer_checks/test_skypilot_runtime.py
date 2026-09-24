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
