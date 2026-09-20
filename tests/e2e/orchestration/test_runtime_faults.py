"""Non-live fault generation, ownership, and false-acceptance attacks."""

from copy import deepcopy
import json
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.scenarios import runtime_faults as faults
from tests.e2e.orchestration.scenarios.http import Unsupported
from tests.e2e.orchestration.scenarios.stop import assert_stop

QID = "q-runtime0123456789"


@pytest.fixture
def provider(valid_config, monkeypatch):
    client = Mock(config=valid_config, deadline=float("inf"))
    manifest = NS(runtime={"engine": NS(cluster="offline")})
    provider = faults.RuntimeFixtureProvider(client, manifest, "qualification-runtime")
    provider.image = (
        "111122223333.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "a" * 64
    )
    kube = Mock()
    kube.get.return_value = {
        "metadata": {
            "annotations": {**valid_config.ownership_tags(QID), "kubernetes": "extra"}
        }
    }
    kube.request.return_value = {"metadata": {"uid": "owned-uid"}}
    monkeypatch.setattr(provider, "kube", lambda: kube)
    return provider, kube


def test_runtime_fixture_has_no_gateway_credentials_or_startup(provider, valid_config):
    provider, kube = provider
    resource = provider.create(
        intended_identity=QID + "/runtime-deployment",
        ownership_tags=valid_config.ownership_tags(QID),
        idempotency_token="offline",
    )
    assert json.loads(resource)["uid"] == "owned-uid"
    method, path, body = kube.request.call_args.args
    assert method == "POST" and f"/namespaces/{QID}/" in path
    spec = body["spec"]
    assert spec["replicas"] == 1 and spec["strategy"] == {"type": "Recreate"}
    pod = spec["template"]["spec"]
    assert pod["automountServiceAccountToken"] is False
    assert "volumes" not in pod
    container = pod["containers"][0]
    assert "env" not in container and "envFrom" not in container
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["command"] == ["python", "-c", "import time; time.sleep(600)"]
    assert container["image"] == provider.image


@pytest.mark.parametrize("attack", ["namespace", "deadline", "image", "identity"])
def test_runtime_fixture_refuses_unsafe_creation(provider, valid_config, attack):
    provider, kube = provider
    identity = QID + "/runtime-deployment"
    if attack == "namespace":
        kube.get.return_value["metadata"]["annotations"] = {}
    elif attack == "deadline":
        provider.client.deadline = 0
    elif attack == "image":
        provider.image = None
    else:
        identity = "shared/gateway"
    with pytest.raises(Unsupported):
        provider.create(
            intended_identity=identity,
            ownership_tags=valid_config.ownership_tags(QID),
            idempotency_token="offline",
        )
    kube.request.assert_not_called()


def observation(name):
    image = (
        "111122223333.dkr.ecr.us-east-1.amazonaws.com/adp-gateway@sha256:" + "a" * 64
    )
    baseline = {
        "metadata": {
            "namespace": QID,
            "name": "bedrockgateway",
            "uid": "uid",
            "generation": 1,
            "labels": {faults.LABEL: QID},
        },
        "spec": {
            "replicas": 1,
            "template": {"spec": {"containers": [{"image": image}]}},
        },
        "status": {
            "availableReplicas": 1,
            "updatedReplicas": 1,
            "observedGeneration": 1,
        },
    }
    deployment = deepcopy(baseline)
    if name == "failed-deploy":
        deployment["metadata"]["generation"] = 2
        deployment["status"].update(availableReplicas=0, observedGeneration=2)
        deployment["spec"]["template"]["spec"]["containers"][0]["readinessProbe"] = {
            "exec": {"command": ["python", "-c", "raise SystemExit(1)"]}
        }
    return {
        "namespace": QID,
        "pods": [
            {
                "status": {
                    "phase": "Running",
                    "containerStatuses": [
                        {"ready": False, "state": {"running": {"startedAt": "offline"}}}
                    ],
                }
            }
        ]
        if name == "failed-deploy"
        else [],
        "baseline": baseline,
        "deployment": deployment,
        "runtime": {
            "actual_revision": "a" * 40,
            "required_revision": "b" * 40,
            "actual_digest": "sha256:" + "a" * 64,
            "required_digest": "sha256:" + "b" * 64,
        },
        "injection": {
            "operation": "retain-verified-prerequisite-image"
            if name == "stale-image"
            else "fail-fixture-readiness"
        },
        "after": {
            "status": "BLOCKED",
            "reason": "deployment_gateway_revision_mismatch"
            if name == "stale-image"
            else "deployment_gateway_rollout_incomplete",
        },
    }


@pytest.mark.parametrize("name", ["stale-image", "failed-deploy"])
def test_native_runtime_rejection_with_matching_fault_generation(name):
    faults.assert_runtime_outcome(name, observation(name))


@pytest.mark.parametrize("name", ["stale-image", "failed-deploy"])
@pytest.mark.parametrize(
    "attack",
    [
        "native-reason",
        "healthy-baseline",
        "uid",
        "namespace",
        "digest",
        "generation",
        "image",
    ],
)
def test_unrelated_failure_cannot_pass_runtime_fault(name, attack):
    value = observation(name)
    if attack == "native-reason":
        value["after"]["reason"] = "some-other-error"
    elif attack == "healthy-baseline":
        value["baseline"]["status"]["availableReplicas"] = 0
    elif attack == "uid":
        value["deployment"]["metadata"]["uid"] = "replacement"
    elif attack == "namespace":
        value["deployment"]["metadata"]["namespace"] = "shared"
    elif attack == "digest":
        value["runtime"]["actual_digest"] = value["runtime"]["required_digest"]
    elif attack == "generation":
        value["deployment"]["status"]["observedGeneration"] = 0
    else:
        value["deployment"]["spec"]["template"]["spec"]["containers"][0]["image"] = (
            "untrusted/gateway:latest"
        )
    with pytest.raises(AssertionError):
        faults.assert_runtime_outcome(name, value)


@pytest.mark.parametrize("attack", ["http", "ack", "graph", "termination", "liveness"])
def test_halt_without_proven_stop_never_passes(attack):
    value = {
        "injection": {"status": 200, "acknowledged": True},
        "graph": {"nodes": [{"node_ref": "worker", "state": "halted"}]},
        "worker": {
            "termination_confirmed": True,
            "invocation": {"liveness": "exited", "status": "aborted"},
        },
    }
    assert_stop(value)
    if attack == "http":
        value["injection"]["status"] = 501
    elif attack == "ack":
        value["injection"]["acknowledged"] = False
    elif attack == "graph":
        value["graph"]["nodes"][0]["state"] = "failed"
    elif attack == "termination":
        value["worker"]["termination_confirmed"] = False
    else:
        value["worker"]["invocation"]["liveness"] = "live"
    with pytest.raises(AssertionError):
        assert_stop(value)
