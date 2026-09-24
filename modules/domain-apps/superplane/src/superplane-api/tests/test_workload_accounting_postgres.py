"""The real admission ledgers remain distinct from cost and resource evidence."""

import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.models.workspace import Workspace
from app.models.workspace_grant import WorkspaceGrantRecord
from app.services import workload_accounting
from app.services.provisioning import ProvisioningRefused
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
async def paid(batch_workload, monkeypatch):
    c = batch_workload
    monkeypatch.setattr(workload_accounting, "async_session_factory", c.sessions)
    return SimpleNamespace(c=c, created=await create(c))


async def read(context, **overrides):
    c = context.c
    with c.actor(workspace_id=c.workload_id):
        async with c.sessions() as db:
            return await workload_accounting.accounting(
                c.api_request,
                db,
                c.org_id,
                overrides.get("workspace_id", c.workload_id),
                overrides.get("deployment_id", context.created["job_id"]),
                kind="batch",
            )


async def test_admitted_envelope_is_a_reservation_not_observed_cost(paid):
    result = await read(paid)
    original = result["operations"][0]
    assert original["action"] == "provision"
    assert (
        original["approved_max_cost_micros"]
        == paid.c.preview.request.parameters["max_cost_micros"]
    )
    assert original["budget_held_micros"] == original["approved_max_cost_micros"]
    assert (
        result["observed_cost_micros"] is None
        and result["estimated_cost_micros"] is None
    )
    assert (
        result["cleanup_status"] == "unconfirmed" and result["recorded_resources"] == []
    )
    assert original["accounting_consistent"] is True
    async with paid.c.connections.connect() as conn:
        assert await conn.fetchval("SELECT count(*) FROM harness_operations") == 1
        assert (
            await conn.fetchval("SELECT count(*) FROM harness_provider_call_intent")
            == 0
        )


async def test_lost_acknowledgement_retains_ceiling_and_reports_both_ledgers(paid):
    async with paid.c.connections.connect() as conn:
        await conn.execute(
            "UPDATE harness_approval_consumption SET reservation_state='retained'"
        )
    original = (await read(paid))["operations"][0]
    assert original["budget_state"] == "confirmed"
    assert original["shared_reservation_state"] == "retained"
    assert original["accounting_consistent"] is False
    assert int(original["budget_held_micros"]) > 0


async def test_zero_workspace_cap_is_exhausted_not_unconfigured(paid):
    async with paid.c.sessions() as db:
        workspace = await db.get(Workspace, paid.c.workload_id)
        workspace.budget_max_daily_usd = 0
        await db.commit()
    result = await read(paid)
    assert result["workspace_reservation_cap_micros"] == "0"
    assert result["workspace_budget_state"] == "exhausted"
    assert int(result["workspace_committed_budget_micros"]) > 0


async def test_original_and_stop_budgets_remain_separate_after_owned_cleanup(
    paid, batch_runtime
):
    runtime = batch_runtime
    worker = await runtime.publish(SimpleNamespace(**paid.created))
    await runtime.execute(worker)
    stopped = await stop(paid.c, paid.created)
    cleanup = await runtime.publish(SimpleNamespace(**stopped))
    await runtime.execute(cleanup)
    result = await read(paid)
    assert [row["action"] for row in result["operations"]] == ["provision", "teardown"]
    assert int(result["operations"][0]["approved_max_cost_micros"]) > 0
    assert result["operations"][1]["approved_max_cost_micros"] == "0"
    assert result["cleanup_status"] == "confirmed"
    assert result["recorded_resources"] and result["observed_cost_micros"] is None
    assert runtime.cloud.launches == 1 and not runtime.kube.stored


async def test_read_only_grant_and_removed_profile_still_allow_original_accounting(
    paid,
):
    c = paid.c
    c.policy_path.unlink()
    async with c.sessions() as db:
        grant = await db.scalar(
            select(WorkspaceGrantRecord).where(
                WorkspaceGrantRecord.workspace_id == c.workload_id,
                WorkspaceGrantRecord.principal == "requester",
            )
        )
        grant.permissions = "workspace:read"
        await db.commit()
    assert (await read(paid))["operations"][0]["operation_id"] == paid.created[
        "operation_id"
    ]
    with pytest.raises(HTTPException) as error:
        await read(paid, workspace_id=uuid.uuid4())
    assert error.value.status_code == 403
    with pytest.raises(ProvisioningRefused):
        await read(paid, deployment_id=uuid.uuid4())


async def test_mid_read_revocation_refuses_accounting(paid, monkeypatch):
    c = paid.c
    actual = workload_accounting.GrantBackedAuthority.budget_limits_for

    async def revoke(authority, **kwargs):
        value = await actual(authority, **kwargs)
        async with c.sessions() as db:
            grant = await db.scalar(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.workspace_id == c.workload_id,
                    WorkspaceGrantRecord.principal == "requester",
                )
            )
            grant.revoked_at = datetime.now(UTC)
            await db.commit()
        return value

    monkeypatch.setattr(
        workload_accounting.GrantBackedAuthority, "budget_limits_for", revoke
    )
    with pytest.raises(HTTPException) as error:
        await read(paid)
    assert error.value.status_code == 403


async def test_missing_ledger_never_becomes_zero_budget(paid):
    async with paid.c.connections.connect() as conn:
        await conn.execute("DELETE FROM operation_budget_reservations")
    with pytest.raises(HTTPException) as error:
        await read(paid)
    assert error.value.status_code == 503
