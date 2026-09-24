"""Real database replay/fencing; Kubernetes and credential delivery are doubled."""

import asyncio
import json
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from kubernetes.client.rest import ApiException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.database import Base
from app.models.cluster import Cluster
from app.models.deployment import Deployment
from app.models.event import Event
from app.models.organization import Organization
from app.models.reconcile_lock import ReconcileLock
from app.models.workspace import Workspace
from app.routers import proxy
from app.schemas.proxy import CreateDeploymentRequest
from tests.test_installation_postgres import (
    installation_postgres_url as installation_postgres_url,
    isolated_database as isolated_database,
    pytestmark as postgres_required,
)

pytestmark = postgres_required


class KubernetesTransport:
    def __init__(self):
        self.object = None
        self.creates = 0
        self.deletes = 0
        self.lose_create_reply = False
        self.lose_delete_reply = False
        self.delete_body = None

    def read_namespaced_deployment(self, **kwargs):
        if self.object is None:
            raise ApiException(status=404)
        return deepcopy(self.object)

    def create_namespaced_deployment(self, *, namespace, body):
        if self.object is not None:
            raise ApiException(status=409)
        self.creates += 1
        self.object = deepcopy(body)
        self.object["metadata"].update(uid="provider-uid-1", generation=1)
        if self.lose_create_reply:
            self.lose_create_reply = False
            raise ApiException(status=504)
        return deepcopy(self.object)

    def delete_namespaced_deployment(self, *, name, namespace, body=None):
        self.delete_body = body
        if self.object is None:
            raise ApiException(status=404)
        if body and body["preconditions"]["uid"] != self.object["metadata"]["uid"]:
            raise ApiException(status=409)
        self.deletes += 1
        self.object = None
        if self.lose_delete_reply:
            self.lose_delete_reply = False
            raise ApiException(status=504)


@pytest.fixture
async def runtime(isolated_database, monkeypatch):
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
            )
        )
        await db.commit()
    cloud = KubernetesTransport()
    state = SimpleNamespace(
        factory=factory,
        org_id=org_id,
        workspace_id=workspace_id,
        cluster_id=cluster_id,
        cloud=cloud,
        client_calls=0,
        entered=None,
        resume=None,
        body=CreateDeploymentRequest(name="model", model_name="example/model"),
    )

    async def clients(workspace_id, org_id, db):
        state.client_calls += 1
        # A second connection must already see the immutable target before any
        # credential is obtained or provider mutation can begin.
        async with factory() as observer:
            record = await observer.scalar(select(Deployment))
            assert record.operation_target_json
        if state.entered is not None:
            state.entered.set()
            await asyncio.wait_for(state.resume.wait(), 5)
        workspace, cluster = await proxy.get_workspace_cluster(workspace_id, org_id, db)
        return None, cloud, workspace, cluster

    monkeypatch.setattr(proxy, "get_k8s_clients", clients)
    yield state


async def create(runtime, body=None):
    async with runtime.factory() as db:
        return await proxy.create_deployment(
            runtime.workspace_id, body or runtime.body, runtime.org_id, db
        )


async def delete(runtime, deployment_id):
    async with runtime.factory() as db:
        return await proxy.delete_deployment(
            runtime.workspace_id, str(deployment_id), "default", runtime.org_id, db
        )


async def test_lost_create_reply_survives_new_session_without_repeating_create(runtime):
    runtime.cloud.lose_create_reply = True
    with pytest.raises(HTTPException) as lost:
        await create(runtime)
    assert lost.value.status_code == 502
    recovered = await create(runtime)
    assert recovered.status == "Created"
    assert runtime.cloud.creates == 1
    async with runtime.factory() as db:
        record = await db.scalar(select(Deployment))
        assert record.provider_uid == "provider-uid-1"
        assert json.loads(record.operation_target_json)["cluster_id"] == str(
            runtime.cluster_id
        )


async def test_replay_after_api_release_keeps_original_manifest(runtime, monkeypatch):
    runtime.cloud.lose_create_reply = True
    with pytest.raises(HTTPException):
        await create(runtime)
    original = deepcopy(runtime.cloud.object)
    generate = proxy.create_deployment_manifest

    def newer_manifest(*args, **kwargs):
        manifest = generate(*args, **kwargs)
        manifest["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "registry.example/new-release:2"
        )
        return manifest

    monkeypatch.setattr(proxy, "create_deployment_manifest", newer_manifest)
    assert (await create(runtime)).status == "Created"
    assert runtime.cloud.object == original
    assert runtime.cloud.creates == 1


