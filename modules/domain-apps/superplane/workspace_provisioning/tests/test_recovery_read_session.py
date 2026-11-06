"""Real botocore pagination goes through finite read and region guards."""

from concurrent.futures import ThreadPoolExecutor

import boto3
from botocore.config import Config
from botocore.stub import Stubber
import pytest
from harness_jobs.identity import OperationRefused
from workspace_provisioning.recovery_read_session import ReadSession


def test_paginated_reads_cannot_escape_budget_region_or_method():
    session = boto3.Session(
        aws_access_key_id="fixture", aws_secret_access_key="fixture"
    )
    facade = ReadSession(
        session,
        region="us-west-2",
        config=Config(),
        reads={
            ("iam", "list_attached_role_policies"): "iam:ListAttachedRolePolicies",
        },
        limit=1,
    )
    client = facade.client("iam", region_name="us-west-2")
    raw = facade._clients["iam"]
    with Stubber(raw) as stub:
        stub.add_response(
            "list_attached_role_policies",
            {
                "AttachedPolicies": [],
                "IsTruncated": True,
                "Marker": "next-page",
            },
            {"RoleName": "owned"},
        )
        pages = iter(
            client.get_paginator("list_attached_role_policies").paginate(
                RoleName="owned"
            )
        )
        assert next(pages)["IsTruncated"]
        with pytest.raises(OperationRefused, match="bound"):
            next(pages)
        stub.assert_no_pending_responses()
    for action in [
        lambda: client.delete_role(RoleName="owned"),
        lambda: client.get_paginator("list_roles"),
        lambda: facade.client("iam", region_name="us-east-1"),
        lambda: facade.client("sts", region_name="us-west-2"),
    ]:
        with pytest.raises(OperationRefused):
            action()
    facade.close()


def test_concurrent_reads_share_one_finite_budget():
    from types import SimpleNamespace

    raw = SimpleNamespace(
        get_caller_identity=lambda: {"Account": "123456789012"}, close=lambda: None
    )
    facade = ReadSession(
        SimpleNamespace(client=lambda *a, **k: raw),
        region="us-west-2",
        config=Config(),
        reads={
            ("sts", "get_caller_identity"): "sts:GetCallerIdentity",
        },
        limit=7,
    )
    client = facade.client("sts", region_name="us-west-2")

    def read(_):
        try:
            client.get_caller_identity()
            return True
        except OperationRefused:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(read, range(24))) == 7
    facade.close()
