"""Read back the pinned chat supervisor and crew release from the scoped cluster."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from urllib.parse import quote

from .deployment_runtime_contract import CHAT_MANIFEST_KEYS, RuntimeComponent
from .deployment_runtime_reader import KubernetesRuntime, require
from .review_cycle import CycleBlockedError


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class ChatRuntimeReader:
    def __init__(self, *, kubernetes=KubernetesRuntime):
        self.kubernetes = kubernetes

    def inspect(self, scoped, description, namespace, *, artifact, artifact_hash, evidence_ref):
        require(namespace == "adp-gateway-agents", "deployment_runtime_mismatch")
        kube = self.kubernetes(scoped, description, namespace)
        ns = quote(namespace, safe="")
        paths = {
            "scaledjob/chat-agent-worker": f"/apis/keda.sh/v1alpha1/namespaces/{ns}/scaledjobs/chat-agent-worker",
            "job/adp-chat-supervisor-once": f"/apis/batch/v1/namespaces/{ns}/jobs/adp-chat-supervisor-once",
            "configmap/chat-crew-pod-template": f"/api/v1/namespaces/{ns}/configmaps/chat-crew-pod-template",
            "networkpolicy/chat-sandbox-deny": f"/apis/networking.k8s.io/v1/namespaces/{ns}/networkpolicies/chat-sandbox-deny",
        }
        for name in ("adp-chat-sandbox-template", "adp-chat-supervisor-pods", "adp-crew-pod-template"):
            paths[f"validatingadmissionpolicy/{name}"] = f"/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies/{name}"
            paths[f"validatingadmissionpolicybinding/{name}"] = f"/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicybindings/{name}"
        require(set(paths) == CHAT_MANIFEST_KEYS, "deployment_runtime_mismatch")
        try:
            objects = {key: kube.get(path) for key, path in paths.items()}
            for key, value in objects.items():
                kind, name = key.split("/", 1)
                metadata = value.get("metadata", {})
                require(
                    metadata.get("name") == name
                    and not metadata.get("deletionTimestamp")
                    and ("/namespaces/" not in paths[key] or metadata.get("namespace") == namespace)
                    and _hash(value.get("data") if kind == "configmap" else value.get("spec")) == artifact.assets[key],
                    "deployment_runtime_mismatch",
                )
            supervisor_image = f"{artifact.account_id}.dkr.ecr.{artifact.region}.amazonaws.com/adp-chat-agent@{artifact.supervisor_image_digest}"
            sandbox_image = f"{artifact.account_id}.dkr.ecr.{artifact.region}.amazonaws.com/adp-chat-agent@{artifact.image_digest}"
            scaledjob = objects["scaledjob/chat-agent-worker"]["spec"]["jobTargetRef"]["template"]["spec"]
            supervisor = objects["job/adp-chat-supervisor-once"]["spec"]["template"]["spec"]
            crew_data = objects["configmap/chat-crew-pod-template"]["data"]
            crew = json.loads(crew_data["pod.json"])
            require(
                scaledjob.get("serviceAccountName") == "adp-chat-supervisor"
                and len(scaledjob.get("containers", [])) == 1
                and scaledjob["containers"][0]["image"] == supervisor_image
                and supervisor.get("serviceAccountName") == "adp-chat-supervisor"
                and len(supervisor.get("containers", [])) == 1
                and supervisor["containers"][0]["image"] == supervisor_image
                and crew["spec"].get("serviceAccountName") == "adp-chat-sandbox"
                and crew["spec"]["containers"][0]["image"] == sandbox_image
                and crew["metadata"]["labels"].get("adp.io/chat-sandbox") == "true",
                "deployment_runtime_mismatch",
            )
            network = objects["networkpolicy/chat-sandbox-deny"]["spec"]
            require(
                network.get("podSelector", {}).get("matchLabels", {}).get("adp.io/chat-sandbox") == "true"
                and set(network.get("policyTypes", [])) == {"Ingress", "Egress"}
                and network.get("egress") == network.get("ingress") == [],
                "deployment_runtime_mismatch",
            )
            for name in ("adp-chat-sandbox-template", "adp-chat-supervisor-pods", "adp-crew-pod-template"):
                policy = objects[f"validatingadmissionpolicy/{name}"]["spec"]
                binding = objects[f"validatingadmissionpolicybinding/{name}"]["spec"]
                require(
                    policy.get("failurePolicy") == "Fail"
                    and binding.get("policyName") == name
                    and binding.get("validationActions") == ["Deny"]
                    and binding.get("matchResources", {}).get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name")
                    == namespace
                    and any(
                        rule.get("resources") == ["pods"] and {"CREATE", "UPDATE"} <= set(rule.get("operations", []))
                        for rule in policy.get("matchConstraints", {}).get("resourceRules", [])
                    ),
                    "deployment_runtime_mismatch",
                )
            sandbox_rules = objects["validatingadmissionpolicy/adp-chat-sandbox-template"]["spec"].get("validations", [])
            require(
                any(
                    "object.spec.activeDeadlineSeconds == 900" in rule.get("expression", "") and "?" not in rule["expression"]
                    for rule in sandbox_rules
                ),
                "deployment_runtime_mismatch",
            )
            crew_rules = objects["validatingadmissionpolicy/adp-crew-pod-template"]["spec"].get("validations", [])
            require(
                any("!has(object.spec.activeDeadlineSeconds)" in rule.get("expression", "") for rule in crew_rules),
                "deployment_runtime_mismatch",
            )
            probe_uid, internal_allowed, external_denied = kube.probe_network(sandbox_image)
            require(bool(probe_uid) and internal_allowed is True and external_denied is True, "deployment_runtime_mismatch")
            for key in ("scaledjob/chat-agent-worker", "job/adp-chat-supervisor-once", "configmap/chat-crew-pod-template"):
                require(
                    kube.get(paths[key])["metadata"].get("resourceVersion") == objects[key]["metadata"].get("resourceVersion"),
                    "deployment_runtime_mismatch",
                )
        except (KeyError, ValueError, IndexError, TypeError) as exc:
            raise CycleBlockedError("deployment_runtime_mismatch") from exc
        finally:
            kube.close()
        return RuntimeComponent(
            component=artifact.component,
            actual_revision=artifact.source_revision,
            artifact_hash=artifact_hash,
            image_digest=artifact.image_digest,
            manifests_sha256=artifact.manifests_sha256,
            healthy=True,
            evidence_ref=evidence_ref,
            observed_at=datetime.now(UTC),
            pod_uids=[probe_uid],
            asset_count=len(artifact.assets),
        )
