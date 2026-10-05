"""Original paid UID and live manager lease guard public workload observations."""

import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.config import settings
from app.models.workspace_grant import WorkspaceGrantRecord
from app.services import workload_observations
from app.services.provisioning import ProvisioningRefused
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
async def observed(batch_workload, batch_runtime, monkeypatch):
    c, runtime = batch_workload, batch_runtime
    created = await create(c)
    worker = await runtime.publish(SimpleNamespace(**created))
    await runtime.execute(worker)
    instance = str(uuid.uuid4())
    expiry = datetime.now(UTC) + timedelta(seconds=45)
    monkeypatch.setattr(workload_observations, "async_session_factory", c.sessions)
    monkeypatch.setattr(settings, "controller_observation_submitter_id", "reader")
    async with c.connections.connect() as conn:
        await conn.execute(
            "ALTER TABLE observation_leases ADD COLUMN fence_token bigint DEFAULT 7, ADD COLUMN last_holder text, ADD COLUMN acquire_count integer DEFAULT 1, ADD COLUMN updated_at timestamptz DEFAULT now()"
        )
        await conn.execute(
            "UPDATE observation_leases SET holder=$1,expires_at=$2",
            "reader:" + instance,
            expiry,
        )
    captured = []
    response = {
        "org_id": str(c.org_id),
        "workspace_id": str(c.workload_id),
        "cluster_id": c.preview.deployment_target["cluster_id"],
        "namespace": c.preview.deployment_target["namespace"],
        "deployment_id": str(created["job_id"]),
        "operation_id": created["operation_id"],
        "uid": runtime.kube.stored[
            runtime.kube.path(c.preview.deployment_target, "Job", c.batch_body.name)
        ]["metadata"]["uid"],
        "plan_digest": worker.operation.plan_digest,
        "kind": "batch",
        "state": "running",
        "pods": [
            {
                "uid": "original-pod",
                "phase": "Running",
                "ready": False,
                "restarts": 0,
                "exit_code": None,
            }
        ],
        "logs": None,
        "logs_pod_uid": "",
        "logs_truncated": False,
        "checked_at": datetime.now(UTC).isoformat(),
        "instance_id": instance,
        "fence_token": 7,
        "lease_expires_at": expiry.isoformat(),
        "unsafe_extra": {"password": "must-not-escape"},
    }

    async def manager(path, params):
        captured.append((path, params))
        assert params["uid"] == response["uid"]
        assert params["image"] == c.batch_body.batch_options.image
        return response.copy()

    monkeypatch.setattr(workload_observations, "manager_document", manager)
    return SimpleNamespace(c=c, created=created, response=response, captured=captured)


async def read(context, **kwargs):
    c = context.c
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            return await workload_observations.observe(
                c.api_request,
                db,
                c.org_id,
                c.workload_id,
                context.created["job_id"],
                kind="batch",
                **kwargs,
            )


async def test_paid_original_uid_and_current_lease_produce_status_without_quota_release(
    observed,
):
    result = await read(observed)
    assert result["uid"] == observed.response["uid"] and result["state"] == "running"
    assert (
        result["observed_cost_micros"] is None
        and result["cleanup_status"] == "unconfirmed"
    )
    assert "must-not-escape" not in str(result)
    assert observed.captured[0][0] == "/workload-observation"
    async with observed.c.sessions() as db:
        assert await count_workspace_deployment_gpus(observed.c.workload_id, db) == 1


@pytest.mark.parametrize(
    "change", ["workspace", "operation", "digest", "expired", "holder", "oversize"]
)
async def test_mismatched_stale_or_oversized_manager_projection_is_refused(
    observed, change
):
    if change in {"workspace", "operation", "digest"}:
        key = {
            "workspace": "workspace_id",
            "operation": "operation_id",
            "digest": "plan_digest",
        }[change]
        observed.response[key] = "different"
    elif change == "expired":
        observed.response["checked_at"] = (
            datetime.now(UTC) - timedelta(minutes=1)
        ).isoformat()
    elif change == "holder":
        observed.response["instance_id"] = str(uuid.uuid4())
    else:
        observed.response["pods"] *= 33
    with pytest.raises(HTTPException) as error:
        await read(observed)
    assert error.value.status_code == 503


async def test_read_grant_can_observe_but_revocation_during_transport_hides_result(
    observed, monkeypatch
):
    c = observed.c
    async with c.sessions() as db:
        grant = await db.scalar(
            select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.workspace_id == c.workload_id,
                WorkspaceGrantRecord.principal == "requester",
            )
        )
        grant.permissions = "workspace:read"
        await db.commit()
    assert (await read(observed))["state"] == "running"
    actual = workload_observations.manager_document

    async def revoke(*args):
        result = await actual(*args)
        async with c.sessions() as db:
            grant = await db.scalar(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.workspace_id == c.workload_id,
                    WorkspaceGrantRecord.principal == "requester",
                )
            )
            grant.revoked_at = datetime.now(UTC)
            await db.commit()
        return result

    monkeypatch.setattr(workload_observations, "manager_document", revoke)
    with pytest.raises(HTTPException) as error:
        await read(observed)
    assert error.value.status_code == 403


async def test_logs_require_original_selected_pod_and_remain_bounded(observed):
    with pytest.raises(HTTPException) as error:
        await read(observed, logs=True)
    assert error.value.status_code == 422 and not observed.captured
    observed.response.update(
        logs="epoch 1\n", logs_pod_uid="original-pod", logs_truncated=True
    )
    result = await read(observed, logs=True, pod_uid="original-pod")
    assert result["logs"] == "epoch 1\n" and result["logs_truncated"]
    observed.response["logs_pod_uid"] = "replacement-pod"
    with pytest.raises(HTTPException):
        await read(observed, logs=True, pod_uid="original-pod")
    observed.response.update(logs="a" * 65537, logs_pod_uid="original-pod")
    with pytest.raises(HTTPException):
        await read(observed, logs=True, pod_uid="original-pod")


async def test_no_original_uid_cannot_be_observed_by_resource_name(
    batch_workload, monkeypatch
):
    c = batch_workload
    created = await create(c)
    monkeypatch.setattr(workload_observations, "async_session_factory", c.sessions)

    async def forbidden(*args):
        raise AssertionError("manager called without original UID")

    monkeypatch.setattr(workload_observations, "manager_document", forbidden)
    with pytest.raises(HTTPException) as error:
        await read(SimpleNamespace(c=c, created=created))
    assert error.value.status_code == 503
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            with pytest.raises(ProvisioningRefused):
                await workload_observations.observe(
                    c.api_request,
                    db,
                    c.org_id,
                    c.workload_id,
                    uuid.uuid4(),
                    kind="batch",
                )
