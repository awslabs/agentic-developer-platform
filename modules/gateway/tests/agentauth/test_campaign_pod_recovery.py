import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from src.agentauth.campaign_pod_recovery import FINALIZER, INVOCATION, OPERATION, TENANT
from src.agentauth.exit_retention import ExitRetentionError
from src.agentauth.workload import BOOTSTRAP_AUDIENCE, KubernetesWorkloadVerifier
from tests.activity.test_campaign_recovery_routes import register, request
from tests.agentauth.test_campaign_execution import native as native_fixture
from tests.agentauth.test_campaign_execution import retirement

native = native_fixture


@pytest.fixture
def cluster(native, tmp_path):
    register(native)
    token = tmp_path / "gateway-token"
    token.write_text("test-gateway-token")
    image = "sha256:" + "a" * 64
    pods = {
        invocation: {
            "metadata": {
                "name": "worker-" + invocation,
                "uid": "pod-" + invocation,
                "namespace": "agents",
                "resourceVersion": "1",
                "finalizers": ["other.example/retain"],
                "annotations": {"preserve": "operator"},
            },
            "spec": {
                "serviceAccountName": "worker",
                "restartPolicy": "Never",
                "containers": [{"name": "agent-worker", "env": [{"name": "ADP_AGENT_AUTHORITY_ENABLED", "value": "true"}]}],
            },
            "status": {
                "phase": "Running",
                "podIP": "127.0.0.1",
                "containerStatuses": [{"name": "agent-worker", "imageID": "test-worker@" + image, "state": {"running": {}}}],
            },
        }
        for invocation in ("root", "child", "unrelated")
    }
    state = SimpleNamespace(pods=pods, writes=[], lost=None, race=None)

    def transport(message):
        assert message.headers["Authorization"] == "Bearer test-gateway-token"
        if message.url.path == "/apis/authentication.k8s.io/v1/tokenreviews":
            invocation = json.loads(message.content)["spec"]["token"]
            return httpx.Response(
                200,
                json={
                    "status": {
                        "authenticated": True,
                        "audiences": [BOOTSTRAP_AUDIENCE],
                        "user": {
                            "username": "system:serviceaccount:agents:worker",
                            "extra": {
                                "authentication.kubernetes.io/pod-name": ["worker-" + invocation],
                                "authentication.kubernetes.io/pod-uid": ["pod-" + invocation],
                            },
                        },
                    }
                },
            )
        invocation = message.url.path.rsplit("/worker-", 1)[-1]
        assert message.url.path == "/api/v1/namespaces/agents/pods/worker-" + invocation
        pod = pods.get(invocation)
        if pod is None:
            return httpx.Response(404, json={"kind": "Status", "reason": "NotFound", "details": {"name": "worker-" + invocation, "kind": "pods"}})
        if message.method == "GET":
            return httpx.Response(200, json=copy.deepcopy(pod))
        document = json.loads(message.content)
        state.writes.append((message.method, invocation, document))
        if state.race:
            state.race(pod)
            state.race = None
        metadata = pod["metadata"]
        if message.method == "PATCH":
            assert document[:2] == [
                {"op": "test", "path": "/metadata/uid", "value": "pod-" + invocation},
                {"op": "test", "path": "/metadata/resourceVersion", "value": document[1]["value"]},
            ]
            if metadata["uid"] != document[0]["value"] or metadata["resourceVersion"] != document[1]["value"]:
                return httpx.Response(409)
            for operation in document[2:]:
                assert operation["op"] == "add"
                metadata[operation["path"].split("/")[-1]] = operation["value"]
        else:
            assert message.method == "DELETE"
            assert document["gracePeriodSeconds"] == 30
            assert document["preconditions"]["uid"] == "pod-" + invocation
            assert FINALIZER in metadata["finalizers"]
            if document["preconditions"] != {"uid": metadata["uid"], "resourceVersion": metadata["resourceVersion"]}:
                return httpx.Response(409)
            metadata["deletionTimestamp"] = "2026-10-19T00:00:00Z"
        metadata["resourceVersion"] = str(int(metadata["resourceVersion"]) + 1)
        if state.lost == message.method:
            state.lost = None
            raise httpx.ReadTimeout("lost committed response", request=message)
        return httpx.Response(200, json=copy.deepcopy(pod))

    client = httpx.Client(base_url="https://kubernetes.default.svc", transport=httpx.MockTransport(transport))
    native.runtime.workloads = KubernetesWorkloadVerifier(
        client=client, image_digests=frozenset({image}), namespace="agents", service_account="worker", gateway_token_path=token
    )
    state.recovery = native.runtime.workloads.campaign_recovery
    yield state
    client.close()


