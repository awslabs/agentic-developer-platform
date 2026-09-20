"""Offline attacks against the role, image, subprocess and worker UID fences."""

from copy import deepcopy
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.orchestration.scenarios.http import Unsupported
from tests.e2e.orchestration.scenarios.kubernetes import registered_role
from tests.e2e.orchestration.scenarios.native_probe import execute_source
from tests.e2e.orchestration.scenarios.runtime import image_reference
from tests.e2e.orchestration.scenarios.workers import WorkerProvider


@pytest.fixture
def registered(valid_config):
    account = valid_config.expected_account_id
    row = {
        "id": valid_config.connection_ref,
        "service": "aws",
        "credential_type": "aws_role",
        "scopes": {
            "role_arn": f"arn:aws:iam::{account}:role/registered",
            "status": "verified",
        },
    }
    client = Mock(config=valid_config)
    client.get.return_value = [row]
    identity = {
        "Account": account,
        "Arn": f"arn:aws:sts::{account}:assumed-role/registered/test",
    }
    return client, identity, row


def test_only_actual_registered_assumed_role_is_accepted(registered):
    client, identity, row = registered
    assert registered_role(client, identity) == row["scopes"]["role_arn"]


@pytest.mark.parametrize(
    "attack",
    [
        "account",
        "role",
        "expiry",
        "revoked",
        "duplicate",
        "service",
        "type",
        "no-registration",
    ],
)
def test_refuse_broader_or_unverifiable_credentials(registered, attack):
    client, identity, row = registered
    if attack == "account":
        identity["Account"] = "999988887777"
    elif attack == "role":
        identity["Arn"] = identity["Arn"].replace("registered/", "Admin/")
    elif attack == "expiry":
        row["expires_at"] = "2020-01-01T00:00:00Z"
    elif attack == "revoked":
        row["scopes"]["status"] = "revoked"
    elif attack == "duplicate":
        client.get.return_value.append(deepcopy(row))
    elif attack == "no-registration":
        client.get.return_value = []
    else:
        row["service" if attack == "service" else "credential_type"] = "other"
    with pytest.raises(Unsupported, match="registered role"):
        registered_role(client, identity)


@pytest.mark.parametrize("suffix", [":abc", "@sha256:" + "a" * 64])
def test_exact_ecr_image_reference(suffix):
    result = image_reference(
        "111122223333.dkr.ecr.us-east-1.amazonaws.com/worker" + suffix,
        "111122223333",
        "us-east-1",
        "worker",
    )
    assert len(result) == 1


@pytest.mark.parametrize(
    "image",
    [
        "111122223333.dkr.ecr.us-east-1.amazonaws.com/worker-other:abc",
        "999988887777.dkr.ecr.us-east-1.amazonaws.com/worker:abc",
        "111122223333.dkr.ecr.eu-west-1.amazonaws.com/worker:abc",
        "111122223333.dkr.ecr.us-east-1.amazonaws.com/worker@sha256:bad",
        "111122223333.dkr.ecr.us-east-1.amazonaws.com/worker",
        "111122223333.dkr.ecr.us-east-1.amazonaws.com/worker/nested:abc",
    ],
)
def test_lookalike_image_cannot_prove_runtime(image):
    with pytest.raises(Unsupported):
        image_reference(image, "111122223333", "us-east-1", "worker")


