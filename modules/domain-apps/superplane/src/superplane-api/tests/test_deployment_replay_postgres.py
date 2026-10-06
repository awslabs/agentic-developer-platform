"""Workspace lifecycle replay and canonical publication against PostgreSQL.

Governed deployment replay lives in test_controller_deployment_postgres; UID and
create-only transport behavior remains in test_deployment_recovery.
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import Base
from app.adapters.operation_authority_source import (
    ActingPrincipal,
    reset_acting_principal,
    set_acting_principal,
)
from app.models.cluster import Cluster
from app.models.event import Event
from app.models.organization import Organization
from app.models.reconcile_lock import ReconcileLock
from app.models.workspace import Workspace
from app.routers import proxy
from tests.test_installation_postgres import (
    installation_postgres_url as installation_postgres_url,
    isolated_database as isolated_database,
    pytestmark as postgres_required,
)

pytestmark = postgres_required


@pytest.fixture
async def runtime(isolated_database):  # noqa: F811
    _, engine, *_ = isolated_database
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    org_id, workspace_id, cluster_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    async with factory() as db:
        db.add(Organization(id=org_id, name=str(org_id), billing_plan="enterprise"))
        await db.commit()
        db.add(
            Cluster(
                id=cluster_id,
                org_id=org_id,
                workspace_id=workspace_id,
                name="cluster",
                endpoint="https://workspace.example.invalid",
                eks_cluster_arn="arn:aws:eks:us-east-1:123456789012:cluster/workspace",
            )
        )
        await db.commit()
        db.add(
            Workspace(
                id=workspace_id,
                org_id=org_id,
                cluster_id=cluster_id,
                name="workspace",
                status="Active",
                isolation_mode="dedicated",
                namespace_name="ws-replay",
            )
        )
        await db.commit()
    state = SimpleNamespace(
        factory=factory,
        org_id=org_id,
        workspace_id=workspace_id,
        cluster_id=cluster_id,
    )

    token = set_acting_principal(
        ActingPrincipal("test-user", str(org_id), str(workspace_id))
    )
    try:
        yield state
    finally:
        reset_acting_principal(token)


async def test_unavailable_teardown_phase_refuses_without_changing_workspace(
    runtime, monkeypatch
):
    from app.routers import workspaces
    from app.services import provisioning

    facade = SimpleNamespace(open_operation=AsyncMock())
    monkeypatch.setattr(provisioning, "_facade", facade)
    for _ in range(2):
        async with runtime.factory() as db:
            with pytest.raises(HTTPException) as refusal:
                await workspaces.delete_workspace(
                    runtime.workspace_id, runtime.org_id, db
                )
            assert refusal.value.status_code == 503
    facade.open_operation.assert_not_called()
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        assert workspace.status == "Active"
        assert workspace.teardown_operation_id is None


@pytest.mark.parametrize("teardown_state", ["running", "succeeded"])
async def test_create_retry_refreshes_cached_workspace_after_concurrent_teardown(
    runtime, monkeypatch, teardown_state
):
    from app.routers import workspaces
    from app.schemas.workspace import CreateWorkspaceRequest

    body = CreateWorkspaceRequest(name="workspace")
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        workspace.operation_id = body.operation_id
        workspace.operation_request_json = workspaces._operation_request(body)
        workspace.provisioning_operation_id = "admitted-create"
        workspace.status = "Provisioning"
        await db.commit()

    monkeypatch.setattr(
        "app.operation_activation.require_installed_lifecycle_binding", AsyncMock()
    )
    cached, resume = asyncio.Event(), asyncio.Event()
    lookup = workspaces._workspace_for_operation

    async def pause_after_initial_read(*args, **kwargs):
        workspace = await lookup(*args, **kwargs)
        if not kwargs.get("for_update"):
            assert workspace.status == "Provisioning"
            cached.set()
            await asyncio.wait_for(resume.wait(), 5)
        return workspace

    monkeypatch.setattr(
        workspaces, "_workspace_for_operation", pause_after_initial_read
    )

    async def replay():
        async with runtime.factory() as db:
            return await workspaces.create_workspace(body, runtime.org_id, db)

    retry = asyncio.create_task(replay())
    try:
        await asyncio.wait_for(cached.wait(), 5)
        async with runtime.factory() as db:
            workspace = await db.get(Workspace, runtime.workspace_id)
            workspace.status = (
                "Deleted" if teardown_state == "succeeded" else "Teardown"
            )
            await db.commit()
        resume.set()
        response = await asyncio.wait_for(retry, 5)
    finally:
        resume.set()
        if not retry.done():
            retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)
    expected_status = "Deleted" if teardown_state == "succeeded" else "Teardown"
    assert response.status == expected_status
    async with runtime.factory() as db:
        assert (await db.get(Workspace, runtime.workspace_id)).status == expected_status


async def test_completed_phase_waits_for_bootstrap_registration_before_proxy_and_kubeconfig(
    runtime, monkeypatch
):
    from app.routers import workspaces
    from app.schemas.workspace import CreateWorkspaceRequest
    from app.services import eks_auth, kubeconfig, provisioning
    from app.services import proxy as proxy_service

    monkeypatch.setattr(
        "app.operation_activation.require_installed_lifecycle_binding", AsyncMock()
    )
    body = CreateWorkspaceRequest(name="workspace")
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        workspace.operation_id = body.operation_id
        workspace.operation_request_json = workspaces._operation_request(body)
        workspace.provisioning_operation_id = "admitted-create"
        workspace.status = "Provisioning"
        await db.commit()

    class FacadeTransport:
        async def report_progress(self, operation_id):
            return provisioning.OperationProgress(
                operation_id=operation_id, state="succeeded"
            )

    monkeypatch.setattr(provisioning, "_facade", FacadeTransport())
    async with runtime.factory() as db:
        result = await workspaces.create_workspace(body, runtime.org_id, db)
        assert result.status == "Provisioning"
        with pytest.raises(HTTPException) as unavailable:
            await workspaces.generate_kubeconfig(
                runtime.workspace_id, runtime.org_id, db
            )
        assert unavailable.value.status_code == 400
        with pytest.raises(proxy_service.ProxyError, match="not active"):
            await proxy.get_workspace_cluster(runtime.workspace_id, runtime.org_id, db)

    # Exercise the actual canonical publication used by successful bootstrap.
    # The phase's success above cannot produce this registration on its own.
    from superplane_bootstrap.canonical import publish

    class Store:
        def __init__(self, session):
            self.session = session

        def execute(self, sql, parameters):
            result = self.session.execute(text(sql), parameters)
            return result.mappings().all() if result.returns_rows else []

    async with runtime.factory() as db:
        org = await db.get(Organization, runtime.org_id)
        org.adp_org_id = "adp-org-for-bootstrap"
        await db.flush()
        identity = {
            "workspace_id": str(runtime.workspace_id),
            "org_id": str(runtime.org_id),
            "cluster_name": "workspace",
            "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/workspace",
            "endpoint": "https://workspace.example.invalid",
            "namespace": "ws-replay",
        }
        await db.run_sync(lambda session: publish(Store(session), identity))
        await db.commit()
    async with runtime.factory() as db:
        workspace, cluster = await proxy.get_workspace_cluster(
            runtime.workspace_id, runtime.org_id, db
        )
        assert workspace.status == "active"
        assert workspace.namespace_name == "ws-replay"
        assert cluster.id == runtime.cluster_id

    # Exercise the real API status gate without credential or provider access.
    calls = []

    def assumed(*args, **kwargs):
        calls.append((args, kwargs))
        return {}

    monkeypatch.setattr(proxy_service, "assume_role_for_cluster", assumed)
    monkeypatch.setattr(eks_auth, "describe_cluster_ca", lambda **kwargs: "test-ca")
    monkeypatch.setattr(
        kubeconfig,
        "generate_kubeconfig",
        lambda **kwargs: ("test-config", datetime.now(timezone.utc)),
    )
    async with runtime.factory() as db:
        response = await workspaces.generate_kubeconfig(
            runtime.workspace_id, runtime.org_id, db
        )
    assert response.kubeconfig == "test-config"
    assert calls == [
        (
            ("123456789012", "workspace"),
            {"session_suffix": "kubeconfig", "external_id": None},
        )
    ]


async def test_background_recovery_preserves_original_account_request_and_server_id(
    runtime, monkeypatch
):
    from app.services import provisioning
    from app.services.workspace_reconciler import WorkspaceReconciler

    captured = []

    class FacadeTransport:
        async def open_operation(self, **request):
            captured.append(request)
            return provisioning.OperationProgress(
                operation_id="actual-admitted-create", state="running"
            )

    monkeypatch.setattr(provisioning, "_facade", FacadeTransport())
    request_id = uuid.uuid4()
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        workspace.status = "Failed"
        workspace.operation_id = request_id
        workspace.operation_request_json = json.dumps(
            {
                "name": "original-name",
                "isolation_mode": "dedicated",
                "account": "123456789012",
            }
        )
        await db.commit()
        reconciler = WorkspaceReconciler(session_factory=runtime.factory)
        assert await reconciler._retry_bootstrap(db, workspace) == "retried"
    assert captured[0]["parameters"] == {
        "workspace_name": "original-name",
        "isolation_mode": "dedicated",
        "aws_account_id": "123456789012",
        "idempotency_key": str(request_id),
    }
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        assert workspace.provisioning_operation_id == "actual-admitted-create"
        event = (
            await db.execute(
                select(Event).where(
                    Event.org_id == runtime.org_id,
                    Event.resource_id == runtime.workspace_id,
                )
            )
        ).scalar_one()
        assert event.action == "updated"
        assert event.event_type == "WorkspaceBootstrapRetry"
        assert json.loads(event.details_json)["retry_count"] == 1
        assert (
            await db.get(
                ReconcileLock,
                ("workspace_reconciler", f"workspace-reconcile:{runtime.workspace_id}"),
            )
            is None
        )


async def test_background_recovery_releases_lock_after_failed_audit_flush(
    runtime, monkeypatch
):
    from app.services import provisioning
    from app.services.workspace_reconciler import WorkspaceReconciler

    class FacadeTransport:
        async def report_progress(self, operation_id):
            return provisioning.OperationProgress(
                operation_id=operation_id, state="running"
            )

    monkeypatch.setattr(provisioning, "_facade", FacadeTransport())
    reconciler = WorkspaceReconciler(session_factory=runtime.factory)

    async def invalid_audit(session, **event):
        # Force a real PostgreSQL flush error after the admitted operation and
        # reconciliation lock have committed, without masking it with a mock.
        session.add(
            Event(
                org_id=event["org_id"],
                action=None,
                resource_type="Workspace",
                resource_id=event["resource_id"],
                event_type=event["event_type"],
            )
        )
        await session.commit()

    monkeypatch.setattr(reconciler, "_emit_event", invalid_audit)
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        workspace.status = "Failed"
        workspace.provisioning_operation_id = "actual-admitted-create"
        await db.commit()
        with pytest.raises(IntegrityError):
            await reconciler._retry_bootstrap(db, workspace)
        assert db.is_active

    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        assert workspace.provisioning_operation_id == "actual-admitted-create"
        assert workspace.status == "reconciling"
        assert workspace.bootstrap_retry_count == 1
        assert (
            await db.get(
                ReconcileLock,
                ("workspace_reconciler", f"workspace-reconcile:{runtime.workspace_id}"),
            )
            is None
        )
        assert await db.scalar(select(Event)) is None
