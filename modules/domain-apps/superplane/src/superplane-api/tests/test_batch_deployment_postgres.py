"""Batch uses actual grants, approvals, quota, outbox, worker RPC and finalizer.

Only provider/network transports are doubled. These are code integration checks,
not live batch acceptance or proof of actual GPU capacity.
"""

import asyncio
import json
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.deployment import Deployment
from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.routers import proxy
from app.schemas.proxy import CreateBatchRequest, DeleteDeploymentRequest
from app.services import deployment_operations
from app.services.provisioning import ProvisioningRefused, ProvisioningUnavailable
from app.services.quota import count_workspace_deployment_gpus
from superplane_executor.deployment_plan import BATCH_FIELDS, compact
from superplane_executor.deployment_registry import registration_values
from harness_jobs.identity import OperationRefused

from tests.test_controller_deployment_postgres import (
    workload as workload,
    worker_runtime,
    assert_completed_worker,
    assert_replaced_workload_cannot_complete,
)
from tests.test_lifecycle_api_postgres import lifecycle as lifecycle
from tests.test_operation_budget_ledger_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    postgres_available,
)

pytestmark = [] if os.environ.get("CI") else postgres_available


@pytest.fixture
async def batch_workload(workload):  # noqa: F811
    policy = json.loads(workload.policy_path.read_text())
    profiles = policy["tenants"][str(workload.org_id)]["workspaces"][
        str(workload.workload_id)
    ]
    profile = json.loads(compact(profiles["approved-model"]))
    profile["model_options"] = {}
    profile["serving_auth_contract"] = None
    profile["workload"].update(
        kind="batch",
        port=None,
        auth_secret=None,
        image="registry.example/batch-with-data@sha256:" + "b" * 64,
        command=["/app/run"],
        args=["--input", "/app/immutable-data.json"],
    )
    profiles["approved-batch"] = profile
    workload.policy_path.write_text(compact(policy))
    body = CreateBatchRequest(
        operation_id=uuid.uuid4(),
        profile_id="approved-batch",
        name="batch-run",
        batch_options={key: profile["workload"][key] for key in BATCH_FIELDS},
    )
    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            review = await deployment_operations.preview_create(
                db, workload.org_id, workload.workload_id, body
            )
    approval = await workload.approve(review.public(str(workload.workload_id)))
    workload.preview = review
    workload.batch_body = body.model_copy(
        update={
            "approval_id": approval,
            "plan_revision": review.public(str(workload.workload_id))["revision"],
        }
    )
    return workload


@pytest.fixture
async def batch_runtime(batch_workload, tmp_path):
    async for runtime in worker_runtime.__wrapped__(batch_workload, tmp_path):
        yield runtime


async def create(context, body=None):
    with context.actor(workspace_id=context.workload_id):
        async with context.sessions() as db:
            return await proxy.create_batch(
                context.workload_id,
                body or context.batch_body,
                context.api_request,
                context.org_id,
                db,
            )


async def stop(context, created, *, cleanup_mode="aggregate"):
    body = DeleteDeploymentRequest(operation_id=uuid.uuid4(), cleanup_mode=cleanup_mode)
    with context.actor(workspace_id=context.workload_id):
        async with context.sessions() as db:
            review = await proxy.preview_batch_teardown(
                context.workload_id,
                created["job_id"],
                body,
                context.api_request,
                context.org_id,
                db,
            )
    approval = await context.approve(review)
    context.stop_body = body.model_copy(
        update={"approval_id": approval, "plan_revision": review["revision"]}
    )
    with context.actor(workspace_id=context.workload_id):
        async with context.sessions() as db:
            return await proxy.delete_batch(
                context.workload_id,
                created["job_id"],
                context.stop_body,
                context.api_request,
                context.org_id,
                db,
            )


async def test_batch_catalog_is_disjoint_and_cannot_admit_or_expose_credentials(
    batch_workload,
):
    c = batch_workload
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            batch = await proxy.batch_profiles(
                c.workload_id, c.api_request, c.org_id, db
            )
            serving = await proxy.deployment_profiles(
                c.workload_id, c.api_request, c.org_id, db
            )
    assert [p["profile_id"] for p in batch["profiles"]] == ["approved-batch"]
    assert [p["profile_id"] for p in serving["profiles"]] == ["approved-model"]
    assert batch["can_submit"] and batch["can_review_teardown"]
    assert (
        batch["profiles"][0]["batch_options"] == c.batch_body.batch_options.model_dump()
    )
    assert "credential" not in compact(batch)
    async with c.connections.connect() as conn:
        assert await conn.fetchval("SELECT count(*) FROM harness_operations") == 0


