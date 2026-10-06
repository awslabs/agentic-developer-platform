"""Real RPC, admission, inventory and accounting with simulated AWS/SkyPilot/K8s I/O.

These tests are offline transport/provider-contract evidence, not live EKS acceptance.
"""

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from botocore.exceptions import ClientError
from harness_jobs import OperationStore
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.execution_plan import confirmed_plan_progress, PlanProgress
from harness_jobs.identity import OperationRequest, OperationRefused
from harness_jobs.execution import ProviderCallRefused
from harness_jobs.leases import acquire
from superplane_executor.authority import VerifiedOperation
from superplane_executor.inventory import Finalizer
from superplane_executor.provider import Provider
from superplane_executor.registry import AssignmentRegistry
from superplane_executor.skypilot import SkyPilot
from superplane_executor.service import ControllerRPCServer
from superplane_executor.workspace import Workspace
from tests.conftest import admit_paid, requires_postgres
from tests.test_admission_postgres import principal
from handoff_support import granted

pytestmark = requires_postgres


class Cloud:
    def __init__(self, data):
        self.data = data
        self.exists = False
        self.ever_created = False
        self.leaked_volume = False
        self.launches = 0
        self.sky_account = "123456789012"
        self.sky_role = "arn:aws:iam::123456789012:role/approved"
        self.sky_source = "web_identity"
        self.lose_launch_response = False
        # A kill AFTER the durable handle is journalled but BEFORE the launch is
        # observed: the state scoped recovery exists to resolve.
        self.lose_status_response = False
        self.instance = {
            "InstanceId": "i-0123456789abcdef0",
            "Placement": {"AvailabilityZone": data["region"] + "a"},
            "ImageId": data["image_id"],
            "InstanceType": data["instance_type"],
            "State": {"Name": "running"},
            "BlockDeviceMappings": [{"Ebs": {"VolumeId": "vol-0123456789abcdef0"}}],
            "NetworkInterfaces": [{"NetworkInterfaceId": "eni-0123456789abcdef0"}],
        }

    def client(self, name, **kwargs):
        return self

    def get_caller_identity(self):
        return {
            "Account": self.data["provider_account_id"],
            "Arn": "arn:aws:sts::123456789012:assumed-role/approved/provider",
        }

    def describe_cluster(self, **kwargs):
        d = self.data
        return {
            "cluster": {
                "name": "workspace",
                "arn": d["cluster_arn"],
                "endpoint": d["endpoint"],
                "status": "ACTIVE",
                "certificateAuthority": {"data": d["certificate_authority"]},
                "kubernetesNetworkConfig": {"serviceIpv4Cidr": d["service_cidr"]},
                "resourcesVpcConfig": {"vpcId": "vpc-approved"},
            }
        }

    def describe_vpcs(self, **kwargs):
        return {"Vpcs": [{"VpcId": "vpc-approved"}]}

    def describe_security_groups(self, **kwargs):
        return {"SecurityGroups": [{"GroupId": "sg-approved"}]}

    def get_instance_profile(self, **kwargs):
        return {
            "InstanceProfile": {
                "Arn": "arn:aws:iam::123456789012:instance-profile/approved",
                "Roles": [{"Arn": "arn:aws:iam::123456789012:role/node"}],
            }
        }

    def describe_access_entry(self, **kwargs):
        return {"accessEntry": {"type": "EC2_LINUX"}}

    def get_paginator(self, method):
        cloud = self

        class Paginator:
            def paginate(self, **kwargs):
                if method == "describe_instances":
                    include_old = not any(
                        f["Name"] == "instance-state-name"
                        for f in kwargs.get("Filters", [])
                    )
                    return [
                        cloud.describe_instances(InstanceIds=["known"])
                        if include_old or cloud.exists
                        else {"Reservations": []}
                    ]
                return [getattr(cloud, method)(**kwargs)]

        return Paginator()

    def describe_instances(self, **kwargs):
        if not self.ever_created:
            return {"Reservations": []}
        return {
            "Reservations": [
                {
                    "Instances": [
                        dict(
                            self.instance,
                            State={"Name": "running" if self.exists else "terminated"},
                        )
                    ]
                }
            ]
        }

    def describe_volumes(self, **kwargs):
        if "VolumeIds" in kwargs and not (self.exists or self.leaked_volume):
            raise ClientError(
                {"Error": {"Code": "InvalidVolume.NotFound"}}, "DescribeVolumes"
            )
        return {
            "Volumes": [{"VolumeId": "vol-0123456789abcdef0", "State": "in-use"}]
            if self.exists or self.leaked_volume
            else []
        }

    def describe_network_interfaces(self, **kwargs):
        if "NetworkInterfaceIds" in kwargs and not self.exists:
            raise ClientError(
                {"Error": {"Code": "InvalidNetworkInterfaceID.NotFound"}},
                "DescribeNetworkInterfaces",
            )
        return {
            "NetworkInterfaces": [
                {"NetworkInterfaceId": "eni-0123456789abcdef0", "Status": "in-use"}
            ]
            if self.exists
            else []
        }

    def describe_addresses(self, **kwargs):
        if "AllocationIds" in kwargs:
            raise ClientError(
                {"Error": {"Code": "InvalidAllocationID.NotFound"}}, "DescribeAddresses"
            )
        return {"Addresses": []}


