"""Real approved cleanup admission and paid RPC after cancelled-source fencing."""

# Imported fixture names are intentionally also test parameters.
# ruff: noqa: F811

import asyncio
import os
from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException
from harness_jobs.execution import ProviderCallRefused
from harness_jobs.identity import OperationRefused, ResolvedPrincipal
from harness_jobs.leases import LeaseRefused, acquire, fence_expired_lease

from app.routers.proxy import (
    cancel_deployment,
    delete_deployment,
    preview_deployment_teardown,
)
from app.schemas.proxy import CancelWorkloadRequest, DeleteDeploymentRequest
from app.services import controller_deployments, workload_cancellation
from app.services.provisioning import ProvisioningRefused
from tests.test_batch_deployment_postgres import (
    batch_workload as batch_workload,
    batch_runtime as batch_runtime,
    create as create_batch,
)
from tests.test_controller_deployment_postgres import (
    api_create,
    workload as workload,
    worker_runtime as worker_runtime,
)
from tests.test_lifecycle_api_postgres import lifecycle as lifecycle
from tests.test_operation_budget_ledger_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    postgres_available,
)

pytestmark = [] if os.environ.get("CI") else postgres_available


async def cancel_source(c, created, ledger, monkeypatch):
    c.composition.ledger = ledger[0]
    monkeypatch.setattr(workload_cancellation, "async_session_factory", c.sessions)
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            result = await cancel_deployment(
                c.workload_id,
                created.deployment_id,
                CancelWorkloadRequest(operation_id=created.operation_id),
                c.api_request,
                c.org_id,
                db,
            )
    assert result["cancellation_requested"]


def recovery_principal(c):
    return ResolvedPrincipal(
        str(c.org_id),
        str(c.workload_id),
        "trusted-recovery-run",
        frozenset({"workspace:recover"}),
    )


async def expire(c, operation_id):
    async with c.connections.connect() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second',"
            "runtime_deadline=clock_timestamp()-interval '1 second' WHERE operation_id=$1",
            operation_id,
        )


async def takeover(c, operation_id):
    async with c.connections.connect() as connection:
        return await fence_expired_lease(
            connection,
            operation_id=operation_id,
            recovery_principal=recovery_principal(c),
        )


async def review(c, created, body=None):
    body = body or DeleteDeploymentRequest(operation_id=uuid.uuid4())
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            document = await preview_deployment_teardown(
                c.workload_id, created.deployment_id, body, c.api_request, c.org_id, db
            )
    return body, document


async def approve_cleanup(c, created, body=None):
    body, document = await review(c, created, body)
    approval_id = await c.approve(document)
    return body.model_copy(
        update={"approval_id": approval_id, "plan_revision": document["revision"]}
    )


async def submit(c, created, body):
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            return await delete_deployment(
                c.workload_id, created.deployment_id, body, c.api_request, c.org_id, db
            )


@pytest.fixture
async def interrupted(workload, worker_runtime, ledger, monkeypatch):
    """Real capacity creation followed by a crash at the admitted deploy intent."""
    c = workload
    created = await api_create(c)
    worker = await worker_runtime.publish(created)
    for step_id in ("1", "2"):
        await worker.server.dispatch(
            {
                "token": worker.token,
                "method": "execute_step",
                "arguments": {"step_id": step_id},
            }
        )
    original_provider = worker.server._provider_call

    async def crash_before_workload(call):
        assert call.operation_kind == "deploy"
        await expire(c, created.operation_id)
        raise OperationRefused("simulated executor loss before workload dispatch")

    worker.server._provider_call = crash_before_workload
    with pytest.raises((OperationRefused, ProviderCallRefused)):
        await worker.server.dispatch(
            {
                "token": worker.token,
                "method": "execute_step",
                "arguments": {"step_id": "3"},
            }
        )
    worker.server._provider_call = original_provider
    async with c.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT stage FROM harness_provider_call_intent WHERE operation_id=$1 AND operation_kind='deploy'",
                created.operation_id,
            )
            == "intended"
        )
    await cancel_source(c, created, ledger, monkeypatch)
    claim = await takeover(c, created.operation_id)
    assert claim is not None and claim.cancel_requested
    return SimpleNamespace(
        c=c, created=created, worker=worker, runtime=worker_runtime, claim=claim
    )


