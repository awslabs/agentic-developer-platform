"""Actual readiness composition refuses a Job counter without ordinary Pod proof."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from harness_jobs.identity import OperationRefused
from superplane_executor.workspace import Workspace
from test_workload_boundaries import serving_plan
from workload_support import completed_job_pod, declared_placement


@pytest.mark.parametrize(
    "change",
    [
        "none",
        "canonical_resources",
        "no_pods",
        "foreign_owner",
        "host_network",
        "host_pid",
        "wrong_image",
        "wrong_command",
        "wrong_workspace",
        "wrong_capacity",
        "wrong_selector",
        "missing_pod_ip",
        "no_node",
        "nonzero_exit",
        "wrong_gpu_request",
        "dns_none",
        "duplicate",
        "truncated",
        "replaced",
        "revoked",
        "missing-placement",
        "changed-placement",
    ],
)
async def test_governed_batch_requires_original_ordinary_pod(change):
    workspace = Workspace("/unused", "https://management.example")
    plan = serving_plan()
    plan.data["workload"]["kind"] = "batch"
    operation = SimpleNamespace(
        request=SimpleNamespace(parameters={"controller_deployment_id": "deployment"}),
        grant=SimpleNamespace(
            lease=SimpleNamespace(
                workspace_id="workspace-a",
                runtime_deadline=datetime.now(UTC) + timedelta(seconds=60),
            )
        ),
        max_runtime_seconds=60,
        plan_digest="a" * 64,
    )
    target = {"namespace": "tenant"}
    job = workspace.objects(operation, target, plan)[0]
    job["metadata"].update(uid="original-job", generation=1)
    job["status"] = {"succeeded": 1}
    pod = completed_job_pod(job)
    if change == "canonical_resources":
        for resources in pod["spec"]["containers"][0]["resources"].values():
            resources["cpu"] = "1000m"
            resources["memory"] = "1024Mi"
    elif change == "foreign_owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    elif change in {"host_network", "host_pid"}:
        pod["spec"]["hostNetwork" if change == "host_network" else "hostPID"] = True
    elif change == "wrong_image":
        pod["spec"]["containers"][0]["image"] = "unapproved:latest"
    elif change == "wrong_command":
        pod["spec"]["containers"][0]["command"] = ["true"]
    elif change == "wrong_workspace":
        pod["metadata"]["labels"]["superplane.io/workspace"] = "other-workspace"
    elif change == "wrong_capacity":
        pod["metadata"]["labels"]["superplane.ai/capacity"] = "other-allocation"
    elif change == "wrong_selector":
        pod["spec"]["nodeSelector"] = {"superplane.ai/capacity": "other-allocation"}
    elif change == "missing_pod_ip":
        pod["status"].pop("podIP")
    elif change == "no_node":
        pod["spec"].pop("nodeName")
    elif change == "nonzero_exit":
        pod["status"]["containerStatuses"][0]["state"]["terminated"]["exitCode"] = 1
    elif change == "wrong_gpu_request":
        pod["spec"]["containers"][0]["resources"]["requests"]["nvidia.com/gpu"] = "0"
    elif change == "dns_none":
        pod["spec"]["dnsPolicy"] = "None"
    reads = []
    checks = 0

    async def authorize():
        nonlocal checks
        checks += 1
        if change == "revoked" and checks == 2:
            raise OperationRefused("authority withdrawn")

    async def request(operation, target, method, path, **kwargs):
        reads.append(path)
        assert method == "GET" and "/namespaces/tenant/" in path
        if "/pods?" in path:
            items = [] if change == "no_pods" else [pod]
            if change == "duplicate":
                items.append(deepcopy(pod))
            return httpx.Response(
                200,
                json={
                    "items": items,
                    "metadata": {"continue": "next" if change == "truncated" else ""},
                },
            )
        if "/pods/" in path:
            current = deepcopy(pod)
            if change == "replaced":
                current["metadata"]["uid"] = "replacement"
            return httpx.Response(200, json=current)
        return httpx.Response(200, json=job)

    workspace.request = request
    placement_reads = 0

    async def placement(pod):
        nonlocal placement_reads
        placement_reads += 1
        original = await declared_placement(pod)
        if change == "changed-placement" and placement_reads > 1:
            return (*original, "replaced-node")
        return original

    async def ready():
        return await workspace.workload_ready(
            operation,
            target,
            plan,
            known_references={workspace.reference("Job", job)},
            authorize=authorize,
            verify_placement=None if change == "missing-placement" else placement,
        )

    if change in {"none", "canonical_resources", "no_pods", "foreign_owner"}:
        assert await ready() is (change in {"none", "canonical_resources"})
    else:
        with pytest.raises(OperationRefused):
            await ready()
    assert all("/nodes" not in path for path in reads)
