"""Real grants, approval, ledger, shared admission and immutable workload registry.

Runtime execution belongs to remote CI. No product/provider transports run here.
"""

import json
import os
import uuid
from types import SimpleNamespace

import pytest
from harness_jobs.identity import OperationRefused, encode_payload, payload_digest
from sqlalchemy import select
from superplane_executor.deployment_plan import compact
from superplane_executor.deployment_registry import registration_values

from app.models.cloud_account import CloudAccount
from app.models.cluster import Cluster
from app.models.controller_deployment import ControllerDeploymentOperation
from app.models.deployment import Deployment
from app.models.workspace import Workspace
from app.models.node_pool import NodePool
from app.models.node import Node
from app.models.workspace_grant import WorkspaceGrantRecord
from app.services import controller_deployments
from app.services.provisioning import ProvisioningRefused
from app.adapters.operation_dispatch import OperationDispatcher
from tests.test_operation_dispatch_postgres import GatewayTransport
from tests.test_controller_deployment_plan import profile_fixture
from tests.test_lifecycle_api_postgres import lifecycle as lifecycle
from tests.test_operation_budget_ledger_postgres import (
    installation_postgres_url as installation_postgres_url,
    ledger as ledger,
    postgres_available,
)

pytestmark = [] if os.environ.get("CI") else postgres_available


@pytest.fixture
async def workload(lifecycle, monkeypatch, tmp_path, request):  # noqa: F811
    context = lifecycle
    workspace_id, cluster_id, request_id = [uuid.uuid4() for _ in range(3)]
    profile = profile_fixture(cluster_id)
    if getattr(request, "param", False):
        del profile["instance_type"]
        profile.update(
            accelerators=["A10G:1", "L4:1"], max_gpus_per_node=1, cpus=4, memory_gb=32
        )
    engine = context.sessions.kw["bind"]
    async with engine.begin() as connection:
        await connection.run_sync(Deployment.__table__.create)
        await connection.run_sync(ControllerDeploymentOperation.__table__.create)
        from app.models.controller_execution import ControllerBatchResult

        await connection.run_sync(ControllerBatchResult.__table__.create)
        await connection.run_sync(NodePool.__table__.create)
        await connection.run_sync(Node.__table__.create)
    async with context.sessions() as db:
        account = await db.scalar(
            select(CloudAccount).where(CloudAccount.org_id == context.org_id)
        )
        db.add(
            Cluster(
                id=cluster_id,
                org_id=context.org_id,
                name="workspace",
                status="Ready",
                eks_cluster_arn=profile["cluster_arn"],
                endpoint=profile["endpoint"],
            )
        )
        await db.flush()
        db.add(
            Workspace(
                id=workspace_id,
                org_id=context.org_id,
                name="workspace",
                status="active",
                isolation_mode="dedicated",
                cluster_id=cluster_id,
                aws_account_id=account.id,
                namespace_name=profile["namespace"],
                provisioning_operation_id="original-bootstrap",
            )
        )
        await db.flush()
        db.add_all(
            [
                WorkspaceGrantRecord(
                    org_id=context.org_id,
                    workspace_id=workspace_id,
                    principal=subject,
                    principal_type="human",
                    permissions="workspace:administer",
                )
                for subject in ("requester", "approver")
            ]
        )
        await db.commit()
    monkeypatch.setattr(
        controller_deployments, "async_session_factory", context.sessions
    )
    policy = tmp_path / "workload-policy.json"
    policy.write_text(
        compact(
            {
                "version": 1,
                "tenants": {
                    str(context.org_id): {
                        "adp_org_id": "adp-test",
                        "workspaces": {str(workspace_id): {"approved-model": profile}},
                    }
                },
            }
        )
    )

    async def ready(org_id):
        return org_id == str(context.org_id)

    context.composition = SimpleNamespace(
        operation_connect=context.connections.connect,
        dispatcher=SimpleNamespace(ready=ready),
    )
    with context.actor(workspace_id=workspace_id):
        async with context.sessions() as db:
            preview = await controller_deployments.preview_controller_deployment(
                db,
                policy_path=policy,
                org_id=context.org_id,
                workspace_id=workspace_id,
                request_id=request_id,
                profile_id="approved-model",
                name="model-server",
                model_options=profile["model_options"],
            )
    approval_id = await context.approve(preview.public(str(workspace_id)))

    async def admit(*, commit=True, approval=approval_id):
        with context.actor(workspace_id=workspace_id):
            async with context.sessions() as db:
                await db.scalar(
                    select(Workspace)
                    .where(Workspace.id == workspace_id)
                    .with_for_update()
                )
                intent = await db.get(Deployment, uuid.UUID(preview.deployment_id))
                if intent is None:
                    db.add(
                        Deployment(
                            id=uuid.UUID(preview.deployment_id),
                            cluster_id=cluster_id,
                            org_id=context.org_id,
                            workspace_id=workspace_id,
                            name="model-server",
                            namespace=profile["namespace"],
                            operation_id=request_id,
                            operation_request_json=compact(preview.deployment_request),
                            operation_target_json=compact(preview.deployment_target),
                            controller_request_payload=encode_payload(preview.request),
                            controller_approval_id=str(approval_id),
                            status="Pending",
                            desired_replicas=profile["model_options"]["replicas"],
                            **{
                                key: value
                                for key, value in profile["model_options"].items()
                                if key != "replicas"
                            },
                        )
                    )
                result = await controller_deployments.admit_controller_deployment(
                    context.composition,
                    db,
                    org_id=context.org_id,
                    workspace_id=workspace_id,
                    preview=preview,
                    approval_id=approval,
                    revision=payload_digest(preview.request),
                )
                if commit:
                    await db.commit()
                else:
                    await db.rollback()
                return result

    from app.config import settings
    from app.schemas.proxy import CreateDeploymentRequest

    monkeypatch.setattr(settings, "superplane_controller_profiles_file", str(policy))
    context.api_body = CreateDeploymentRequest(
        operation_id=request_id,
        profile_id="approved-model",
        name="model-server",
        approval_id=approval_id,
        plan_revision=payload_digest(preview.request),
        **profile["model_options"],
    )
    context.api_request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(trust_composition=context.composition)
        ),
        state=SimpleNamespace(),
    )
    context.policy_path = policy
    context.admit_workload = admit
    context.workload_id = workspace_id
    context.preview = preview
    return context


