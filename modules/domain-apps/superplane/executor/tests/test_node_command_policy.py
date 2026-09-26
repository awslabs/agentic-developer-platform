"""Installation permission boundary; no AWS or live role changes."""

import pytest

from superplane_executor.node_command_policy import executor_policy, native_agent_policy

ACCOUNT = "123456789012"
ORG = "11111111-1111-4111-8111-111111111111"
WORKSPACE = "22222222-2222-4222-8222-222222222222"
REGIONS = ["us-east-1", "eu-west-1"]
CAPACITY = "sp-" + "a" * 32 + "-" + "b" * 8


def test_executor_command_targets_require_both_fixed_document_and_allocation_tag():
    policy = executor_policy(
        ACCOUNT, REGIONS, org_id=ORG, workspace_id=WORKSPACE, capacity_names=[CAPACITY]
    )
    documents, instances, reads = policy["Statement"]
    assert len(documents["Resource"]) == 4
    assert all(
        "*" not in arn
        and arn.endswith(("/SuperplaneNativeBootstrapV1", "/SuperplaneNodeProbeV1"))
        for arn in documents["Resource"]
    )
    assert instances["Action"] == "ssm:SendCommand"
    assert instances["Condition"] == {
        "StringEquals": {
            "ssm:resourceTag/superplane-org": ORG,
            "ssm:resourceTag/superplane-workspace": WORKSPACE,
            "ssm:resourceTag/ray-cluster-name": [CAPACITY],
        }
    }
    assert set(instances["Resource"]) == {
        f"arn:aws:ec2:{r}:{ACCOUNT}:instance/*" for r in REGIONS
    }
    assert reads["Resource"] == "*"
    assert set(reads["Action"]) == {
        "ssm:DescribeInstanceInformation",
        "ssm:GetCommandInvocation",
        "ec2:DescribeImages",
        "ec2:DescribeInstances",
    }
    assert reads["Condition"] == {
        "StringEquals": {"aws:RequestedRegion": sorted(REGIONS)}
    }


@pytest.mark.parametrize(
    "names",
    [
        ["*"],
        ["sp-*"],
        [CAPACITY + "*"],
        [CAPACITY, CAPACITY],
        [],
        ["mi-1234567890abcdef0"],
    ],
)
def test_no_wildcard_or_hybrid_capacity_scope(names):
    with pytest.raises(ValueError):
        executor_policy(
            ACCOUNT, REGIONS, org_id=ORG, workspace_id=WORKSPACE, capacity_names=names
        )


@pytest.mark.parametrize(
    "account,regions",
    [
        ("*", REGIONS),
        (ACCOUNT, ["*"]),
        (ACCOUNT, []),
        (ACCOUNT, ["us-east-1", "us-east-1"]),
        (ACCOUNT, ["us-gov-west-1"]),
        (ACCOUNT, ["cn-north-1"]),
    ],
)
def test_refuse_unbounded_or_wrong_partition_scope(account, regions):
    with pytest.raises(ValueError):
        native_agent_policy(account, regions)


def test_node_cannot_dispatch_commands_or_start_interactive_sessions():
    statements = native_agent_policy(ACCOUNT, REGIONS)["Statement"]
    actions = {
        action
        for row in statements
        for action in (
            row["Action"] if isinstance(row["Action"], list) else [row["Action"]]
        )
    }
    assert actions == {
        "ssm:UpdateInstanceInformation",
        "ssmmessages:CreateControlChannel",
        "ssmmessages:CreateDataChannel",
        "ssmmessages:OpenControlChannel",
        "ssmmessages:OpenDataChannel",
    }
    for row in statements[:2]:
        assert row["Condition"]["ArnLike"]["ec2:SourceInstanceARN"] == [
            f"arn:aws:ec2:{r}:{ACCOUNT}:instance/*" for r in sorted(REGIONS)
        ]
    assert all("managed-instance/" not in str(row) for row in statements)


def test_repeat_allocations_keep_workspace_boundary_without_iam_mutation():
    policy = executor_policy(ACCOUNT, REGIONS, org_id=ORG, workspace_id=WORKSPACE)
    assert policy["Statement"][1]["Condition"] == {
        "StringEquals": {
            "ssm:resourceTag/superplane-org": ORG,
            "ssm:resourceTag/superplane-workspace": WORKSPACE,
        }
    }


@pytest.mark.parametrize("value", ["*", "", None, "tenant-name", ORG + "*"])
def test_no_ambient_or_wildcard_workspace_authority(value):
    with pytest.raises(ValueError):
        executor_policy(ACCOUNT, REGIONS, org_id=ORG, workspace_id=value)
    with pytest.raises(ValueError):
        executor_policy(ACCOUNT, REGIONS, org_id=value, workspace_id=WORKSPACE)
