import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src.internal import domain_provider_cleanup as cleanup

CLUSTER = "arn:aws:eks:us-east-1:123456789012:cluster/workspace"
ENTRY = CLUSTER.replace(":cluster/", ":access-entry/") + "/role/123456789012/installer/immutable"


@pytest.fixture
def journal(monkeypatch):
    claim = hashlib.sha256(b"superplane-workspace-bootstrap-claim:v1:token").hexdigest()
    generation = hashlib.sha256(("operation:" + claim).encode()).hexdigest()
    spec = {
        "key": "installer",
        "kind": "eks-entry",
        "generation": generation,
        "cluster_arn": CLUSTER,
        "principal_arn": "arn:aws:iam::123456789012:role/installer",
        "lifetime": "temporary",
    }
    identity = {"arn": ENTRY, "generation": generation, "groups": [], "username": "installer"}
    plan = {"grants": [spec]}
    progress = {"phase": "revoking", "installer": {"phase": "revoke_intent", "identity": identity}}
    row = dict(
        workspace_id="workspace",
        org_id="domain",
        operation_id="operation",
        generation=generation,
        cluster_arn=CLUSTER,
        claim=claim,
        attempt_token="token",
        reservation_status="reserved",
        identity_json=json.dumps(dict(workspace_id="workspace", org_id="domain", cluster_arn=CLUSTER)),
    )

    @asynccontextmanager
    async def connect(binding):
        async def fetch(sql, *values):
            assert values == ("workspace", "domain", "operation")
            return [{**row, "plan_json": json.dumps(plan), "progress_json": json.dumps(progress)}]

        yield SimpleNamespace(fetch=fetch)

    monkeypatch.setattr(cleanup, "domain_connect", connect)
    return row, plan, progress


async def test_cleanup_policy_is_exact_registered_generation_entry(journal):
    policy, selected = await cleanup.cleanup_policy(
        SimpleNamespace(org_id="domain"), {"operation_id": "operation", "workspace_id": "workspace"}, ENTRY
    )
    (statement,) = json.loads(policy)["Statement"]
    assert statement["Resource"] == ENTRY
    assert set(statement["Action"]) == {
        "eks:DeleteAccessEntry",
        "eks:DisassociateAccessPolicy",
        "eks:DescribeAccessEntry",
        "eks:ListAssociatedAccessPolicies",
    }
    assert selected[0] == journal[0]["generation"]


@pytest.mark.parametrize("mutation", ["claim", "generation", "state", "registered", "phase", "entry", "adopted", "retained", "resource"])
async def test_other_or_replaced_entry_never_gets_cleanup_policy(journal, mutation):
    row, plan, progress = journal
    if mutation in {"claim", "generation"}:
        row[mutation] = "different"
    elif mutation == "state":
        row["reservation_status"] = "ready"
    elif mutation == "registered":
        row["identity_json"] = "{}"
    elif mutation == "phase":
        progress["phase"] = "active"
    elif mutation == "entry":
        progress["installer"]["identity"]["arn"] += "replacement"
    elif mutation == "adopted":
        progress["installer"]["phase"] = "adopted"
    elif mutation == "retained":
        plan["grants"][0]["lifetime"] = "workspace"
        progress["retain_workspace"] = True
    else:
        plan["grants"][0]["lifetime"] = "resource"
    with pytest.raises(HTTPException):
        await cleanup.cleanup_policy(SimpleNamespace(org_id="domain"), {"operation_id": "operation", "workspace_id": "workspace"}, ENTRY)
