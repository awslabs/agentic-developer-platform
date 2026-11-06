"""Checkpoint projected tokens must bind to the approved helper, not a Crew pod."""

import re
from copy import deepcopy

import httpx
import pytest

from src.crew.checkpoint_identity import (
    AUDIENCE,
    CheckpointHelperVerifier,
    CheckpointIdentityRefusedError,
    CheckpointIdentityUnavailableError,
    RestoreHelperVerifier,
)

DIGEST = "sha256:" + "a" * 64
OWNER = "b" * 52
CHECKPOINT_HASH = "c" * 64
NAME = "chat-checkpoint-" + CHECKPOINT_HASH[:12] + "-abcde"


def helper_pod():
    return {
        "metadata": {
            "uid": "pod-uid",
            "name": NAME,
            "namespace": "adp-gateway-agents",
            "labels": {
                "adp.io/crew-checkpoint-helper": "true",
                "adp.io/owner-key": OWNER,
            },
            "annotations": {"adp.io/checkpoint-hash": CHECKPOINT_HASH, "adp.io/session-hash": "d" * 64},
        },
        "spec": {
            "serviceAccountName": "adp-crew-checkpoint-helper",
            "automountServiceAccountToken": False,
            "volumes": [
                {
                    "name": "checkpoint-identity",
                    "projected": {
                        "sources": [
                            {
                                "serviceAccountToken": {
                                    "audience": AUDIENCE,
                                    "expirationSeconds": 600,
                                    "path": "token",
                                }
                            }
                        ]
                    },
                },
                {"name": "crew-home", "persistentVolumeClaim": {"claimName": "crew-home-" + OWNER, "readOnly": True}},
                {"name": "gateway-ca", "configMap": {"name": "gateway-ca"}},
                {"name": "scratch", "emptyDir": {"sizeLimit": "512Mi"}},
            ],
            "containers": [
                {
                    "name": "checkpoint-helper",
                    "image": "registry/helper@" + DIGEST,
                    "command": ["/app/chat-checkpoint-helper"],
                    "env": [
                        {"name": name, "value": value}
                        for name, value in {
                            "ADP_WORKLOAD_TOKEN_FILE": "/var/run/adp-checkpoint/token",
                            "ADP_CHAT_DATA_URL": "https://chat-sandbox-gateway.adp-gateway.svc:8443",
                            "NODE_EXTRA_CA_CERTS": "/var/run/adp-chat-ca/ca.crt",
                            "ADP_CHECKPOINT_ID": "a" * 36,
                            "ADP_OWNER_KEY": OWNER,
                            "KIROCREW_HOME": "/mnt/crew",
                        }.items()
                    ],
                    "volumeMounts": [
                        {"name": "checkpoint-identity", "mountPath": "/var/run/adp-checkpoint", "readOnly": True},
                        {"name": "crew-home", "mountPath": "/mnt/crew", "readOnly": True},
                        {"name": "gateway-ca", "mountPath": "/var/run/adp-chat-ca", "readOnly": True},
                        {"name": "scratch", "mountPath": "/tmp"},
                    ],
                    "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False, "runAsUser": 10001},
                }
            ],
        },
        "status": {"phase": "Running", "containerStatuses": [{"imageID": "registry/helper@" + DIGEST, "state": {"running": {}}}]},
    }


@pytest.fixture
def verifier(tmp_path):
    state = {"audience": AUDIENCE, "service_account": "adp-crew-checkpoint-helper", "pod": helper_pod()}
    token_path = tmp_path / "gateway-token"
    token_path.write_text("gateway-credential")

    def kubernetes(request):
        assert request.headers["Authorization"] == "Bearer gateway-credential"
        if request.method == "POST":
            assert request.url.path.endswith("/tokenreviews")
            assert request.content and b'"audiences":["adp-checkpoint-upload"]' in request.content
            return httpx.Response(
                201,
                json={
                    "status": {
                        "authenticated": True,
                        "audiences": [state["audience"]],
                        "user": {
                            "username": "system:serviceaccount:adp-gateway-agents:" + state["service_account"],
                            "extra": {"authentication.kubernetes.io/pod-name": [NAME], "authentication.kubernetes.io/pod-uid": ["pod-uid"]},
                        },
                    }
                },
            )
        assert request.url.path.endswith("/pods/" + NAME)
        return httpx.Response(200, json=state["pod"])

    with httpx.Client(transport=httpx.MockTransport(kubernetes), base_url="https://kubernetes.default.svc") as client:
        yield CheckpointHelperVerifier(client, frozenset({DIGEST}), token_path), state


def test_verified_helper_token_binds_owner_and_checkpoint(verifier):
    client, _ = verifier
    identity = client.verify("projected-credential")
    assert (identity.pod_uid, identity.owner_key, identity.checkpoint_hash, identity.image_digest) == ("pod-uid", OWNER, CHECKPOINT_HASH, DIGEST)


