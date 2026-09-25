"""Real fenced Node membership and cleanup with simulated EC2/Kubernetes I/O."""

from copy import deepcopy
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from harness_jobs import OperationStore
from harness_jobs.execution import record_intent
from harness_jobs.execution_plan import admitted_steps, step_key
from harness_jobs.execution_rpc import ExecutionGrant
from harness_jobs.identity import OperationRequest, OperationRefused
from harness_jobs.inventory import (
    AllocationResource,
    InventoryAuthority,
    ResourcePresence,
)
from harness_jobs.leases import acquire, lock_lease
from superplane_executor.authority import VerifiedOperation
from superplane_executor import node_cleanup, node_inventory
from superplane_executor.plan import Plan
from superplane_executor.workspace import Workspace
from tests.conftest import admit_paid, requires_postgres
from tests.test_admission_postgres import principal

pytestmark = requires_postgres


@pytest.fixture
async def nodes(pool):
    actor = principal()
    data = {
        "version": 4,
        "region": "us-east-1",
        "regions": [{"region": "us-east-1"}],
        "provider_account_id": "123456789012",
    }
    plan = Plan(data, "original", ())
    parameters = {
        "allocation_id": "original",
        "controller_deployment_id": str(uuid4()),
        "controller_plan": json.dumps(data),
    }

    async def admit(action, source=None):
        request = OperationRequest(
            action=action,
            idempotency_key=str(uuid4()),
            parameters={
                **parameters,
                **({"controller_source_operation_id": source} if source else {}),
                "execution_steps": json.dumps(
                    [
                        {
                            "step_id": "1",
                            "provider": "aws",
                            "operation_kind": "launch"
                            if action == "provision"
                            else "delete_cluster",
                            "target": "original",
                        }
                    ]
                ),
            },
        )
        async with pool.acquire() as c:
            admitted = await admit_paid(OperationStore(), c, actor, request)
            lease = await acquire(
                c,
                operation_id=admitted.record.operation_id,
                holder=actor.subject,
                attempt_id=str(uuid4()),
            )
        operation = VerifiedOperation(
            ExecutionGrant(actor, lease),
            admitted.record.job_id,
            admitted.record.plan_digest,
            admitted.record.request_payload,
            "confirmed",
            4,
            3600,
            5000000,
        )
        return operation, admitted.record

    source, record = await admit("provision")
    key = step_key(record, admitted_steps(record)[0])
    async with pool.acquire() as c:
        await record_intent(
            c,
            source.grant.lease,
            idempotency_key=key,
            provider="aws",
            operation_kind="launch",
            target="original",
        )
    cleanup, _ = await admit("teardown", source.grant.lease.operation_id)
    target = {
        "cluster_id": str(uuid4()),
        "cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/original",
        "namespace": "tenant",
    }
    instance = {
        "InstanceId": "i-0123456789abcdef0",
        "Placement": {"AvailabilityZone": "us-east-1a"},
        "State": {"Name": "running"},
        "SuperplaneRegion": "us-east-1",
    }
    native = plan.resource_reference("instance", instance["InstanceId"], "us-east-1")
    compute = AllocationResource(native, "aws", native, "instance", frozenset({key}))
    authority = InventoryAuthority(connect=pool.acquire, authenticate=lambda _: None)
    async with pool.acquire() as c:
        await authority.enumerate_resources(c, source.grant.lease, resources=(compute,))
    node = {
        "metadata": {
            "name": "original-node",
            "uid": "original-uid",
            "resourceVersion": "1",
            "labels": {
                "superplane.ai/capacity": "original",
                "superplane.ai/workspace": actor.workspace_id,
                "topology.kubernetes.io/region": "us-east-1",
                "topology.kubernetes.io/zone": "us-east-1a",
            },
        },
        "spec": {"providerID": "aws:///us-east-1a/i-0123456789abcdef0"},
        "status": {"conditions": [{"type": "Ready", "status": "False"}]},
    }
    state = SimpleNamespace(
        node=node,
        pods=[],
        events=[],
        denied=False,
        incomplete=False,
        empty_ec2=False,
        instance=instance,
    )

    class Kube(Workspace):
        async def request(
            self, operation, target, method, path, *, body=None, headers=None
        ):
            state.events.append((method, path))
            if state.denied:
                return httpx.Response(403)
            if path.startswith("/api/v1/nodes?"):
                return httpx.Response(
                    200,
                    json={
                        "items": [state.node] if state.node else [],
                        "metadata": {"continue": "more"} if state.incomplete else {},
                    },
                )
            if "/pods?" in path:
                return httpx.Response(200, json={"items": state.pods})
            if path == "/api/v1/nodes/original-node":
                if state.node is None:
                    return httpx.Response(404)
                if method == "PATCH":
                    assert body[:2] == [
                        {
                            "op": "test",
                            "path": "/metadata/uid",
                            "value": state.node["metadata"]["uid"],
                        },
                        {
                            "op": "test",
                            "path": "/metadata/resourceVersion",
                            "value": "1",
                        },
                    ]
                    state.node["spec"]["unschedulable"] = True
                if method == "DELETE":
                    assert state.instance["State"]["Name"] == "terminated"
                    assert body["preconditions"]["uid"] == state.node["metadata"]["uid"]
                    state.node = None
                    return httpx.Response(200, json={})
                return httpx.Response(200, json=state.node)
            return httpx.Response(404)

        async def delete(self, *args, **kwargs):
            state.events.append(("DELETE", "owned-workloads"))
            state.pods = []

    async def instances(*args, **kwargs):
        return [deepcopy(state.instance)]

    class Cloud:
        def client(self, *args, **kwargs):
            return self

        def describe_instances(self, **kwargs):
            assert kwargs == {"InstanceIds": [instance["InstanceId"]]}
            return {
                "Reservations": []
                if state.empty_ec2
                else [{"Instances": [state.instance]}]
            }

    async def session_for(*args):
        return Cloud(), None

    async def authorize():
        async with pool.acquire() as c:
            if not await lock_lease(c, cleanup.grant.lease):
                raise OperationRefused("cleanup fence expired")

    provider = SimpleNamespace(
        execution_pool=pool,
        domain_pool=pool,
        workspace=Kube(),
        instances=instances,
        session_for=session_for,
        registry=SimpleNamespace(authenticate=lambda _: None),
    )
    return SimpleNamespace(
        pool=pool,
        operation=cleanup,
        source=source,
        target=target,
        plan=plan,
        provider=provider,
        state=state,
        authorize=authorize,
        compute=compute,
        key=key,
    )