async def test_paid_admission_registration_replay_never_moves_workspace_bootstrap(
    workload,
):
    result = await workload.admit_workload()
    assert result == await workload.admit_workload()
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations"
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_deployment_operations"
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT provisioning_operation_id FROM workspaces"
            )
            == "original-bootstrap"
        )
        assert (
            await connection.fetchval("SELECT count(*) FROM harness_dispatch_outbox")
            == 1
        )


async def test_lost_domain_commit_recovers_original_paid_admission_without_second_spend(
    workload,
):
    lost = await workload.admit_workload(commit=False)
    async with workload.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_deployment_operations"
            )
            == 0
        )
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
    recovered = await workload.admit_workload()
    assert recovered == lost
    async with workload.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations"
            )
            == 1
        )


async def test_other_approval_cannot_purchase_a_deployment(workload):
    with pytest.raises(ProvisioningRefused):
        await workload.admit_workload(approval=uuid.uuid4())
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0


@pytest.mark.parametrize("change", ["endpoint", "intent", "paid-envelope"])
async def test_registration_recheck_refuses_retargeted_or_unpaid_workload(
    workload, change
):
    result = await workload.admit_workload()
    async with workload.connections.connect() as connection:
        if change == "endpoint":
            await connection.execute(
                "UPDATE clusters SET endpoint='https://replacement.example.invalid'"
            )
        elif change == "intent":
            target = dict(workload.preview.deployment_target)
            target["namespace"] = "foreign-workspace"
            await connection.execute(
                "UPDATE deployments SET operation_target_json=$1", json.dumps(target)
            )
        else:
            await connection.execute(
                "UPDATE operation_budget_reservations SET max_resource_units=2"
            )
        with pytest.raises(OperationRefused):
            await registration_values(
                connection,
                operation_id=result["operation_id"],
                org_id=str(workload.org_id),
                workspace_id=str(workload.workload_id),
                deployment_id=workload.preview.deployment_id,
            )


async def test_registered_workload_dispatches_without_replacing_bootstrap_or_lifecycle_policy(
    workload,
):
    transport = GatewayTransport()
    transport.alter = {"adp_org_id": "adp-test"}
    dispatcher = OperationDispatcher(workload.connections.connect, transport)
    lost = await workload.admit_workload(commit=False)
    assert (await dispatcher.drain_once()).handled == 0
    assert transport.calls == []
    recovered = await workload.admit_workload()
    assert recovered == lost
    assert (await dispatcher.drain_once()).delivered == 1
    assert (await dispatcher.drain_once()).handled == 0
    assert transport.calls[0][1]["operation_id"] == recovered["operation_id"]
    async with workload.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT provisioning_operation_id FROM workspaces"
            )
            == "original-bootstrap"
        )


async def test_changed_canonical_target_cannot_dispatch_registered_workload(workload):
    await workload.admit_workload()
    async with workload.connections.connect() as connection:
        await connection.execute(
            "UPDATE deployments SET namespace='replacement-namespace'"
        )
    transport = GatewayTransport()
    transport.alter = {"adp_org_id": "adp-test"}
    dispatcher = OperationDispatcher(workload.connections.connect, transport)
    assert (await dispatcher.drain_once()).delivered == 0
    assert transport.calls == []


async def test_api_preview_create_and_durable_list_share_original_paid_request(
    workload,
):
    from app.routers.proxy import (
        create_deployment,
        list_deployments,
        preview_deployment,
    )

    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            review = await preview_deployment(
                workload.workload_id, workload.api_body, workload.org_id, db
            )
            assert review == workload.preview.public(str(workload.workload_id))
        async with workload.sessions() as db:
            result = await create_deployment(
                workload.workload_id,
                workload.api_body,
                workload.org_id,
                db,
                workload.api_request,
            )
        async with workload.sessions() as db:
            listed = await list_deployments(
                workload.workload_id, workload.org_id, db, workload.api_request
            )
    assert result.operation_state == "pending"
    assert listed.deployments[0].operation_id == result.operation_id
    assert listed.deployments[0].deployment_id == result.deployment_id
    assert listed.deployments[0].provider_uid is None