async def test_cancelled_source_cleanup_has_own_grant_and_retains_unknown_creation(
    interrupted,
):
    f = interrupted
    body = await approve_cleanup(f.c, f.created)
    stopped = await submit(f.c, f.created, body)
    cleanup = await f.runtime.publish(stopped)
    assert [s["operation_kind"] for s in cleanup.plan.steps] == ["delete_cluster"]
    assert (
        cleanup.operation.max_resource_units == cleanup.operation.max_cost_micros == 0
    )
    assert cleanup.operation.grant.lease.operation_id != f.created.operation_id
    assert (
        cleanup.operation.grant.lease.runtime_deadline > f.claim.lease.runtime_deadline
    )
    # Source observation expiry and a legitimate later observation claim do not
    # revoke this independent approved cleanup operation.
    await expire(f.c, f.created.operation_id)
    await cleanup.registry.publish(stopped.operation_id)
    next_claim = await takeover(f.c, f.created.operation_id)
    assert next_claim.lease.fence_token > f.claim.lease.fence_token
    await cleanup.registry.publish(stopped.operation_id)
    with pytest.raises((OperationRefused, ProviderCallRefused)):
        await f.runtime.execute(cleanup)
    assert f.runtime.cloud.launches == 1
    assert not f.runtime.cloud.exists
    async with f.c.connections.connect() as connection:
        source = await connection.fetchrow(
            "SELECT state,cancel_requested_at FROM harness_operations WHERE operation_id=$1",
            f.created.operation_id,
        )
        assert source["cancel_requested_at"] is not None and source["state"] not in {
            "succeeded",
            "failed",
            "cancelled",
        }
        assert (
            await connection.fetchval(
                "SELECT stage FROM harness_provider_call_intent WHERE operation_id=$1 AND operation_kind='deploy'",
                f.created.operation_id,
            )
            == "intended"
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations WHERE state='released'"
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_cleanup_bindings"
            )
            == 1
        )
        with pytest.raises(LeaseRefused):
            await acquire(
                connection,
                operation_id=f.created.operation_id,
                holder="late-original-worker",
                attempt_id="late-attempt",
            )


async def test_cleanup_review_requires_cancellation_and_actual_dispatch_quiescence(
    workload, worker_runtime, ledger, monkeypatch
):
    c = workload
    created = await api_create(c)
    await worker_runtime.publish(created)
    with pytest.raises(OperationRefused, match="cancellation"):
        await review(c, created)
    await cancel_source(c, created, ledger, monkeypatch)
    assert await takeover(c, created.operation_id) is None  # Original lease live.
    with pytest.raises(OperationRefused, match="takeover"):
        await review(c, created)
    await expire(c, created.operation_id)
    async with c.connections.connect() as dispatch:
        key = "harness-provider-dispatch:" + created.operation_id
        await dispatch.execute("SELECT pg_advisory_lock(hashtextextended($1,0))", key)
        try:
            assert await takeover(c, created.operation_id) is None
            with pytest.raises(OperationRefused, match="takeover"):
                await review(c, created)
        finally:
            await dispatch.execute(
                "SELECT pg_advisory_unlock(hashtextextended($1,0))", key
            )
    assert await takeover(c, created.operation_id) is not None
    await review(c, created)


async def test_cleanup_binding_survives_failed_ledger_and_rejects_competing_approval(
    interrupted, monkeypatch
):
    f = interrupted
    first = await approve_cleanup(f.c, f.created)
    second = await approve_cleanup(f.c, f.created)
    facade = controller_deployments.get_operation_facade()
    original = facade.open_operation

    async def unavailable(**kwargs):
        raise ConnectionError("simulated ledger boundary outage")

    monkeypatch.setattr(facade, "open_operation", unavailable)
    with pytest.raises(ConnectionError):
        await submit(f.c, f.created, first)
    async with f.c.connections.connect() as connection:
        assert await connection.fetchval(
            "SELECT request_id FROM controller_cleanup_bindings"
        ) == str(first.operation_id)
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
    monkeypatch.setattr(facade, "open_operation", original)
    with pytest.raises(
        (OperationRefused, ProvisioningRefused), match="different original request"
    ):
        await submit(f.c, f.created, second)
    a, b = await asyncio.gather(
        submit(f.c, f.created, first), submit(f.c, f.created, first)
    )
    assert a.operation_id == b.operation_id
    async with f.c.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_operations WHERE action='teardown'"
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations"
            )
            == 2
        )