class Kubernetes(Workspace):
    def __init__(self, cloud):
        self.cloud = cloud
        self.stored = {}
        self.requests = []

    async def request(
        self, operation, target, method, path, *, body=None, headers=None
    ):
        self.requests.append((method, path))
        if path == "/api/v1/namespaces/tenant-a":
            return httpx.Response(200, json={"status": {"phase": "Active"}})
        if "/superplane.ai/" in path:
            return httpx.Response(200, json={"items": []})
        if path.startswith("/api/v1/nodes?"):
            zone = self.cloud.instance["Placement"]["AvailabilityZone"]
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "metadata": {
                                "name": "allocated-node",
                                "uid": "allocated-node-uid",
                                "labels": {
                                    "superplane.ai/capacity": self.cloud.cluster_name,
                                    "superplane.ai/workspace": operation.grant.lease.workspace_id,
                                    "topology.kubernetes.io/region": zone[:-1],
                                    "topology.kubernetes.io/zone": zone,
                                },
                            },
                            "spec": {
                                "providerID": f"aws:///{zone}/{self.cloud.instance['InstanceId']}"
                            },
                            "status": {
                                "conditions": [{"type": "Ready", "status": "True"}],
                                "allocatable": {"nvidia.com/gpu": "1"},
                            },
                        }
                    ]
                    if self.cloud.exists
                    else []
                },
            )
        if "/pods?" in path:
            from workload_support import completed_job_pod

            pods = [
                completed_job_pod(obj)
                for obj in self.stored.values()
                if obj.get("kind") == "Job"
            ]
            return httpx.Response(200, json={"items": pods})
        if "/pods/" in path and method == "GET":
            from workload_support import completed_job_pod

            for obj in self.stored.values():
                if obj.get("kind") == "Job":
                    pod = completed_job_pod(obj)
                    if path.endswith("/" + pod["metadata"]["name"]):
                        return httpx.Response(200, json=pod)

        if method == "POST":
            obj = json.loads(json.dumps(body))
            obj["metadata"]["uid"] = str(uuid4())
            obj["status"] = {
                "succeeded": 1,
                "availableReplicas": 1,
                "observedGeneration": 1,
            }
            obj["metadata"]["generation"] = 1
            key = path + "/" + obj["metadata"]["name"]
            if key in self.stored:
                return httpx.Response(409)
            self.stored[key] = obj
            return httpx.Response(201, json=obj)
        if method == "DELETE":
            obj = self.stored.get(path)
            if obj and body["preconditions"]["uid"] != obj["metadata"]["uid"]:
                return httpx.Response(409)
            self.stored.pop(path, None)
            return httpx.Response(200, json={})
        obj = self.stored.get(path)
        return httpx.Response(200, json=obj) if obj else httpx.Response(404)


