"""Pure installation policy rendering; never grants or changes a live AWS role."""

import re
import uuid

DOCUMENT_NAMES = ("SuperplaneNativeBootstrapV1", "SuperplaneNodeProbeV1")


def _scope(account_id, regions):
    if not isinstance(account_id, str) or not re.fullmatch(r"[0-9]{12}", account_id):
        raise ValueError("exact commercial AWS account required")
    if (
        not isinstance(regions, (list, tuple))
        or not 1 <= len(regions) <= 16
        or any(
            not isinstance(r, str)
            or not re.fullmatch(r"(?:us|eu|ap|ca|sa|me|af|il|mx)-[a-z]+-[1-9][0-9]*", r)
            for r in regions
        )
        or len(set(regions)) != len(regions)
    ):
        raise ValueError("finite unique commercial AWS regions required")
    return sorted(regions)


def executor_policy(account_id, regions, *, org_id, workspace_id, capacity_names=None):
    """Bind installation permission to one organization and workspace.

    Optional capacity names further restrict individual approved allocations. This grants no permission to alter instance
    tags, create documents or assume roles. The trusted executor independently
    binds each SendCommand to original journalled EC2 instance IDs.
    """
    regions = _scope(account_id, regions)
    for value in (org_id, workspace_id):
        if not isinstance(value, str):
            raise ValueError("canonical organization/workspace UUID required")
        try:
            valid = str(uuid.UUID(value)) == value
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("canonical organization/workspace UUID required")
    conditions = {
        "ssm:resourceTag/superplane-org": org_id,
        "ssm:resourceTag/superplane-workspace": workspace_id,
    }
    if capacity_names is not None and (
        not isinstance(capacity_names, (list, tuple))
        or not 1 <= len(capacity_names) <= 16
        or any(
            not isinstance(n, str)
            or not re.fullmatch(r"sp-[0-9a-f]{32}-[0-9a-f]{8}", n)
            for n in capacity_names
        )
        or len(set(capacity_names)) != len(capacity_names)
    ):
        raise ValueError("exact approved SkyPilot capacity names required")
    if capacity_names is not None:
        conditions["ssm:resourceTag/ray-cluster-name"] = sorted(capacity_names)
    instances = [f"arn:aws:ec2:{r}:{account_id}:instance/*" for r in regions]
    documents = [
        f"arn:aws:ssm:{r}:{account_id}:document/{n}"
        for r in regions
        for n in DOCUMENT_NAMES
    ]
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "ApprovedNativeDocuments",
                "Effect": "Allow",
                "Action": [
                    "ssm:DescribeDocument",
                    "ssm:GetDocument",
                    "ssm:SendCommand",
                ],
                "Resource": documents,
            },
            {
                "Sid": "ApprovedNativeAllocationTargets",
                "Effect": "Allow",
                "Action": "ssm:SendCommand",
                "Resource": instances,
                "Condition": {"StringEquals": conditions},
            },
            {
                "Sid": "NativeCommandObservations",
                "Effect": "Allow",
                "Action": [
                    "ssm:DescribeInstanceInformation",
                    "ssm:GetCommandInvocation",
                    "ec2:DescribeImages",
                    "ec2:DescribeInstances",
                ],
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": regions}},
            },
        ],
    }


def native_agent_policy(account_id, regions):
    """Run Command channel permissions for the existing EC2 instance role.

    Prepared SSM Agent must support ssmmessages. This is neither a hybrid
    activation policy nor permission to initiate Session Manager sessions.
    """
    regions = _scope(account_id, regions)
    instances = [f"arn:aws:ec2:{r}:{account_id}:instance/*" for r in regions]
    native = {
        "StringEquals": {"aws:RequestedRegion": regions},
        "ArnLike": {"ec2:SourceInstanceARN": instances},
    }
    return {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "NativeInstanceRegistration",
                "Effect": "Allow",
                "Action": "ssm:UpdateInstanceInformation",
                "Resource": instances,
                "Condition": native,
            },
            {
                "Sid": "NativeControlChannel",
                "Effect": "Allow",
                "Action": "ssmmessages:CreateControlChannel",
                "Resource": "*",
                "Condition": native,
            },
            {
                "Sid": "NativeCommandChannels",
                "Effect": "Allow",
                "Action": [
                    "ssmmessages:CreateDataChannel",
                    "ssmmessages:OpenControlChannel",
                    "ssmmessages:OpenDataChannel",
                ],
                "Resource": "*",
                "Condition": {"StringEquals": {"aws:RequestedRegion": regions}},
            },
        ],
    }
