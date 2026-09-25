"""Actual provider/session/Node binding with explicitly simulated AWS and API I/O."""

from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest
from harness_jobs.identity import OperationRefused
from superplane_executor.provider import Provider
from superplane_executor.workspace import Workspace
from test_workload_boundaries import serving_plan, node_labels


class AWS:
    def __init__(self):
        self.region = None
        self.reads = []
        self.instance = {
            "InstanceId": "i-11111111111111111",
            "Placement": {"AvailabilityZone": "us-east-1a"},
            "State": {"Name": "running"},
        }
        self.fail_region = None
        self.identity = {
            "Account": "123456789012",
            "Arn": "arn:aws:sts::123456789012:assumed-role/worker/session",
        }
        self.assumptions = []

    def client(self, service, *, region_name):
        self.region = region_name
        return self

    def get_caller_identity(self):
        return deepcopy(self.identity)

    def assume_role(self, **arguments):
        self.assumptions.append(arguments)
        return {
            "Credentials": {
                "AccessKeyId": "simulated-key",
                "SecretAccessKey": "simulated-secret",
                "SessionToken": "simulated-token",
            }
        }

    def get_paginator(self, method):
        assert method == "describe_instances"
        return self

    def paginate(self, *, Filters):
        self.reads.append((self.region, Filters))
        if self.region == self.fail_region:
            raise OSError("declared region unavailable")
        instances = [deepcopy(self.instance)] if self.instance is not None else []
        return [{"Reservations": [{"Instances": instances}]}]


@pytest.fixture
def binding():
    aws = AWS()
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    plan.data.update(node_count=1, provider_account_id="123456789012")
    plan.cluster_region = "us-east-1"
    plan.cloud_cluster_name = "approved-allocation"
    operation = SimpleNamespace(
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                workspace_id="workspace-a", operation_id="original-operation"
            )
        )
    )
    target = {"cluster_arn": "arn:aws:eks:us-east-1:123456789012:cluster/fixed"}
    node = {
        "metadata": {
            "name": "allocated-node",
            "uid": "original-node-uid",
            "labels": node_labels(),
        },
        "spec": {"providerID": "aws:///us-east-1a/i-11111111111111111"},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "allocatable": {"nvidia.com/gpu": "1"},
        },
    }
    pod = {"metadata": {"uid": "original-pod"}, "spec": {"nodeName": "allocated-node"}}
    state = SimpleNamespace(
        nodes=[node], continuation="", node_reads=0, checks=0, revoke_at=None
    )

    async def request(actual_operation, actual_target, method, path, **kwargs):
        assert actual_operation is operation and actual_target is target
        assert method == "GET" and path.startswith("/api/v1/nodes?")
        state.node_reads += 1
        return httpx.Response(
            200,
            json={
                "items": deepcopy(state.nodes),
                "metadata": {"continue": state.continuation},
            },
        )

    async def delivery_role(actual):
        assert actual is operation
        return {"role_arn": "arn:aws:iam::123456789012:role/worker"}

    async def authorize():
        state.checks += 1
        if state.checks == state.revoke_at:
            raise OperationRefused("original authority revoked")

    workspace.request = request
    provider = Provider(
        sky=None,
        workspace=workspace,
        domain_pool=None,
        execution_pool=None,
        session=aws,
    )
    provider.registry = SimpleNamespace(
        authority=SimpleNamespace(delivery_role=delivery_role)
    )
    return SimpleNamespace(
        aws=aws,
        workspace=workspace,
        plan=plan,
        operation=operation,
        target=target,
        node=node,
        pod=pod,
        state=state,
        provider=provider,
        authorize=authorize,
    )


async def proof(f):
    return await f.provider.verify_pod_allocation(
        f.operation, f.target, f.plan, f.pod, f.authorize
    )


@pytest.mark.parametrize("region", ["us-east-1", "us-west-2"])
async def test_proof_joins_actual_pod_to_provider_region_az_and_node_uid(
    binding, region
):
    f = binding
    f.plan.region_bindings = [{"region": region}]
    f.aws.instance["Placement"]["AvailabilityZone"] = region + "a"
    f.node["spec"]["providerID"] = f"aws:///{region}a/i-11111111111111111"
    f.node["metadata"]["labels"].update(
        {
            "topology.kubernetes.io/region": region,
            "topology.kubernetes.io/zone": region + "a",
        }
    )
    result = await proof(f)
    assert result == (
        f.target["cluster_arn"],
        "original-pod",
        "allocated-node",
        "original-node-uid",
        f"aws:///{region}a/i-11111111111111111",
        "123456789012",
        region,
        region + "a",
        "i-11111111111111111",
    )
    assert f.aws.reads[0][1][0] == {
        "Name": "tag:ray-cluster-name",
        "Values": ["approved-allocation"],
    }
    f.node["metadata"]["resourceVersion"] = "new-heartbeat"
    assert await proof(f) == result
    f.node["metadata"]["uid"] = "recreated-node"
    assert await proof(f) != result


