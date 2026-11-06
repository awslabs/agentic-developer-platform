"""Shared AWS accounts must not select another workspace's provider authority."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from harness_jobs.identity import OperationRequest, encode_payload, payload_digest

from app.services.workspace_provider_reading import provisioning_credential


class Composition:
    def __init__(self, rows):
        self.rows = rows

    @asynccontextmanager
    async def operation_connect(self):
        yield self

    async def fetchrow(self, query, org, workspace, operation):
        assert "org_id=$1 AND workspace_id=$2 AND operation_id=$3" in query
        return self.rows.get((org, workspace, operation))


def operation(handle):
    request = OperationRequest(
        action="provision", idempotency_key="create-workspace",
        parameters={"credential_id": handle},
    )
    return {"request_payload": encode_payload(request), "plan_digest": payload_digest(request)}


@pytest.mark.asyncio
async def test_each_workspace_keeps_its_own_provider_with_one_shared_account():
    first = SimpleNamespace(org_id="org", id="one", provisioning_operation_id="op-one", aws_account_id="shared")
    second = SimpleNamespace(org_id="org", id="two", provisioning_operation_id="op-two", aws_account_id="shared")
    composition = Composition({
        ("org", "one", "op-one"): operation("spda1:first"),
        ("org", "two", "op-two"): operation("spda1:second"),
    })
    assert await provisioning_credential(composition, first) == "spda1:first"
    assert await provisioning_credential(composition, second) == "spda1:second"
    second.org_id = "other-org"
    with pytest.raises(HTTPException, match="provisioning request is unavailable"):
        await provisioning_credential(composition, second)


@pytest.mark.asyncio
async def test_changed_payload_cannot_select_a_different_credential():
    row = operation("spda1:first")
    row["request_payload"] = operation("spda1:second")["request_payload"]
    workspace = SimpleNamespace(org_id="org", id="one", provisioning_operation_id="op-one")
    with pytest.raises(HTTPException, match="provider observation is unavailable"):
        await provisioning_credential(Composition({("org", "one", "op-one"): row}), workspace)