async def test_lost_batch_admission_reply_and_concurrent_retries_keep_one_paid_job(
    batch_workload, monkeypatch
):
    c = batch_workload
    actual = deployment_operations.admit_controller_deployment

    async def lose_reply(*args, **kwargs):
        await actual(*args, **kwargs)
        raise ProvisioningUnavailable("lost domain registration reply")

    monkeypatch.setattr(
        deployment_operations, "admit_controller_deployment", lose_reply
    )
    with pytest.raises(ProvisioningUnavailable):
        await create(c)
    monkeypatch.setattr(deployment_operations, "admit_controller_deployment", actual)
    # The original durable intent survives removal of all current profiles.
    c.policy_path.unlink()
    a, b = await asyncio.wait_for(asyncio.gather(create(c), create(c)), 10)
    assert a == b
    assert a["execution_outcome"] == "unknown" and a["observed_cost_micros"] is None
    async with c.connections.connect() as conn:
        for table in (
            "deployments",
            "harness_operations",
            "operation_budget_reservations",
            "controller_deployment_operations",
            "harness_dispatch_outbox",
        ):
            assert await conn.fetchval(f"SELECT count(*) FROM {table}") == 1
        assert await conn.fetchval("SELECT workload_kind FROM deployments") == "batch"
        assert (
            await conn.fetchval("SELECT provisioning_operation_id FROM workspaces")
            == "original-bootstrap"
        )


@pytest.mark.parametrize(
    "change", ["image", "command", "gpu", "kind", "approval", "revoked"]
)
async def test_changed_batch_request_or_authority_cannot_reserve_or_admit(
    batch_workload, change
):
    c = batch_workload
    data = c.batch_body.model_dump()
    if change == "image":
        data["batch_options"]["image"] = "registry.example/other@sha256:" + "c" * 64
    elif change == "command":
        data["batch_options"]["command"] = ["/app/other"]
    elif change == "gpu":
        data["batch_options"]["gpu_count"] = 2
    elif change == "kind":
        data["profile_id"] = "approved-model"
    elif change == "approval":
        data["approval_id"] = uuid.uuid4()
    else:
        async with c.sessions() as db:
            grant = await db.scalar(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.workspace_id == c.workload_id,
                    WorkspaceGrantRecord.principal == "requester",
                )
            )
            grant.revoked_at = datetime.now(UTC)
            await db.commit()
    with pytest.raises(ProvisioningRefused):
        await create(c, CreateBatchRequest(**data))
    async with c.connections.connect() as conn:
        assert await conn.fetchval("SELECT count(*) FROM harness_operations") == 0
        assert await conn.fetchval("SELECT count(*) FROM deployments") == 0


async def test_batch_and_serving_share_the_same_quota_without_sharing_ids(
    batch_workload,
):
    c = batch_workload
    created = await create(c)
    async with c.sessions() as db:
        workspace = await db.get(Workspace, c.workload_id)
        workspace.budget_max_gpus = 1
        await db.commit()
        assert await count_workspace_deployment_gpus(c.workload_id, db) == 1
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            with pytest.raises(HTTPException) as error:
                await proxy.create_deployment(
                    c.workload_id, c.api_body, c.org_id, db, c.api_request
                )
            assert error.value.status_code == 429
        async with c.sessions() as db:
            listed = await proxy.list_deployments(
                c.workload_id, c.org_id, db, c.api_request
            )
            assert listed.deployments == []
            with pytest.raises(ProvisioningRefused):
                await deployment_operations.intent_for(
                    db, c.org_id, c.workload_id, created["job_id"]
                )
            with pytest.raises(ProvisioningRefused):
                await deployment_operations.intent_for(
                    db, c.org_id, uuid.uuid4(), created["job_id"], workload_kind="batch"
                )


