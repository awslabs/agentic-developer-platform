"""Actual admission/RPC/provider wiring with PostgreSQL and declared AWS fixtures."""
# ruff: noqa: F811 - pytest fixture imports are intentionally shadowed by parameters

import base64
import hashlib
import json
import re
from pathlib import Path
from uuid import uuid4

import certifi
import pytest
from harness_jobs.identity import OperationRefused
from network_support import AWS, HOME, REMOTE, policy, schema
from superplane_executor.deployment_plan import BATCH_FIELDS, build_deployment_preview
from superplane_executor.deployment_registry import register_deployment_operation
from superplane_executor.network_plan import canonical
from test_lifecycle_postgres import (
    system,  # noqa: F401 - reuse actual registered-worker fixture
)

from tests.conftest import requires_postgres

pytestmark = requires_postgres


@pytest.mark.parametrize(
    "deny_network,deny_node_role,probe_mode",
    [
        (False, False, None),
        (True, False, None),
        (False, True, None),
        (False, False, "approved"),
        (False, False, "foreign_service"),
    ],
)
@pytest.mark.parametrize("shared_membership", [False, True])
async def test_registered_worker_networks_selected_region_before_launch_success(
    system, deny_network, deny_node_role, shared_membership, probe_mode
):
    pool, admit, server, cloud, _kube, _registry, _ = system
    cloud.instance["Placement"]["AvailabilityZone"] = REMOTE + "a"
    await schema(pool)
    async with pool.acquire() as c:
        await c.execute("ALTER TABLE clusters ADD COLUMN workspace_id uuid")
        await c.execute(
            "UPDATE clusters c SET workspace_id=w.id FROM workspaces w WHERE w.cluster_id=c.id"
        )
        cluster = await c.fetchval("SELECT id::text FROM clusters")
        await c.execute("""CREATE TABLE cluster_memberships(
            workspace_id uuid,org_id uuid,cluster_id uuid,generation text,
            namespace text,state text);
            INSERT INTO cluster_memberships
            SELECT id,org_id,cluster_id,repeat('a',64),namespace_name,'active'
            FROM workspaces;""")
        if not shared_membership:
            await c.execute("DROP TABLE cluster_memberships")
            await c.execute("""CREATE TABLE workspace_bootstrap_reservations(
                workspace_id text,state text,identity_json text,attempt_token text);
                CREATE TABLE workspace_bootstrap_authority(
                workspace_id text,org_id text,cluster_arn text,generation text,
                claim text,progress_json text,revoked boolean);""")
            workspace = await c.fetchrow(
                "SELECT id::text,org_id::text,namespace_name FROM workspaces"
            )
            identity = json.dumps(
                {
                    "workspace_id": workspace["id"],
                    "org_id": workspace["org_id"],
                    "namespace": workspace["namespace_name"],
                    "cluster_arn": cloud.data["cluster_arn"],
                }
            )
            await c.execute(
                "INSERT INTO workspace_bootstrap_reservations VALUES($1,'registered',$2,'test-attempt')",
                workspace["id"],
                identity,
            )
            await c.execute(
                "INSERT INTO workspace_bootstrap_authority VALUES($1,$2,$3,$4,$5,$6,true)",
                workspace["id"],
                workspace["org_id"],
                cloud.data["cluster_arn"],
                "a" * 64,
                hashlib.sha256(
                    b"superplane-workspace-bootstrap-claim:v1:test-attempt"
                ).hexdigest(),
                json.dumps({"complete": True, "retain_workspace": True}),
            )
    network = policy(cluster)
    aws = AWS()
    cloud.network_mode = True
    cloud.network_ready = lambda: bool(aws.peerings and aws.routes)
    cert = min(
        re.findall(
            r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
            Path(certifi.where()).read_text(),
            re.DOTALL,
        ),
        key=len,
    )
    ca = base64.b64encode(cert.encode()).decode()
    data = dict(cloud.data)
    for key in [
        "region",
        "image_id",
        "instance_type",
        "instance_profile",
        "vpc_name",
        "security_group",
        "certificate_authority",
    ]:
        data.pop(key)
    data.update(
        version=4,
        accelerators=["A10G:1"],
        max_gpus_per_node=4,
        cpus=4,
        memory_gb=32,
        certificate_authority_sha256=hashlib.sha256(ca.encode()).hexdigest(),
    )
    bindings = []
    for region, side in aws.sides.items():
        bindings.append(
            {
                "region": region,
                "image_id": "ami-" + ("1" * 8 if region == HOME else "2" * 8),
                "vpc_name": "approved",
                "security_group": "approved",
                "instance_profile": "approved",
                "vpc_id": side["vpc_id"],
                "security_group_id": side["security_group_id"],
                "subnet_ids": side["subnet_ids"],
            }
        )
    data["regions_sha256"] = hashlib.sha256(canonical(bindings).encode()).hexdigest()
    original_respond = aws.respond

    def respond(region, service, name, args):
        if name == "describe_cluster":
            result = original_respond(region, service, name, args)
            result["cluster"].update(
                name="workspace",
                arn=data["cluster_arn"],
                endpoint=data["endpoint"],
                status="ACTIVE",
                certificateAuthority={"data": ca},
                kubernetesNetworkConfig={"serviceIpv4Cidr": data["service_cidr"]},
            )
            return result
        if name == "get_resolver_rule":
            result = original_respond(region, service, name, args)
            result["ResolverRule"]["DomainName"] = "workspace.example."
            return result
        if name == "describe_images":
            return {"Images": [{"ImageId": args["ImageIds"][0], "State": "available"}]}
        return original_respond(region, service, name, args)

    aws.respond = respond
    original_client = cloud.client
    access_checks = []

    def client(service, **kwargs):
        region = kwargs.get("region_name", HOME)
        native = aws.client(service, region_name=region)

        class Client:
            def get_paginator(self, name):
                class Paginator:
                    def paginate(self, **arguments):
                        if name == "describe_instances":
                            include_old = not any(
                                f["Name"] == "instance-state-name"
                                for f in arguments.get("Filters", [])
                            )
                            return (
                                [cloud.describe_instances()]
                                if region == REMOTE and (cloud.exists or include_old)
                                else [{"Reservations": []}]
                            )
                        return [getattr(self_client, name)(**arguments)]

                self_client = self
                return Paginator()

            def __getattr__(self, name):
                if name == "describe_access_entry":

                    def access(**arguments):
                        access_checks.append((region, arguments))
                        assert region == HOME
                        if deny_node_role:
                            return {"accessEntry": {"type": "STANDARD"}}
                        return cloud.describe_access_entry(**arguments)

                    return access
                if name in {
                    "get_caller_identity",
                    "get_instance_profile",
                    "describe_access_entry",
                    "describe_instances",
                    "describe_volumes",
                    "describe_network_interfaces",
                    "describe_addresses",
                }:
                    return getattr(cloud, name)
                return getattr(native, name)

        return Client()

    cloud.client = client
    if deny_network:
        aws.denied = "describe_transit_gateway_route_tables"
    async with pool.acquire() as c:
        ws = await c.fetchrow("SELECT id::text,org_id::text FROM workspaces")
    target = {
        key: data[key]
        for key in ("cluster_arn", "endpoint", "namespace", "provider_account_id")
    }
    target["cluster_id"] = cluster
    workload = dict(data["workload"])
    workload_name = workload.pop("name")
    profile = {
        **target,
        **{
            key: data[key]
            for key in (
                "node_count",
                "disk_size",
                "service_cidr",
                "accelerators",
                "max_gpus_per_node",
                "cpus",
                "memory_gb",
            )
        },
        "certificate_authority": ca,
        "regions": bindings,
        "network": network,
        "workload": workload,
        "model_options": {},
        "physical_gpus": 4,
        "max_runtime_seconds": 3600,
        "max_cost_micros": 5000000,
        "credential_reference": {
            "credential_id": "approved",
            "credential_service": "aws",
            "credential_label": "selected",
        },
        "serving_auth_contract": None,
    }
    service_reads = []
    if probe_mode:
        from superplane_executor.network_probe_contract import COMMAND
        import httpx

        workload.update(
            command=COMMAND, args=[], image="registry.example/probe@sha256:" + "b" * 64
        )
        profile["network_probe"] = {
            "version": 1,
            "namespace": target["namespace"],
            "service_name": "acceptance",
            "service_uid": "approved-service",
            "port": 8080,
            "cidrs": ["172.20.1.4/32"],
        }
        original_request = _kube.request

        async def request(operation, actual_target, method, path, **kwargs):
            if path.endswith("/services/acceptance"):
                assert (
                    method == "GET"
                    and actual_target["namespace"] == target["namespace"]
                )
                service_reads.append(cloud.launches)
                return httpx.Response(
                    200,
                    json={
                        "metadata": {
                            "name": "acceptance",
                            "namespace": target["namespace"],
                            "uid": "foreign-service"
                            if probe_mode == "foreign_service"
                            else "approved-service",
                        },
                        "spec": {
                            "type": "ClusterIP",
                            "clusterIP": "172.20.1.4",
                            "ports": [{"port": 8080}],
                        },
                    },
                )
            return await original_request(
                operation, actual_target, method, path, **kwargs
            )

        _kube.request = request
    preview = build_deployment_preview(
        org_id=ws["org_id"],
        workspace_id=ws["id"],
        request_id=str(uuid4()),
        profile_id="network-test",
        profile=profile,
        target=target,
        name=workload_name,
        model_options={},
        workload_kind="batch",
        batch_options={key: workload[key] for key in BATCH_FIELDS},
    )

    async def register(c, operation):
        # These fixture tables model the API's already-committed intent and paid
        # envelope. The production registration adapter checks all their joins.
        await c.execute("""ALTER TABLE workspaces ADD COLUMN aws_account_id uuid;
            CREATE TABLE cloud_accounts(id uuid,org_id uuid,account_identifier text,provider text,status text);
            INSERT INTO cloud_accounts SELECT id,org_id,'123456789012','aws','Active' FROM workspaces;
            UPDATE workspaces SET aws_account_id=id;
            CREATE TABLE operation_budget_reservations(reservation_id text,org_id text,workspace_id text,
                job_id text,attempt_id text,max_resource_units bigint,max_runtime_seconds bigint,max_cost_micros bigint,state text);
            INSERT INTO operation_budget_reservations
            SELECT a.reservation_id,a.org_id,a.workspace_id,o.job_id,o.attempt_id,a.max_resource_units,a.max_runtime_seconds,a.max_cost_micros,'confirmed'
            FROM harness_approval_consumption a JOIN harness_operations o USING(operation_id);
            CREATE TABLE deployments(id uuid,org_id uuid,workspace_id uuid,cluster_id uuid,
                namespace text,name text,operation_id text,operation_target_json text,operation_request_json text,
                controller_request_payload text,controller_approval_id text,workload_kind text,desired_replicas int,gpu_per_replica int,
                model_name text,precision text,serving_framework text,tensor_parallel_size int,max_model_len int);
            CREATE TABLE controller_deployment_operations(operation_id text, deployment_id text,org_id text,workspace_id text,
                action text,request_id text,allocation_id text,source_operation_id text,plan_digest text,request_sha256 text,target_sha256 text);
        """)
        approval = await c.fetchval(
            "SELECT approval_id FROM harness_approval_consumption WHERE operation_id=$1",
            operation.grant.lease.operation_id,
        )
        await c.execute(
            """INSERT INTO deployments(id,org_id,workspace_id,cluster_id,namespace,name,operation_id,
            operation_target_json,operation_request_json,controller_request_payload,controller_approval_id,
            workload_kind,desired_replicas,gpu_per_replica)
            VALUES($1::text::uuid,$2::text::uuid,$3::text::uuid,$4::text::uuid,$5,$6,$7,$8,$9,$10,$11,'batch',1,1)""",
            preview.deployment_id,
            ws["org_id"],
            ws["id"],
            cluster,
            data["namespace"],
            workload_name,
            preview.request.idempotency_key,
            canonical(preview.deployment_target),
            canonical(preview.deployment_request),
            operation.request_payload,
            approval,
        )
        await register_deployment_operation(
            c,
            operation_id=operation.grant.lease.operation_id,
            org_id=ws["org_id"],
            workspace_id=ws["id"],
            deployment_id=preview.deployment_id,
        )

    operation, token = await admit(
        "provision", admitted_request=preview.request, prepare_registration=register
    )
    if probe_mode:
        from superplane_executor.network_probe_contract import for_operation
        from superplane_executor.plan import Plan

        contract = for_operation(operation, Plan.read(operation, target))
        assert contract["org_id"] == ws["org_id"]
        assert contract["workspace_id"] == ws["id"]
        assert contract["service_uid"] == "approved-service"
        assert preview.deployment_request["args"] == []
    if deny_node_role or probe_mode == "foreign_service":
        # The provider returns UNKNOWN; Harness retains the original unsettled
        # intent for recovery rather than terminalizing it as unresolved. The
        # finalizer refuses empty inventory, not the prerequisite exception.
        with pytest.raises(
            OperationRefused, match="provider resource inventory is not established"
        ):
            await server.dispatch(
                {
                    "token": token,
                    "method": "execute_step",
                    "arguments": {"step_id": "1"},
                }
            )
        if deny_node_role:
            assert access_checks
        else:
            # Service proof runs before cloud/node-role checks and before spend.
            assert service_reads == [0] and not access_checks
        assert cloud.launches == 0
        assert not cloud.exists and not cloud.ever_created
        assert not aws.attachments and not aws.peerings and not aws.routes
        async with pool.acquire() as c:
            intent = await c.fetchrow(
                "SELECT stage,outcome,provider_ref FROM harness_provider_call_intent WHERE operation_id=$1",
                operation.grant.lease.operation_id,
            )
            assert intent is not None
            assert intent["stage"] == "intended" and intent["outcome"] is None
            assert json.loads(intent["provider_ref"])["request_id"] is None
            assert (
                await c.fetchval(
                    "SELECT count(*) FROM controller_provider_requests WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
                == 0
            )
    elif deny_network:
        with pytest.raises(
            OperationRefused, match="provider resource inventory is not established"
        ):
            await server.dispatch(
                {
                    "token": token,
                    "method": "execute_step",
                    "arguments": {"step_id": "1"},
                }
            )
        assert cloud.launches == 0
        assert any(call[1] == aws.denied for call in aws.calls)
        assert not aws.attachments and not aws.peerings and not aws.routes
    else:
        result = await server.dispatch(
            {"token": token, "method": "execute_step", "arguments": {"step_id": "1"}}
        )
        assert result[1] == "settle"
        assert cloud.launches == 1 and aws.peerings and aws.routes
        if probe_mode:
            assert service_reads == [0]
        async with pool.acquire() as c:
            assert (
                await c.fetchval(
                    "SELECT compute_region FROM controller_network_completion WHERE operation_id=$1",
                    operation.grant.lease.operation_id,
                )
                == REMOTE
            )
            assert (
                await c.fetchval(
                    "SELECT count(*) FROM harness_allocation_resource WHERE kind='network_dependency'"
                )
                > 0
            )
    cloud.client = original_client