async def test_cleanup_source_change_across_admission_cannot_register_or_dispatch(
    interrupted, monkeypatch
):
    f = interrupted
    body = await approve_cleanup(f.c, f.created)
    facade = controller_deployments.get_operation_facade()
    original = facade.open_operation

    async def tampered(**kwargs):
        result = await original(**kwargs)
        async with f.c.connections.connect() as connection:
            await connection.execute(
                "UPDATE harness_operations SET cancel_requested_at=NULL WHERE operation_id=$1",
                f.created.operation_id,
            )
        return result

    monkeypatch.setattr(facade, "open_operation", tampered)
    with pytest.raises(ProvisioningRefused, match="cancellation"):
        await submit(f.c, f.created, body)
    async with f.c.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_operations WHERE action='teardown'"
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_deployment_operations WHERE action='teardown'"
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_cleanup_bindings"
            )
            == 1
        )
    assert f.runtime.cloud.exists and f.runtime.cloud.launches == 1


async def test_cleanup_missing_approval_and_foreign_workspace_never_buy_removal(
    interrupted,
):
    f = interrupted
    body, _ = await review(f.c, f.created)
    with pytest.raises(ProvisioningRefused, match="approval"):
        await submit(f.c, f.created, body)
    body = await approve_cleanup(f.c, f.created)
    with f.c.actor(workspace_id=f.c.workload_id):
        async with f.c.sessions() as db:
            with pytest.raises(HTTPException):
                await preview_deployment_teardown(
                    uuid.uuid4(),
                    f.created.deployment_id,
                    body,
                    f.c.api_request,
                    f.c.org_id,
                    db,
                )
    async with f.c.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_cleanup_bindings"
            )
            == 0
        )


async def test_distinct_approved_cleanup_race_admits_one_owner(interrupted):
    f = interrupted
    first = await approve_cleanup(f.c, f.created)
    second = await approve_cleanup(f.c, f.created)
    results = await asyncio.gather(
        submit(f.c, f.created, first),
        submit(f.c, f.created, second),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    refused = next(result for result in results if isinstance(result, Exception))
    assert isinstance(refused, (OperationRefused, ProvisioningRefused))
    async with f.c.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_operations WHERE action='teardown'"
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_cleanup_bindings"
            )
            == 1
        )


async def test_source_execution_resumption_invalidates_cleanup_registration(
    interrupted,
):
    f = interrupted
    body = await approve_cleanup(f.c, f.created)
    stopped = await submit(f.c, f.created, body)
    cleanup = await f.runtime.publish(stopped)
    async with f.c.connections.connect() as connection:
        await connection.execute(
            "UPDATE harness_operation_leases SET holder='untrusted-executor',attempt_id='new-execution' WHERE operation_id=$1",
            f.created.operation_id,
        )
    with pytest.raises(OperationRefused):
        await cleanup.registry.publish(stopped.operation_id)
    with pytest.raises((OperationRefused, ProviderCallRefused)):
        await f.runtime.execute(cleanup)
    assert f.runtime.cloud.exists and f.runtime.cloud.launches == 1


async def test_cleanup_schema_preserves_owner_on_duplicate_and_rollback(interrupted):
    import ast
    from pathlib import Path
    import asyncpg

    f = interrupted
    body = await approve_cleanup(f.c, f.created)
    await submit(f.c, f.created, body)
    async with f.c.connections.connect() as connection:
        row = dict(
            await connection.fetchrow("SELECT * FROM controller_cleanup_bindings")
        )
        duplicate = {
            **row,
            "source_operation_id": "different-source",
            "request_id": str(uuid.uuid4()),
        }
        with pytest.raises(asyncpg.UniqueViolationError):
            async with connection.transaction():
                await connection.execute(
                    "INSERT INTO controller_cleanup_bindings ("
                    + ",".join(duplicate)
                    + ") VALUES ("
                    + ",".join("$" + str(i) for i in range(1, len(duplicate) + 1))
                    + ")",
                    *duplicate.values(),
                )
        migration = (
            Path(__file__).resolve().parents[1]
            / "alembic/versions/040_controller_cleanup_bindings.py"
        )
        downgrade = next(
            n
            for n in ast.parse(migration.read_text()).body
            if isinstance(n, ast.FunctionDef) and n.name == "downgrade"
        )
        with pytest.raises(
            asyncpg.RaiseError, match="ownership evidence must be preserved"
        ):
            async with connection.transaction():
                for statement in downgrade.body[:2]:
                    await connection.execute(ast.literal_eval(statement.value.args[0]))
        assert (
            dict(await connection.fetchrow("SELECT * FROM controller_cleanup_bindings"))
            == row
        )