@pytest.mark.parametrize(
    "change",
    [
        "cluster",
        "endpoint",
        "arn",
        "role-name",
        "legacy",
        "foreign-org",
        "foreign-workspace",
    ],
)
async def test_replay_refuses_changed_or_missing_target_before_credentials(
    runtime, change
):
    runtime.cloud.lose_create_reply = True
    with pytest.raises(HTTPException):
        await create(runtime)
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        cluster = await db.get(Cluster, runtime.cluster_id)
        if change == "cluster":
            replacement = Cluster(
                id=uuid.uuid4(),
                org_id=runtime.org_id,
                workspace_id=workspace.id,
                name="replacement",
                endpoint=cluster.endpoint,
                eks_cluster_arn=cluster.eks_cluster_arn,
            )
            db.add(replacement)
            await db.flush()
            workspace.cluster_id = replacement.id
        elif change == "endpoint":
            cluster.endpoint = "https://replacement.example.invalid"
        elif change == "arn":
            cluster.eks_cluster_arn += "-replacement"
        elif change == "role-name":
            workspace.name = "replacement"
        elif change == "legacy":
            (await db.scalar(select(Deployment))).operation_target_json = None
        elif change == "foreign-org":
            other_org = Organization(id=uuid.uuid4(), name="foreign")
            db.add(other_org)
            await db.flush()
            cluster.org_id = other_org.id
        else:
            cluster.workspace_id = uuid.uuid4()
        await db.commit()
    before = runtime.client_calls
    with pytest.raises(HTTPException) as refused:
        await create(runtime)
    assert refused.value.status_code in {400, 409}
    assert runtime.client_calls == before
    assert runtime.cloud.creates == 1


async def test_concurrent_retries_share_committed_intent_and_one_provider_create(
    runtime,
):
    runtime.entered, runtime.resume = asyncio.Event(), asyncio.Event()
    first = asyncio.create_task(create(runtime))
    await asyncio.wait_for(runtime.entered.wait(), 5)
    second = asyncio.create_task(create(runtime))
    await asyncio.sleep(0.05)
    assert runtime.client_calls == 1
    runtime.resume.set()
    left, right = await asyncio.wait_for(asyncio.gather(first, second), 10)
    assert left.deployment_id == right.deployment_id
    assert runtime.cloud.creates == 1
    assert runtime.client_calls == 1


async def test_delete_waits_for_create_and_replay_preserves_tombstone(runtime):
    runtime.entered, runtime.resume = asyncio.Event(), asyncio.Event()
    creating = asyncio.create_task(create(runtime))
    await asyncio.wait_for(runtime.entered.wait(), 5)
    async with runtime.factory() as db:
        record = await db.scalar(select(Deployment))
        deployment_id = record.id
    deleting = asyncio.create_task(delete(runtime, deployment_id))
    await asyncio.sleep(0.05)
    assert runtime.cloud.deletes == 0
    runtime.resume.set()
    await asyncio.wait_for(asyncio.gather(creating, deleting), 10)
    replay = await create(runtime)
    assert replay.status == "Deleted"
    assert runtime.cloud.creates == runtime.cloud.deletes == 1
    assert runtime.cloud.delete_body == {"preconditions": {"uid": "provider-uid-1"}}


async def test_lost_delete_reply_blocks_create_before_delete_recovery(runtime):
    result = await create(runtime)
    runtime.cloud.lose_delete_reply = True
    with pytest.raises(HTTPException):
        await delete(runtime, result.deployment_id)
    assert (await create(runtime)).status == "Deleting"
    assert runtime.cloud.creates == 1
    assert (await delete(runtime, result.deployment_id)).status == "Deleted"
    assert (await create(runtime)).status == "Deleted"


async def test_observed_uid_replacement_is_not_recovered_as_original(runtime):
    await create(runtime)
    async with runtime.factory() as db:
        record = await db.scalar(select(Deployment))
        record.status = "Unknown"
        await db.commit()
    runtime.cloud.object["metadata"]["uid"] = "replacement-uid"
    with pytest.raises(HTTPException) as refused:
        await create(runtime)
    assert refused.value.status_code == 409
    assert runtime.cloud.creates == 1


async def test_unknown_create_cannot_delete_another_operations_object(runtime):
    runtime.cloud.lose_create_reply = True
    with pytest.raises(HTTPException):
        await create(runtime)
    async with runtime.factory() as db:
        record = await db.scalar(select(Deployment))
        deployment_id = record.id
        assert record.provider_uid is None
    runtime.cloud.object["metadata"]["annotations"] = {}
    with pytest.raises(HTTPException) as refused:
        await delete(runtime, deployment_id)
    assert refused.value.status_code == 409
    assert runtime.cloud.deletes == 0


