"""Trusted AWS/SkyPilot hook. All worker effects enter through OperationExecutor."""

import asyncio
import hashlib
import json
from datetime import UTC, datetime

import boto3
from harness_jobs.execution import CallOutcome
from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.identity import OperationRefused
from harness_jobs.store import OperationStore

from .plan import Plan


class Provider:
    def __init__(self, *, sky, workspace, domain_pool, execution_pool, session=None):
        self.sky, self.workspace = sky, workspace
        self.domain_pool, self.execution_pool = domain_pool, execution_pool
        self.session = session or boto3.Session()
        self.registry = None

    async def validate_plan(self, operation, target):
        Plan.read(operation, target)
        await self.workspace.verify(operation, target)
        health = await self.sky.request("GET", "/api/health")
        return {
            "skypilot_healthy": health.status_code == 200,
            "checked_at": datetime.now(UTC).isoformat(),
        }

    async def session_for(self, operation, plan):
        # Re-read the admitted vault role, never select a provider from ambient
        # profiles. Temporary AWS credentials stay in this trusted process only.
        role = await self.registry.authority.delivery_role(operation)

        def resolve():
            sts = self.session.client("sts", region_name=plan.data["region"])
            identity = sts.get_caller_identity()
            expected = f"arn:aws:sts::{plan.data['provider_account_id']}:assumed-role/{role['role_arn'].rsplit('/', 1)[-1]}/"
            if identity.get("Arn", "").startswith(expected):
                return self.session
            arguments = {
                "RoleArn": role["role_arn"],
                "RoleSessionName": "sp-"
                + hashlib.sha256(
                    operation.grant.lease.operation_id.encode()
                ).hexdigest()[:32],
                "DurationSeconds": 900,
            }
            if role.get("external_id"):
                arguments["ExternalId"] = role["external_id"]
            credentials = sts.assume_role(**arguments)["Credentials"]
            return boto3.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
                region_name=plan.data["region"],
            )

        return await asyncio.to_thread(resolve), role["role_arn"]

    async def cloud(self, operation, plan):
        data = plan.data
        session, role = await self.session_for(operation, plan)

        def inspect():
            region = data["region"]
            sts = session.client("sts", region_name=region).get_caller_identity()
            eks = session.client("eks", region_name=region)
            cluster = eks.describe_cluster(name=data["cluster_arn"].split("/")[-1])[
                "cluster"
            ]
            ec2 = session.client("ec2", region_name=region)
            vpcs = ec2.describe_vpcs(
                Filters=[{"Name": "tag:Name", "Values": [data["vpc_name"]]}]
            )["Vpcs"]
            groups = ec2.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [data["security_group"]]},
                    {
                        "Name": "vpc-id",
                        "Values": [cluster["resourcesVpcConfig"]["vpcId"]],
                    },
                ]
            )["SecurityGroups"]
            profile = session.client("iam").get_instance_profile(
                InstanceProfileName=data["instance_profile"]
            )["InstanceProfile"]
            if len(profile["Roles"]) != 1:
                raise OperationRefused("node instance role ambiguous")
            access = eks.describe_access_entry(
                clusterName=cluster["name"], principalArn=profile["Roles"][0]["Arn"]
            )["accessEntry"]
            if (
                sts["Account"] != data["provider_account_id"]
                or cluster["arn"] != data["cluster_arn"]
                or cluster["endpoint"] != data["endpoint"]
                or cluster["status"] != "ACTIVE"
                or cluster["certificateAuthority"]["data"]
                != data["certificate_authority"]
                or cluster["kubernetesNetworkConfig"].get("serviceIpv4Cidr")
                != data["service_cidr"]
                or len(vpcs) != 1
                or vpcs[0]["VpcId"] != cluster["resourcesVpcConfig"]["vpcId"]
                or len(groups) != 1
                or access["type"] != "EC2_LINUX"
                or profile["Arn"].split(":")[4] != data["provider_account_id"]
            ):
                raise OperationRefused(
                    "approved AWS/EKS prerequisites do not match provider truth"
                )

        await asyncio.to_thread(inspect)
        sky_identity = await self.sky.identity()
        if (
            sky_identity.get("version") != 1
            or sky_identity.get("provider") != "aws"
            or sky_identity.get("account_id") != data["provider_account_id"]
            or sky_identity.get("role_arn") != role
            or sky_identity.get("credential_source") != "web_identity"
            or sky_identity.get("allocation_tags")
            != ["instance", "volume", "network-interface"]
        ):
            raise OperationRefused("SkyPilot provider identity mismatch")

    async def instances(self, operation, plan, *, include_terminated=False):
        session, _ = await self.session_for(operation, plan)

        def inspect():
            ec2 = session.client("ec2", region_name=plan.data["region"])
            paginator = ec2.get_paginator("describe_instances")
            result = []
            filters = [
                {
                    "Name": "tag:ray-cluster-name",
                    "Values": [plan.cloud_cluster_name],
                },
                {
                    "Name": "instance-state-name",
                    "Values": [
                        "pending",
                        "running",
                        "stopping",
                        "stopped",
                        "shutting-down",
                    ],
                },
            ]
            if include_terminated:
                filters = filters[:1]
            for page in paginator.paginate(Filters=filters):
                result.extend(
                    instance
                    for reservation in page["Reservations"]
                    for instance in reservation["Instances"]
                )
            return result

        return await asyncio.to_thread(inspect)

    async def remember(self, call, plan, request_id=None):
        # A separate domain journal supplements the shared, already committed
        # intent with the asynchronous handle BEFORE waiting for SkyPilot. A kill
        # during the wait must not discard the only provider request reference.
        async with self.domain_pool.acquire() as connection:
            await connection.execute(
                """
                INSERT INTO controller_provider_requests
                  (idempotency_key, operation_id, org_id, workspace_id, cluster_name, operation_kind, request_id)
                VALUES ($1,$2,$3,$4,$5,$6,$7)
                ON CONFLICT (idempotency_key) DO UPDATE SET request_id=COALESCE(EXCLUDED.request_id, controller_provider_requests.request_id)
                """,
                call.idempotency_key,
                call.operation_id,
                call.org_id,
                call.workspace_id,
                plan.cluster_name,
                call.operation_kind,
                request_id,
            )

    async def __call__(self, call):
        request_id = None
        try:
            operation, target, _, _ = await self.registry.verify(
                call.operation_id, require_active=True
            )
            lease = operation.grant.lease
            if (
                call.org_id,
                call.workspace_id,
                call.job_id,
                call.attempt_id,
                call.fence_token,
            ) != (
                lease.org_id,
                lease.workspace_id,
                operation.job_id,
                lease.attempt_id,
                lease.fence_token,
            ):
                raise OperationRefused("provider call binding mismatch")
            if (
                call.operation_kind in ("launch", "deploy")
                and operation.reservation_state != "confirmed"
            ):
                raise OperationRefused("creating work requires confirmed budget")
            plan = Plan.read(operation, target)
            async with self.execution_pool.acquire() as connection:
                record = await OperationStore().get(
                    connection, operation.grant.principal, call.operation_id
                )
            steps = admitted_steps(record)
            selected = next(
                (
                    step
                    for step in steps
                    if step_key(record, step) == call.idempotency_key
                ),
                None,
            )
            if selected is None or (
                call.provider,
                call.operation_kind,
                call.target,
            ) != (selected.provider, selected.operation_kind, selected.target):
                raise OperationRefused("provider descriptor mismatch")

            async def authorize():
                current, _, _, handoff = await self.registry.verify(
                    call.operation_id, require_active=True
                )
                if current.grant.lease != lease:
                    # Lease renewal can change expiry, but not its authority tuple.
                    a, b = current.grant.lease, lease
                    if (a.operation_id, a.holder, a.attempt_id, a.fence_token) != (
                        b.operation_id,
                        b.holder,
                        b.attempt_id,
                        b.fence_token,
                    ):
                        raise OperationRefused("provider authority changed")
                async with self.execution_pool.acquire() as connection:
                    row = await connection.fetchrow(
                        "SELECT cancel_requested_at FROM harness_operations WHERE operation_id=$1",
                        call.operation_id,
                    )
                if row is None or row["cancel_requested_at"] is not None:
                    raise OperationRefused("operation cancelled")
                # The cancellation query and pool release both await. Withdrawal
                # during either must be visible before the provider mutation.
                self.registry.check_handoff(current, handoff)

            await self.cloud(operation, plan)
            reference = json.dumps({"cluster_name": plan.cluster_name})
            if call.operation_kind == "launch":
                # The allocation has exactly one creating attempt for this stable
                # capacity name. A replacement requires a newly admitted allocation.
                async with self.domain_pool.acquire() as connection:
                    inserted = await connection.fetchval(
                        """
                        INSERT INTO controller_capacity (org_id,workspace_id,cluster_name,state)
                        VALUES ($1,$2,$3,'creating') ON CONFLICT DO NOTHING RETURNING cluster_name
                        """,
                        call.org_id,
                        call.workspace_id,
                        plan.cluster_name,
                    )
                if inserted is None or await self.instances(operation, plan):
                    raise OperationRefused(
                        "capacity already exists or requires recovery"
                    )
                await self.remember(call, plan)
                await authorize()
                request_id = await self.sky.submit(
                    "/launch",
                    {
                        "task": plan.task(operation),
                        "cluster_name": plan.cluster_name,
                        "retry_until_up": False,
                        "down": True,
                        "idle_minutes_to_autostop": 0,
                        "env_vars": plan.request_environment,
                        "override_skypilot_config": {
                            "aws": {
                                "remote_identity": plan.data["instance_profile"],
                                "vpc_name": plan.data["vpc_name"],
                                "security_group_name": plan.data["security_group"],
                                "disk_encrypted": True,
                            }
                        },
                    },
                )
                await self.remember(call, plan, request_id)
                reference = json.dumps(
                    {"cluster_name": plan.cluster_name, "request_id": request_id}
                )
                if not await self.sky.complete(request_id, authorize):
                    return CallOutcome.UNKNOWN, None, reference
                instances = await self.instances(operation, plan)
                if len(instances) != plan.data["node_count"]:
                    return CallOutcome.UNKNOWN, None, reference
                return (
                    CallOutcome.SUCCEEDED,
                    "SkyPilot launch completed; join remains a separate step",
                    instances[0]["InstanceId"],
                )
            if call.operation_kind == "deploy":
                from harness_jobs.inventory import (
                    AllocationResource,
                    InventoryAuthority,
                )

                async def record_created(reference):
                    authority = InventoryAuthority(
                        connect=self.execution_pool.acquire,
                        authenticate=self.registry.authenticate,
                    )
                    async with self.execution_pool.acquire() as connection:
                        await authority.enumerate_resources(
                            connection,
                            lease,
                            resources=(
                                AllocationResource(
                                    reference,
                                    "aws",
                                    reference,
                                    "workspace_object",
                                    frozenset({call.idempotency_key}),
                                ),
                            ),
                        )

                references = await self.workspace.apply(
                    operation,
                    target,
                    plan,
                    authorize,
                    record_created=record_created
                    if "controller_deployment_id" in operation.request.parameters
                    else None,
                )
                return (
                    CallOutcome.SUCCEEDED,
                    "workspace workload created",
                    references[0],
                )
            if call.operation_kind == "status":
                async with asyncio.timeout(800):
                    while True:
                        await authorize()
                        instances = await self.instances(operation, plan)
                        if selected.step_id == "2":
                            # Native EKS provider IDs must resolve to this allocation's
                            # actual EC2 instances, not merely Ready nodes in the cluster.
                            ready = len(instances) == plan.data[
                                "node_count"
                            ] and await self.workspace.ready_nodes(
                                operation,
                                target,
                                plan,
                                {i["InstanceId"] for i in instances},
                            )
                        else:
                            known_references = None
                            if (
                                "controller_deployment_id"
                                in operation.request.parameters
                            ):
                                async with self.execution_pool.acquire() as connection:
                                    rows = await connection.fetch(
                                        "SELECT provider_reference FROM harness_allocation_resource "
                                        "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 "
                                        "AND provider='aws' AND kind='workspace_object' AND operation_id=$4",
                                        lease.org_id,
                                        lease.workspace_id,
                                        operation.request.parameters["allocation_id"],
                                        lease.operation_id,
                                    )
                                known_references = frozenset(
                                    row["provider_reference"] for row in rows
                                )
                            ready = await self.workspace.workload_ready(
                                operation,
                                target,
                                plan,
                                known_references=known_references,
                                authorize=authorize,
                            )
                        if ready:
                            # Readiness I/O can outlive its authority, just like writes.
                            await authorize()
                            return (
                                CallOutcome.SUCCEEDED,
                                "verified workspace observation",
                                None,
                            )
                        await asyncio.sleep(5)
            if call.operation_kind == "delete_cluster":
                async with self.domain_pool.acquire() as connection:
                    found = await connection.fetchval(
                        """
                        UPDATE controller_capacity SET state='retiring'
                         WHERE org_id=$1 AND workspace_id=$2 AND cluster_name=$3
                        RETURNING cluster_name
                        """,
                        call.org_id,
                        call.workspace_id,
                        plan.cluster_name,
                    )
                if found is None:
                    raise OperationRefused("capacity ownership unavailable")
                known_references = None
                if "controller_deployment_id" in operation.request.parameters:
                    async with self.execution_pool.acquire() as connection:
                        rows = await connection.fetch(
                            "SELECT provider_reference FROM harness_allocation_resource "
                            "WHERE org_id=$1 AND workspace_id=$2 AND allocation_id=$3 "
                            "AND provider='aws' AND kind='workspace_object' AND operation_id=$4",
                            call.org_id,
                            call.workspace_id,
                            operation.request.parameters["allocation_id"],
                            operation.request.parameters[
                                "controller_source_operation_id"
                            ],
                        )
                    known_references = frozenset(
                        row["provider_reference"] for row in rows
                    )
                await self.workspace.delete(
                    operation,
                    target,
                    plan,
                    authorize,
                    known_references=known_references,
                )
                await self.remember(call, plan)
                await authorize()
                request_id = await self.sky.submit(
                    "/down",
                    {
                        "cluster_name": plan.cluster_name,
                        "purge": False,
                        "env_vars": plan.request_environment,
                    },
                )
                await self.remember(call, plan, request_id)
                reference = json.dumps(
                    {"cluster_name": plan.cluster_name, "request_id": request_id}
                )
                if not await self.sky.complete(
                    request_id, authorize
                ) or await self.instances(operation, plan):
                    return CallOutcome.UNKNOWN, None, reference
                # Success records the approved removal call, not allocation release.
                # The trusted after-step hook must independently enumerate, seal and
                # assess ALL durable handles before publishing zero exposure.
                return CallOutcome.SUCCEEDED, "SkyPilot removal completed", None
            raise OperationRefused("unsupported provider action")
        except Exception:
            reference = json.dumps(
                {"cluster_name": call.target, "request_id": request_id}
            )
            return CallOutcome.UNKNOWN, None, reference
