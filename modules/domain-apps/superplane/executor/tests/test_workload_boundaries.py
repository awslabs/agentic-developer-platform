"""Serving authentication, node identity and cancellation at multi-write boundaries."""

import base64
from types import SimpleNamespace

import httpx
import pytest
from harness_jobs.identity import OperationRefused
from superplane_executor.workspace import Workspace


def serving_plan():
    return SimpleNamespace(
        cluster_name="sp-" + "a" * 32,
        region_bindings=[{"region": "us-east-1"}],
        data={
            "node_count": 2,
            "workload": {
                "kind": "serving",
                "name": "service",
                "image": "test@sha256:" + "a" * 64,
                "command": ["serve"],
                "args": [],
                "cpu": "1",
                "memory": "1Gi",
                "gpu_count": 1,
                "auth_secret": "service-auth",
                "port": 8080,
            },
        },
    )


def node_operation():
    return SimpleNamespace(
        grant=SimpleNamespace(lease=SimpleNamespace(workspace_id="workspace-a"))
    )


def observed_instances(*ids):
    return [
        {
            "InstanceId": identity,
            "SuperplaneRegion": "us-east-1",
            "Placement": {"AvailabilityZone": "us-east-1a"},
        }
        for identity in ids
    ]


def node_labels():
    return {
        "superplane.ai/capacity": serving_plan().cluster_name,
        "superplane.ai/workspace": "workspace-a",
        "topology.kubernetes.io/region": "us-east-1",
        "topology.kubernetes.io/zone": "us-east-1a",
    }


@pytest.mark.parametrize(
    "denied,admitted,ready",
    [
        (401, 200, True),
        (403, 200, True),
        (200, 200, False),
        (401, 401, False),
        (302, 200, False),
    ],
)
async def test_serving_requires_both_denial_and_authenticated_success(
    denied, admitted, ready
):
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    seen = []
    token = "service-secret-" + "a" * 32

    async def request(operation, target, method, path, **kwargs):
        seen.append((path, kwargs.get("headers")))
        if "/deployments/" in path:
            return httpx.Response(
                200,
                json={
                    "metadata": {
                        "labels": {"superplane.ai/capacity": plan.cluster_name},
                        "generation": 2,
                    },
                    "status": {"observedGeneration": 2, "availableReplicas": 1},
                },
            )
        if "/secrets/" in path:
            return httpx.Response(
                200, json={"data": {"token": base64.b64encode(token.encode()).decode()}}
            )
        assert path.endswith("/proxy/healthz")
        headers = kwargs.get("headers")
        if headers:
            assert headers == {"X-Superplane-Token": token}
        return httpx.Response(admitted if headers else denied)

    workspace.request = request
    assert await workspace.workload_ready(None, {"namespace": "tenant"}, plan) is ready
    assert len(seen) == (4 if denied in (401, 403) else 3)


async def test_revocation_between_serving_writes_stops_before_service_creation():
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    operation = SimpleNamespace(
        request=SimpleNamespace(
            parameters={"controller_deployment_id": "deployment-1"}
        ),
        grant=SimpleNamespace(lease=SimpleNamespace(workspace_id="workspace-1")),
        plan_digest="b" * 64,
    )
    writes = []

    async def authorize():
        if writes:
            raise OperationRefused("cancelled after deployment")

    async def request(operation, target, method, path, **kwargs):
        obj = kwargs["body"]
        assert obj["kind"] == "Deployment"
        assert obj["spec"]["template"]["spec"]["securityContext"]["fsGroup"] == 65532
        assert obj["metadata"]["labels"]["superplane.io/workspace"] == "workspace-1"
        assert (
            obj["metadata"]["annotations"]["superplane.io/deployment"] == "deployment-1"
        )
        assert (
            "superplane.io/workspace"
            not in obj["spec"]["template"]["spec"]["nodeSelector"]
        )
        writes.append(path)
        return httpx.Response(
            201,
            json={
                "metadata": {
                    "namespace": "tenant",
                    "name": "service",
                    "uid": "original",
                }
            },
        )

    workspace.request = request
    recorded = []

    async def record_created(reference):
        recorded.append(reference)

    with pytest.raises(OperationRefused):
        await workspace.apply(
            operation,
            {"namespace": "tenant"},
            plan,
            authorize,
            record_created=record_created,
        )
    assert recorded == ["kubernetes:Deployment:tenant:service:original"]
    assert len(writes) == 1


