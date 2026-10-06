"""Real admission, cancellation fencing, budget and quota; remote CI only."""

import asyncio
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from harness_jobs.execution import CancellationPending, ProviderCallRefused
from harness_jobs.leases import LeaseRefused, acquire
from sqlalchemy import select

from app.models.workspace_grant import WorkspaceGrantRecord
from app.models.workspace import Workspace
from app.routers import proxy
from app.schemas.proxy import CancelWorkloadRequest
from app.services import workload_cancellation
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable
from app.services.quota import count_workspace_deployment_gpus
from tests.test_batch_deployment_postgres import (
    batch_workload as batch_workload,
    batch_runtime as batch_runtime,
    create,
)
from tests.test_controller_deployment_postgres import workload as workload
from tests.test_lifecycle_api_postgres import lifecycle as lifecycle
from tests.test_operation_budget_ledger_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    postgres_available,
)

pytestmark = [] if os.environ.get("CI") else postgres_available


@pytest.fixture
async def cancellable(batch_workload, ledger, monkeypatch):  # noqa: F811
    c = batch_workload
    c.composition.ledger = ledger[0]
    monkeypatch.setattr(workload_cancellation, "async_session_factory", c.sessions)
    return c


async def cancel(c, created, **changes):
    values = {
        "workspace_id": c.workload_id,
        "job_id": created["job_id"],
        "body": CancelWorkloadRequest(operation_id=created["operation_id"]),
        "request": c.api_request,
        "org_id": c.org_id,
    }
    values.update(changes)
    with c.actor(workspace_id=values["workspace_id"]):
        async with c.sessions() as db:
            return await proxy.cancel_batch(**values, db=db)


async def test_queued_cancel_withdraws_original_outbox_and_releases_both_holds(
    cancellable,
):
    c = cancellable
    created = await create(c)
    c.policy_path.unlink()
    a, b = await asyncio.wait_for(
        asyncio.gather(cancel(c, created), cancel(c, created)), 10
    )
    assert a == b
    assert a["cancellation_requested"] and a["operation_state"] == "cancelled"
    assert (
        a["status"] == "CancelledBeforeDispatch"
        and a["cleanup_status"] == "not-required"
    )
    assert a["observed_cost_micros"] is None
    replay = await create(c)
    assert replay["operation_id"] == created["operation_id"]
    assert replay["status"] == "CancelledBeforeDispatch"
    async with c.sessions() as db:
        assert await count_workspace_deployment_gpus(c.workload_id, db) == 0
    async with c.connections.connect() as conn:
        assert await conn.fetchval("SELECT count(*) FROM harness_dispatch_outbox") == 0
        assert (
            await conn.fetchval("SELECT count(*) FROM harness_provider_call_intent")
            == 0
        )
        assert await conn.fetchval("SELECT count(*) FROM harness_operations") == 1
        assert (
            await conn.fetchval("SELECT state FROM operation_budget_reservations")
            == "released"
        )
        row = await conn.fetchrow("SELECT * FROM harness_operations")
        with pytest.raises(LeaseRefused):
            await acquire(
                conn,
                operation_id=row["operation_id"],
                holder="late-worker",
                attempt_id=row["attempt_id"],
            )


async def test_lost_release_ack_is_recovered_without_reopening_dispatch(
    cancellable, monkeypatch
):
    c = cancellable
    created = await create(c)
    release = c.composition.ledger.release

    async def lose_ack(**kwargs):
        await release(**kwargs)
        raise ProvisioningUnavailable("lost release acknowledgement")

    monkeypatch.setattr(c.composition.ledger, "release", lose_ack)
    with pytest.raises(ProvisioningUnavailable):
        await cancel(c, created)
    async with c.sessions() as db:
        assert await count_workspace_deployment_gpus(c.workload_id, db) == 1
    monkeypatch.setattr(c.composition.ledger, "release", release)
    assert (await cancel(c, created))["cleanup_status"] == "not-required"
    async with c.sessions() as db:
        assert await count_workspace_deployment_gpus(c.workload_id, db) == 0


@pytest.mark.parametrize("change", ["workspace", "job", "operation", "revoked"])
async def test_wrong_identity_or_revoked_grant_cannot_cancel(cancellable, change):
    c = cancellable
    created = await create(c)
    changes = {}
    if change == "workspace":
        changes["workspace_id"] = uuid.uuid4()
    elif change == "job":
        changes["job_id"] = uuid.uuid4()
    elif change == "operation":
        changes["body"] = CancelWorkloadRequest(operation_id="foreign-operation")
    else:
        async with c.sessions() as db:
            row = await db.scalar(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.workspace_id == c.workload_id,
                    WorkspaceGrantRecord.principal == "requester",
                )
            )
            row.revoked_at = datetime.now(UTC)
            await db.commit()
    with pytest.raises(ProvisioningRefused):
        await cancel(c, created, **changes)
    async with c.connections.connect() as conn:
        assert (
            await conn.fetchval("SELECT cancel_requested_at FROM harness_operations")
            is None
        )
        assert (
            await conn.fetchval("SELECT state FROM operation_budget_reservations")
            == "confirmed"
        )


