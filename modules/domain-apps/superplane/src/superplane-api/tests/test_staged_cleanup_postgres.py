"""Staged previews and execution use the original paid source and exact snapshot."""
# ruff: noqa: F811

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.schemas.proxy import DeleteDeploymentRequest
from app.services import deployment_operations
from harness_jobs.identity import OperationRefused, decode_payload, encode_payload
from superplane_executor.cleanup_binding import validate
from superplane_executor.cleanup_graph import PARAMETER, canonical, header
from superplane_executor.cleanup_snapshot import retained, select
from superplane_executor.deployment_plan import teardown_request
from tests.test_cleanup_snapshot_postgres import records
from tests.test_batch_deployment_postgres import stop
from tests.test_batch_results_postgres import (
    output as output,
    batch_workload as batch_workload,
    batch_runtime as batch_runtime,
    workload as workload,
    lifecycle as lifecycle,
    ledger as ledger,
    installation_postgres_url as installation_postgres_url,
    pytestmark as pytestmark,
)


async def preview(output, body):
    c = output.c
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            return await deployment_operations.preview_delete(
                c.api_request,
                db,
                c.org_id,
                c.workload_id,
                output.created["job_id"],
                body,
                workload_kind="batch",
            )


async def test_staged_preview_only_reads_existing_original_snapshot(output):
    await output.runtime.execute(
        await output.runtime.publish(SimpleNamespace(**output.created))
    )
    source, stored = await records(output)
    async with output.c.connections.connect() as c:
        snapshot = await retained(c, source, stored)
        before = await c.fetchrow(
            "SELECT (SELECT count(*) FROM harness_operations) AS operations, (SELECT count(*) FROM controller_cleanup_snapshots) AS snapshots, (SELECT count(*) FROM harness_provider_report) AS reports"
        )
    body = DeleteDeploymentRequest(operation_id=uuid4(), cleanup_mode="staged-v1")
    first = await preview(output, body)
    second = await preview(output, body)
    assert first == second
    assert first.public(str(output.c.workload_id))["cleanup_snapshot"] == snapshot
    assert first.request.parameters[PARAMETER] == canonical(header(snapshot))
    assert [
        s["step_id"] for s in json.loads(first.request.parameters["execution_steps"])
    ] == ["cordon:0", "root:0", "drain", "down", "node:0", "inventory"]
    assert (await records(output))[1] == stored
    async with output.c.connections.connect() as c:
        assert (
            await c.fetchrow(
                "SELECT (SELECT count(*) FROM harness_operations) AS operations, (SELECT count(*) FROM controller_cleanup_snapshots) AS snapshots, (SELECT count(*) FROM harness_provider_report) AS reports"
            )
            == before
        )
    aggregate = await preview(
        output, body.model_copy(update={"cleanup_mode": "aggregate"})
    )
    expected = teardown_request(
        output.c.preview.request,
        org_id=source["org_id"],
        workspace_id=source["workspace_id"],
        request_id=str(body.operation_id),
        source_operation_id=source["operation_id"],
    )
    assert encode_payload(aggregate.request) == encode_payload(expected)
    assert "cleanup_snapshot" not in aggregate.public(str(output.c.workload_id))


async def test_missing_snapshot_refuses_staged_without_creating_evidence(output):
    await output.runtime.execute(
        await output.runtime.publish(SimpleNamespace(**output.created))
    )
    async with output.c.connections.connect() as c:
        await c.execute("DELETE FROM controller_cleanup_snapshots")
    with pytest.raises(OperationRefused):
        await preview(
            output,
            DeleteDeploymentRequest(operation_id=uuid4(), cleanup_mode="staged-v1"),
        )
    assert (await records(output))[1] is None
    assert (
        PARAMETER
        not in (
            await preview(output, DeleteDeploymentRequest(operation_id=uuid4()))
        ).request.parameters
    )