@pytest.mark.parametrize(
    "ids,ready",
    [
        (["i-11111111111111111", "i-22222222222222222"], True),
        (["i-11111111111111111", "i-11111111111111111"], False),
        (["i-11111111111111111", "i-33333333333333333"], False),
    ],
)
async def test_node_join_requires_each_exact_provider_identity(ids, ready):
    workspace = Workspace("/unused", "https://management.example")

    async def request(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {"labels": node_labels()},
                        "spec": {"providerID": "aws:///us-east-1a/" + identity},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "allocatable": {"nvidia.com/gpu": "1"},
                        },
                    }
                    for identity in ids
                ]
            },
        )

    workspace.request = request
    assert (
        await workspace.ready_nodes(
            node_operation(),
            {},
            serving_plan(),
            observed_instances("i-11111111111111111", "i-22222222222222222"),
        )
        is ready
    )


@pytest.mark.parametrize(
    "allocatable,ready",
    [
        ({"nvidia.com/gpu": "1"}, True),
        ({"nvidia.com/gpu": "2"}, True),
        ({"nvidia.com/gpu": "1000m"}, True),
        ({"nvidia.com/gpu": "1e0"}, True),
        ({"nvidia.com/gpu": "500m"}, False),
        ({"nvidia.com/gpu": "-1"}, False),
        (None, False),
        ({}, False),
        ({"nvidia.com/gpu": "0"}, False),
        ({"nvidia.com/gpu": "not-a-number"}, False),
    ],
)
async def test_node_join_requires_sufficient_allocatable_gpu(allocatable, ready):
    """A Ready, correctly-identified node is not yet usable for a GPU workload
    until the device plugin has actually published allocatable nvidia.com/gpu
    capacity meeting what the workload requested (serving_plan asks for 1)."""
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    plan.data["node_count"] = 1

    async def request(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {"labels": node_labels()},
                        "spec": {"providerID": "aws:///us-east-1a/i-11111111111111111"},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "allocatable": allocatable,
                        },
                    }
                ]
            },
        )

    workspace.request = request
    assert (
        await workspace.ready_nodes(
            node_operation(), {}, plan, observed_instances("i-11111111111111111")
        )
        is ready
    )


@pytest.mark.parametrize("allocatable", [{}, None, {"nvidia.com/gpu": "-1"}])
async def test_node_join_gpu_requirement_is_skipped_for_cpu_only_workload(allocatable):
    """A batch workload with gpu_count 0 must not be blocked on GPU capacity
    that was never requested."""
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    plan.data["node_count"] = 1
    plan.data["workload"]["gpu_count"] = 0

    async def request(*args, **kwargs):
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {"labels": node_labels()},
                        "spec": {"providerID": "aws:///us-east-1a/i-11111111111111111"},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "allocatable": allocatable,
                        },
                    }
                ]
            },
        )

    workspace.request = request
    assert (
        await workspace.ready_nodes(
            node_operation(), {}, plan, observed_instances("i-11111111111111111")
        )
        is True
    )


@pytest.mark.parametrize(
    "change",
    [
        "provider_scheme",
        "provider_zone",
        "provider_instance",
        "provider_path",
        "workspace",
        "capacity",
        "zone_label",
        "region_label",
        "missing_labels",
        "unapproved_region",
        "provider_zone_region",
        "missing_placement",
        "not_ready",
        "null_spec",
        "null_status",
    ],
)
async def test_node_readiness_rejects_foreign_or_incomplete_location(change):
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    plan.data["node_count"] = 1
    instances = observed_instances("i-11111111111111111")
    node = {
        "metadata": {"labels": node_labels()},
        "spec": {"providerID": "aws:///us-east-1a/i-11111111111111111"},
        "status": {
            "conditions": [{"type": "Ready", "status": "True"}],
            "allocatable": {"nvidia.com/gpu": "1"},
        },
    }
    if change.startswith("provider_") and change != "provider_zone_region":
        node["spec"]["providerID"] = {
            "provider_scheme": "gce:///us-east-1a/i-11111111111111111",
            "provider_zone": "aws:///us-west-2a/i-11111111111111111",
            "provider_instance": "aws:///us-east-1a/i-22222222222222222",
            "provider_path": "aws://us-east-1a/i-11111111111111111",
        }[change]
    elif change in {"workspace", "capacity", "zone_label", "region_label"}:
        key = {
            "workspace": "superplane.ai/workspace",
            "capacity": "superplane.ai/capacity",
            "zone_label": "topology.kubernetes.io/zone",
            "region_label": "topology.kubernetes.io/region",
        }[change]
        node["metadata"]["labels"][key] = "foreign"
    elif change == "missing_labels":
        node["metadata"] = {}
    elif change == "unapproved_region":
        instances[0]["SuperplaneRegion"] = "us-west-2"
        instances[0]["Placement"]["AvailabilityZone"] = "us-west-2a"
        node["spec"]["providerID"] = "aws:///us-west-2a/i-11111111111111111"
        node["metadata"]["labels"].update(
            {
                "topology.kubernetes.io/region": "us-west-2",
                "topology.kubernetes.io/zone": "us-west-2a",
            }
        )
    elif change == "provider_zone_region":
        instances[0]["Placement"]["AvailabilityZone"] = "us-west-2a"
        node["spec"]["providerID"] = "aws:///us-west-2a/i-11111111111111111"
        node["metadata"]["labels"]["topology.kubernetes.io/zone"] = "us-west-2a"
    elif change == "missing_placement":
        instances[0].pop("Placement")
    elif change == "not_ready":
        node["status"]["conditions"][0]["status"] = "False"
    elif change == "null_spec":
        node["spec"] = None
    elif change == "null_status":
        node["status"] = None

    async def request(*args, **kwargs):
        return httpx.Response(200, json={"items": [node]})

    workspace.request = request
    assert await workspace.ready_nodes(node_operation(), {}, plan, instances) is False