@pytest.mark.parametrize("leaked_volume", [False, True])
async def test_batch_api_to_real_worker_and_finalizer_preserves_uid_and_allocation(
    batch_workload, batch_runtime, leaked_volume
):
    c, runtime = batch_workload, batch_runtime
    created = await create(c)
    worker = await runtime.publish(SimpleNamespace(**created))
    assert all(result[1] == "settle" for result in await runtime.execute(worker))
    await assert_completed_worker(runtime, worker)
    path = runtime.kube.path(c.preview.deployment_target, "Job", c.batch_body.name)
    obj = runtime.kube.stored[path]
    assert len(runtime.kube.stored) == 1 and obj["kind"] == "Job"
    assert obj["spec"]["backoffLimit"] == 0
    assert 0 < obj["spec"]["activeDeadlineSeconds"] <= 900
    assert not obj["spec"]["template"]["spec"]["automountServiceAccountToken"]
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            detail = await proxy.get_batch(
                c.workload_id, created["job_id"], c.api_request, c.org_id, db
            )
    assert detail["provider_uid"] == obj["metadata"]["uid"]
    assert detail["cleanup_status"] == "unconfirmed"
    stopped = await stop(c, created)
    retirement = await runtime.publish(SimpleNamespace(**stopped))
    assert (
        retirement.operation.request.parameters["allocation_id"]
        == worker.operation.request.parameters["allocation_id"]
    )
    assert (
        retirement.operation.max_resource_units
        == retirement.operation.max_cost_micros
        == 0
    )
    runtime.cloud.leaked_volume = leaked_volume
    if leaked_volume:
        with pytest.raises(
            OperationRefused, match="allocation reconciliation requires recovery"
        ):
            await runtime.execute(retirement)
    else:
        await runtime.execute(retirement)
    async with c.sessions() as db:
        intent = await db.get(Deployment, created["job_id"])
        assert intent.status == ("Deleting" if leaked_volume else "Deleted")
        assert await count_workspace_deployment_gpus(c.workload_id, db) == int(
            leaked_volume
        )
    assert not runtime.kube.stored and runtime.cloud.launches == 1


async def test_batch_status_cannot_complete_from_replacement_job(
    batch_workload, batch_runtime
):
    created = await create(batch_workload)
    worker = await batch_runtime.publish(SimpleNamespace(**created))
    await assert_replaced_workload_cannot_complete(batch_runtime, worker, "Job")


async def test_batch_registration_rejects_changed_quota_kind_or_command(batch_workload):
    c = batch_workload
    created = await create(c)
    async with c.connections.connect() as conn:
        for sql in (
            "UPDATE deployments SET workload_kind='serving'",
            "UPDATE deployments SET gpu_per_replica=2",
            "UPDATE deployments SET model_name='invented-serving-model'",
        ):
            tx = conn.transaction()
            await tx.start()
            try:
                await conn.execute(sql)
                with pytest.raises(OperationRefused):
                    await registration_values(
                        conn,
                        operation_id=created["operation_id"],
                        org_id=str(c.org_id),
                        workspace_id=str(c.workload_id),
                        deployment_id=str(created["job_id"]),
                    )
            finally:
                await tx.rollback()


async def test_replacement_job_with_original_name_is_never_adopted_or_deleted(
    batch_workload, batch_runtime
):
    c, runtime = batch_workload, batch_runtime
    created = await create(c)
    original = await runtime.publish(SimpleNamespace(**created))
    await runtime.execute(original)
    await assert_completed_worker(runtime, original)
    path = runtime.kube.path(c.preview.deployment_target, "Job", c.batch_body.name)
    runtime.kube.stored[path]["metadata"]["uid"] = "replacement-job-uid"
    stopped = await stop(c, created)
    retirement = await runtime.publish(SimpleNamespace(**stopped))
    with pytest.raises(
        OperationRefused, match="original workload UID evidence unavailable"
    ):
        await runtime.execute(retirement)
    assert runtime.kube.stored[path]["metadata"]["uid"] == "replacement-job-uid"
    assert all(method != "DELETE" for method, _ in runtime.kube.requests)
    async with c.sessions() as db:
        assert await count_workspace_deployment_gpus(c.workload_id, db) == 1


async def test_malformed_installed_batch_policy_returns_unavailable_without_admission(
    batch_workload,
):
    c = batch_workload
    policy = json.loads(c.policy_path.read_text())
    policy["tenants"][str(c.org_id)]["workspaces"][str(c.workload_id)][
        "approved-batch"
    ]["workload"] = None
    c.policy_path.write_text(compact(policy))
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            result = await proxy.batch_profiles(
                c.workload_id, c.api_request, c.org_id, db
            )
    assert result["profiles"] == [] and not result["can_submit"]
    assert result["reason"] == "unavailable"
    async with c.connections.connect() as conn:
        assert await conn.fetchval("SELECT count(*) FROM harness_operations") == 0