async def test_at_cap_teardown_reserves_zero_new_spend_and_retains_original_quota(
    workload,
):
    from harness_jobs.leases import acquire, close
    from app.adapters.operation_authority_source import GrantBackedAuthority
    from app.routers.proxy import (
        create_deployment,
        delete_deployment,
        preview_deployment_teardown,
    )
    from app.schemas.proxy import DeleteDeploymentRequest
    from app.services.provisioning import get_operation_facade
    from app.services.quota import count_workspace_deployment_gpus

    # The maintained budget reader bounds the real durable ledger at exactly the
    # already-approved physical resource/cost ceiling.
    get_operation_facade()._service.ledger._limits_for = GrantBackedAuthority(
        workload.sessions
    ).budget_limits_for
    async with workload.sessions() as db:
        workspace = await db.get(Workspace, workload.workload_id)
        workspace.budget_max_gpus, workspace.budget_max_daily_usd = 1, 1
        await db.commit()
    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            created = await create_deployment(
                workload.workload_id,
                workload.api_body,
                workload.org_id,
                db,
                workload.api_request,
            )
    # This scenario starts from an original completed/closed create, and tests
    # paid control admission. Actual provider/RPC completion has separate coverage.
    async with workload.connections.connect() as connection:
        lease = await acquire(
            connection,
            operation_id=created.operation_id,
            holder="original#1",
            attempt_id="original-attempt",
        )
        await connection.execute(
            "UPDATE harness_operations SET state='succeeded' WHERE operation_id=$1",
            created.operation_id,
        )
        assert await close(
            connection,
            operation_id=created.operation_id,
            reason="completed fixture source",
            fence_token=lease.fence_token,
            holder=lease.holder,
        )
    body = DeleteDeploymentRequest(operation_id=uuid.uuid4())
    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            review = await preview_deployment_teardown(
                workload.workload_id,
                created.deployment_id,
                body,
                workload.api_request,
                workload.org_id,
                db,
            )
    parameters = review["approval_request"]["parameters"]
    assert parameters["max_resource_units"] == parameters["max_cost_micros"] == "0"
    assert (
        parameters["allocation_id"]
        == workload.preview.request.parameters["allocation_id"]
    )
    approval = await workload.approve(review)
    body = body.model_copy(
        update={"approval_id": approval, "plan_revision": review["revision"]}
    )
    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            stopped = await delete_deployment(
                workload.workload_id,
                created.deployment_id,
                body,
                workload.api_request,
                workload.org_id,
                db,
            )
        async with workload.sessions() as db:
            assert await count_workspace_deployment_gpus(workload.workload_id, db) == 1
    assert stopped.status == "Deleting"
    async with workload.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT sum(max_resource_units) FROM operation_budget_reservations"
            )
            == 1
        )
        assert (
            await connection.fetchval(
                "SELECT sum(max_cost_micros) FROM operation_budget_reservations"
            )
            == 1_000_000
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations WHERE state='released'"
            )
            == 0
        )
    # A different stop UUID must refuse before it creates another paid admission.
    other = body.model_copy(update={"operation_id": uuid.uuid4()})
    with (
        workload.actor(workspace_id=workload.workload_id),
        pytest.raises(ProvisioningRefused),
    ):
        async with workload.sessions() as db:
            await delete_deployment(
                workload.workload_id,
                created.deployment_id,
                other,
                workload.api_request,
                workload.org_id,
                db,
            )
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 2


async def api_create(workload, body=None):
    from app.routers.proxy import create_deployment

    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            return await create_deployment(
                workload.workload_id,
                body or workload.api_body,
                workload.org_id,
                db,
                workload.api_request,
            )


@pytest.mark.parametrize(
    "approval_state", ["missing", "unapproved", "expired", "revoked"]
)
async def test_unapproved_create_refuses_before_quota_or_paid_admission(
    workload, approval_state
):
    from datetime import datetime, timedelta, timezone
    from app.models.operation_approval import OperationApproval

    body = workload.api_body
    if approval_state == "missing":
        body = body.model_copy(update={"approval_id": None})
    else:
        async with workload.sessions() as db:
            approval = await db.get(OperationApproval, str(body.approval_id))
            if approval_state == "expired":
                approval.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            elif approval_state == "revoked":
                approval.revoked = True
            else:
                approval.result = None
            await db.commit()
    with pytest.raises(ProvisioningRefused):
        await api_create(workload, body)
    async with workload.connections.connect() as connection:
        for table in (
            "deployments",
            "harness_operations",
            "operation_budget_reservations",
            "harness_dispatch_outbox",
        ):
            assert await connection.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_api_replay_keeps_original_request_after_installed_profile_disappears(
    workload,
):
    original = await api_create(workload)
    workload.policy_path.unlink()
    assert await api_create(workload) == original
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations"
            )
            == 1
        )


async def test_lost_registration_reply_and_concurrent_api_retries_keep_one_intent_and_paid_operation(
    workload, monkeypatch
):
    import asyncio
    from app.services import deployment_operations
    from app.services.provisioning import ProvisioningUnavailable

    actual = deployment_operations.admit_controller_deployment

    async def lose_reply(*args, **kwargs):
        await actual(*args, **kwargs)
        raise ProvisioningUnavailable(
            "simulated lost reply after paid admission and registration"
        )

    monkeypatch.setattr(
        deployment_operations, "admit_controller_deployment", lose_reply
    )
    with pytest.raises(ProvisioningUnavailable):
        await api_create(workload)
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT status FROM deployments") == "Pending"
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_deployment_operations"
            )
            == 0
        )
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1
    monkeypatch.setattr(deployment_operations, "admit_controller_deployment", actual)
    results = await asyncio.wait_for(
        asyncio.gather(api_create(workload), api_create(workload)), 10
    )
    assert results[0] == results[1]
    async with workload.connections.connect() as connection:
        for table in (
            "deployments",
            "harness_operations",
            "operation_budget_reservations",
            "controller_deployment_operations",
            "harness_dispatch_outbox",
        ):
            assert await connection.fetchval(f"SELECT count(*) FROM {table}") == 1


@pytest.mark.parametrize(
    "change", ["endpoint", "namespace", "cluster", "request", "approval"]
)
async def test_original_api_request_cannot_be_retargeted_or_exchanged(workload, change):
    await api_create(workload)
    body = workload.api_body
    async with workload.sessions() as db:
        workspace = await db.get(Workspace, workload.workload_id)
        if change == "endpoint":
            cluster = await db.get(Cluster, workspace.cluster_id)
            cluster.endpoint = "https://replacement.example.invalid"
        elif change == "namespace":
            workspace.namespace_name = "replacement"
        elif change == "cluster":
            replacement = Cluster(
                id=uuid.uuid4(),
                org_id=workload.org_id,
                name="replacement",
                status="Ready",
                endpoint=workload.preview.deployment_target["endpoint"],
                eks_cluster_arn=workload.preview.deployment_target["cluster_arn"],
            )
            db.add(replacement)
            await db.flush()
            workspace.cluster_id = replacement.id
        elif change == "request":
            body = body.model_copy(update={"model_name": "different-model"})
        else:
            body = body.model_copy(update={"approval_id": uuid.uuid4()})
        await db.commit()
    with pytest.raises(ProvisioningRefused):
        await api_create(workload, body)
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1