async def test_remote_ready_node_uses_observed_region_without_retargeting_cluster():
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    plan.data["node_count"] = 1
    plan.region_bindings = [{"region": "us-east-1"}, {"region": "us-west-2"}]
    target = {"endpoint": "https://bound-east-cluster.example"}
    instances = [
        {
            "InstanceId": "i-11111111111111111",
            "SuperplaneRegion": "us-west-2",
            "Placement": {"AvailabilityZone": "us-west-2b"},
        }
    ]
    labels = {
        **node_labels(),
        "topology.kubernetes.io/region": "us-west-2",
        "topology.kubernetes.io/zone": "us-west-2b",
    }

    async def request(operation, actual_target, method, path):
        assert actual_target == target
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "metadata": {"labels": labels},
                        "spec": {"providerID": "aws:///us-west-2b/i-11111111111111111"},
                        "status": {
                            "conditions": [{"type": "Ready", "status": "True"}],
                            "allocatable": {"nvidia.com/gpu": "1"},
                        },
                    }
                ]
            },
        )

    workspace.request = request
    assert (
        await workspace.ready_nodes(node_operation(), target, plan, instances) is True
    )


@pytest.mark.parametrize("kind", ["Deployment", "Service"])
@pytest.mark.parametrize("evidence", ["original", "missing", "different", "ambiguous"])
async def test_governed_delete_checks_every_original_uid_before_first_mutation(
    kind, evidence
):
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    operation = SimpleNamespace(
        request=SimpleNamespace(
            parameters={"controller_deployment_id": "deployment-1"}
        ),
        grant=SimpleNamespace(lease=SimpleNamespace(workspace_id="workspace-1")),
        plan_digest="b" * 64,
    )
    objects = {}
    for obj in workspace.objects(operation, {"namespace": "tenant"}, plan):
        obj["metadata"] = {**obj["metadata"], "uid": "original-" + obj["kind"]}
        objects[workspace.path({"namespace": "tenant"}, obj["kind"], "service")] = obj
    known = {workspace.reference(obj["kind"], obj) for obj in objects.values()}
    prefix = f"kubernetes:{kind}:tenant:service:"
    if evidence in {"missing", "different"}:
        known = {ref for ref in known if not ref.startswith(prefix)}
    if evidence in {"different", "ambiguous"}:
        known.add(prefix + "different-uid")
    writes = []

    async def authorize():
        return None

    async def request(operation, target, method, path, *, body=None):
        obj = objects[path]
        if method == "GET":
            return httpx.Response(200, json=obj)
        assert method == "DELETE"
        assert body["preconditions"] == {"uid": obj["metadata"]["uid"]}
        writes.append(path)
        return httpx.Response(200, json={})

    workspace.request = request
    if evidence == "original":
        await workspace.delete(
            operation, {"namespace": "tenant"}, plan, authorize, known_references=known
        )
        assert len(writes) == 2
    else:
        with pytest.raises(OperationRefused, match="original workload UID"):
            await workspace.delete(
                operation,
                {"namespace": "tenant"},
                plan,
                authorize,
                known_references=known,
            )
        assert writes == []


