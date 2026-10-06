"""Only actual provider identities from Terraform state enter allocation metadata."""

from copy import deepcopy

import pytest

from workspace_provisioning.applied_inventory import (
    identities_from_state,
    infrastructure_document,
)
from workspace_provisioning.runtime_config import LifecycleRefused


def state():
    return {
        "format_version": "1.0",
        "values": {
            "root_module": {
                "resources": [
                    {
                        "mode": "managed",
                        "type": "aws_vpc",
                        "address": "aws_vpc.workspace[0]",
                        "values": {
                            "id": "vpc-owned",
                            "arn": "arn:aws:ec2:us-east-1:111122223333:vpc/vpc-owned",
                            "private_value": "must-not-be-published",
                        },
                    },
                    {
                        "mode": "data",
                        "type": "aws_caller_identity",
                        "values": {"id": "account"},
                    },
                    {
                        "mode": "managed",
                        "type": "terraform_data",
                        "values": {"input": "guard"},
                    },
                ]
            }
        },
    }


def test_actual_state_identity_projection_drops_secrets_and_non_resources():
    rows = identities_from_state(state())
    assert rows == [
        {
            "address": "aws_vpc.workspace[0]",
            "type": "aws_vpc",
            "identity": {
                "id": "vpc-owned",
                "arn": "arn:aws:ec2:us-east-1:111122223333:vpc/vpc-owned",
            },
        }
    ]
    document = infrastructure_document(rows)
    assert document["resource_changes"][0]["change"]["before"] == rows[0]["identity"]
    assert "actions" not in document["resource_changes"][0]["change"]


@pytest.mark.parametrize(
    "change", ["format", "missing", "duplicate", "no_identity", "unsupported"]
)
def test_incomplete_applied_state_cannot_be_certified(change):
    value = deepcopy(state())
    rows = value["values"]["root_module"]["resources"]
    if change == "format":
        value["format_version"] = "unknown"
    elif change == "missing":
        value["values"] = {}
    elif change == "duplicate":
        rows.append(rows[0])
    elif change == "no_identity":
        rows[0]["values"] = {"private_value": "not-an-identity"}
    else:
        rows[0]["type"] = "foreign_cloud_resource"
    with pytest.raises(LifecycleRefused):
        identities_from_state(value)