@pytest.fixture
async def system(pool, tmp_path):
    org, workspace, cluster, instance = [str(uuid4()) for _ in range(4)]
    data = {
        "version": 1,
        "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/workspace",
        "endpoint": "https://workspace.example",
        "namespace": "tenant-a",
        "provider_account_id": "123456789012",
        "region": "us-east-1",
        "image_id": "ami-0123456789abcdef0",
        "instance_type": "g5.xlarge",
        "node_count": 1,
        "disk_size": 100,
        "instance_profile": "approved",
        "vpc_name": "workspace",
        "security_group": "approved",
        "service_cidr": "172.20.0.0/16",
        "certificate_authority": "public-ca",
        "workload": {
            "kind": "batch",
            "name": "approved-work",
            "image": "registry.example/work@sha256:" + "a" * 64,
            "command": ["run"],
            "args": [],
            "gpu_count": 1,
            "cpu": "1000m",
            "memory": "4Gi",
            "port": None,
            "auth_secret": None,
        },
    }
    cloud = Cloud(data)
    kube = Kubernetes(cloud)
    async with pool.acquire() as c:
        await c.execute("""
            CREATE TABLE organizations(id uuid PRIMARY KEY,adp_org_id text UNIQUE);
            CREATE TABLE clusters(id uuid PRIMARY KEY,org_id uuid,eks_cluster_arn text,endpoint text,status text);
            CREATE TABLE workspaces(id uuid PRIMARY KEY,org_id uuid,cluster_id uuid,namespace_name text,status text,shared_cluster_id uuid);
            CREATE TABLE observation_leases(scope text PRIMARY KEY,holder text,expires_at timestamptz);
            CREATE TABLE controller_executions(operation_id text PRIMARY KEY,org_id uuid,workspace_id uuid,controller_holder text,assignment json,expires_at timestamptz);
            CREATE TABLE controller_provider_requests(idempotency_key text PRIMARY KEY,operation_id text,org_id text,workspace_id text,cluster_name text,operation_kind text,request_id text,region text);
            CREATE TABLE controller_capacity(org_id text,workspace_id text,cluster_name text,state text,PRIMARY KEY(org_id,workspace_id,cluster_name));
            CREATE TABLE controller_execution_accounting(operation_id text PRIMARY KEY,org_id uuid,workspace_id uuid,observation json);
        """)
        await c.execute(
            "INSERT INTO organizations VALUES($1::text::uuid,$2)", org, "adp-org"
        )
        await c.execute(
            "INSERT INTO clusters VALUES($1::text::uuid,$2::text::uuid,$3,$4,'Ready')",
            cluster,
            org,
            data["cluster_arn"],
            data["endpoint"],
        )
        await c.execute(
            "INSERT INTO workspaces(id,org_id,cluster_id,namespace_name,status) VALUES($1::text::uuid,$2::text::uuid,$3::text::uuid,'tenant-a','active')",
            workspace,
            org,
            cluster,
        )
        await c.execute(
            "INSERT INTO observation_leases VALUES($1,$2,$3)",
            "controller_management/" + org,
            "controller:" + instance,
            datetime.now(UTC) + timedelta(seconds=45),
        )
    verified = {}

    class Authority:
        revoked = False

        async def resolve(self, operation_id):
            if self.revoked or operation_id not in verified:
                raise PermissionError("revoked")
            return verified[operation_id]

        async def delivery_role(self, operation):
            await self.preflight(operation)
            return {"role_arn": "arn:aws:iam::123456789012:role/approved"}

        async def preflight(self, operation):
            if self.revoked:
                raise PermissionError("credential revoked")

    authority = Authority()
    token_file = tmp_path / "sky-token"
    token_file.write_text("provider-secret-" + "a" * 32)

    def transport(request):
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"status": "healthy"})
        if path == "/internal/provider-identity":
            return httpx.Response(
                200,
                json={
                    "version": 1,
                    "provider": "aws",
                    "account_id": cloud.sky_account,
                    "role_arn": cloud.sky_role,
                    "credential_source": cloud.sky_source,
                    "allocation_tags": ["instance", "volume", "network-interface"],
                    **(
                        {
                            "regional_binding_guard": 1,
                            "capacity_constraints": ["physical_gpu_limit"],
                        }
                        if getattr(cloud, "network_mode", False)
                        else {}
                    ),
                },
            )
        if path == "/launch":
            payload = json.loads(request.content)
            assert isinstance(payload["task"], str)
            task = json.loads(payload["task"])
            cloud.cluster_name = task["name"]
            assert "SSM_ACTIVATION" not in payload["task"]
            assert "nodeadm init" in task["setup"] and "remaining=" in task["run"]
            assert payload["retry_until_up"] is False
            cloud.launches += 1
            cloud.exists = True
            cloud.ever_created = True
            if cloud.lose_launch_response:
                raise httpx.ReadError("simulated response loss")
        elif path == "/down":
            assert json.loads(request.content)["purge"] is False
            cloud.exists = False
        elif path == "/api/status":
            if getattr(cloud, "network_mode", False):
                assert cloud.network_ready(), (
                    "private network must precede launch completion"
                )
            if cloud.lose_status_response:
                raise httpx.ReadError("simulated status loss")
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
            200, json=None, headers={"X-Skypilot-Request-ID": "request-" + str(uuid4())}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
    sky = SkyPilot("https://sky.example", token_file, http)
    provider = Provider(
        sky=sky, workspace=kube, domain_pool=pool, execution_pool=pool, session=cloud
    )
    instance_file = tmp_path / "instance"
    instance_file.write_text(instance)
    registry = AssignmentRegistry(
        domain_pool=pool,
        execution_pool=pool,
        authority=authority,
        instance_file=instance_file,
        token_dir=tmp_path / "tokens",
        submitter_id="controller",
        validate_plan=provider.validate_plan,
    )
    provider.registry = registry
    finalizer = Finalizer(provider, registry)
    server = ControllerRPCServer(
        connect=pool.acquire,
        provider_call=provider,
        authenticate=registry.authenticate,
        after_step=finalizer,
    )

    async def admit(
        action,
        key=None,
        *,
        extra_parameters=None,
        admitted_request=None,
        prepare_registration=None,
    ):
        actor = principal(org=org, workspace=workspace, subject="invocation#1")
        name = (
            "sp-"
            + hashlib.sha256(
                json.dumps([org, workspace, "allocation"]).encode()
            ).hexdigest()[:32]
        )
        verbs = (
            ["launch", "status", "deploy", "status"]
            if action == "provision"
            else ["delete_cluster"]
        )
        steps = [
            {
                "step_id": str(i + 1),
                "provider": "aws",
                "operation_kind": verb,
                "target": name,
            }
            for i, verb in enumerate(verbs)
        ]
        req = OperationRequest(
            action=action,
            idempotency_key=key or action,
            parameters={
                "allocation_id": "allocation",
                "controller_plan": json.dumps(data),
                "execution_steps": json.dumps(steps),
                "provider": "aws",
                "provider_account_id": "123456789012",
                "credential_id": "approved",
                "credential_service": "aws",
                "credential_label": "selected",
                **(extra_parameters or {}),
            },
        )
        if admitted_request is not None:
            req = admitted_request
        async with pool.acquire() as c:
            admitted = await admit_paid(OperationStore(), c, actor, req)
            # The outbox helper's placeholder digest is insufficient for the
            # protected paid recovery selector, which rechecks original approval.
            await c.execute(
                "UPDATE harness_approval_consumption SET plan_digest=$2 WHERE operation_id=$1",
                admitted.record.operation_id,
                admitted.record.plan_digest,
            )
            lease = await acquire(
                c,
                operation_id=admitted.record.operation_id,
                holder=actor.subject,
                attempt_id="attempt-" + action,
            )
        op = VerifiedOperation(
            ExecutionGrant(actor, lease),
            admitted.record.job_id,
            admitted.record.plan_digest,
            admitted.record.request_payload,
            "confirmed",
            4,
            3600,
            5000000,
        )
        if prepare_registration is not None:
            async with pool.acquire() as c:
                await prepare_registration(c, op)
        verified[lease.operation_id] = op
        # The trusted service only reaches execution for operations its run was
        # actually granted, so the grant is delivered alongside each admission.
        registry.handoffs.update(granted(op))
        name = await registry.publish(lease.operation_id)
        token = (tmp_path / "tokens" / name).read_text()
        return op, token

    yield pool, admit, server, cloud, kube, registry, authority
    await http.aclose()


