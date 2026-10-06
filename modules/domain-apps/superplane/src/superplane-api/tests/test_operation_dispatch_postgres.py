"""The actual shared outbox waits for durable domain workspace registration."""

import json
import json
import os
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from harness_jobs.identity import ResolvedPrincipal
from harness_jobs.leases import acquire, fence_expired_lease, read_lease, release
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.operation_dispatch import OperationDispatcher
from app.config import settings
from app.database import Base
from app.models.cloud_account import CloudAccount
from app.models.cluster import Cluster
from app.models.deployment import Deployment
from app.models.controller_deployment import ControllerDeploymentOperation
from app.models.organization import Organization
from app.models.workspace import Workspace
from app.models.lifecycle import WorkspaceLifecycleControlOperation
from tests.test_operation_settlement_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    postgres_available,
    settlement as settlement,
)

pytestmark = [] if os.environ.get("CI") else postgres_available


class GatewayTransport:
    def __init__(self, binding, adp_org_id="adp-tenant"):
        self.calls = []
        self.alter = {}
        self.binding = binding
        self.adp_org_id = adp_org_id
        self.proofs = []

    async def post(self, route, payload):
        if route == "/binding-proof":
            self.proofs.append(payload)
            return {
                "version": 1,
                "installed": True,
                "checked_at": datetime.now(UTC).isoformat(),
                "domain": "superplane",
                "org_id": payload["org_id"],
                "adp_org_id": self.adp_org_id,
                **self.binding,
            }
        self.calls.append((route, payload))
        return {
            "version": 1,
            **payload,
            "domain_org_id": payload["org_id"],
            "adp_org_id": self.adp_org_id,
            "invocation_id": "real-invocation",
            "principal": "real-invocation#1",
            "status": "pending",
            "not_after": (datetime.now(UTC) + timedelta(minutes=2)).isoformat(),
            **self.alter,
        }


@pytest.fixture
async def dispatch(settlement, installation_postgres_url, monkeypatch, tmp_path):  # noqa: F811
    _, connections, _, identity, _ = settlement
    async with connections.connect() as connection:
        schema = await connection.fetchval("SELECT current_schema()")
        await connection.execute("UPDATE harness_operations SET state='pending'")
        # The shared fixture also creates a terminal settlement lease. Dispatcher
        # scenarios start from its real admission, before any execution lease.
        await connection.execute("DELETE FROM harness_operation_leases")
    engine = create_async_engine(
        installation_postgres_url,
        connect_args={"server_settings": {"search_path": schema}},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[
                Organization.__table__,
                CloudAccount.__table__,
                Cluster.__table__,
                Workspace.__table__,
                Deployment.__table__,
                ControllerDeploymentOperation.__table__,
                WorkspaceLifecycleControlOperation.__table__,
            ],
        )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with sessions() as session:
        session.add(
            Organization(
                id=uuid.UUID(identity["org_id"]), name="org", adp_org_id="adp-tenant"
            )
        )
        await session.commit()

    async def register(operation_id=None):
        async with sessions() as session:
            session.add(
                Workspace(
                    id=uuid.UUID(identity["workspace_id"]),
                    org_id=uuid.UUID(identity["org_id"]),
                    name="ws",
                    isolation_mode="dedicated",
                    status="Provisioning",
                    provisioning_operation_id=operation_id or identity["operation_id"],
                )
            )
            await session.commit()

    binding = {
        "producer_registry_id": "producer",
        "worker_registry_id": "worker",
        "worker_namespace": "domain-system",
        "worker_service_account": "paid-worker",
        "worker_role_arn": "arn:aws:iam::123456789012:role/paid-worker",
        "worker_image_digest": "sha256:" + "a" * 64,
        "operation_schema": "superplane",
        "queue_arn": "arn:aws:sqs:us-east-1:123456789012:paid-operations",
    }
    binding_file = tmp_path / "worker-binding.json"
    binding_file.write_text(json.dumps(binding))
    monkeypatch.setattr(settings, "superplane_paid_worker_mode", "native-lifecycle")
    monkeypatch.setattr(
        settings, "superplane_paid_worker_binding_file", str(binding_file)
    )
    monkeypatch.setattr(
        settings, "superplane_operation_gateway_url", "https://gateway.example"
    )
    monkeypatch.setattr(settings, "superplane_operation_dispatch_enabled", True)
    transport = GatewayTransport(binding)
    dispatcher = OperationDispatcher(
        connections.connect,
        transport,
        policy_for=lambda org: SimpleNamespace(adp_org_id="adp-tenant"),
    )
    try:
        yield dispatcher, transport, connections, identity, register
    finally:
        await engine.dispose()


async def test_admission_before_workspace_commit_is_not_delivered_or_exhausted(
    dispatch,
):
    dispatcher, transport, connections, identity, register = dispatch
    for _ in range(6):
        assert (await dispatcher.drain_once()).handled == 0
    assert transport.calls == []
    async with connections.connect() as connection:
        assert (
            await connection.fetchval("SELECT attempts FROM harness_dispatch_outbox")
            == 0
        )
    await register()
    assert (await dispatcher.drain_once()).delivered == 1
    assert transport.proofs == [{"domain": "superplane", "org_id": identity["org_id"]}]
    assert (await dispatcher.drain_once()).handled == 0
    assert transport.calls == [
        ("/dispatch", {"domain": "superplane", "mode": "execution", **identity})
    ]