@pytest.mark.parametrize("status", ["Deleting", "Deleted"])
async def test_create_tombstone_replay_requires_original_revision_and_never_recreates(
    workload, status
):
    created = await api_create(workload)
    async with workload.sessions() as db:
        intent = await db.get(Deployment, created.deployment_id)
        intent.status = status
        await db.commit()
    with pytest.raises(ProvisioningRefused, match="original reviewed"):
        await api_create(
            workload, workload.api_body.model_copy(update={"plan_revision": "0" * 64})
        )
    replay = await api_create(workload)
    assert replay.deployment_id == created.deployment_id
    assert replay.status == status
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM deployments") == 1
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 1


@pytest.fixture
async def worker_runtime(workload, tmp_path):
    """Real paid task registry/RPC/finalizer; only remote I/O is simulated."""
    from datetime import UTC, datetime, timedelta
    import httpx
    from harness_jobs.execution_rpc import ExecutionGrant
    from harness_jobs.identity import ResolvedPrincipal
    from harness_jobs.leases import acquire
    from superplane_executor.authority import VerifiedOperation
    from superplane_executor.handoff import RunHandoff
    from superplane_executor.inventory import Finalizer
    from superplane_executor.plan import Plan
    from superplane_executor.provider import Provider
    from superplane_executor.service import ControllerRPCServer
    from superplane_executor.skypilot import SkyPilot
    from superplane_executor.task_registry import TaskRegistry
    from superplane_executor.task_worker import write_private
    from tests.controller_provider_support import Cloud, Kubernetes

    pool = SimpleNamespace(acquire=workload.connections.connect)
    async with pool.acquire() as connection:
        await connection.execute("""
            CREATE TABLE observation_leases(scope text PRIMARY KEY,holder text,expires_at timestamptz);
            CREATE TABLE controller_provider_requests(idempotency_key text PRIMARY KEY,operation_id text,org_id text,workspace_id text,cluster_name text,operation_kind text,request_id text);
            CREATE TABLE controller_capacity(org_id text,workspace_id text,cluster_name text,state text,PRIMARY KEY(org_id,workspace_id,cluster_name));
            CREATE TABLE controller_execution_accounting(operation_id text PRIMARY KEY,org_id uuid,workspace_id uuid,observation json);
        """)
        await connection.execute(
            "INSERT INTO observation_leases VALUES($1,'registration-manager',$2)",
            "controller_management/" + str(workload.org_id),
            datetime.now(UTC) + timedelta(minutes=5),
        )
    data = json.loads(workload.preview.request.parameters["controller_plan"])
    data["certificate_authority"] = workload.preview.request.parameters[
        "controller_certificate_authority"
    ]
    cloud, verified = Cloud(data), {}
    cloud.sky_tasks = []
    cloud.capacity_constraints = ["physical_gpu_limit"]
    kube = Kubernetes(cloud)
    role = f"arn:aws:iam::{data['provider_account_id']}:role/approved"

    class Authority:
        async def resolve(self, operation_id):
            return verified[operation_id]

        async def preflight(self, operation):
            assert operation == verified[operation.grant.lease.operation_id]

        async def delivery_role(self, operation):
            await self.preflight(operation)
            return {"role_arn": role}

    def sky_transport(request):
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"status": "healthy"})
        if path == "/internal/provider-identity":
            return httpx.Response(
                200,
                json={
                    "version": 1,
                    "provider": "aws",
                    "account_id": data["provider_account_id"],
                    "role_arn": role,
                    "credential_source": "web_identity",
                    "allocation_tags": ["instance", "volume", "network-interface"],
                    "capacity_constraints": cloud.capacity_constraints,
                },
            )
        if path == "/launch":
            cloud.sky_tasks.append(json.loads(json.loads(request.content)["task"]))
            cloud.launches += 1
            cloud.exists = cloud.ever_created = True
        elif path == "/down":
            assert json.loads(request.content)["purge"] is False
            cloud.exists = False
        elif path == "/api/status":
            return httpx.Response(
                200,
                json=[
                    {
                        "request_id": request.url.params["request_ids"],
                        "status": "SUCCEEDED",
                    }
                ],
            )
        else:
            raise AssertionError(path)
        return httpx.Response(
            200,
            json=None,
            headers={"X-Skypilot-Request-ID": "test-request-" + uuid.uuid4().hex},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(sky_transport))
    sky_token = tmp_path / "sky-token"
    sky_token.write_text("test-only-provider-token-" + "a" * 32)
    sky = SkyPilot("https://sky.example.invalid", sky_token, http)
    gateway = GatewayTransport()
    gateway.alter = {"adp_org_id": "adp-test"}
    dispatcher = OperationDispatcher(workload.connections.connect, gateway)

    async def publish(result):
        # The request here is the actual API admission, delivered by its real outbox.
        assert (await dispatcher.drain_once()).delivered == 1
        assert gateway.calls[-1][1]["operation_id"] == result.operation_id
        async with pool.acquire() as connection:
            original = await connection.fetchrow(
                "SELECT o.*,a.reservation_state,a.max_resource_units,a.max_runtime_seconds,a.max_cost_micros FROM harness_operations o JOIN harness_approval_consumption a ON a.operation_id=o.operation_id WHERE o.operation_id=$1",
                result.operation_id,
            )
            lease = await acquire(
                connection,
                operation_id=result.operation_id,
                holder="real-invocation#1",
                attempt_id=original["attempt_id"],
            )
        principal = ResolvedPrincipal(
            lease.org_id,
            lease.workspace_id,
            lease.holder,
            frozenset({"workspace:provision"}),
        )
        operation = VerifiedOperation(
            ExecutionGrant(principal, lease),
            original["job_id"],
            original["plan_digest"],
            original["request_payload"],
            original["reservation_state"],
            original["max_resource_units"],
            original["max_runtime_seconds"],
            original["max_cost_micros"],
        )
        verified[result.operation_id] = operation
        directory = tmp_path / result.operation_id
        directory.mkdir()
        provider = Provider(
            sky=sky,
            workspace=kube,
            domain_pool=pool,
            execution_pool=pool,
            session=cloud,
        )
        registry = TaskRegistry(
            original=operation,
            write_private=write_private,
            assignment_file=directory / "assignment.json",
            domain_pool=pool,
            execution_pool=pool,
            authority=Authority(),
            instance_file=directory / "unused-instance",
            token_dir=directory / "tokens",
            submitter_id="paid-worker",
            validate_plan=provider.validate_plan,
            handoffs={
                result.operation_id: RunHandoff(
                    result.operation_id,
                    lease.attempt_id,
                    operation.job_id,
                    datetime.now(UTC) + timedelta(minutes=5),
                )
            },
        )
        provider.registry = registry
        finalizer = Finalizer(provider, registry)
        server = ControllerRPCServer(
            connect=pool.acquire,
            provider_call=provider,
            authenticate=registry.authenticate,
            after_step=finalizer,
        )
        name = await registry.publish(result.operation_id)
        token = (directory / "tokens" / name).read_text()
        target = await registry.target(operation, lease.holder)
        plan = Plan.read(operation, target)
        return SimpleNamespace(
            operation=operation,
            registry=registry,
            server=server,
            token=token,
            finalizer=finalizer,
            plan=plan,
        )

    async def execute(worker):
        results = []
        for step in worker.plan.steps:
            results.append(
                await worker.server.dispatch(
                    {
                        "token": worker.token,
                        "method": "execute_step",
                        "arguments": {"step_id": step["step_id"]},
                    }
                )
            )
        return results

    try:
        yield SimpleNamespace(
            publish=publish, execute=execute, cloud=cloud, kube=kube, pool=pool
        )
    finally:
        await http.aclose()


