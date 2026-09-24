"""Exact-object retention through the actual HTTP/JSON-patch boundary."""

import copy
import json

import httpx
import pytest

from src.agentauth.exit_retention import FINALIZER, INVOCATION, LABEL, TENANT, ExitRetentionError, PodExitRetention


@pytest.fixture
def retention(tmp_path):
    token = tmp_path / "token"
    token.write_text("gateway-token")
    state = {
        "metadata": {
            "name": "worker",
            "uid": "uid-1",
            "resourceVersion": "10",
            "finalizers": ["other.example/retain"],
            "annotations": {"keep": "annotation"},
            "labels": {"keep": "label"},
        },
        "spec": {"serviceAccountName": "agent-authority-worker-sa"},
    }
    writes = []

    def transport(request):
        assert request.url.path == "/api/v1/namespaces/adp-agents/pods/worker"
        assert request.headers["Authorization"] == "Bearer gateway-token"
        if request.method == "GET":
            return httpx.Response(200, json=copy.deepcopy(state))
        assert request.method == "PATCH"
        assert request.headers["Content-Type"] == "application/json-patch+json"
        patch = json.loads(request.content)
        writes.append(patch)
        assert patch[:2] == [
            {"op": "test", "path": "/metadata/uid", "value": "uid-1"},
            {"op": "test", "path": "/metadata/resourceVersion", "value": "10"},
        ]
        if state.get("conflict"):
            return httpx.Response(409)
        for operation in patch[2:]:
            assert operation["op"] == "add"
            state["metadata"][operation["path"].split("/")[-1]] = operation["value"]
        return httpx.Response(200, json=state)

    client = httpx.Client(base_url="https://kubernetes.default.svc", transport=httpx.MockTransport(transport))
    yield PodExitRetention(client=client, namespace="adp-agents", service_account="agent-authority-worker-sa", token_path=token), state, writes
    client.close()


def args(**overrides):
    return {"name": "worker", "uid": "uid-1", "invocation_id": "run-1", "tenant_id": "tenant-1", **overrides}


def test_retain_and_release_preserve_other_controllers_metadata(retention):
    control, state, writes = retention
    original = copy.deepcopy(state)
    control.retain(**args())
    assert state["metadata"]["finalizers"] == ["other.example/retain", FINALIZER]
    assert state["metadata"]["annotations"][INVOCATION] == "run-1"
    assert state["metadata"]["annotations"][TENANT] == "tenant-1"
    assert state["metadata"]["labels"][LABEL] == "true"
    # Deletion may start after retention. It must not defeat idempotent retry.
    state["metadata"]["deletionTimestamp"] = "2026-09-24T10:00:00Z"
    control.retain(**args())
    assert len(writes) == 1
    control.release(**args())
    del state["metadata"]["deletionTimestamp"]
    assert state == original
    control.release(**args())
    assert len(writes) == 2


@pytest.mark.parametrize("mutation", ["uid", "service-account", "deleting", "foreign-run", "foreign-tenant", "incomplete"])
def test_retention_refuses_unsafe_identity_before_patch(retention, mutation):
    control, state, writes = retention
    metadata = state["metadata"]
    if mutation == "uid":
        metadata["uid"] = "replacement"
    elif mutation == "service-account":
        state["spec"]["serviceAccountName"] = "other-worker"
    elif mutation == "deleting":
        metadata["deletionTimestamp"] = "2026-09-24T10:00:00Z"
    elif mutation == "foreign-run":
        metadata["annotations"][INVOCATION] = "another-run"
    elif mutation == "foreign-tenant":
        metadata["annotations"][TENANT] = "another-tenant"
    else:
        metadata["finalizers"].append(FINALIZER)
    with pytest.raises(ExitRetentionError):
        control.retain(**args())
    assert writes == []


@pytest.mark.parametrize("operation", ["retain", "release"])
def test_concurrent_update_is_not_reported_as_success(retention, operation):
    control, state, writes = retention
    if operation == "release":
        control.retain(**args())
    state["conflict"] = True
    with pytest.raises(ExitRetentionError):
        getattr(control, operation)(**args())
    assert writes
    if operation == "release":
        assert FINALIZER in state["metadata"]["finalizers"]
    else:
        assert FINALIZER not in state["metadata"]["finalizers"]


def test_release_refuses_a_different_run(retention):
    control, state, writes = retention
    control.retain(**args())
    with pytest.raises(ExitRetentionError):
        control.release(**args(invocation_id="other-run"))
    assert len(writes) == 1
    assert FINALIZER in state["metadata"]["finalizers"]


def test_discovery_is_paged_and_includes_terminating_retained_pods(tmp_path):
    token = tmp_path / "token"
    token.write_text("gateway-token")
    pod = {
        "metadata": {
            "name": "worker",
            "uid": "uid-1",
            "finalizers": [FINALIZER],
            "deletionTimestamp": "2026-09-24T10:00:00Z",
            "annotations": {INVOCATION: "run-1", TENANT: "tenant-1"},
        },
        "spec": {"serviceAccountName": "agent-authority-worker-sa"},
    }
    foreign = copy.deepcopy(pod)
    foreign["spec"]["serviceAccountName"] = "other-sa"
    unretained = copy.deepcopy(pod)
    unretained["metadata"]["finalizers"] = []

    def transport(request):
        assert request.url.path == "/api/v1/namespaces/adp-agents/pods"
        assert request.url.params["labelSelector"] == f"{LABEL}=true"
        assert request.url.params["limit"] == "100"
        assert request.url.params["continue"] == "previous-page"
        return httpx.Response(200, json={"items": [pod, foreign, unretained], "metadata": {"continue": "next-page"}})

    with httpx.Client(base_url="https://kubernetes.default.svc", transport=httpx.MockTransport(transport)) as client:
        control = PodExitRetention(client=client, namespace="adp-agents", service_account="agent-authority-worker-sa", token_path=token)
        hints, cursor = control.discover(cursor="previous-page")
    assert hints == [args()]
    assert cursor == "next-page"


@pytest.mark.parametrize("fault", ["unavailable", "malformed-cursor"])
def test_discovery_failure_does_not_report_an_empty_success(tmp_path, fault):
    token = tmp_path / "token"
    token.write_text("gateway-token")

    def transport(request):
        if fault == "unavailable":
            return httpx.Response(503)
        return httpx.Response(200, json={"items": [], "metadata": {"continue": 123}})

    with httpx.Client(base_url="https://kubernetes.default.svc", transport=httpx.MockTransport(transport)) as client:
        control = PodExitRetention(client=client, namespace="adp-agents", service_account="worker-sa", token_path=token)
        with pytest.raises(ExitRetentionError):
            control.discover()


@pytest.mark.parametrize("override", [{"name": "../pods/other"}, {"uid": ""}, {"invocation_id": ""}, {"tenant_id": ""}])
def test_invalid_retention_identity_is_refused_without_mutation(retention, override):
    control, _, writes = retention
    with pytest.raises(ExitRetentionError):
        control.retain(**args(**override))
    assert not writes