@pytest.mark.parametrize(
    "key", ["snapshot_id", "snapshot_sha256", "nodes", "roots", "network"]
)
async def test_staged_snapshot_header_requires_exact_original_evidence(output, key):
    await output.runtime.execute(
        await output.runtime.publish(SimpleNamespace(**output.created))
    )
    source, stored = await records(output)
    async with output.c.connections.connect() as c:
        graph = header(await retained(c, source, stored))
        graph[key] = "f" * 64 if isinstance(graph[key], str) else graph[key] + 1
        with pytest.raises(OperationRefused):
            await select(c, source, graph)
        forged = teardown_request(
            decode_payload(source["request_payload"]),
            org_id=source["org_id"],
            workspace_id=source["workspace_id"],
            request_id=str(uuid4()),
            source_operation_id=source["operation_id"],
            cleanup_graph=graph,
        )
        with pytest.raises(OperationRefused):
            await validate(c, source, forged)


async def test_staged_teardown_completes_original_paid_allocation(output):
    runtime = output.runtime
    await runtime.execute(await runtime.publish(SimpleNamespace(**output.created)))
    stopped = await stop(output.c, output.created, cleanup_mode="staged-v1")
    worker = await runtime.publish(SimpleNamespace(**stopped))
    assert worker.plan.cleanup_graph is not None
    await runtime.execute(worker)
    async with output.c.connections.connect() as c:
        assert (
            await c.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                stopped["operation_id"],
            )
            == "succeeded"
        )
        assert await c.fetchval("SELECT count(*) FROM harness_operations") == 2
    assert not runtime.cloud.exists and runtime.cloud.launches == 1
    assert runtime.cloud.downs == 1
    assert not runtime.kube.stored