async def test_workspace_for_another_operation_does_not_unlock_dispatch(dispatch):
    dispatcher, transport, _, _, register = dispatch
    await register("unrelated-operation")
    assert (await dispatcher.drain_once()).handled == 0
    assert transport.calls == []


async def test_mismatched_gateway_receipt_does_not_acknowledge_outbox(dispatch):
    dispatcher, transport, connections, _, register = dispatch
    await register()
    transport.alter = {"job_id": "wrong-job"}
    assert (await dispatcher.drain_once()).failed == 1
    async with connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT delivered_at FROM harness_dispatch_outbox"
            )
            is None
        )


async def execution_lease(dispatch, *, expired=True):
    _, _, connections, identity, _ = dispatch
    async with connections.connect() as connection:
        lease = await acquire(
            connection,
            operation_id=identity["operation_id"],
            holder="original-execution#1",
            attempt_id="execution-attempt",
        )
        if expired:
            await connection.execute(
                "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second'"
            )
        return lease


async def test_expired_recovery_dispatch_survives_restart_with_original_paid_ids(
    dispatch,
):
    dispatcher, transport, connections, identity, register = dispatch
    await register()
    await execution_lease(dispatch)
    async with connections.connect() as connection:
        before = await read_lease(connection, operation_id=identity["operation_id"])
    assert await dispatcher.recover_once() == (identity["operation_id"],)
    restarted = OperationDispatcher(
        connections.connect, transport, policy_for=dispatcher.policy_for
    )
    assert await restarted.recover_once() == (identity["operation_id"],)
    assert (
        transport.calls
        == [("/dispatch", {"domain": "superplane", "mode": "recovery", **identity})] * 2
    )
    async with connections.connect() as connection:
        assert (
            await read_lease(connection, operation_id=identity["operation_id"])
            == before
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations"
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_approval_consumption"
            )
            == 1
        )


@pytest.mark.parametrize(
    "ineligible", ["active", "unregistered", "wrong-adp", "closed"]
)
async def test_recovery_does_not_schedule_ineligible_or_foreign_leases(
    dispatch, ineligible
):
    dispatcher, transport, connections, _, register = dispatch
    if ineligible != "unregistered":
        await register()
    await execution_lease(dispatch, expired=ineligible != "active")
    async with connections.connect() as connection:
        if ineligible == "wrong-adp":
            await connection.execute(
                "UPDATE organizations SET adp_org_id='another-tenant'"
            )
        elif ineligible == "closed":
            await connection.execute(
                "UPDATE harness_operation_leases SET closed_at=now()"
            )
    assert await dispatcher.recover_once() == ()
    assert not transport.calls


async def test_cancelled_released_budget_still_gets_observation_recovery(dispatch):
    dispatcher, transport, connections, identity, register = dispatch
    await register()
    await execution_lease(dispatch)
    async with connections.connect() as connection:
        await connection.execute(
            "UPDATE harness_operations SET cancel_requested_at=now(),cleanup_required=true"
        )
        await connection.execute(
            "UPDATE operation_budget_reservations SET state='released', "
            "reason='fixture: earlier settlement does not replace recovery observations'"
        )
    assert await dispatcher.recover_once() == (identity["operation_id"],)
    assert transport.calls[0][1]["mode"] == "recovery"


async def test_recovery_released_claim_resumes_paid_execution_without_new_admission(
    dispatch,
):
    dispatcher, transport, connections, identity, register = dispatch
    await register()
    assert (await dispatcher.drain_once()).delivered == 1
    await execution_lease(dispatch)
    actor = ResolvedPrincipal(
        identity["org_id"],
        identity["workspace_id"],
        "actual-recovery#1",
        frozenset({"workspace:recover"}),
    )
    async with connections.connect() as connection:
        claim = await fence_expired_lease(
            connection, operation_id=identity["operation_id"], recovery_principal=actor
        )
        assert await release(connection, claim.lease)
    transport.calls.clear()
    assert await dispatcher.recover_once() == (identity["operation_id"],)
    assert transport.calls == [
        ("/dispatch", {"domain": "superplane", "mode": "execution", **identity})
    ]
    async with connections.connect() as connection:
        await connection.execute(
            "UPDATE operation_budget_reservations SET state='retained', "
            "reason='fixture: accounting hold blocks execution resumption'"
        )
    assert await dispatcher.recover_once() == ()


async def test_paid_dispatch_lost_before_lease_retries_and_cursor_wraps(dispatch):
    dispatcher, transport, _, identity, register = dispatch
    await register()
    assert (await dispatcher.drain_once()).delivered == 1
    transport.calls.clear()
    assert await dispatcher.recover_once(limit=1) == (identity["operation_id"],)
    assert await dispatcher.recover_once(limit=1) == ()
    assert await dispatcher.recover_once(limit=1) == (identity["operation_id"],)
    assert all(call[1]["mode"] == "execution" for call in transport.calls)


async def test_recovery_mismatched_receipt_preserves_retryable_durable_lease(dispatch):
    dispatcher, transport, _, identity, register = dispatch
    await register()
    await execution_lease(dispatch)
    transport.alter = {"attempt_id": "foreign-attempt"}
    assert await dispatcher.recover_once() == ()
    transport.alter = {}
    assert await dispatcher.recover_once() == (identity["operation_id"],)