def test_native_process_uses_signed_stdin_not_ambient_kubeconfig(monkeypatch):
    from tests.e2e.orchestration.scenarios import native_probe

    kube = Mock()
    kube.kubeconfig.return_value = '{"users": ["scoped-simulation"]}'
    monkeypatch.setattr(native_probe, "ScopedKubernetes", Mock(return_value=kube))
    process = Mock(
        return_value=SimpleNamespace(
            returncode=0, stdout='ADP_Q2_RESULT:{"live":true}\n'
        )
    )
    monkeypatch.setattr(native_probe.subprocess, "run", process)
    target = SimpleNamespace(
        cluster="fixture",
        namespace="fixture",
        deployment="gateway",
        container="gateway",
    )
    runtime = {
        "pod_names": {"verified-pod": "verified-uid"},
        "digest": "sha256:" + "a" * 64,
    }
    kube.get.return_value = {
        "metadata": {"uid": "verified-uid"},
        "status": {
            "containerStatuses": [
                {
                    "name": "gateway",
                    "ready": True,
                    "imageID": "image@" + runtime["digest"],
                }
            ]
        },
    }
    assert execute_source(
        Mock(), target, "# reviewed source", {"mode": "read"}, runtime=runtime
    ) == {"live": True}
    args, kwargs = process.call_args
    assert args[0][1:5] == ["--kubeconfig", "/dev/stdin", "--context", "qualification"]
    assert kwargs["input"] == kube.kubeconfig.return_value and kwargs["timeout"] == 70
    assert "pod/verified-pod" in args[0]
    assert "shell" not in kwargs
    process.side_effect = subprocess.TimeoutExpired("kubectl", 70)
    with pytest.raises(Unsupported, match="unknown"):
        execute_source(Mock(), target, "# reviewed source", {}, runtime=runtime)
    assert process.call_count == 2  # One call per invocation, never a retry.
    kube.get.return_value["metadata"]["uid"] = "replacement-uid"
    with pytest.raises(Unsupported, match="generation or image changed"):
        execute_source(Mock(), target, "# reviewed source", {}, runtime=runtime)
    assert process.call_count == 2  # A replacement Pod never receives the source.


@pytest.fixture
def disposable(monkeypatch):
    from tests.e2e.orchestration.scenarios import workers

    resource = {
        "namespace": "fixture",
        "pod_name": "pod-one",
        "pod_uid": "uid-one",
        "job_name": "job-one",
        "job_uid": "job-uid",
        "invocation_id": "invocation",
    }
    target = SimpleNamespace(cluster="fixture", namespace="fixture")
    provider = WorkerProvider(
        SimpleNamespace(
            client=Mock(), manifest=SimpleNamespace(runtime={"worker": target})
        )
    )
    provider._binding = Mock(return_value=({}, {"liveness": "live"}))
    pod = {
        "metadata": {
            "uid": "uid-one",
            "ownerReferences": [{"kind": "Job", "uid": "job-uid"}],
        },
        "status": {"phase": "Running"},
    }
    kube = Mock()
    kube.get.side_effect = (
        lambda path: {"metadata": {"uid": "job-uid"}} if "/jobs/" in path else pod
    )
    kube.request.return_value = {"status": "Success"}
    monkeypatch.setattr(workers, "ScopedKubernetes", Mock(return_value=kube))
    return provider, resource, pod, kube


def test_worker_loss_deletes_one_pod_with_uid_precondition(disposable):
    provider, resource, _, kube = disposable
    result = provider.inject("worker-loss", json.dumps(resource))
    assert result["scope"] == "single-inventoried-pod"
    args = kube.request.call_args.args
    assert args[:2] == ("DELETE", "/api/v1/namespaces/fixture/pods/pod-one")
    assert args[2]["preconditions"] == {"uid": "uid-one"}


@pytest.mark.parametrize(
    "attack", ["replacement", "owner", "namespace", "exited", "pending"]
)
def test_worker_loss_refuses_unowned_or_replaced_worker(disposable, attack):
    provider, resource, pod, kube = disposable
    if attack == "replacement":
        pod["metadata"]["uid"] = "replacement"
    elif attack == "owner":
        pod["metadata"]["ownerReferences"][0]["uid"] = "foreign"
    elif attack == "namespace":
        resource["namespace"] = "foreign"
    elif attack == "exited":
        provider._binding.return_value = ({}, {"liveness": "exited"})
    else:
        pod["status"]["phase"] = "Pending"
    with pytest.raises(Unsupported):
        provider.inject("worker-loss", json.dumps(resource))
    kube.request.assert_not_called()


def test_cleanup_does_not_stop_an_extra_worker(disposable):
    provider, resource, _, kube = disposable
    with pytest.raises(Unsupported, match="still exists"):
        provider.delete(json.dumps(resource))
    kube.request.assert_not_called()
