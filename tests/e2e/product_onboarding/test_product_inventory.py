"""A Product release must agree with every serving pod before API writes."""

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.e2e.cli_uplift import deployment_provenance as provenance
from tests.e2e.product_onboarding import product_inventory


SOURCE = "a" * 40
DIGEST = "sha256:" + "b" * 64
SELECTED = {
    "cluster": "adp-dev-cluster",
    "cluster_arn": "arn:aws:eks:us-east-1:000000000101:cluster/adp-dev-cluster",
    "region": "us-east-1",
    "namespace": "adp-gateway",
    "deployment": "bedrockgateway",
    "container": "bedrockgateway",
    "selector": {"app": "bedrockgateway"},
    "image_repository": "000000000101.dkr.ecr.us-east-1.amazonaws.com/adp-gateway",
}
DEPLOYMENT = {
    "metadata": {"uid": "deployment-uid", "generation": 3},
    "spec": {
        "replicas": 1,
        "template": {"spec": {"containers": [{"name": "bedrockgateway"}]}},
    },
    "status": {"observedGeneration": 3, "readyReplicas": 1},
}
REPLICAS = {
    "kind": "ReplicaSetList",
    "items": [
        {
            "metadata": {
                "uid": "replica-uid",
                "ownerReferences": [{"uid": "deployment-uid", "controller": True}],
            }
        }
    ],
}
POD = {
    "metadata": {
        "uid": "pod-uid",
        "namespace": "adp-gateway",
        "labels": {"app": "bedrockgateway"},
        "ownerReferences": [{"uid": "replica-uid", "controller": True}],
    },
    "spec": {
        "containers": [
            {
                "name": "bedrockgateway",
                "image": SELECTED["image_repository"] + "@" + DIGEST,
                "ports": [{"containerPort": 8080, "protocol": "TCP"}],
                "env": [
                    {"name": "BG_PRODUCT_PROFILE_ENABLED", "value": "true"},
                    {"name": "BG_PRODUCT_ONBOARDING_ENABLED", "value": "false"},
                ],
            }
        ]
    },
    "status": {
        "podIP": "10.1.1.10",
        "containerStatuses": [
            {
                "name": "bedrockgateway",
                "ready": True,
                "imageID": SELECTED["image_repository"] + "@" + DIGEST,
                "state": {"running": {}},
            }
        ],
        "conditions": [{"type": "Ready", "status": "True"}],
    },
}


@pytest.fixture
def observed(monkeypatch):
    selected = deepcopy(SELECTED)
    deployment, replicas, pod = deepcopy(DEPLOYMENT), deepcopy(REPLICAS), deepcopy(POD)
    cluster = {
        "arn": selected["cluster_arn"],
        "name": selected["cluster"],
        "status": "ACTIVE",
        "endpoint": "https://cluster.us-east-1.eks.amazonaws.com",
        "certificateAuthority": {"data": "test-ca"},
    }
    calls = []
    monkeypatch.setattr(provenance, "binding", lambda _: selected)
    monkeypatch.setattr(
        provenance,
        "resolve",
        lambda aws, cfg, record: record.update(
            revision_evidence={
                "image_digest": DIGEST,
                "deployment_uid": "deployment-uid",
                "generation": 3,
            }
        )
        or SOURCE,
    )
    monkeypatch.setattr(provenance, "eks_bearer", lambda *a: "private-test-token")

    def read(url, bearer, ca):
        assert bearer == "private-test-token" and ca == "test-ca"
        calls.append(url)
        if url.endswith("/deployments/bedrockgateway"):
            return deepcopy(deployment)
        if url.endswith("/replicasets"):
            return deepcopy(replicas)
        if url.endswith("/pods"):
            return {"kind": "PodList", "items": [deepcopy(pod)]}
        pytest.fail("Only read-only deployment, replica and pod endpoints are allowed")

    monkeypatch.setattr(provenance, "get_json", read)
    aws = SimpleNamespace(
        call=lambda service, operation, **kwargs: {"cluster": deepcopy(cluster)}
    )
    return aws, selected, deployment, replicas, pod, cluster, calls


def test_inventory_reads_every_serving_pod_without_exec_or_mutation(observed):
    aws, _, _, _, _, _, calls = observed
    assert product_inventory.observe(aws, {}, SOURCE) == {
        "source_revision": SOURCE,
        "digest": DIGEST,
        "pods": [{"uid": "pod-uid", "address": "10.1.1.10", "digest": DIGEST}],
    }
    assert len(calls) == 4
    assert all("/pods/" not in url and "/exec" not in url for url in calls)