async def test_notready_node_captured_then_removed_after_positive_termination(nodes):
    f = nodes
    members, resources = await node_cleanup.prepare(
        f.provider, f.operation, f.target, f.plan, f.authorize
    )
    assert len(members) == 1 and f.state.node["spec"]["unschedulable"] is True
    ref = next(iter(members))
    async with f.pool.acquire() as c:
        row = await c.fetchrow(
            "SELECT * FROM harness_allocation_resource WHERE kind='kubernetes_node'"
        )
        assert (
            row["operation_id"] == f.operation.grant.lease.operation_id
        )  # observer, not fabricated creator
        assert row["operation_keys"] == [f.key]
        assert row["provider_reference"] == ref
    await node_cleanup.drain(
        f.provider, f.operation, f.target, f.plan, members, frozenset(), f.authorize
    )
    assert f.state.node is not None
    f.state.events.append(("POST", "SkyPilot/down"))
    f.state.instance["State"]["Name"] = "terminated"
    terminated = await node_cleanup.terminated(
        f.provider, f.operation, f.plan, resources, f.authorize
    )
    await node_cleanup.remove(
        f.provider,
        f.operation,
        f.target,
        f.plan,
        members,
        resources,
        terminated,
        f.authorize,
    )
    assert f.state.node is None
    events = f.state.events
    assert (
        events.index(("PATCH", "/api/v1/nodes/original-node"))
        < events.index(("DELETE", "owned-workloads"))
        < events.index(("POST", "SkyPilot/down"))
        < events.index(("DELETE", "/api/v1/nodes/original-node"))
    )
    async with f.pool.acquire() as c:
        assert (
            await c.fetchval(
                "SELECT outcome FROM harness_provider_call_intent WHERE idempotency_key=$1",
                f.key,
            )
            is None
        )


