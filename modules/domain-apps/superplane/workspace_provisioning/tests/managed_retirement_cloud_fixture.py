"""Stateful AWS/Kubernetes/subprocess transports for the native composer test."""

from copy import deepcopy
from types import SimpleNamespace

from harness_jobs.leases import lock_lease
from harness_jobs.store import OperationStore
from superplane_bootstrap.retirement_fence import WORKLOADS

from workspace_bootstrap.tests import conftest as ids
from workspace_bootstrap.tests.test_authority_runtime_postgres import (
    RESOURCES,
    ApiError,
)
from workspace_provisioning import retirement_clients
from workspace_provisioning.process import WorkerProcesses


def transport(case, monkeypatch):
    cloud = case.runtime.cloud
    state = {"destroyed": False, "process_calls": [], "credential_reads": 0}
    describe_original = cloud.describe_cluster

    def describe_cluster(**kwargs):
        if state["destroyed"]:
            raise ApiError(404, "ResourceNotFoundException")
        return describe_original(**kwargs)

    monkeypatch.setattr(cloud, "describe_cluster", describe_cluster)
    for version, kind, resource in WORKLOADS:
        monkeypatch.setitem(
            RESOURCES, kind, (version.split("/")[0] if "/" in version else "", resource)
        )
    monkeypatch.setitem(RESOURCES, "ServiceAccount", ("", "serviceaccounts"))
    monkeypatch.setitem(RESOURCES, "Secret", ("", "secrets"))

    class Session:
        def __init__(self, role="provider"):
            self.role = role
            self._superplane_role_arn = f"arn:aws:iam::{ids.ACCOUNT_ID}:role/{role}"
            self._superplane_source_session = self
            self._superplane_external_id = None
            self._superplane_scoped_entry = lambda arn: self

        def client(self, service, **kwargs):
            if service == "eks":
                return cloud
            if service in {"sts", "iam", "ec2", "resourcegroupstaggingapi"}:
                return self
            raise AssertionError(service)

        def get_caller_identity(self):
            state["credential_reads"] += 1
            return {
                "Account": ids.ACCOUNT_ID,
                "UserId": "role-id-" + self.role + ":session",
            }

        def get_role(self, *, RoleName):
            return {
                "Role": {
                    "Arn": f"arn:aws:iam::{ids.ACCOUNT_ID}:role/{RoleName}",
                    "RoleId": "role-id-" + RoleName,
                }
            }

        def get_paginator(self, method):
            key = {
                "describe_instances": "Reservations",
                "describe_network_interfaces": "NetworkInterfaces",
                "describe_volumes": "Volumes",
                "get_resources": "ResourceTagMappingList",
            }[method]
            return SimpleNamespace(paginate=lambda **kwargs: [{key: []}])

        def describe_addresses(self, **kwargs):
            return {"Addresses": []}

        def describe_vpcs(self, **kwargs):
            return {
                "Vpcs": []
                if state["destroyed"] and not state.get("retain_vpc")
                else [{"VpcId": ids.VPC_ID}]
            }

        def describe_security_group_rules(self, **kwargs):
            # Original rule absence is an explicit provider fixture observation.
            return {"SecurityGroupRules": []}

    session = Session()

    def assume(source, *, role_arn, region, verify, **kwargs):
        verify()
        return Session(role_arn.rsplit("/", 1)[-1])

    monkeypatch.setattr(retirement_clients, "assume_session", assume)

    def kubernetes(process, outputs, *, name):
        original = cloud.kube(
            process.session._superplane_role_arn,
            case.runtime.clients.supervisor_kubernetes.client.configuration.ssl_ca_cert,
        )
        original.client.close = lambda: None
        get = original.resources.get

        def resource(**kwargs):
            underlying = get(**kwargs)

            class Resource:
                def get(self, *, name=None, namespace=None, **query):
                    if name:
                        return underlying.get(name=name, namespace=namespace)
                    underlying.check("list", namespace, None)
                    rows = [
                        deepcopy(value)
                        for (kind, ns, _name), value in cloud.objects.items()
                        if kind == kwargs["kind"]
                        and (namespace is None or namespace == ns)
                    ]
                    if query.get("label_selector"):
                        rows = [
                            body
                            for body in rows
                            if "superplane.ai/capacity"
                            in body["metadata"].get("labels", {})
                        ]
                    return {"items": rows, "metadata": {}}

                def delete(self, **values):
                    return underlying.delete(**values)

            return Resource()

        original.resources.get = resource
        return original

    monkeypatch.setattr(retirement_clients, "kubernetes_client", kubernetes)

    def process_run(self, arguments, *, timeout):
        self.verify()
        state["process_calls"].append(tuple(arguments))
        assert "--plan-file" in arguments and "--authorization" in arguments
        state["destroyed"] = True
        cloud.objects.clear()
        cloud.entries.clear()
        cloud.policies.clear()
        self.verify()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(WorkerProcesses, "run", process_run)
    operation = case.operation

    class Authority:
        async def resolve(self, identifier):
            assert identifier == operation.grant.lease.operation_id
            async with case.harness.connect() as connection:
                row = await OperationStore().get(
                    connection, operation.grant.principal, identifier
                )
                consumption = await connection.fetchrow(
                    "SELECT reservation_state FROM harness_approval_consumption WHERE operation_id=$1",
                    identifier,
                )
            assert row.request_payload == operation.request_payload
            assert consumption["reservation_state"] == "confirmed"
            return operation

        async def preflight(self, current):
            async with case.harness.connect() as connection, connection.transaction():
                assert await lock_lease(connection, current.grant.lease)

        async def provider_session(self, current, region, *, verify):
            assert current is operation and region == ids.REGION
            await verify()
            return session

    context = SimpleNamespace(
        connect=case.harness.connect,
        domain_connect=case.harness.connect,
        authority=Authority(),
        brokered_provider=True,
        policy_file=case.policy_file,
        state_root=case.state_root,
    )
    return context, state
