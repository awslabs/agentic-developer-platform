"""Original paid executor capture, retained downloads, and authorization failures."""

import hashlib
import json
import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import select

from tests.controller_provider_support import completed_job_pod

from app.models.workspace_grant import WorkspaceGrantRecord
from app.services import batch_results
from superplane_executor import results
from harness_jobs.identity import OperationRefused
from tests.test_batch_deployment_postgres import (
    batch_workload as batch_workload,
    batch_runtime as batch_runtime,
    create,
    stop,
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
async def output(batch_workload, batch_runtime, monkeypatch):  # noqa: F811 - imported fixtures
    c, runtime = batch_workload, batch_runtime
    context = SimpleNamespace(
        c=c,
        runtime=runtime,
        created=await create(c),
        text="accuracy=0.95\n<script>untrusted</script>\ntoken=private-value",
        foreign=False,
        change=None,
    )
    request = runtime.kube.request
    monkeypatch.setattr(batch_results, "async_session_factory", c.sessions)

    async def transport(operation, target, method, path, **kwargs):
        if method == "GET" and ("/pods?" in path or "/pods/result-pod" in path):
            job = next(
                (obj for obj in runtime.kube.stored.values() if obj["kind"] == "Job"),
                None,
            )
            if job is None:
                return httpx.Response(200, json={"items": []})
            pod = completed_job_pod(job)
            pod["metadata"].update(name="result-pod", uid="original-pod")
            if context.foreign:
                pod["metadata"]["ownerReferences"][0]["uid"] = "foreign"
            pod["status"]["containerStatuses"][0]["state"]["terminated"]["message"] = (
                json.dumps(
                    {
                        "superplane_result_version": 1,
                        "text": context.text,
                    }
                )
            )
            if context.change == "image":
                pod["spec"]["containers"][0]["image"] = "foreign"
            elif context.change == "namespace":
                pod["metadata"]["namespace"] = "foreign"
            elif context.change == "exit":
                pod["status"]["containerStatuses"][0]["state"]["terminated"][
                    "exitCode"
                ] = 1
            if "/pods?" in path:
                listing = {"items": [pod]}
                if context.change == "multiple":
                    listing["items"].append(pod)
                elif context.change == "pagination":
                    listing["metadata"] = {"continue": "another-page"}
                return httpx.Response(200, json=listing)
            if context.change == "uid":
                pod["metadata"]["uid"] = "replacement-pod"
            return httpx.Response(200, json=pod)
        return await request(operation, target, method, path, **kwargs)

    monkeypatch.setattr(runtime.kube, "request", transport)
    return context


async def read(context, workspace=None):
    c = context.c
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            return await batch_results.read(
                c.api_request,
                db,
                c.org_id,
                workspace or c.workload_id,
                context.created["job_id"],
            )


async def test_result_is_captured_before_success_and_survives_owned_cleanup(output):
    before = await read(output)
    assert before["status"] == "not_captured" and before["result"] is None
    runtime = output.runtime
    worker = await runtime.publish(SimpleNamespace(**output.created))
    await runtime.execute(worker)
    result = await read(output)
    assert result["status"] == "retained"
    assert (
        result["result"]["content"]
        == "accuracy=0.95\n<script>untrusted</script>\ntoken=[REDACTED]"
    )
    assert result["result"]["redacted"] is True
    assert (
        result["result"]["sha256"]
        == hashlib.sha256(result["result"]["content"].encode()).hexdigest()
    )
    stopped = await stop(output.c, output.created)
    await runtime.execute(await runtime.publish(SimpleNamespace(**stopped)))
    assert not runtime.kube.stored
    output.c.policy_path.unlink()
    assert await read(output) == result
    assert (
        result["operation_id"]
        == output.created["operation_id"]
        != stopped["operation_id"]
    )


async def test_foreign_labelled_pod_cannot_publish_result(output):
    output.foreign = True
    worker = await output.runtime.publish(SimpleNamespace(**output.created))
    # A foreign-owned Pod never establishes readiness. The real 30-second
    # assignment expires before the 60-second lease; finalization refuses it.
    with pytest.raises(OperationRefused, match="^execution assignment revoked$"):
        await output.runtime.execute(worker)
    result = await read(output)
    assert result["status"] == "not_captured" and result["result"] is None
    assert output.runtime.cloud.exists
    assert output.runtime.cloud.launches == 1
    async with output.c.connections.connect() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                output.created["operation_id"],
            )
            != "succeeded"
        )


@pytest.mark.parametrize(
    "change", ["uid", "image", "namespace", "exit", "pagination", "multiple"]
)
async def test_changed_pod_refuses_capture_and_successful_settlement(output, change):
    output.change = change
    await output.runtime.execute(
        await output.runtime.publish(SimpleNamespace(**output.created))
    )
    assert (await read(output))["status"] == "not_captured"
    async with output.c.connections.connect() as conn:
        assert (
            await conn.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                output.created["operation_id"],
            )
            != "succeeded"
        )


async def test_result_publication_replay_is_immutable_and_survives_lost_reply(
    output, monkeypatch
):
    capture = results.capture

    async def lose_reply(*args):
        await capture(*args)
        await capture(*args)  # Identical re-acknowledgement is allowed.
        output.text = "different output"
        with pytest.raises(
            OperationRefused, match="retained batch result identity changed"
        ):
            await capture(*args)
        raise RuntimeError("simulated lost acknowledgement after result commit")

    monkeypatch.setattr(results, "capture", lose_reply)
    await output.runtime.execute(
        await output.runtime.publish(SimpleNamespace(**output.created))
    )
    result = await read(output)
    assert "accuracy=0.95" in result["result"]["content"]
    async with output.c.connections.connect() as conn:
        assert await conn.fetchval("SELECT count(*) FROM controller_batch_results") == 1


async def test_wrong_workspace_revocation_and_corrupt_result_are_refused(output):
    await output.runtime.execute(
        await output.runtime.publish(SimpleNamespace(**output.created))
    )
    with pytest.raises(HTTPException) as error:
        await read(output, uuid.uuid4())
    assert error.value.status_code == 403
    async with output.c.connections.connect() as conn:
        await conn.execute("UPDATE controller_batch_results SET content='changed'")
    with pytest.raises(HTTPException) as error:
        await read(output)
    assert error.value.status_code == 503
    async with output.c.sessions() as db:
        grant = await db.scalar(
            select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.workspace_id == output.c.workload_id,
                WorkspaceGrantRecord.principal == "requester",
            )
        )
        grant.revoked_at = datetime.now(UTC)
        await db.commit()
    with pytest.raises(HTTPException) as error:
        await read(output)
    assert error.value.status_code == 403