def test_helper_metadata_uses_kubernetes_valid_labels_and_full_digest_annotations():
    metadata = helper_pod()["metadata"]
    for value in metadata["labels"].values():
        assert len(value) <= 63
        assert re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9_.-]*[A-Za-z0-9])?", value)
    assert metadata["annotations"]["adp.io/checkpoint-hash"] == CHECKPOINT_HASH
    assert len(metadata["annotations"]["adp.io/session-hash"]) == 64


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.update(audience="adp-agent-bootstrap"),
        lambda state: state.update(service_account="adp-chat-sandbox"),
        lambda state: state["pod"]["metadata"]["annotations"].pop("adp.io/checkpoint-hash"),
        lambda state: state["pod"]["metadata"]["annotations"].update({"adp.io/checkpoint-hash": "c" * 63}),
        lambda state: state["pod"]["metadata"]["annotations"].update({"adp.io/session-hash": "d" * 63}),
        lambda state: state["pod"]["metadata"].update(annotations={}),
        lambda state: state["pod"]["metadata"]["labels"].update({"adp.io/owner-key": "d" * 52}),
        lambda state: state["pod"]["spec"]["volumes"][1]["persistentVolumeClaim"].update(readOnly=False),
        lambda state: state["pod"]["spec"]["containers"][0].update(image="registry/helper@sha256:" + "e" * 64),
        lambda state: state["pod"]["status"]["containerStatuses"][0].update(imageID="registry/helper@sha256:" + "e" * 64),
        lambda state: state["pod"]["spec"]["containers"][0]["volumeMounts"][1].update(readOnly=False),
        lambda state: state["pod"]["spec"]["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"].update(audience="adp-agent-bootstrap"),
        lambda state: state["pod"]["spec"]["containers"][0].update(command=["/bin/sh"]),
        lambda state: state["pod"]["metadata"].update(deletionTimestamp="now"),
        lambda state: state["pod"]["spec"]["containers"][0]["env"].append({"name": "AWS_ACCESS_KEY_ID", "value": "untrusted"}),
        lambda state: state["pod"]["spec"]["containers"][0]["env"][4].update(value="d" * 52),
    ],
)
def test_substituted_helper_is_refused(verifier, mutation):
    client, state = verifier
    mutation(state)
    with pytest.raises(CheckpointIdentityRefusedError):
        client.verify("projected-credential")


def test_missing_gateway_credentials_fail_closed(verifier):
    client, _ = verifier
    client.token_path.unlink()
    with pytest.raises(CheckpointIdentityUnavailableError):
        client.verify("projected-credential")


def test_restore_identity_requires_separate_pod_and_writable_owner_mount(tmp_path):
    pod = deepcopy(helper_pod())
    restore_name = "chat-restore-" + CHECKPOINT_HASH[:12] + "-abcde"
    pod["metadata"]["name"] = restore_name
    pod["metadata"]["labels"] = {
        "adp.io/crew-restore-helper": "true",
        "adp.io/owner-key": OWNER,
    }
    pod["spec"]["serviceAccountName"] = "adp-crew-restore-helper"
    pod["spec"]["volumes"][0]["projected"]["sources"][0]["serviceAccountToken"]["audience"] = "adp-checkpoint-restore"
    pod["spec"]["volumes"][1]["persistentVolumeClaim"]["readOnly"] = False
    container = pod["spec"]["containers"][0]
    container["command"] = ["/app/chat-checkpoint-restore"]
    container["volumeMounts"][1]["readOnly"] = False
    gateway_token = tmp_path / "gateway-token"
    gateway_token.write_text("gateway-credential")

    def kubernetes(request):
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "status": {
                        "authenticated": True,
                        "audiences": ["adp-checkpoint-restore"],
                        "user": {
                            "username": "system:serviceaccount:adp-gateway-agents:adp-crew-restore-helper",
                            "extra": {"authentication.kubernetes.io/pod-name": [restore_name], "authentication.kubernetes.io/pod-uid": ["pod-uid"]},
                        },
                    }
                },
            )
        return httpx.Response(200, json=pod)

    with httpx.Client(transport=httpx.MockTransport(kubernetes), base_url="https://kubernetes.default.svc") as client:
        verifier = RestoreHelperVerifier(client, frozenset({DIGEST}), gateway_token)
        assert verifier.verify("restore-token").owner_key == OWNER
        with pytest.raises(CheckpointIdentityRefusedError):
            CheckpointHelperVerifier(client, frozenset({DIGEST}), gateway_token).verify("restore-token")
        container["volumeMounts"][1]["readOnly"] = True
        with pytest.raises(CheckpointIdentityRefusedError):
            verifier.verify("restore-token")


def test_recovery_requires_kubernetes_to_confirm_original_pod_gone(tmp_path):
    pod_name = "chat-restore-" + CHECKPOINT_HASH[:12] + "-abcde"
    current = {"status_code": 200, "uid": "old-pod", "phase": "Running"}
    token_path = tmp_path / "gateway-token"
    token_path.write_text("gateway-credential")

    def get_pod(request):
        assert request.headers["Authorization"] == "Bearer gateway-credential"
        if current["status_code"] != 200:
            return httpx.Response(current["status_code"])
        return httpx.Response(
            200,
            json={
                "metadata": {"uid": current["uid"]},
                "status": {
                    "phase": current["phase"],
                    "containerStatuses": [{"state": {"terminated": {} if current["phase"] == "Succeeded" else None}}],
                },
            },
        )

    with httpx.Client(transport=httpx.MockTransport(get_pod), base_url="https://kubernetes.default.svc") as client:
        verifier = RestoreHelperVerifier(client, frozenset({DIGEST}), token_path)
        assert not verifier.pod_is_gone(pod_name, "old-pod")
        current["status_code"] = 503
        with pytest.raises(CheckpointIdentityUnavailableError):
            verifier.pod_is_gone(pod_name, "old-pod")
        current["status_code"], current["phase"] = 200, "Succeeded"
        assert verifier.pod_is_gone(pod_name, "old-pod")
        current["status_code"] = 404
        assert verifier.pod_is_gone(pod_name, "old-pod")
        with pytest.raises(CheckpointIdentityRefusedError):
            verifier.pod_is_gone("foreign-pod", "old-pod")