async def approved_teardown(workload, created):
    from app.routers.proxy import delete_deployment, preview_deployment_teardown
    from app.schemas.proxy import DeleteDeploymentRequest

    body = DeleteDeploymentRequest(operation_id=uuid.uuid4())
    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            review = await preview_deployment_teardown(
                workload.workload_id,
                created.deployment_id,
                body,
                workload.api_request,
                workload.org_id,
                db,
            )
    approval = await workload.approve(review)
    body = body.model_copy(
        update={"approval_id": approval, "plan_revision": review["revision"]}
    )
    workload.teardown_body = body
    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            return await delete_deployment(
                workload.workload_id,
                created.deployment_id,
                body,
                workload.api_request,
                workload.org_id,
                db,
            )


async def assert_completed_worker(worker_runtime, worker):
    lease = worker.operation.grant.lease
    async with worker_runtime.pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                lease.operation_id,
            )
            == "succeeded"
        )
        assert (
            await connection.fetchval(
                "SELECT closed_at FROM harness_operation_leases WHERE operation_id=$1",
                lease.operation_id,
            )
            is not None
        )


@pytest.mark.parametrize("leaked_volume", [False, True])
@pytest.mark.parametrize("workload", [False, True], indirect=True)
async def test_actual_api_dispatch_paid_worker_rpc_and_owned_absence_quota_projection(
    workload, worker_runtime, leaked_volume
):
    from app.routers.proxy import list_deployments
    from app.services.quota import count_workspace_deployment_gpus

    created = await api_create(workload)
    worker = await worker_runtime.publish(created)
    assert all(result[1] == "settle" for result in await worker_runtime.execute(worker))
    assert worker_runtime.cloud.launches == 1
    if worker.plan.data["version"] == 3:
        resources = worker_runtime.cloud.sky_tasks[0]["resources"]
        assert "instance_type" not in resources
        assert resources["any_of"] == [
            {"accelerators": "A10G:1"},
            {"accelerators": "L4:1"},
        ]
        assert resources["labels"]["superplane-max-gpus-per-node"] == "1"
        assert resources["cpus"] == "4+" and resources["memory"] == "32+"
    # A completed task's closed lease cannot authorize a duplicate launch.
    from harness_jobs.execution import ProviderCallRefused

    with pytest.raises((OperationRefused, ProviderCallRefused)):
        await worker.server.dispatch(
            {
                "token": worker.token,
                "method": "execute_step",
                "arguments": {"step_id": worker.plan.steps[0]["step_id"]},
            }
        )
    assert worker_runtime.cloud.launches == 1
    await assert_completed_worker(worker_runtime, worker)
    async with workload.sessions() as db:
        listed = await list_deployments(
            workload.workload_id, workload.org_id, db, workload.api_request
        )
        assert await count_workspace_deployment_gpus(workload.workload_id, db) == 1
    deployment_path = worker_runtime.kube.path(
        workload.preview.deployment_target, "Deployment", workload.api_body.name
    )
    uid = worker_runtime.kube.stored[deployment_path]["metadata"]["uid"]
    assert listed.deployments[0].provider_uid == uid
    assert listed.deployments[0].status == "Created"
    stopped = await approved_teardown(workload, created)
    retirement = await worker_runtime.publish(stopped)
    assert (
        retirement.operation.request.parameters["allocation_id"]
        == worker.operation.request.parameters["allocation_id"]
    )
    assert (
        retirement.operation.max_cost_micros
        == retirement.operation.max_resource_units
        == 0
    )
    worker_runtime.cloud.leaked_volume = leaked_volume
    assert all(
        result[1] == "settle" for result in await worker_runtime.execute(retirement)
    )
    async with workload.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                stopped.operation_id,
            )
            == "succeeded"
        )
        accounting = json.loads(
            await connection.fetchval(
                "SELECT observation::text FROM controller_execution_accounting WHERE operation_id=$1",
                stopped.operation_id,
            )
        )
        assert accounting["may_mark_released"] is (not leaked_volume)
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations WHERE state='released'"
            )
            == 0
        )
    async with workload.sessions() as db:
        intent = await db.get(Deployment, created.deployment_id)
        assert intent.status == ("Deleting" if leaked_volume else "Deleted")
        assert await count_workspace_deployment_gpus(workload.workload_id, db) == int(
            leaked_volume
        )
    assert not worker_runtime.kube.stored
    assert worker_runtime.cloud.launches == 1