@pytest.mark.parametrize(
    "stage,gap",
    [(s, "completed") for s in ["cordon:0", "root:0", "drain", "down", "node:0"]]
    + [("cordon:0", "before-send"), ("down", "lost-handle")],
)
async def test_paid_stage_recovery_only_continues_confirmed_original_effects(
    output, monkeypatch, stage, gap
):
    import asyncio
    from contextlib import asynccontextmanager
    from dataclasses import replace
    from unittest.mock import AsyncMock

    from app.routers import controller_recovery as routes
    from harness_jobs.execution import CallOutcome
    from superplane_executor import staged_cleanup
    from superplane_executor.recovery import ScopedRecovery

    runtime = output.runtime
    await runtime.execute(await runtime.publish(SimpleNamespace(**output.created)))
    stopped = await stop(output.c, output.created, cleanup_mode="staged-v1")
    worker = await runtime.publish(SimpleNamespace(**stopped))
    complete = staged_cleanup.completed

    async def lose_completed_reply(*args, **kwargs):
        result = await complete(*args, **kwargs)
        if args[4] == stage and result:
            raise asyncio.CancelledError(
                "completed stage before shared acknowledgement"
            )
        return result

    if gap == "completed":
        monkeypatch.setattr(staged_cleanup, "completed", lose_completed_reply)
    elif gap == "before-send":

        async def never_sent(*args, **kwargs):
            raise asyncio.CancelledError(
                "shared intent committed before first mutation"
            )

        monkeypatch.setattr(staged_cleanup, "execute", never_sent)
    else:
        remember = worker.finalizer.provider.remember

        async def lose_handle(call, plan, request_id=None, region=None):
            if request_id is not None:
                raise asyncio.CancelledError(
                    "SkyPilot accepted down before handle commit"
                )
            return await remember(call, plan, request_id, region)

        monkeypatch.setattr(worker.finalizer.provider, "remember", lose_handle)
    with pytest.raises(asyncio.CancelledError):
        await runtime.execute(worker)
    monkeypatch.setattr(staged_cleanup, "completed", complete)
    lease = worker.operation.grant.lease
    async with runtime.pool.acquire() as c:
        await c.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1",
            lease.operation_id,
        )
    observer = SimpleNamespace(
        lease_scopes=frozenset({"controller_recovery/" + lease.org_id})
    )
    monkeypatch.setattr(routes, "async_session_factory", output.c.sessions)
    monkeypatch.setattr(
        routes, "_authenticated_submitter", AsyncMock(return_value=observer)
    )

    @asynccontextmanager
    async def provider_for(*_):
        yield worker.finalizer.provider

    monkeypatch.setattr(routes, "observation_provider", provider_for)
    original_role = await worker.registry.authority.delivery_role(worker.operation)
    current_claim = None

    async def recovery_role(operation):
        assert operation == await routes.claim_operation(
            output.c.api_request, current_claim
        )
        return original_role

    monkeypatch.setattr(worker.registry.authority, "delivery_role", recovery_role)
    request = runtime.kube.request
    observing = True

    async def only_reads(operation, target, method, path, **kwargs):
        if observing:
            assert method == "GET", "recovery cannot replay a Kubernetes mutation"
        return await request(operation, target, method, path, **kwargs)

    monkeypatch.setattr(runtime.kube, "request", only_reads)

    async def observe_claim(claim, key, *_):
        nonlocal current_claim
        current_claim = routes.Claim(
            **{name: getattr(claim, name) for name in routes.Claim.model_fields}
        )
        result = await routes.observe(
            routes.StatusRequest(
                claim=current_claim,
                query_id="staged-original-read",
                idempotency_key=key,
            ),
            output.c.api_request,
            observer,
        )
        return (
            CallOutcome(result["outcome"]),
            "original stage observed",
            result["provider_ref"],
        )

    recovery = ScopedRecovery(
        worker.finalizer.provider,
        principal=replace(
            worker.operation.grant.principal,
            permissions=frozenset({"workspace:recover"}),
        ),
        observe_claim=observe_claim,
        operation_id=lease.operation_id,
    )
    reports = await recovery.run()
    assert len(reports) == 1
    if gap != "completed":
        assert reports[0].action == "deferred"
        async with runtime.pool.acquire() as c:
            assert (
                await c.fetchval(
                    "SELECT count(*) FROM harness_provider_call_intent WHERE operation_id=$1 AND stage='intended'",
                    lease.operation_id,
                )
                == 1
            )
            assert await c.fetchval("SELECT count(*) FROM harness_operations") == 2
            assert (
                await c.fetchval(
                    "SELECT state FROM harness_operations WHERE operation_id=$1",
                    lease.operation_id,
                )
                != "succeeded"
            )
        assert getattr(runtime.cloud, "downs", 0) == int(gap == "lost-handle")
        return
    async with runtime.pool.acquire() as c:
        assert (
            await c.fetchval(
                "SELECT holder FROM harness_operation_leases WHERE operation_id=$1",
                lease.operation_id,
            )
            is None
        )
        assert await c.fetchval("SELECT count(*) FROM harness_operations") == 2
    observing = False
    successor = await runtime.publish(SimpleNamespace(**stopped), continuation=True)
    assert successor.operation.grant.lease.fence_token > lease.fence_token
    await runtime.execute(successor)
    async with runtime.pool.acquire() as c:
        assert (
            await c.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                lease.operation_id,
            )
            == "succeeded"
        )
        assert await c.fetchval("SELECT count(*) FROM harness_operations") == 2
    assert not runtime.cloud.exists and runtime.cloud.launches == 1
    assert runtime.cloud.downs == 1


async def test_staged_cleanup_cannot_create_a_second_teardown_uuid(output):
    from app.services.provisioning import ProvisioningRefused

    await output.runtime.execute(
        await output.runtime.publish(SimpleNamespace(**output.created))
    )
    await stop(output.c, output.created, cleanup_mode="staged-v1")
    with pytest.raises((OperationRefused, ProvisioningRefused)):
        await stop(output.c, output.created, cleanup_mode="staged-v1")
    async with output.c.connections.connect() as c:
        assert await c.fetchval("SELECT count(*) FROM harness_operations") == 2
        assert (
            await c.fetchval(
                "SELECT count(*) FROM controller_deployment_operations WHERE action='teardown'"
            )
            == 1
        )