def test_recovery_observes_quiesced_source_and_every_serving_pod(observed):
    aws, _, _, _, pod, _, calls = observed
    pod["spec"]["containers"][0]["env"][0]["value"] = "false"
    assert product_inventory.observe(aws, {}, SOURCE, profile_enabled=False)[
        "pods"
    ] == [{"uid": "pod-uid", "address": "10.1.1.10", "digest": DIGEST}]
    assert len(calls) == 4
    with pytest.raises(provenance.PortError, match="capability"):
        product_inventory.observe(aws, {}, SOURCE)


@pytest.mark.parametrize("invalid", ["false", 0, None])
def test_recovery_requires_boolean_admissions_expectation(observed, invalid):
    aws, *_ = observed
    with pytest.raises(provenance.PortError, match="admissions expectation"):
        product_inventory.observe(aws, {}, SOURCE, profile_enabled=invalid)


@pytest.mark.parametrize(
    "failure",
    [
        "wrong_source",
        "other_cluster",
        "old_pod",
        "unready",
        "missing_flag",
        "other_owner",
        "no_address",
        "wrong_port",
        "empty",
        "partial_page",
        "rollout_changed",
    ],
)
def test_incomplete_or_mismatched_inventory_fails_closed(
    observed, failure, monkeypatch
):
    aws, _, deployment, replicas, pod, cluster, _ = observed
    source = SOURCE
    if failure == "wrong_source":
        source = "c" * 40
    elif failure == "other_cluster":
        cluster["name"] = "wrong"
    elif failure == "old_pod":
        pod["status"]["containerStatuses"][0]["imageID"] = "sha256:" + "f" * 64
    elif failure == "unready":
        pod["status"]["containerStatuses"][0]["ready"] = False
    elif failure == "missing_flag":
        pod["spec"]["containers"][0]["env"].pop()
    elif failure == "other_owner":
        replicas["items"][0]["metadata"]["ownerReferences"] = []
    elif failure == "no_address":
        pod["status"]["podIP"] = None
    elif failure == "wrong_port":
        pod["spec"]["containers"][0]["ports"] = [
            {"containerPort": 9090, "protocol": "TCP"}
        ]
    elif failure == "empty":
        pod["metadata"]["labels"] = {"other": "service"}
    elif failure == "partial_page":
        monkeypatch.setattr(
            provenance,
            "get_json",
            lambda *a: {"kind": "PodList", "metadata": {"continue": "next"}},
        )
    else:
        original = provenance.get_json
        count = 0

        def changed(*args):
            nonlocal count
            count += 1
            result = original(*args)
            if count == 4:
                result["metadata"]["generation"] = 4
            return result

        monkeypatch.setattr(provenance, "get_json", changed)
    with pytest.raises(provenance.PortError):
        product_inventory.observe(aws, {}, source)


def test_unavailable_kubernetes_view_refuses_without_fallback(observed, monkeypatch):
    aws, *_ = observed
    monkeypatch.setattr(
        provenance,
        "get_json",
        lambda *args: (_ for _ in ()).throw(provenance.PortError("read refused")),
    )
    with pytest.raises(provenance.PortError, match="read refused"):
        product_inventory.observe(aws, {}, SOURCE)


def test_every_serving_replica_is_checked_independently(observed, monkeypatch):
    aws, _, deployment, _, pod, _, _ = observed
    deployment["spec"]["replicas"] = deployment["status"]["readyReplicas"] = 2
    second = deepcopy(pod)
    second["metadata"]["uid"] = "pod-uid-2"
    second["status"]["podIP"] = "10.1.1.11"
    read = provenance.get_json

    def two_pods(url, bearer, ca):
        if url.endswith("/pods"):
            return {"kind": "PodList", "items": [deepcopy(pod), deepcopy(second)]}
        return read(url, bearer, ca)

    monkeypatch.setattr(provenance, "get_json", two_pods)
    assert len(product_inventory.observe(aws, {}, SOURCE)["pods"]) == 2
    second["status"]["containerStatuses"][0]["imageID"] = "sha256:" + "f" * 64
    with pytest.raises(provenance.PortError, match="serving pod identity"):
        product_inventory.observe(aws, {}, SOURCE)