def pod_arguments(operation="operation-1", invocation="root"):
    return {"name": "worker-" + invocation, "uid": "pod-" + invocation, "invocation_id": invocation, "tenant_id": "tenant", "operation_id": operation}


def exit_pod(cluster, invocation):
    cluster.pods[invocation]["status"]["phase"] = "Succeeded"
    cluster.pods[invocation]["status"]["containerStatuses"][0]["state"] = {"terminated": {"exitCode": 0}}


async def test_enrollment_retains_exit_evidence_after_lost_reply_without_revoking_credentials(native, cluster, monkeypatch):
    arguments = retirement(native)
    cluster.lost = "PATCH"
    response = await request(native, monkeypatch, arguments=arguments, action="enrollment")
    assert response.status_code == 503
    assert native.store._read("TENANT#tenant", f"CAMPAIGN_ENROLL#{arguments['operation_id']}")
    assert FINALIZER in cluster.pods["root"]["metadata"]["finalizers"]
    native.runtime.authenticate(native.credentials["root"], "root")
    replay = await request(native, monkeypatch, arguments=arguments, action="enrollment")
    assert replay.status_code == 200
    assert replay.json()["status"] == "prepared"
    assert replay.json()["version"] == arguments["expected_version"]
    assert replay.headers["cache-control"] == "no-store"
    changed = native.store._read("TENANT#tenant", "REG#root#1")
    changed["generation"] = {"N": "8"}
    native.client.put_item(TableName=native.store.table, Item=changed)
    assert (await request(native, monkeypatch, arguments=arguments, action="enrollment")).status_code == 409


async def test_enrollment_requires_bounded_grant_and_journals_before_pod_mutation(native, cluster, monkeypatch):
    retain = cluster.recovery.retain

    def verify_receipt(recovery, **kwargs):
        assert native.store._read("TENANT#tenant", f"CAMPAIGN_ENROLL#{kwargs['operation_id']}")
        return retain(**kwargs)

    monkeypatch.setattr(type(cluster.recovery), "retain", verify_receipt)
    arguments = retirement(native)
    assert (await request(native, monkeypatch, arguments=arguments, action="enrollment")).status_code == 200
    grant = native.store._read("TENANT#tenant", "GRANT#unrelated#1")
    grant["allowed_actions"]["SS"].append("dispatch")
    native.client.put_item(TableName=native.store.table, Item=grant)
    unrelated = retirement(native, "unrelated")
    assert (await request(native, monkeypatch, arguments=unrelated, action="enrollment", invocation="unrelated")).status_code == 409
    assert FINALIZER not in cluster.pods["unrelated"]["metadata"]["finalizers"]


