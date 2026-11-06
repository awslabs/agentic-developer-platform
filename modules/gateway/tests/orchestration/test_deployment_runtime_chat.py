"""Crew verification checks the live objects rather than a health response."""

import copy
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.orchestration.deployment_controller import ADAPTERS
from src.orchestration.deployment_runtime_contract import CHAT_MANIFEST_KEYS, ReleaseArtifact
from src.orchestration.deployment_runtime_reader import PilotRuntimeReader
from src.orchestration.review_cycle import CycleBlockedError

ACCOUNT = "123456789012"
REGION = "us-east-1"
DIGEST = "sha256:" + "a" * 64
SUPERVISOR_DIGEST = "sha256:" + "b" * 64


def sha(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@pytest.fixture
def live():
    supervisor_image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-chat-agent@{SUPERVISOR_DIGEST}"
    sandbox_image = f"{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/adp-chat-agent@{DIGEST}"
    objects = {}
    for key in CHAT_MANIFEST_KEYS:
        kind, name = key.split("/", 1)
        metadata = {"name": name, "namespace": "adp-gateway-agents", "resourceVersion": "5"}
        if kind == "scaledjob":
            spec = {
                "jobTargetRef": {"template": {"spec": {"serviceAccountName": "adp-chat-supervisor", "containers": [{"image": supervisor_image}]}}}
            }
        elif kind == "job":
            spec = {"template": {"spec": {"serviceAccountName": "adp-chat-supervisor", "containers": [{"image": supervisor_image}]}}}
        elif kind == "configmap":
            spec = None
        elif kind == "networkpolicy":
            spec = {
                "podSelector": {"matchLabels": {"adp.io/chat-sandbox": "true"}},
                "policyTypes": ["Ingress", "Egress"],
                "ingress": [],
                "egress": [],
            }
        elif kind == "validatingadmissionpolicybinding":
            spec = {
                "policyName": name,
                "validationActions": ["Deny"],
                "matchResources": {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "adp-gateway-agents"}}},
            }
        else:
            spec = {
                "failurePolicy": "Fail",
                "matchConstraints": {"resourceRules": [{"resources": ["pods"], "operations": ["CREATE", "UPDATE"]}]},
                "validations": [
                    {
                        "expression": (
                            "!has(object.spec.activeDeadlineSeconds)"
                            if name == "adp-crew-pod-template"
                            else "object.spec.activeDeadlineSeconds == 900"
                        )
                    }
                ],
            }
        objects[key] = {"metadata": metadata, "spec": spec}
        if kind == "configmap":
            objects[key]["data"] = {
                "pod.json": json.dumps(
                    {
                        "metadata": {"labels": {"adp.io/chat-sandbox": "true"}},
                        "spec": {"serviceAccountName": "adp-chat-sandbox", "containers": [{"image": sandbox_image}]},
                    }
                )
            }

    def get(path):
        for key, item in objects.items():
            kind, name = key.split("/", 1)
            if path.endswith("/" + name) and ((kind == "validatingadmissionpolicybinding") == ("validatingadmissionpolicybindings" in path)):
                return copy.deepcopy(item)
        raise AssertionError(path)

    kube = SimpleNamespace(get=Mock(side_effect=get), probe_network=Mock(return_value=("fixture-uid", True, True)), close=Mock())
    return SimpleNamespace(objects=objects, kube=kube)


def release(live):
    assets = {key: sha(value.get("data") if key.startswith("configmap/") else value["spec"]) for key, value in live.objects.items()}
    return ReleaseArtifact(
        schema_version=1,
        repository_id=17,
        run_id=42,
        run_attempt=1,
        source_revision="c" * 40,
        workflow_revision="d" * 40,
        workflow_path=".github/workflows/chat-agent-deploy.yml",
        account_id=ACCOUNT,
        region=REGION,
        resource_id="cluster/adp-gateway-agents",
        component="agent-worker",
        image_digest=DIGEST,
        supervisor_image_digest=SUPERVISOR_DIGEST,
        manifests_sha256=sha(assets),
        assets=assets,
        produced_at=datetime.now(UTC),
    )


