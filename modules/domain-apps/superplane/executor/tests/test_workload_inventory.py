"""A missing original root needs complete dependent-Pod absence evidence."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from harness_jobs.inventory import AllocationResource, ResourcePresence
from superplane_executor.inventory import Finalizer
from superplane_executor.workspace import Workspace


@pytest.mark.parametrize(
    "listing,expected",
    [
        ({"items": [], "metadata": {}}, ResourcePresence.ABSENT),
        ({"items": [{"metadata": {"uid": "original-pod"}}]}, ResourcePresence.PRESENT),
        ({"items": [], "metadata": {"continue": "next"}}, ResourcePresence.UNKNOWN),
        ({"items": [], "metadata": None}, ResourcePresence.UNKNOWN),
        ({"items": [], "metadata": []}, ResourcePresence.UNKNOWN),
        ({}, ResourcePresence.UNKNOWN),
        ({"items": None}, ResourcePresence.UNKNOWN),
        ({"items": {}}, ResourcePresence.UNKNOWN),
        ({"items": [None]}, ResourcePresence.UNKNOWN),
        ({"items": [{}] * 257}, ResourcePresence.UNKNOWN),
        ([], ResourcePresence.UNKNOWN),
    ],
)
async def test_root_absence_requires_complete_bounded_pod_listing(listing, expected):
    workspace = Workspace("/unused-credentials", "https://management.example")
    workspace.request = AsyncMock(
        side_effect=[httpx.Response(404), httpx.Response(200, json=listing)]
    )
    finalizer = object.__new__(Finalizer)
    finalizer.provider = SimpleNamespace(workspace=workspace)
    ref = "kubernetes:Job:tenant:original-job:original-uid"
    resource = AllocationResource(ref, "aws", ref, "workspace_object", frozenset())
    observation = await finalizer.observe(
        None,
        {"namespace": "tenant"},
        SimpleNamespace(cluster_name="original"),
        resource,
    )
    assert observation.presence is expected
    assert workspace.request.await_args_list[-1].args[-1] == (
        "/api/v1/namespaces/tenant/pods?labelSelector="
        "superplane.ai%2Fcapacity%3Doriginal&limit=257"
    )
