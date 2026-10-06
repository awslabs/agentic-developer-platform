"""Cloud transport for the actual original apply inventory producer hook."""

from types import SimpleNamespace

from harness_jobs.execution import CallOutcome, OperationExecutor
from harness_jobs.execution_rpc import ExecutionRPCServer
from harness_jobs.leases import lock_lease
from harness_jobs.store import OperationStore

from workspace_bootstrap.tests import conftest as ids
from workspace_provisioning.applied_inventory import seal_applied_inventory


async def execute_source(harness, operation, runtime):
    class Session:
        _superplane_role_arn = f"arn:aws:iam::{ids.ACCOUNT_ID}:role/fixture-provider"

        def client(self, service, **kwargs):
            assert kwargs["region_name"] == ids.REGION
            if service == "eks":
                return runtime.cloud
            assert service in {"sts", "ec2", "resourcegroupstaggingapi"}
            return self

        def get_caller_identity(self):
            return {"Account": ids.ACCOUNT_ID}

        def describe_vpcs(self, **kwargs):
            return {"Vpcs": [{"VpcId": ids.VPC_ID}]}

        def describe_addresses(self, **kwargs):
            return {"Addresses": []}

        def get_paginator(self, method):
            key = {
                "describe_instances": "Reservations",
                "describe_network_interfaces": "NetworkInterfaces",
                "describe_volumes": "Volumes",
                "get_resources": "ResourceTagMappingList",
            }[method]
            return SimpleNamespace(paginate=lambda **kwargs: [{key: []}])

    class Authority:
        async def resolve(self, operation_id):
            assert operation_id == operation.grant.lease.operation_id
            async with harness.connect() as connection:
                record = await OperationStore().get(
                    connection, operation.grant.principal, operation_id
                )
                assert record.request_payload == operation.request_payload
            return operation

        async def preflight(self, current):
            async with harness.connect() as connection, connection.transaction():
                assert await lock_lease(connection, current.grant.lease)

        async def provider_session(self, current, region, *, verify):
            assert current is operation and region == ids.REGION
            await verify()
            return Session()

    context = SimpleNamespace(
        connect=harness.connect,
        domain_connect=harness.connect,
        authority=Authority(),
        brokered_provider=True,
    )

    async def provider(_call):
        return CallOutcome.SUCCEEDED, "fixture original provider result", None

    async def after_step(_grant, result):
        await seal_applied_inventory(operation, context, result)

    executor = OperationExecutor(
        operation.grant.lease, connect=harness.connect, provider_call=provider
    )
    server = ExecutionRPCServer(
        connect=harness.connect,
        provider_call=provider,
        authenticate=lambda _: None,
        after_step=after_step,
    )
    await server.execute_step(operation.grant, executor, "apply-infrastructure")