def inspect(live, artifact):
    reader = PilotRuntimeReader(kubernetes=lambda *args: live.kube)
    return reader.inspect(
        SimpleNamespace(region_name=REGION),
        {"name": "cluster", "arn": f"arn:aws:eks:{REGION}:{ACCOUNT}:cluster/cluster"},
        "adp-gateway-agents",
        artifact=artifact,
        artifact_hash="e" * 64,
        evidence_ref="github/actions/runs/42/artifacts/77",
        environment=None,
    )


def test_chat_adapter_accepts_pinned_verified_crew_release(live):
    component = inspect(live, release(live))
    assert component.image_digest == DIGEST
    assert component.manifests_sha256 == release(live).manifests_sha256
    assert component.pod_uids == ["fixture-uid"]
    assert component.asset_count == len(CHAT_MANIFEST_KEYS)
    live.kube.close.assert_called()


@pytest.mark.parametrize(
    "failure",
    [
        "scaledjob",
        "supervisor",
        "crew",
        "policy",
        "wrong_namespace",
        "wrong_rules",
        "deadline",
        "network",
        "probe",
        "probe_log",
        "health_only",
        "changed",
    ],
)
def test_chat_adapter_blocks_unverified_runtime(live, failure):
    artifact = release(live)
    if failure == "scaledjob":
        live.objects["scaledjob/chat-agent-worker"]["spec"]["jobTargetRef"]["template"]["spec"]["containers"][0]["image"] = "wrong-image"
    elif failure == "supervisor":
        live.objects["job/adp-chat-supervisor-once"]["spec"]["template"]["spec"]["containers"][0]["image"] = "wrong-image"
    elif failure == "crew":
        live.objects["configmap/chat-crew-pod-template"]["data"]["pod.json"] = "{}"
    elif failure == "policy":
        live.objects["validatingadmissionpolicybinding/adp-chat-sandbox-template"]["spec"]["validationActions"] = ["Warn"]
    elif failure == "wrong_namespace":
        live.objects["validatingadmissionpolicybinding/adp-crew-pod-template"]["spec"]["matchResources"]["namespaceSelector"]["matchLabels"][
            "kubernetes.io/metadata.name"
        ] = "other"
    elif failure == "wrong_rules":
        live.objects["validatingadmissionpolicy/adp-crew-pod-template"]["spec"]["matchConstraints"]["resourceRules"][0]["resources"] = ["deployments"]
    elif failure == "deadline":
        live.objects["validatingadmissionpolicy/adp-chat-sandbox-template"]["spec"]["validations"][0]["expression"] = (
            "!has(object.spec.activeDeadlineSeconds)"
        )
    elif failure == "network":
        live.objects["networkpolicy/chat-sandbox-deny"]["spec"]["egress"] = [{}]
    elif failure == "probe":
        live.kube.probe_network.return_value = ("fixture-uid", False, True)
    elif failure == "probe_log":
        live.kube.probe_network.return_value = ("fixture-uid", True, False)
    elif failure == "health_only":
        live.kube.get = Mock(return_value={"health": "ok"})
    else:
        original = live.kube.get.side_effect
        count = 0

        def changed(path):
            nonlocal count
            if path.endswith("/scaledjobs/chat-agent-worker"):
                count += 1
                if count == 2:
                    value = original(path)
                    value["metadata"]["resourceVersion"] = "6"
                    return value
            return original(path)

        live.kube.get = Mock(side_effect=changed)
    if failure in {"scaledjob", "supervisor", "policy", "wrong_namespace", "wrong_rules", "deadline", "network"}:
        artifact = release(live)
    with pytest.raises(CycleBlockedError, match="deployment_runtime_mismatch"):
        inspect(live, artifact)
    live.kube.close.assert_called()


def test_chat_release_requires_distinct_images_and_complete_manifest_hash(live):
    artifact = release(live)
    with pytest.raises(ValueError, match="distinct pinned images"):
        ReleaseArtifact.model_validate(artifact.model_dump() | {"supervisor_image_digest": DIGEST})
    with pytest.raises(ValueError, match="all hashed manifests"):
        ReleaseArtifact.model_validate(artifact.model_dump() | {"manifests_sha256": "0" * 64})


def test_crew_adapter_registration_is_limited_to_chat_components():
    assert ADAPTERS["crew-pod-verification"] == frozenset({"agent-worker", "chat-worker"})