@pytest.mark.parametrize(
    "failure", ["uid", "provider_id", "cluster", "denied", "incomplete", "fence"]
)
async def test_original_node_failure_never_authorizes_removal(nodes, failure):
    f = nodes
    members, resources = await node_cleanup.prepare(
        f.provider, f.operation, f.target, f.plan, f.authorize
    )
    if failure == "uid":
        f.state.node["metadata"]["uid"] = "replacement"
    elif failure == "provider_id":
        f.state.node["spec"]["providerID"] = "aws:///us-east-1a/i-11111111111111111"
    elif failure == "cluster":
        f.target["cluster_id"] = str(uuid4())
    elif failure == "denied":
        f.state.denied = True
    elif failure == "incomplete":
        f.state.incomplete = True
    else:
        async with f.pool.acquire() as c:
            await c.execute(
                "UPDATE harness_operation_leases SET expires_at=clock_timestamp()-interval '1 second' WHERE operation_id=$1",
                f.operation.grant.lease.operation_id,
            )
    f.state.instance["State"]["Name"] = "terminated"
    with pytest.raises(OperationRefused):
        await node_cleanup.remove(
            f.provider,
            f.operation,
            f.target,
            f.plan,
            members,
            resources,
            [f.state.instance],
            f.authorize,
        )
    assert ("DELETE", "/api/v1/nodes/original-node") not in f.state.events


async def test_empty_ec2_is_not_positive_termination(nodes):
    f = nodes
    f.state.empty_ec2 = True
    with pytest.raises(OperationRefused, match="termination"):
        await node_cleanup.terminated(
            f.provider,
            f.operation,
            f.plan,
            {f.compute.provider_reference: f.compute},
            f.authorize,
        )


async def test_node_absence_observation_cannot_accept_replacement(nodes):
    f = nodes
    members, _ = await node_cleanup.prepare(
        f.provider, f.operation, f.target, f.plan, f.authorize
    )
    resource = next(iter(members.values()))
    f.state.node["metadata"]["uid"] = "replacement"
    observed = await node_inventory.observe(
        f.provider, f.operation, f.target, f.plan, resource
    )
    assert observed.presence is ResourcePresence.UNKNOWN


async def test_namespace_dependents_drain_even_without_any_node_binding(nodes):
    f = nodes
    f.state.node = None
    f.state.pods = [
        {
            "metadata": {
                "name": "original-pod",
                "namespace": "tenant",
                "uid": "pod-uid",
                "ownerReferences": [
                    {"kind": "Job", "name": "job", "uid": "job-uid", "controller": True}
                ],
            },
            "spec": {"nodeName": "disappeared-node"},
        }
    ]
    await node_cleanup.drain(
        f.provider,
        f.operation,
        f.target,
        f.plan,
        {},
        frozenset({"kubernetes:Job:tenant:job:job-uid"}),
        f.authorize,
    )
    assert not f.state.pods
    assert ("GET", "/api/v1/namespaces/tenant/pods?limit=257") in f.state.events


async def test_unrelated_pod_on_original_node_prevents_down(nodes):
    f = nodes
    members, _ = await node_cleanup.prepare(
        f.provider, f.operation, f.target, f.plan, f.authorize
    )
    f.state.pods = [
        {
            "metadata": {
                "name": "foreign",
                "namespace": "tenant",
                "uid": "foreign-uid",
                "ownerReferences": [
                    {
                        "kind": "Job",
                        "name": "foreign",
                        "uid": "foreign-job",
                        "controller": True,
                    }
                ],
            },
            "spec": {"nodeName": "original-node"},
        }
    ]
    with pytest.raises(OperationRefused):
        await node_cleanup.drain(
            f.provider, f.operation, f.target, f.plan, members, frozenset(), f.authorize
        )
    assert ("DELETE", "owned-workloads") not in f.state.events


def test_native_node_reference_local_zone_and_full_identity_bound():
    cluster = str(uuid4())
    value = node_inventory.reference(
        cluster,
        "node",
        "uid",
        "aws:///us-east-1-bos-1a/i-0123456789abcdef0",
        "us-east-1",
    )
    assert node_inventory.decode(value)["provider_id"].endswith("/i-0123456789abcdef0")
    with pytest.raises(OperationRefused, match="bound"):
        node_inventory.reference(
            cluster,
            "n" * 253,
            "uid",
            "aws:///us-east-1a/i-0123456789abcdef0",
            "us-east-1",
        )