async def test_held_cancel_blocks_new_provider_steps_and_retains_quota(
    cancellable, batch_runtime
):
    c = cancellable
    created = await create(c)
    worker = await batch_runtime.publish(SimpleNamespace(**created))
    cancelled = await cancel(c, created)
    assert cancelled["cancellation_requested"]
    assert cancelled["cleanup_status"] == "unconfirmed"
    with pytest.raises(ProviderCallRefused, match="Cancelled or terminal"):
        await batch_runtime.execute(worker)
    assert batch_runtime.cloud.launches == 0 and not batch_runtime.kube.stored
    async with c.sessions() as db:
        assert await count_workspace_deployment_gpus(c.workload_id, db) == 1
    async with c.connections.connect() as conn:
        assert (
            await conn.fetchval("SELECT state FROM operation_budget_reservations")
            == "confirmed"
        )


async def test_completed_workload_is_not_retroactively_cancelled(
    cancellable, batch_runtime
):
    c = cancellable
    created = await create(c)
    worker = await batch_runtime.publish(SimpleNamespace(**created))
    await batch_runtime.execute(worker)
    result = await cancel(c, created)
    assert result["operation_state"] == "succeeded"
    assert (
        not result["cancellation_requested"]
        and result["cleanup_status"] == "unconfirmed"
    )
    assert batch_runtime.cloud.launches == 1 and batch_runtime.kube.stored
    async with c.sessions() as db:
        assert await count_workspace_deployment_gpus(c.workload_id, db) == 1


async def test_suspended_workspace_can_cancel_without_spend_permission(cancellable):
    c = cancellable
    created = await create(c)
    async with c.sessions() as db:
        workspace = await db.get(Workspace, c.workload_id)
        workspace.status = "Suspended"
        grant = await db.scalar(
            select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.workspace_id == c.workload_id,
                WorkspaceGrantRecord.principal == "requester",
            )
        )
        grant.permissions = "workspace:read workspace:provision"
        await db.commit()
    assert (await cancel(c, created))["status"] == "CancelledBeforeDispatch"


async def test_cancel_during_provider_reply_keeps_original_effect_and_quota(
    cancellable, batch_runtime, monkeypatch
):
    c = cancellable
    created = await create(c)
    worker = await batch_runtime.publish(SimpleNamespace(**created))
    provider = worker.server._provider_call
    returned, release = asyncio.Event(), asyncio.Event()

    async def delayed_reply(call):
        result = await provider(call)
        returned.set()
        await release.wait()
        return result

    monkeypatch.setattr(worker.server, "_provider_call", delayed_reply)
    running = asyncio.create_task(batch_runtime.execute(worker))
    try:
        await asyncio.wait_for(returned.wait(), 10)
        result = await cancel(c, created)
        assert (
            result["cancellation_requested"]
            and result["cleanup_status"] == "unconfirmed"
        )
    finally:
        release.set()
    with pytest.raises(CancellationPending):
        await asyncio.wait_for(running, 10)
    assert batch_runtime.cloud.launches == 1
    async with c.connections.connect() as conn:
        calls = await conn.fetch(
            "SELECT provider_ref FROM harness_provider_call_intent"
        )
        assert len(calls) == 1 and calls[0]["provider_ref"]
        assert (
            await conn.fetchval("SELECT count(*) FROM harness_allocation_resource") > 0
        )
    async with c.sessions() as db:
        assert await count_workspace_deployment_gpus(c.workload_id, db) == 1


async def test_delivered_but_unheld_cancellation_does_not_release_quota(cancellable):
    from app.adapters.operation_dispatch import OperationDispatcher
    from app.operation_activation import expected_lifecycle_binding
    from tests.test_operation_dispatch_postgres import GatewayTransport

    c = cancellable
    created = await create(c)
    gateway = GatewayTransport(expected_lifecycle_binding(), adp_org_id="adp-test")
    dispatcher = OperationDispatcher(c.connections.connect, gateway)
    assert (await dispatcher.drain_once()).delivered == 1
    result = await cancel(c, created)
    assert (
        result["cancellation_requested"] and result["cleanup_status"] == "unconfirmed"
    )
    assert result["status"] != "CancelledBeforeDispatch"
    async with c.sessions() as db:
        assert await count_workspace_deployment_gpus(c.workload_id, db) == 1
    async with c.connections.connect() as conn:
        assert (
            await conn.fetchval("SELECT state FROM operation_budget_reservations")
            == "retained"
        )


async def test_serving_uses_same_cancellation_and_rejects_a_batch_identity(cancellable):
    from tests.test_controller_deployment_postgres import api_create

    c = cancellable
    created = await api_create(c)
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            with pytest.raises(ProvisioningRefused):
                await proxy.cancel_batch(
                    c.workload_id,
                    created.deployment_id,
                    CancelWorkloadRequest(operation_id=created.operation_id),
                    c.api_request,
                    c.org_id,
                    db,
                )
        async with c.sessions() as db:
            result = await proxy.cancel_deployment(
                c.workload_id,
                created.deployment_id,
                CancelWorkloadRequest(operation_id=created.operation_id),
                c.api_request,
                c.org_id,
                db,
            )
            assert result["status"] == "CancelledBeforeDispatch"
        async with c.sessions() as db:
            assert await count_workspace_deployment_gpus(c.workload_id, db) == 0
