"""Offline Kubernetes API fixtures for scoped, UID-fenced provider cleanup."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from l5_services.adapters.transform.authority import TransformWorkAuthority
from l5_services.adapters.transform.execution import TransformAdapter
from l5_services.adapters.transform.pod_control import TransformPodControl
from l5_services.workers.execution import AuthorityRevoked
from test_tf03_adapter import pinned


NOW = datetime(2026, 10, 29, tzinfo=UTC)
UID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
NODE_UID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class FakeKubernetesAPI:
    def __init__(self, pod, node):
        self.pod = pod
        self.node = node
        self.calls = []
        self.deleted = {"status": "Success"}

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        if path == "/api/v1/nodes/isolated-example":
            return self.node
        if method == "DELETE":
            return self.deleted
        return self.pod


def prepared():
    run, _, attempt = pinned()
    run.admission["scope"]["product_id"] = "product-a"
    intent = TransformAdapter(object())._intent(run, attempt)
    scope = {**run.admission["scope"]}
    request = {"delegation_id": "grant-a", "operation_id": run.run_id, "attempt_id": "work-a",
               "lease_owner": "owner-a", "lease_fence": 2}
    authority = TransformWorkAuthority(scope=scope, source_request=request, capture_limits={},
                                       credentials=None, identity=None, expires_at=NOW + timedelta(minutes=5),
                                       clock=lambda: NOW)
    label = sha256(f"{scope['tenant_id']}:{scope['product_id']}:{scope['project_id']}:"
                   f"{run.run_id}:{intent['attempt_token']}".encode()).hexdigest()[:40]
    node = {"metadata": {"uid": NODE_UID, "labels": {"adp.dev/transform-isolation": "v1"}}}
    pod = {"metadata": {"name": intent["executor_name"], "namespace": "isolated-transform",
                        "uid": UID, "labels": {"adp.dev/transform-work": label},
                        "finalizers": ["adp.dev/transform-termination-observed"]},
           "spec": {"nodeName": "isolated-example"}, "status": {"phase": "Running"}}
    api = FakeKubernetesAPI(pod, node)
    control = TransformPodControl(api, namespace="isolated-transform",
                                  node_name="isolated-example", node_uid=NODE_UID)
    return control, api, intent, authority


@pytest.mark.asyncio
async def test_current_work_reads_pod_identity_but_not_provider_success_or_patch():
    control, api, intent, authority = prepared()
    observed = await control.read_provider(intent=intent, authority=authority)
    assert observed == {"executor_name": intent["executor_name"], "executor_uid": UID,
                        "readback": None}
    assert await control.collect_provider(intent=intent, authority=authority) is None
    assert all(method == "GET" for method, _, _ in api.calls)
    assert all(details["timeout_seconds"] == 2 for _, _, details in api.calls)


@pytest.mark.asyncio
async def test_only_the_committed_uid_can_be_requested_for_local_termination():
    control, api, intent, authority = prepared()
    native = {"executor_name": intent["executor_name"], "executor_uid": UID}
    assert await control.request_stop(intent=intent, native_job_ref=native, authority=authority) == {
        "local_stop_requested": True, "remote_provider_stopped": False}
    assert api.calls[-1][0] == "DELETE"
    assert api.calls[-1][2]["body"]["preconditions"] == {"uid": UID}
    api.deleted = None
    assert (await control.request_stop(intent=intent, native_job_ref=native, authority=authority))[
        "local_stop_requested"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["foreign_scope", "other_work", "expired", "other_node",
                                        "other_node_uid", "other_node_label", "foreign_pod",
                                        "other_namespace", "other_pod_node", "other_uid", "missing_finalizer", "stale_fence"])
async def test_foreign_or_stale_work_cannot_read_or_stop_another_pod(change):
    control, api, intent, authority = prepared()
    native = {"executor_name": intent["executor_name"], "executor_uid": UID}
    if change == "foreign_scope":
        authority.scope["product_id"] = "foreign-product"
    elif change == "other_work":
        authority.source_request["operation_id"] = "foreign-run"
    elif change == "expired":
        authority._clock = lambda: NOW + timedelta(hours=1)
    elif change == "other_node":
        api.node = None
    elif change == "other_node_uid":
        api.node["metadata"]["uid"] = "foreign-uid"
    elif change == "other_node_label":
        api.node["metadata"]["labels"].clear()
    elif change == "foreign_pod":
        api.pod["metadata"]["labels"].clear()
    elif change == "other_namespace":
        api.pod["metadata"]["namespace"] = "foreign-namespace"
    elif change == "other_pod_node":
        api.pod["spec"]["nodeName"] = "foreign-node"
    elif change == "missing_finalizer":
        api.pod["metadata"]["finalizers"] = []
    elif change == "stale_fence":
        authority.source_request["lease_fence"] = 0
    else:
        native["executor_uid"] = "foreign-uid"
    with pytest.raises(AuthorityRevoked):
        await control.request_stop(intent=intent, native_job_ref=native, authority=authority)
    assert all(method != "DELETE" for method, _, _ in api.calls)


@pytest.mark.asyncio
async def test_absent_or_unscheduled_pod_cannot_claim_remote_stop():
    control, api, intent, authority = prepared()
    api.pod = None
    assert await control.read_provider(intent=intent, authority=authority) is None
    with pytest.raises(AuthorityRevoked):
        await control.request_stop(intent=intent, native_job_ref={"executor_uid": UID}, authority=authority)
    api.pod = deepcopy(prepared()[1].pod)
    api.pod["spec"] = {}
    assert (await control.read_provider(intent=intent, authority=authority))["readback"] is None


def test_unpinned_host_configuration_cannot_construct_control():
    control, api, _, _ = prepared()
    with pytest.raises(ValueError):
        TransformPodControl(api, namespace="../other", node_name=control.node_name, node_uid=NODE_UID)


@pytest.mark.asyncio
async def test_strict_cancel_passes_original_native_uid_and_never_claims_remote_stop():
    run, _, attempt = pinned()
    run.admission["transform_qualification"] = deepcopy(run.admission["transform_qualification"])
    run.admission["transform_qualification"]["cancel"] = {
        "status": "supported", "reason": "offline fixture", "receipt_sha256": "c" * 64}

    class StopControl:
        def __init__(self):
            self.stops = []

        async def request_stop(self, *, intent, native_job_ref, authority):
            self.stops.append(native_job_ref)
            return {"local_stop_requested": True, "remote_provider_stopped": False}

        async def read_provider(self, *, intent, authority):
            return None

    control = StopControl()
    adapter = TransformAdapter(control, require_original_patch=True)
    intent = adapter._intent(run, attempt)
    native = {"tenant_id": intent["tenant_id"], "project_id": intent["project_id"],
              "executor_name": intent["executor_name"], "executor_uid": UID,
              "native_id": "native-example", "attempt_token": intent["attempt_token"],
              "input_hash": intent["input_hash"]}
    assert (await adapter.cancel(run, native, attempt, object())).classification == "unknown"
    assert control.stops == [native]