@pytest.mark.parametrize("lost", ["PATCH", "DELETE"])
async def test_actual_http_stop_recovers_lost_replies_after_root_revocation(native, cluster, monkeypatch, lost):
    root, child = retirement(native), retirement(native, "child")
    unchanged = copy.deepcopy(cluster.pods["unrelated"])
    native.client.put_item(
        TableName=native.store.table, Item={"pk": {"S": "TENANT#tenant"}, "sk": {"S": "FINANCIAL_HOLD#fixture"}, "status": {"S": "unknown"}}
    )
    cluster.lost = lost
    first = await request(native, monkeypatch, arguments=root, action="stop")
    assert first.status_code == 503
    assert native.store._read("TENANT#tenant", "EXEC#root")["status"] == {"S": "revoked"}
    assert FINALIZER in cluster.pods["root"]["metadata"]["finalizers"]
    retry = await request(native, monkeypatch, arguments=root, action="stop")
    assert retry.status_code == 202
    assert (await request(native, monkeypatch, arguments=child, action="stop", invocation="child")).status_code == 202
    assert (await request(native, monkeypatch, arguments=root, action="stop")).status_code == 202
    assert native.store._read("TENANT#tenant", f"CAMPAIGN_EXIT#{root['operation_id']}") is None
    for invocation, arguments in (("child", child), ("root", root)):
        exit_pod(cluster, invocation)
        observed = await request(native, monkeypatch, arguments=arguments, invocation=invocation, action="stop")
        assert observed.status_code == 200
        assert observed.json()["status"] == "workload_exited"
        metadata = cluster.pods[invocation]["metadata"]
        assert metadata["finalizers"] == ["other.example/retain"]
        assert metadata["annotations"] == {"preserve": "operator"}
    assert cluster.pods["unrelated"] == unchanged
    assert native.store._read("TENANT#tenant", "FINANCIAL_HOLD#fixture")["status"] == {"S": "unknown"}


@pytest.mark.parametrize("operation", ["retain", "request_stop", "release"])
def test_uid_and_resource_version_guard_every_kubernetes_mutation(cluster, operation):
    if operation != "retain":
        cluster.recovery.retain(**pod_arguments())
    cluster.race = lambda pod: pod["metadata"].update(uid="replacement", resourceVersion="99")
    with pytest.raises(ExitRetentionError):
        getattr(cluster.recovery, operation)(**pod_arguments())
    assert cluster.pods["root"]["metadata"]["uid"] == "replacement"
    assert not cluster.pods["root"]["metadata"].get("deletionTimestamp")


@pytest.mark.parametrize("key", [INVOCATION, TENANT, OPERATION])
def test_foreign_retention_never_grants_stop_authority(cluster, key):
    cluster.recovery.retain(**pod_arguments())
    cluster.pods["root"]["metadata"]["annotations"][key] = "foreign"
    with pytest.raises(ExitRetentionError):
        cluster.recovery.request_stop(**pod_arguments())
    assert not any(method == "DELETE" for method, _, _ in cluster.writes)


async def test_release_reply_loss_reuses_durable_exit_before_touching_the_pod(native, cluster, monkeypatch):
    arguments = retirement(native)
    assert (await request(native, monkeypatch, arguments=arguments, action="stop")).status_code == 202
    exit_pod(cluster, "root")
    cluster.lost = "PATCH"
    first = await request(native, monkeypatch, arguments=arguments, action="stop")
    assert first.status_code == 503
    assert native.store._read("TENANT#tenant", f"CAMPAIGN_EXIT#{arguments['operation_id']}") is not None
    del cluster.pods["root"]
    replay = await request(native, monkeypatch, arguments=arguments, action="stop")
    assert replay.status_code == 200
    assert replay.json()["status"] == "workload_exited"


def test_gateway_delete_permission_is_limited_to_the_existing_worker_namespace():
    root = Path(__file__).resolve().parents[4]
    text = (root / "modules/agent-factory/webhook-ingress/infra/agent-authority-bootstrap.tf").read_text()
    role = text.split('resource "kubernetes_role" "gateway_agent_pod_read"', 1)[1].split('resource "', 1)[0]
    assert "namespace = kubernetes_namespace.adp_agents.metadata[0].name" in role
    assert 'resources  = ["pods"]' in role
    assert 'verbs = ["get", "list", "patch", "delete"]' in role
    assert "deletecollection" not in role