@pytest.mark.parametrize(
    "change",
    [
        "injected-node",
        "wrong-zone",
        "wrong-instance",
        "no-node-uid",
        "deleting-node",
        "duplicate-node",
        "pagination",
        "missing-instance",
        "stopped-instance",
        "not-ready",
        "no-gpu",
    ],
)
async def test_labels_and_selectors_never_substitute_for_actual_allocation(
    binding, change
):
    f = binding
    if change == "injected-node":
        f.pod["spec"]["nodeName"] = "foreign-node"
    elif change == "wrong-zone":
        f.node["spec"]["providerID"] = "aws:///us-west-2a/i-11111111111111111"
    elif change == "wrong-instance":
        f.node["spec"]["providerID"] = "aws:///us-east-1a/i-22222222222222222"
    elif change == "no-node-uid":
        f.node["metadata"].pop("uid")
    elif change == "deleting-node":
        f.node["metadata"]["deletionTimestamp"] = "now"
    elif change == "duplicate-node":
        f.state.nodes.append(deepcopy(f.node))
    elif change == "pagination":
        f.state.continuation = "unseen"
    elif change == "missing-instance":
        f.aws.instance = None
    elif change == "stopped-instance":
        f.aws.instance["State"]["Name"] = "stopped"
    elif change == "not-ready":
        f.node["status"]["conditions"][0]["status"] = "False"
    else:
        f.node["status"]["allocatable"] = {}
    with pytest.raises(OperationRefused):
        await proof(f)


@pytest.mark.parametrize("check", [1, 2, 3])
async def test_revocation_around_provider_and_node_reads_refuses_proof(binding, check):
    f = binding
    f.state.revoke_at = check
    with pytest.raises(OperationRefused, match="revoked"):
        await proof(f)


async def test_one_unavailable_approved_region_never_becomes_empty_success(binding):
    f = binding
    f.plan.region_bindings.append({"region": "us-west-2"})
    f.aws.fail_region = "us-west-2"
    with pytest.raises(OSError, match="region unavailable"):
        await proof(f)
    assert f.state.node_reads == 0


@pytest.mark.parametrize(
    "marker",
    [
        "membership_credential",
        "shared_membership",
        "shared_cluster_id",
        "cluster_placement",
    ],
)
async def test_shared_target_refuses_before_dedicated_provider_or_node_read(
    binding, marker
):
    from superplane_executor.results import capture

    f = binding
    f.target[marker] = "shared"
    with pytest.raises(OperationRefused, match="separate trusted Node"):
        await proof(f)
    with pytest.raises(OperationRefused, match="separate trusted Node"):
        await f.workspace.ready_nodes(f.operation, f.target, f.plan, [])
    with pytest.raises(OperationRefused, match="separate trusted Node"):
        await capture(f.provider, f.operation, f.target, f.plan, (), f.authorize)
    assert not f.aws.reads and f.state.node_reads == 0 and f.state.checks == 0


@pytest.mark.parametrize("account", ["999999999999", None])
async def test_direct_session_requires_actual_sts_account(binding, account):
    f = binding
    f.aws.identity["Account"] = account
    with pytest.raises(OperationRefused, match="session identity"):
        await proof(f)
    assert not f.aws.reads and not f.aws.assumptions
    assert f.state.node_reads == 0


@pytest.mark.parametrize(
    "role_arn",
    [
        "arn:aws:iam::999999999999:role/worker",
        "arn:aws:sts::123456789012:assumed-role/worker/session",
        "arn:aws:iam::123456789012:role/",
    ],
)
async def test_delivered_role_must_match_approved_account(binding, role_arn):
    f = binding

    async def delivery_role(operation):
        assert operation is f.operation
        return {"role_arn": role_arn}

    f.provider.registry.authority.delivery_role = delivery_role
    with pytest.raises(OperationRefused, match="delivered AWS role"):
        await proof(f)
    assert not f.aws.reads and not f.aws.assumptions
    assert f.state.node_reads == 0


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "wrong-account",
        "wrong-arn-account",
        "wrong-role",
        "empty-session",
        "missing-account",
    ],
)
async def test_inventory_uses_the_verified_assumed_session(
    binding, monkeypatch, change
):
    from superplane_executor import provider as provider_module

    f = binding
    # The ambient control-plane caller is allowed to be in a different account;
    # only the delivered and subsequently verified role may enumerate capacity.
    f.aws.identity = {
        "Account": "999999999999",
        "Arn": "arn:aws:sts::999999999999:assumed-role/control/session",
    }
    assumed = AWS()
    if change == "wrong-account":
        assumed.identity["Account"] = "999999999999"
    elif change == "wrong-arn-account":
        assumed.identity["Arn"] = (
            "arn:aws:sts::999999999999:assumed-role/worker/session"
        )
    elif change == "wrong-role":
        assumed.identity["Arn"] = "arn:aws:sts::123456789012:assumed-role/other/session"
    elif change == "empty-session":
        assumed.identity["Arn"] = "arn:aws:sts::123456789012:assumed-role/worker/"
    elif change == "missing-account":
        assumed.identity.pop("Account")
    created = []

    def session(**kwargs):
        assert kwargs == {
            "aws_access_key_id": "simulated-key",
            "aws_secret_access_key": "simulated-secret",
            "aws_session_token": "simulated-token",
            "region_name": "us-east-1",
        }
        created.append(assumed)
        return assumed

    monkeypatch.setattr(provider_module.boto3, "Session", session)
    if change == "none":
        result = await proof(f)
        assert result[5] == assumed.identity["Account"]
        assert assumed.reads and f.state.node_reads == 1
    else:
        with pytest.raises(OperationRefused, match="session identity"):
            await proof(f)
        assert not assumed.reads and f.state.node_reads == 0
    assert created == [assumed]
    assert not f.aws.reads
    assert len(f.aws.assumptions) == 1
    assert f.aws.assumptions[0]["RoleArn"] == "arn:aws:iam::123456789012:role/worker"