@pytest.mark.parametrize("leaked_volume", [False, True])
async def test_real_governance_inventory_and_full_cleanup_decision(
    system, leaked_volume
):
    pool, admit, server, cloud, kube, registry, _ = system
    provision, token = await admit("provision")
    for step in ["1", "2", "3", "4"]:
        result = await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": step}}
        )
        assert result[1] == "settle"
    assert cloud.launches == 1
    async with pool.acquire() as c:
        assert (
            await c.fetchval(
                "SELECT state FROM harness_operations WHERE operation_id=$1",
                provision.grant.lease.operation_id,
            )
            == "succeeded"
        )
        accounting = json.loads(
            await c.fetchval(
                "SELECT observation::text FROM controller_execution_accounting"
            )
        )
        progress = await confirmed_plan_progress(c, provision.grant.lease.operation_id)
        assert progress is PlanProgress.COMPLETE, (
            progress,
            [
                dict(r)
                for r in await c.fetch(
                    "SELECT idempotency_key,stage,outcome,operation_kind FROM harness_provider_call_intent"
                )
            ],
        )
        assert accounting["reason"] != "admitted plan accounting pending", json.dumps(
            accounting
        )
        assert (
            accounting["exposure"] == "active" and not accounting["release_permitted"]
        ), json.dumps(accounting)
        assert len(accounting["call_dispositions"]) == 4
        assert await c.fetchval("SELECT count(*) FROM harness_allocation_resource") == 4
    cloud.leaked_volume = leaked_volume
    retirement, token = await admit("teardown")
    result = await server.dispatch(
        {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
    )
    assert result[1] == "settle"
    async with pool.acquire() as c:
        accounting = json.loads(
            await c.fetchval(
                "SELECT observation::text FROM controller_execution_accounting WHERE operation_id=$1",
                retirement.grant.lease.operation_id,
            )
        )
        assert accounting["release_permitted"] is (not leaked_volume)
        assert accounting["exposure"] == ("active" if leaked_volume else "none")
        assert accounting["resource_dispositions"]["vol-0123456789abcdef0"] == (
            "settle" if leaked_volume else "release"
        )
    assert cloud.launches == 1 and not kube.stored


async def test_revocation_between_steps_prevents_workload_creation(system):
    _, admit, server, cloud, kube, _, authority = system
    _, token = await admit("provision")
    await server.dispatch(
        {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
    )
    authority.revoked = True
    with pytest.raises(PermissionError):
        await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": "2"}}
        )
    assert cloud.launches == 1 and not kube.stored


async def test_concurrent_controller_requests_do_not_exhaust_connection_pool(system):
    _, admit, server, cloud, _, _, _ = system
    _, token = await admit("provision")
    request = {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
    # More requests than the real PostgreSQL pool has connections. They must
    # queue before borrowing a connection and reuse the single durable effect.
    results = await asyncio.wait_for(
        asyncio.gather(*(server.dispatch(request) for _ in range(24))), timeout=20
    )
    assert all(result[1] == "settle" for result in results)
    assert cloud.launches == 1


async def test_retired_capacity_cannot_be_recreated_by_a_new_admitted_operation(system):
    pool, admit, server, cloud, _, _, _ = system
    _, token = await admit("provision")
    for step in ("1", "2", "3", "4"):
        await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": step}}
        )
    _, token = await admit("teardown")
    await server.dispatch(
        {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
    )
    _, token = await admit("provision", key="recreate")
    with pytest.raises(ProviderCallRefused, match="sealed"):
        await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
        )
    assert cloud.launches == 1
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval("SELECT state FROM controller_capacity")
            == "retired"
        )