@pytest.mark.parametrize("workload", [True], indirect=True)
async def test_gpu_selection_requires_backend_physical_limit_support(
    workload, worker_runtime
):
    created = await api_create(workload)
    worker = await worker_runtime.publish(created)
    worker_runtime.cloud.capacity_constraints = []
    with pytest.raises(OperationRefused):
        await worker_runtime.execute(worker)
    assert worker_runtime.cloud.launches == 0
    assert not worker_runtime.kube.stored


@pytest.mark.parametrize("kind", ["Deployment", "Service"])
async def test_governed_teardown_cannot_adopt_replacement_uid_with_same_capacity_label(
    workload, worker_runtime, kind
):
    from harness_jobs.identity import OperationRefused
    from app.services.quota import count_workspace_deployment_gpus

    created = await api_create(workload)
    worker = await worker_runtime.publish(created)
    await worker_runtime.execute(worker)
    await assert_completed_worker(worker_runtime, worker)
    path = worker_runtime.kube.path(
        workload.preview.deployment_target, kind, workload.api_body.name
    )
    worker_runtime.kube.stored[path]["metadata"]["uid"] = "replacement-uid"
    stopped = await approved_teardown(workload, created)
    retirement = await worker_runtime.publish(stopped)
    with pytest.raises(
        OperationRefused, match="original workload UID evidence unavailable"
    ):
        await worker_runtime.execute(retirement)
    assert worker_runtime.kube.stored[path]["metadata"]["uid"] == "replacement-uid"
    assert all(method != "DELETE" for method, _ in worker_runtime.kube.requests)
    assert worker_runtime.cloud.exists
    async with workload.sessions() as db:
        assert (await db.get(Deployment, created.deployment_id)).status == "Deleting"
        assert await count_workspace_deployment_gpus(workload.workload_id, db) == 1
    async with workload.connections.connect() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations WHERE state='released'"
            )
            == 0
        )


async def assert_replaced_workload_cannot_complete(runtime, worker, object_kind):
    """Exercise the real paid status call after successful, UID-recorded creation."""
    for step in worker.plan.steps[:3]:
        await worker.server.dispatch(
            {
                "token": worker.token,
                "method": "execute_step",
                "arguments": {"step_id": step["step_id"]},
            }
        )
    original = next(
        obj for obj in runtime.kube.stored.values() if obj["kind"] == object_kind
    )
    original["metadata"]["uid"] = "replacement-before-readiness"
    before = len(runtime.kube.requests)
    with pytest.raises(OperationRefused, match="original workload UID evidence"):
        await worker.server.dispatch(
            {
                "token": worker.token,
                "method": "execute_step",
                "arguments": {"step_id": worker.plan.steps[3]["step_id"]},
            }
        )
    async with runtime.pool.acquire() as connection:
        from harness_jobs.execution_plan import confirmed_plan_progress, PlanProgress
        from harness_jobs.store import stored_outcome

        assert (
            await confirmed_plan_progress(
                connection, worker.operation.grant.lease.operation_id
            )
            != PlanProgress.COMPLETE
        )
        rows = await connection.fetch(
            "SELECT stage,outcome FROM harness_provider_call_intent WHERE operation_id=$1 "
            "AND operation_kind='status'",
            worker.operation.grant.lease.operation_id,
        )
        assert len(rows) == 2
        assert sum(stored_outcome(row["outcome"]) == "succeeded" for row in rows) == 1
        # Transport uncertainty leaves the intent recoverable, without inventing
        # a terminal observation of the replacement object.
        assert (
            sum(row["stage"] == "intended" and row["outcome"] is None for row in rows)
            == 1
        )
    assert all(
        "/secrets/" not in path and "/proxy/" not in path
        for _, path in runtime.kube.requests[before:]
    )


@pytest.mark.parametrize("kind", ["Deployment", "Service"])
async def test_serving_status_cannot_use_replacement_before_readiness(
    workload, worker_runtime, kind
):
    worker = await worker_runtime.publish(await api_create(workload))
    await assert_replaced_workload_cannot_complete(worker_runtime, worker, kind)


async def test_approved_over_quota_api_request_cannot_open_paid_admission(workload):
    from fastapi import HTTPException

    async with workload.sessions() as db:
        workspace = await db.get(Workspace, workload.workload_id)
        workspace.budget_max_gpus = 1
        db.add(
            Deployment(
                id=uuid.uuid4(),
                org_id=workload.org_id,
                workspace_id=workload.workload_id,
                cluster_id=workspace.cluster_id,
                namespace=workspace.namespace_name,
                name="existing-capacity",
                desired_replicas=1,
                gpu_per_replica=1,
                status="Created",
            )
        )
        await db.commit()
    with pytest.raises(HTTPException) as refused:
        await api_create(workload)
    assert refused.value.status_code == 429
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM deployments") == 1
        for table in (
            "harness_operations",
            "controller_deployment_operations",
            "operation_budget_reservations",
            "harness_dispatch_outbox",
        ):
            assert await connection.fetchval(f"SELECT count(*) FROM {table}") == 0


