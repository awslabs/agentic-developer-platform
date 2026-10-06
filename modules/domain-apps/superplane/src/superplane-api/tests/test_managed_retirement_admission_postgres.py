"""Real approval/admission/recovery; compiled cloud ownership is a fixture fact.

This test exercises the database boundary and dispatch, not provider deletion.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.adapters.operation_dispatch import OperationDispatcher
from app.models.workspace import Workspace
from app.operation_activation import expected_lifecycle_binding
from app.services import managed_retirement, retirement
from app.services.provisioning import ProvisioningRefused
from harness_jobs.identity import OperationRequest, payload_digest

from tests.test_managed_control_registry_postgres import (
    installation_postgres_url,  # noqa: F401
    ledger,  # noqa: F401
    lifecycle,  # noqa: F401
    managed_control,  # noqa: F401
    pytestmark,  # noqa: F401
)
from tests.test_operation_dispatch_postgres import GatewayTransport


@pytest.mark.parametrize("lost_registration", [False, True])
async def test_distinct_removal_approval_admits_and_recovers_one_operation(
    managed_control,  # noqa: F811
    monkeypatch,
    lost_registration,
):
    case = managed_control
    fixture = case.fixture
    workspace_id = uuid.UUID(case.identity["workspace_id"])
    request_id = case.plan.retirement_request_id
    parameters = {
        **dict(case.control.admitted_request().parameters),
        "lifecycle_phase": "retire-workspace",
        "retirement_source_operation_id": case.bootstrap.operation_id,
        "allocation_id": case.plan.original_allocation_id,
        "original_allocation_id": case.plan.original_allocation_id,
        "control_allocation_id": case.plan.allocation_id,
        "execution_steps": "[]",
    }
    request = OperationRequest("teardown", request_id, parameters)
    revision = payload_digest(request)
    review = {
        "revision": revision,
        "approval_request": {
            "workspace_id": str(workspace_id),
            "action": request.action,
            "idempotency_key": request_id,
            "parameters": parameters,
        },
    }
    monkeypatch.setattr(retirement, "async_session_factory", fixture.sessions)
    monkeypatch.setattr(managed_retirement, "async_session_factory", fixture.sessions)
    monkeypatch.setattr(managed_retirement, "require_runtime", AsyncMock())

    async def reviewed(composition, db, org_id, workspace_id, request_id):
        workspace, principal = await retirement._workspace(db, org_id, workspace_id)
        return workspace, principal, request, review

    monkeypatch.setattr(managed_retirement, "preview", reviewed)

    async def admit(approval, *, revision_=revision, request_id_=request_id):
        with fixture.actor(workspace_id=workspace_id):
            async with fixture.sessions() as db:
                return await managed_retirement.admit(
                    fixture.composition,
                    db,
                    fixture.org_id,
                    workspace_id,
                    request_id_,
                    revision_,
                    approval,
                )

    # An approved preparation cannot authorize the separately scoped teardown.
    async with fixture.connections.connect() as connection:
        preparation_approval = await connection.fetchval(
            "SELECT approval_id FROM harness_approval_consumption WHERE operation_id=$1",
            case.control.operation_id,
        )
    with pytest.raises(ProvisioningRefused):
        await admit(preparation_approval)
    approval_id = await fixture.approve(review)
    with pytest.raises(ProvisioningRefused, match="review changed"):
        await admit(approval_id, revision_="0" * 64)
    result = await admit(approval_id)
    assert result["retirement_complete"] is False
    assert result["original_allocation_id"] == case.plan.original_allocation_id
    async with fixture.sessions() as db:
        workspace = await db.get(Workspace, workspace_id)
        assert workspace.status == "Teardown"
        assert workspace.provisioning_operation_id == case.bootstrap.operation_id
        assert workspace.teardown_operation_id == result["operation_id"]
        if lost_registration:
            workspace.status = "Active"
            workspace.teardown_operation_id = None
            await db.commit()
    # Even a lost domain commit recovers the same shared operation and approval.
    assert await admit(approval_id) == result
    with pytest.raises(ProvisioningRefused, match="original approved"):
        await admit(approval_id, request_id_=str(uuid.uuid4()))
    with pytest.raises(ProvisioningRefused, match="original approved"):
        await admit(str(uuid.uuid4()))
    transport = GatewayTransport(expected_lifecycle_binding(), adp_org_id="adp-test")
    dispatcher = OperationDispatcher(
        fixture.connections.connect,
        transport,
        domain_connect=fixture.connections.connect,
        policy_for=lambda _: SimpleNamespace(adp_org_id="adp-test"),
    )
    async with fixture.connections.connect() as connection:
        report = await dispatcher.outbox.drain_once(
            connection,
            dispatcher,
            operation_ids=(result["operation_id"],),
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_operations WHERE workspace_id=$1 AND action='teardown'",
                str(workspace_id),
            )
            == 1
        )
    assert report.delivered == 1
    assert len(transport.calls) == 1