@pytest.mark.parametrize("identity_change", ["account", "role", "credential-source"])
async def test_foreign_backend_identity_refuses_before_launch(system, identity_change):
    _, admit, server, cloud, _, _, _ = system
    _, token = await admit("provision")
    if identity_change == "account":
        cloud.sky_account = "999999999999"
    elif identity_change == "role":
        cloud.sky_role = "arn:aws:iam::123456789012:role/other"
    else:
        cloud.sky_source = "other_source"
    with pytest.raises(OperationRefused):
        await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
        )
    assert cloud.launches == 0


async def test_lost_launch_reply_retains_resources_without_repeating_creation(system):
    pool, admit, server, cloud, _, _, _ = system
    _, token = await admit("provision")
    cloud.lose_launch_response = True
    result = await server.dispatch(
        {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
    )
    assert result[1] == "retain"
    with pytest.raises(ProviderCallRefused):
        await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
        )
    assert cloud.launches == 1
    async with pool.acquire() as connection:
        assert (
            await connection.fetchval(
                "SELECT count(*) FROM harness_allocation_resource"
            )
            == 3
        )
        assert (
            await connection.fetchval("SELECT state FROM controller_capacity")
            == "creating"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("node_count", 17),
        ("provider_account_id", "999999999999"),
        ("namespace", "foreign"),
    ],
)
async def test_unapproved_or_unbounded_plan_cannot_publish_assignment(
    system, field, value
):
    _, admit, _, cloud, _, registry, _ = system
    cloud.data[field] = value
    with pytest.raises(OperationRefused):
        await admit("provision")
    assert not registry.tokens and cloud.launches == 0


async def test_disk_handoff_deleted_during_last_cancellation_read_prevents_launch(
    system, tmp_path
):
    from contextlib import asynccontextmanager
    from handoff_support import handoff_file

    pool, admit, server, cloud, _, registry, _ = system
    _, token = await admit("provision")
    path = handoff_file(registry, tmp_path / "handoff.json")
    reads = []

    class Connection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        async def fetchrow(self, sql, *args):
            result = await self.connection.fetchrow(sql, *args)
            if sql.startswith("SELECT cancel_requested_at FROM harness_operations"):
                reads.append(sql)
                path.unlink()
            return result

    class Pool:
        @asynccontextmanager
        async def acquire(self):
            async with pool.acquire() as connection:
                yield Connection(connection)

    server._after_step.provider.execution_pool = Pool()
    with pytest.raises(OperationRefused):
        await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
        )
    assert reads, "the final awaited cancellation read must actually be reached"
    assert cloud.launches == 0