async def test_paid_cleanup_recovers_lost_domain_registration_without_second_admission(
    interrupted, monkeypatch
):
    f = interrupted
    body = await approve_cleanup(f.c, f.created)
    original = controller_deployments.register_deployment_operation

    async def lost_commit(*args, **kwargs):
        await original(*args, **kwargs)
        raise OperationRefused("simulated lost cleanup registration commit")

    monkeypatch.setattr(
        controller_deployments, "register_deployment_operation", lost_commit
    )
    with pytest.raises(ProvisioningRefused, match="lost cleanup registration"):
        await submit(f.c, f.created, body)
    async with f.c.connections.connect() as connection:
        paid_id = await connection.fetchval(
            "SELECT operation_id FROM harness_operations WHERE action='teardown'"
        )
        assert paid_id
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_deployment_operations WHERE action='teardown'"
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_cleanup_bindings"
            )
            == 1
        )
    monkeypatch.setattr(
        controller_deployments, "register_deployment_operation", original
    )
    recovered = await submit(f.c, f.created, body)
    assert recovered.operation_id == paid_id
    async with f.c.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations"
            )
            == 2
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_deployment_operations WHERE action='teardown'"
            )
            == 1
        )


async def test_batch_cancelled_source_passes_preview_admission_and_paid_assignment(
    batch_workload, batch_runtime, ledger, monkeypatch
):
    from app.routers.proxy import cancel_batch, delete_batch, preview_batch_teardown

    c = batch_workload
    created = await create_batch(c)
    source = await batch_runtime.publish(SimpleNamespace(**created))
    original = source.server._provider_call

    async def interrupted_before_launch(call):
        assert call.operation_kind == "launch"
        await expire(c, created["operation_id"])
        raise OperationRefused("simulated crash before original launch")

    source.server._provider_call = interrupted_before_launch
    with pytest.raises((OperationRefused, ProviderCallRefused)):
        await source.server.dispatch(
            {
                "token": source.token,
                "method": "execute_step",
                "arguments": {"step_id": "1"},
            }
        )
    source.server._provider_call = original
    c.composition.ledger = ledger[0]
    monkeypatch.setattr(workload_cancellation, "async_session_factory", c.sessions)
    job_id = uuid.UUID(str(created["job_id"]))
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            cancelled = await cancel_batch(
                c.workload_id,
                job_id,
                CancelWorkloadRequest(operation_id=created["operation_id"]),
                c.api_request,
                c.org_id,
                db,
            )
    assert cancelled["cancellation_requested"]
    assert await takeover(c, created["operation_id"]) is not None
    body = DeleteDeploymentRequest(operation_id=uuid.uuid4())
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            document = await preview_batch_teardown(
                c.workload_id, job_id, body, c.api_request, c.org_id, db
            )
    approval = await c.approve(document)
    body = body.model_copy(
        update={"approval_id": approval, "plan_revision": document["revision"]}
    )
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            stopped = await delete_batch(
                c.workload_id, job_id, body, c.api_request, c.org_id, db
            )
    cleanup = await batch_runtime.publish(SimpleNamespace(**stopped))
    assert cleanup.plan.data["workload"]["kind"] == "batch"
    assert [step["operation_kind"] for step in cleanup.plan.steps] == ["delete_cluster"]
    # No compute ownership was ever established, so this test stops at the
    # assignment gate and does not invent a capacity row to authorize deletion.
    assert batch_runtime.cloud.launches == 0
    async with c.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT stage FROM harness_provider_call_intent WHERE operation_id=$1",
                created["operation_id"],
            )
            == "intended"
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations WHERE state='released'"
            )
            == 0
        )