async def test_delete_pins_unknown_create_uid_before_lost_reply_and_replacement(
    runtime,
):
    runtime.cloud.lose_create_reply = True
    with pytest.raises(HTTPException):
        await create(runtime)
    original = deepcopy(runtime.cloud.object)
    async with runtime.factory() as db:
        record = await db.scalar(select(Deployment))
        deployment_id = record.id
        assert record.provider_uid is None
    runtime.cloud.lose_delete_reply = True
    with pytest.raises(HTTPException):
        await delete(runtime, deployment_id)
    async with runtime.factory() as db:
        record = await db.get(Deployment, deployment_id)
        assert record.provider_uid == "provider-uid-1"
        assert record.status == "Deleting"
    runtime.cloud.object = original
    runtime.cloud.object["metadata"]["uid"] = "replacement-uid"
    with pytest.raises(HTTPException):
        await delete(runtime, deployment_id)
    assert runtime.cloud.deletes == 1
    assert runtime.cloud.object["metadata"]["uid"] == "replacement-uid"


async def test_missing_previously_observed_object_is_never_recreated(runtime):
    await create(runtime)
    runtime.cloud.object = None
    async with runtime.factory() as db:
        record = await db.scalar(select(Deployment))
        record.status = "Unknown"
        await db.commit()
    with pytest.raises(HTTPException) as refused:
        await create(runtime)
    assert refused.value.status_code == 409
    assert runtime.cloud.creates == 1


async def test_lost_teardown_admission_keeps_workspace_unavailable_and_same_request(
    runtime, monkeypatch
):
    from app.routers import workspaces
    from app.services import provisioning

    class FacadeTransport:
        def __init__(self):
            self.requests = []
            self.pending = True

        async def open_operation(self, **request):
            self.requests.append(request)
            if len(self.requests) == 1:
                raise provisioning.ProvisioningUnavailable("reply lost after admission")
            return provisioning.OperationProgress(
                operation_id="actual-admitted-teardown", state="running"
            )

        async def report_progress(self, operation_id):
            assert operation_id == "actual-admitted-teardown"
            return provisioning.OperationProgress(
                operation_id=operation_id, state="succeeded"
            )

    facade = FacadeTransport()
    monkeypatch.setattr(provisioning, "_facade", facade)
    async with runtime.factory() as db:
        with pytest.raises(HTTPException) as lost:
            await workspaces.delete_workspace(runtime.workspace_id, runtime.org_id, db)
        assert lost.value.status_code == 503
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        assert workspace.status == "Teardown"
        assert workspace.teardown_operation_id is None
    async with runtime.factory() as db:
        retry = await workspaces.delete_workspace(
            runtime.workspace_id, runtime.org_id, db
        )
        assert retry.status == "Teardown"
    assert facade.requests[0] == facade.requests[1]
    async with runtime.factory() as db:
        result = await workspaces.get_workspace(
            runtime.workspace_id, runtime.org_id, db
        )
        assert result.status == "Deleted"


@pytest.mark.parametrize("teardown_state", ["running", "succeeded"])
async def test_create_retry_refreshes_cached_workspace_after_concurrent_teardown(
    runtime, monkeypatch, teardown_state
):
    from app.routers import workspaces
    from app.schemas.workspace import CreateWorkspaceRequest
    from app.services import provisioning

    body = CreateWorkspaceRequest(name="workspace")
    async with runtime.factory() as db:
        workspace = await db.get(Workspace, runtime.workspace_id)
        workspace.operation_id = body.operation_id
        workspace.operation_request_json = workspaces._operation_request(body)
        workspace.provisioning_operation_id = "admitted-create"
        workspace.status = "Provisioning"
        await db.commit()

    class FacadeTransport:
        def __init__(self):
            self.calls = []

        async def open_operation(self, **request):
            self.calls.append(request["action"])
            assert request["action"] == "teardown"
            return provisioning.OperationProgress(
                operation_id="admitted-teardown", state=teardown_state
            )

        async def report_progress(self, operation_id):
            self.calls.append(operation_id)
            return provisioning.OperationProgress(
                operation_id=operation_id, state="succeeded"
            )

    facade = FacadeTransport()
    monkeypatch.setattr(provisioning, "_facade", facade)
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
            deleted = await workspaces.delete_workspace(
                runtime.workspace_id, runtime.org_id, db
            )
        resume.set()
        response = await asyncio.wait_for(retry, 5)
    finally:
        resume.set()
        if not retry.done():
            retry.cancel()
        await asyncio.gather(retry, return_exceptions=True)
    expected_status = "Deleted" if teardown_state == "succeeded" else "Teardown"
    assert deleted.status == response.status == expected_status
    assert facade.calls == ["teardown"]
    async with runtime.factory() as db:
        assert (await db.get(Workspace, runtime.workspace_id)).status == expected_status


async def test_successful_workspace_provision_is_usable_by_proxy_and_kubeconfig(
    runtime, monkeypatch
):
    from app.routers import workspaces
    from app.schemas.workspace import CreateWorkspaceRequest
    from app.services import eks_auth, kubeconfig, provisioning
    from app.services import proxy as proxy_service

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
        assert result.status == "active"
    assert (await create(runtime)).status == "Created"

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