async def test_even_approved_wrong_teardown_source_refuses_before_second_paid_admission(
    workload, worker_runtime
):
    from superplane_executor.deployment_plan import DeploymentPreview, teardown_request

    created = await api_create(workload)
    worker = await worker_runtime.publish(created)
    await worker_runtime.execute(worker)
    await assert_completed_worker(worker_runtime, worker)
    request = teardown_request(
        workload.preview.request,
        org_id=str(workload.org_id),
        workspace_id=str(workload.workload_id),
        request_id=str(uuid.uuid4()),
        source_operation_id="another-deployments-paid-operation",
    )
    altered = DeploymentPreview(
        workload.preview.deployment_id,
        request,
        workload.preview.deployment_request,
        workload.preview.deployment_target,
    )
    approval = await workload.approve(altered.public(str(workload.workload_id)))
    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            with pytest.raises(
                ProvisioningRefused, match="original settled deployment source"
            ):
                await controller_deployments.admit_controller_deployment(
                    workload.composition,
                    db,
                    org_id=workload.org_id,
                    workspace_id=workload.workload_id,
                    preview=altered,
                    approval_id=approval,
                    revision=payload_digest(request),
                )
    async with workload.connections.connect() as connection:
        for table in (
            "harness_operations",
            "operation_budget_reservations",
            "controller_deployment_operations",
            "harness_dispatch_outbox",
        ):
            assert await connection.fetchval(f"SELECT count(*) FROM {table}") == 1
    assert worker_runtime.cloud.exists


@pytest.mark.parametrize(
    "failure", ["before-uid-capture", "replacement-before-first-inventory"]
)
async def test_post_response_uid_evidence_cannot_be_reconstructed_from_later_labelled_object(
    workload, worker_runtime, monkeypatch, failure
):
    from harness_jobs.inventory import InventoryAuthority

    created = await api_create(workload)
    worker = await worker_runtime.publish(created)
    original_request = worker_runtime.kube.request
    expected = {}

    async def request(operation, target, method, path, *, body=None, headers=None):
        if method == "POST" and body["kind"] == "Service":
            async with worker_runtime.pool.acquire() as connection:
                references = await connection.fetch(
                    "SELECT provider_reference FROM harness_allocation_resource WHERE kind='workspace_object'"
                )
            # The successful Deployment UID is durable before Service creation.
            assert {row["provider_reference"] for row in references} == {
                expected["Deployment"]
            }
        response = await original_request(
            operation, target, method, path, body=body, headers=headers
        )
        if method == "POST" and response.status_code == 201:
            expected[body["kind"]] = worker_runtime.kube.reference(
                body["kind"], response.json()
            )
            if (
                failure == "replacement-before-first-inventory"
                and body["kind"] == "Service"
            ):
                worker_runtime.kube.stored[path + "/" + body["metadata"]["name"]][
                    "metadata"
                ]["uid"] = "replacement-service-uid"
        return response

    monkeypatch.setattr(worker_runtime.kube, "request", request)
    if failure == "before-uid-capture":
        enumerate_resources = InventoryAuthority.enumerate_resources

        async def fail_capture(self, connection, lease, *, resources):
            if any(resource.kind == "workspace_object" for resource in resources):
                raise RuntimeError("simulated crash before successful POST UID commit")
            return await enumerate_resources(
                self, connection, lease, resources=resources
            )

        monkeypatch.setattr(InventoryAuthority, "enumerate_resources", fail_capture)
    with pytest.raises(OperationRefused, match="original workload UID evidence"):
        await worker_runtime.execute(worker)
    async with worker_runtime.pool.acquire() as connection:
        rows = await connection.fetch(
            "SELECT provider_reference FROM harness_allocation_resource WHERE kind='workspace_object'"
        )
        known = {row["provider_reference"] for row in rows}
        assert known == (
            set() if failure == "before-uid-capture" else set(expected.values())
        )
        assert not any("replacement-service-uid" in reference for reference in known)
        assert (
            await connection.fetchval(
                "SELECT closed_at FROM harness_operation_leases WHERE operation_id=$1",
                created.operation_id,
            )
            is None
        )
    if failure == "before-uid-capture":
        assert list(expected) == ["Deployment"]
        from harness_jobs.execution import ProviderCallRefused

        writes = [item for item in worker_runtime.kube.requests if item[0] == "POST"]
        assert (await api_create(workload)).operation_id == created.operation_id
        deploy = next(
            step for step in worker.plan.steps if step["operation_kind"] == "deploy"
        )
        with pytest.raises(ProviderCallRefused, match="requires recovery"):
            await worker.server.dispatch(
                {
                    "token": worker.token,
                    "method": "execute_step",
                    "arguments": {"step_id": deploy["step_id"]},
                }
            )
        assert [
            item for item in worker_runtime.kube.requests if item[0] == "POST"
        ] == writes
    # Subsequent provider listing cannot establish missing/replaced UID identity.
    target = await worker.registry.target(
        worker.operation, worker.operation.grant.lease.holder
    )
    with pytest.raises(OperationRefused, match="original workload UID evidence"):
        await worker.finalizer.discover(worker.operation, target, worker.plan, [])
    assert all(method != "DELETE" for method, _ in worker_runtime.kube.requests)
    assert worker_runtime.cloud.exists


async def test_lost_delete_reply_replays_original_paid_intent_without_repeating_mutation(
    workload, worker_runtime, monkeypatch
):
    import httpx
    from harness_jobs.execution import ProviderCallRefused
    from app.routers.proxy import delete_deployment
    from app.services.quota import count_workspace_deployment_gpus

    created = await api_create(workload)
    provision = await worker_runtime.publish(created)
    await worker_runtime.execute(provision)
    await assert_completed_worker(worker_runtime, provision)
    stopped = await approved_teardown(workload, created)
    retirement = await worker_runtime.publish(stopped)
    original_request = worker_runtime.kube.request
    lost = False

    async def lose_delete_reply(*args, **kwargs):
        nonlocal lost
        response = await original_request(*args, **kwargs)
        if args[2] == "DELETE" and not lost:
            lost = True
            raise httpx.ReadError("simulated lost reply after UID-pinned deletion")
        return response

    monkeypatch.setattr(worker_runtime.kube, "request", lose_delete_reply)
    result = await worker_runtime.execute(retirement)
    assert result[0][1] == "retain"
    assert lost
    mutations = [
        item for item in worker_runtime.kube.requests if item[0] in {"POST", "DELETE"}
    ]
    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            replay = await delete_deployment(
                workload.workload_id,
                created.deployment_id,
                workload.teardown_body,
                workload.api_request,
                workload.org_id,
                db,
            )
    assert replay.operation_id == stopped.operation_id
    assert replay.status == "Deleting"
    assert (await api_create(workload)).status == "Deleting"
    with pytest.raises(ProviderCallRefused, match="requires recovery"):
        await worker_runtime.execute(retirement)
    assert [
        item for item in worker_runtime.kube.requests if item[0] in {"POST", "DELETE"}
    ] == mutations
    assert worker_runtime.cloud.launches == 1
    assert worker_runtime.cloud.exists
    async with workload.sessions() as db:
        assert await count_workspace_deployment_gpus(workload.workload_id, db) == 1
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 2
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_allocation_resource WHERE kind='workspace_object'"
            )
            == 2
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations WHERE state='released'"
            )
            == 0
        )