@pytest.mark.parametrize("workload_kind", ["batch", "serving"])
@pytest.mark.parametrize("change", ["none", "missing", "ambiguous", "uid", "image"])
async def test_readiness_uses_original_uid_and_approved_image(workload_kind, change):
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    plan.data["workload"]["kind"] = workload_kind
    operation = SimpleNamespace(
        request=SimpleNamespace(
            parameters={"controller_deployment_id": "deployment-1"}
        ),
        grant=SimpleNamespace(lease=SimpleNamespace(workspace_id="workspace-1")),
        plan_digest="b" * 64,
        max_runtime_seconds=60,
    )
    from datetime import UTC, datetime, timedelta

    operation.grant.lease.runtime_deadline = datetime.now(UTC) + timedelta(seconds=60)
    target = {"namespace": "tenant"}
    objects = {}
    for obj in workspace.objects(operation, target, plan):
        obj["metadata"].update(
            uid="original-" + obj["kind"], generation=1, resourceVersion="1"
        )
        obj["status"] = {
            "succeeded": 1,
            "observedGeneration": 1,
            "availableReplicas": 1,
        }
        objects[workspace.path(target, obj["kind"], "service")] = obj
    known = {workspace.reference(obj["kind"], obj) for obj in objects.values()}
    root_kind = "Job" if workload_kind == "batch" else "Deployment"
    root = objects[workspace.path(target, root_kind, "service")]
    if change == "missing":
        known.clear()
    elif change == "ambiguous":
        known.add(f"kubernetes:{root_kind}:tenant:service:other")
    elif change == "uid":
        root["metadata"]["uid"] = "replacement"
    elif change == "image":
        root["spec"]["template"]["spec"]["containers"][0]["image"] = "foreign"
    probes = []

    async def authorize():
        return None

    async def request(operation, target, method, path, **kwargs):
        assert method == "GET"
        if path in objects:
            return httpx.Response(200, json=objects[path])
        if "/secrets/" in path:
            return httpx.Response(
                200, json={"data": {"token": base64.b64encode(b"a" * 32).decode()}}
            )
        assert path.endswith("/proxy/healthz")
        probes.append(kwargs.get("headers"))
        return httpx.Response(200 if kwargs.get("headers") else 401)

    workspace.request = request
    if change == "none":
        assert await workspace.workload_ready(
            operation, target, plan, known_references=known, authorize=authorize
        )
        assert len(probes) == (2 if workload_kind == "serving" else 0)
    else:
        with pytest.raises(OperationRefused):
            await workspace.workload_ready(
                operation, target, plan, known_references=known, authorize=authorize
            )
        assert not probes


@pytest.mark.parametrize(
    "change", ["service_uid", "selector", "port", "deployment_generation", "revoked"]
)
async def test_serving_readiness_rechecks_identity_and_authority_before_token_probe(
    change,
):
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    operation = SimpleNamespace(
        request=SimpleNamespace(
            parameters={"controller_deployment_id": "deployment-1"}
        ),
        grant=SimpleNamespace(lease=SimpleNamespace(workspace_id="workspace-1")),
        plan_digest="b" * 64,
    )
    target = {"namespace": "tenant"}
    objects = {}
    for obj in workspace.objects(operation, target, plan):
        obj["metadata"].update(
            uid="original-" + obj["kind"], generation=1, resourceVersion="1"
        )
        obj["status"] = {"observedGeneration": 1, "availableReplicas": 1}
        objects[workspace.path(target, obj["kind"], "service")] = obj
    known = {workspace.reference(obj["kind"], obj) for obj in objects.values()}
    probed = False
    authenticated = []

    async def authorize():
        if probed and change == "revoked":
            raise OperationRefused("revoked")

    async def request(operation, target, method, path, **kwargs):
        nonlocal probed
        if path in objects:
            return httpx.Response(200, json=objects[path])
        if "/secrets/" in path:
            return httpx.Response(
                200, json={"data": {"token": base64.b64encode(b"a" * 32).decode()}}
            )
        assert path.endswith("/proxy/healthz")
        if kwargs.get("headers"):
            authenticated.append(path)
            return httpx.Response(200)
        probed = True
        service = objects[workspace.path(target, "Service", "service")]
        if change == "service_uid":
            service["metadata"]["uid"] = "replacement"
        elif change == "selector":
            service["spec"]["selector"] = {"other": "workload"}
        elif change == "port":
            service["spec"]["ports"][0]["port"] = 9999
        elif change == "deployment_generation":
            objects[workspace.path(target, "Deployment", "service")]["metadata"][
                "generation"
            ] = 2
        return httpx.Response(401)

    workspace.request = request
    with pytest.raises(OperationRefused):
        await workspace.workload_ready(
            operation, target, plan, known_references=known, authorize=authorize
        )
    assert not authenticated
