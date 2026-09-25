"""Real paid POST/UID capture and claim-bound recovery; no recovery mutations."""
# ruff: noqa: F811 - shared production fixtures

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from harness_jobs.leases import fence_expired_lease
from superplane_executor import results, workload_submissions

from app.routers import controller_recovery as routes
from tests.test_batch_results_postgres import (
    output as output,
    batch_workload as batch_workload,
    batch_runtime as batch_runtime,
    workload as workload,
    lifecycle as lifecycle,
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    pytestmark as pytestmark,
)


@pytest.mark.parametrize(
    "change",
    [
        "captured",
        "lost-uid",
        "replacement",
        "changed-image",
        "absent",
        "denied",
        "foreign-membership",
        "foreign-step",
        "revoked",
        "status",
        "status-missing-result",
        "privileged",
        "alternate-sa",
        "scheduler",
        "deadline",
        "defaulted",
        "journal-mismatch",
        "before-post",
        "missing-submission",
    ],
)
async def test_original_paid_workload_recovery(output, monkeypatch, change):
    runtime = output.runtime
    monkeypatch.setattr(routes, "async_session_factory", output.c.sessions)
    worker = await runtime.publish(SimpleNamespace(**output.created))
    request = runtime.kube.request
    apply = runtime.kube.apply
    capture = results.capture
    record = workload_submissions.record
    posts = []
    observing = False

    async def transport(operation, target, method, path, **kwargs):
        if observing:
            assert method == "GET", "recovery must not mutate the cluster"
        response = await request(operation, target, method, path, **kwargs)
        if method == "POST" and "/jobs" in path:
            posts.append(response.json()["metadata"]["uid"])
            if change == "lost-uid":
                raise asyncio.CancelledError("POST accepted before UID reply")
        if observing and "/jobs/" in path:
            if change == "revoked":
                async with output.c.connections.connect() as connection:
                    await connection.execute(
                        "UPDATE harness_operation_leases SET fence_token=fence_token+1 WHERE operation_id=$1",
                        operation.grant.lease.operation_id,
                    )
            if change == "absent":
                return httpx.Response(404)
            if change == "denied":
                return httpx.Response(403)
            body = response.json()
            if change == "replacement":
                body["metadata"]["uid"] = "same-name-replacement"
            if change == "changed-image":
                body["spec"]["template"]["spec"]["containers"][0]["image"] = "foreign"
            pod = body["spec"]["template"]["spec"]
            if change == "privileged":
                pod["containers"][0]["securityContext"]["privileged"] = True
            elif change == "alternate-sa":
                pod["serviceAccountName"] = "other-workspace"
            elif change == "scheduler":
                pod["schedulerName"] = "unapproved-scheduler"
            elif change == "deadline":
                body["spec"]["activeDeadlineSeconds"] += 1
            elif change == "defaulted":
                pod.update(
                    serviceAccountName="default",
                    schedulerName="default-scheduler",
                    dnsPolicy="ClusterFirst",
                )
                pod["containers"][0].update(
                    imagePullPolicy="IfNotPresent",
                    terminationMessagePolicy="File",
                    terminationMessagePath="/dev/termination-log",
                )
            return httpx.Response(response.status_code, json=body)
        return response

    async def lose_apply_reply(*args, **kwargs):
        await apply(*args, **kwargs)
        raise asyncio.CancelledError("UID captured before worker reply")

    async def lose_status_reply(*args, **kwargs):
        if change == "status":
            await capture(*args, **kwargs)
        raise asyncio.CancelledError("status reply lost")

    monkeypatch.setattr(runtime.kube, "request", transport)
    if change == "before-post":

        async def before_post(*args, **kwargs):
            await record(*args, **kwargs)
            raise asyncio.CancelledError("submission intent committed before POST")

        monkeypatch.setattr(workload_submissions, "record", before_post)
    if change.startswith("status"):
        monkeypatch.setattr(results, "capture", lose_status_reply)
    else:
        monkeypatch.setattr(runtime.kube, "apply", lose_apply_reply)
    with pytest.raises(asyncio.CancelledError):
        await runtime.execute(worker)
    assert len(posts) == (0 if change == "before-post" else 1)
    lease = worker.operation.grant.lease
    async with runtime.pool.acquire() as connection:
        if change == "foreign-membership":
            await connection.execute(
                "UPDATE harness_allocation_resource SET fence_token=fence_token+1 WHERE operation_id=$1 AND kind='workspace_object'",
                lease.operation_id,
            )
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1",
            lease.operation_id,
        )
        takeover = await fence_expired_lease(
            connection,
            operation_id=lease.operation_id,
            recovery_principal=replace(
                worker.operation.grant.principal,
                permissions=frozenset({"workspace:recover"}),
            ),
        )
        assert takeover is not None
        call = await connection.fetchrow(
            "SELECT * FROM harness_provider_call_intent WHERE operation_id=$1 AND stage='intended'",
            lease.operation_id,
        )
        # Kubernetes deploy/status have no SkyPilot request journal. Recovery
        # must select the real admitted intent, not synthesize a transport ID.
        assert not await connection.fetchval(
            "SELECT request_id FROM controller_provider_requests WHERE idempotency_key=$1",
            call["idempotency_key"],
        )
        if change == "journal-mismatch":
            await connection.execute(
                "INSERT INTO controller_provider_requests VALUES($1,$2,$3,$4,$5,'deploy','foreign-request',NULL)",
                call["idempotency_key"],
                lease.operation_id,
                "foreign-org",
                lease.workspace_id,
                worker.plan.cluster_name,
            )
    claim = routes.Claim(
        **{name: getattr(takeover.lease, name) for name in routes.Claim.model_fields}
    )
    body = routes.StatusRequest(
        claim=claim, query_id="original-read", idempotency_key=call["idempotency_key"]
    )
    if change == "foreign-step":
        body = body.model_copy(update={"idempotency_key": "foreign"})
    observer = SimpleNamespace(
        lease_scopes=frozenset({"controller_recovery/" + lease.org_id})
    )
    monkeypatch.setattr(
        routes, "_authenticated_submitter", AsyncMock(return_value=observer)
    )

    @asynccontextmanager
    async def provider_for(*_):
        yield worker.finalizer.provider

    monkeypatch.setattr(routes, "observation_provider", provider_for)
    original_role = await worker.registry.authority.delivery_role(worker.operation)

    async def recovery_role(operation):
        # Declared fixture credential transport: production observation_provider
        # supplies a separate read-only role under this exact recovery claim.
        assert operation == await routes.claim_operation(output.c.api_request, claim)
        return original_role

    monkeypatch.setattr(worker.registry.authority, "delivery_role", recovery_role)
    observing = True
    if change == "missing-submission":
        monkeypatch.setattr(
            workload_submissions, "originals", AsyncMock(return_value=None)
        )
    if change in {"revoked", "foreign-step", "journal-mismatch"}:
        with pytest.raises(HTTPException) as refused:
            await routes.observe(body, output.c.api_request, observer)
        assert refused.value.status_code == 403
    else:
        first = await routes.observe(body, output.c.api_request, observer)
        second = await routes.observe(body, output.c.api_request, observer)
        expected = (
            "succeeded" if change in {"captured", "status", "defaulted"} else "unknown"
        )
        assert first["outcome"] == second["outcome"] == expected
        assert first["provider_ref"] == second["provider_ref"]
        if change in {"captured", "defaulted"}:
            assert first["provider_ref"].endswith(":" + posts[0])
        else:
            assert first["provider_ref"] is None
    assert len(posts) == (0 if change == "before-post" else 1)
    assert runtime.cloud.launches == 1 and runtime.cloud.exists
    async with runtime.pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT stage FROM harness_provider_call_intent WHERE idempotency_key=$1",
                call["idempotency_key"],
            )
            == "intended"
        )