async def test_missing_original_object_never_recreated_by_api_or_closed_worker_replay(
    workload, worker_runtime
):
    from harness_jobs.execution import ProviderCallRefused

    created = await api_create(workload)
    provision = await worker_runtime.publish(created)
    await worker_runtime.execute(provision)
    await assert_completed_worker(worker_runtime, provision)
    path = worker_runtime.kube.path(
        workload.preview.deployment_target, "Deployment", workload.api_body.name
    )
    original_uid = worker_runtime.kube.stored.pop(path)["metadata"]["uid"]
    mutations = [item for item in worker_runtime.kube.requests if item[0] == "POST"]
    replay = await api_create(workload)
    assert replay.operation_id == created.operation_id
    assert replay.provider_uid == original_uid
    with pytest.raises((OperationRefused, ProviderCallRefused)):
        await worker_runtime.execute(provision)
    assert [
        item for item in worker_runtime.kube.requests if item[0] == "POST"
    ] == mutations
    assert path not in worker_runtime.kube.stored
    # Absence is resolved only by separately approved teardown and full inventory.
    stopped = await approved_teardown(workload, created)
    retirement = await worker_runtime.publish(stopped)
    assert all(
        result[1] == "settle" for result in await worker_runtime.execute(retirement)
    )
    async with workload.sessions() as db:
        assert (await db.get(Deployment, created.deployment_id)).status == "Deleted"
    assert worker_runtime.cloud.launches == 1


async def test_allocation_membership_from_another_operation_is_not_original_uid_evidence(
    workload, worker_runtime
):
    created = await api_create(workload)
    provision = await worker_runtime.publish(created)
    await worker_runtime.execute(provision)
    await assert_completed_worker(worker_runtime, provision)
    async with worker_runtime.pool.acquire() as connection:
        await connection.execute(
            "UPDATE harness_allocation_resource SET operation_id='different-origin-operation' WHERE kind='workspace_object'"
        )
        count = await connection.fetchval(
            "SELECT count(*) FROM harness_allocation_resource"
        )
    target = await provision.registry.target(
        provision.operation, provision.operation.grant.lease.holder
    )
    with pytest.raises(OperationRefused, match="original workload UID provenance"):
        await provision.finalizer.discover(
            provision.operation, target, provision.plan, []
        )
    # The refusal cannot erase allocation-wide resources or free their exposure.
    async with worker_runtime.pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_allocation_resource"
            )
            == count
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations WHERE state='released'"
            )
            == 0
        )
    assert worker_runtime.cloud.exists


@pytest.mark.parametrize(
    "change",
    [None, "dispatcher", "revoked", "provision-only", "target", "unconfigured"],
)
async def test_serving_catalog_checks_current_grants_profiles_and_transport_without_admission(
    workload, monkeypatch, change
):
    from datetime import UTC, datetime
    from app.config import settings
    from app.routers.proxy import deployment_profiles

    if change == "dispatcher":

        async def unavailable(_):
            return False

        workload.composition.dispatcher.ready = unavailable
    elif change in {"revoked", "provision-only"}:
        async with workload.sessions() as db:
            grant = await db.scalar(
                select(WorkspaceGrantRecord).where(
                    WorkspaceGrantRecord.workspace_id == workload.workload_id,
                    WorkspaceGrantRecord.principal == "requester",
                )
            )
            if change == "revoked":
                grant.revoked_at = datetime.now(UTC)
            else:
                grant.permissions = "workspace:provision"
            await db.commit()
    elif change == "target":
        document = json.loads(workload.policy_path.read_text())
        document["tenants"][str(workload.org_id)]["workspaces"][
            str(workload.workload_id)
        ]["approved-model"]["namespace"] = "another-workspace"
        workload.policy_path.write_text(json.dumps(document))
    elif change == "unconfigured":
        monkeypatch.setattr(settings, "superplane_controller_profiles_file", "")
    with workload.actor(workspace_id=workload.workload_id):
        async with workload.sessions() as db:
            result = await deployment_profiles(
                workspace_id=workload.workload_id,
                request=workload.api_request,
                org_id=workload.org_id,
                db=db,
            )
    assert result["workspace_id"] == str(workload.workload_id)
    assert result["can_submit"] is (change is None)
    assert result["can_review_teardown"] is (
        change not in {"dispatcher", "revoked", "provision-only"}
    )
    if change in {None, "dispatcher"}:
        assert len(result["profiles"]) == 1
        profile = result["profiles"][0]
        assert profile["profile_id"] == "approved-model"
        assert profile["model_options"]["model_name"] == "fixture/model"
        assert profile["image"].endswith("@sha256:" + "a" * 64)
        assert profile["max_runtime_seconds"] == 900
        assert profile["max_cost_micros"] == 1_000_000
        assert not any(
            name in json.dumps(result)
            for name in (
                "credential_id",
                "auth_secret",
                "certificate_authority",
                "instance_profile",
            )
        )
    else:
        assert not result["profiles"]
    async with workload.connections.connect() as connection:
        assert await connection.fetchval("SELECT count(*) FROM harness_operations") == 0
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM controller_deployment_operations"
            )
            == 0
        )
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM operation_budget_reservations"
            )
            == 0
        )
