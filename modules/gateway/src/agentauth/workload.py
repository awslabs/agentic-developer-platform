"""Verify the pod presenting a run credential using Kubernetes TokenReview.

The shared worker IAM role authenticates transport only. A pod-bound token with
the dedicated audience, checked against the live pod and an approved image
digest, supplies the individual workload identity. No token claims are trusted
before the Kubernetes API verifies them.
"""

from __future__ import annotations

import os
import re
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from ipaddress import IPv4Address, IPv4Network
from pathlib import Path
from urllib.parse import urlsplit

import httpx

BOOTSTRAP_AUDIENCE = "adp-agent-bootstrap"
WORKLOAD_HEADER = "X-Adp-Workload-Token"
_SA_DIRECTORY = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class WorkloadRefusedError(Exception):
    """The request has no verified, approved workload identity."""


class WorkloadUnavailableError(WorkloadRefusedError):
    """Live workload authority could not be queried."""


@dataclass(frozen=True)
class VerifiedPod:
    uid: str
    name: str
    namespace: str
    service_account: str
    ip: str
    # Optional lifecycle evidence controls pause, not workload identity. A Jobs
    # API blip must not invalidate a task already assigned to the same verified pod.
    deadline_at: str | None = field(default=None, compare=False)
    image_digest: str | None = field(default=None, compare=False)
    run_hash: str | None = field(default=None, compare=False)
    session_hash: str | None = None


def _approved_chat_sandbox(metadata: dict, spec: dict, worker: dict, image_digest: str) -> bool:
    try:
        name = metadata.get("name", "")
        label = metadata.get("labels", {}).get("adp.io/run-hash", "")
        gateway_url = os.environ.get("ADP_CHAT_DATA_URL", "")
        ca_configmap = os.environ.get("ADP_CHAT_SANDBOX_CA_CONFIGMAP", "")
        aliases = spec["hostAliases"]
        gateway_ip = IPv4Address(aliases[0]["ip"])
        parsed = urlsplit(gateway_url)
        expected_env = {
            "ADP_CHAT_MODEL_POLICY_ENABLED": "true",
            "ADP_CHAT_DATA_ENABLED": "true",
            "ADP_CHAT_DATA_URL": gateway_url,
            "CONTEXT_STRATEGY": "gateway",
            "MEMORY_STRATEGY": "gateway",
            "ARTIFACT_STRATEGY": "gateway",
            "ADP_WORKLOAD_TOKEN_FILE": "/var/run/adp-model/token",
            "CLAUDE_CONFIG_DIR": "/tmp/workspace/.claude",
            "HOME": "/tmp/workspace",
            "NODE_EXTRA_CA_CERTS": "/var/run/adp-chat-ca/ca.crt",
        }
        env = worker.get("env", [])
        volumes = {volume["name"]: volume for volume in spec["volumes"]}
        projection = volumes["sandbox-identity"]["projected"]["sources"]
        mounts = {mount["name"]: mount for mount in worker["volumeMounts"]}
        security = worker["securityContext"]
        pod_security = spec["securityContext"]
        return (
            bool(re.fullmatch(r"chat-turn-[a-f0-9]{12}-[a-z0-9]{1,20}", name))
            and re.fullmatch(r"[a-f0-9]{64}", label) is not None
            and name.startswith(f"chat-turn-{label[:12]}-")
            and metadata.get("labels", {}).get("adp.io/chat-sandbox") == "true"
            and gateway_url == "https://chat-sandbox-gateway.adp-gateway.svc:8443"
            and re.fullmatch(r"chat-sandbox-gateway-ca-[a-f0-9]{16}", ca_configmap) is not None
            and any(gateway_ip in IPv4Network(network) for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))
            and aliases == [{"ip": str(gateway_ip), "hostnames": ["chat-sandbox-gateway.adp-gateway.svc"]}]
            and parsed.scheme == "https"
            and bool(parsed.hostname)
            and not parsed.username
            and not parsed.password
            and gateway_url == f"{parsed.scheme}://{parsed.netloc}"
            and spec.get("automountServiceAccountToken") is False
            and spec.get("enableServiceLinks") is False
            and spec.get("shareProcessNamespace") is False
            and spec.get("hostNetwork", False) is False
            and spec.get("hostPID", False) is False
            and spec.get("hostIPC", False) is False
            and spec.get("restartPolicy") == "Never"
            and (
                (metadata.get("labels", {}).get("adp.io/session-hash") is None and spec.get("activeDeadlineSeconds") == 900)
                or (
                    re.fullmatch(r"[a-f0-9]{64}", metadata.get("labels", {}).get("adp.io/session-hash", "")) is not None
                    and "activeDeadlineSeconds" not in spec
                )
            )
            and not spec.get("initContainers")
            and not spec.get("ephemeralContainers")
            and not spec.get("imagePullSecrets")
            and len(spec.get("containers", [])) == 1
            and len(volumes) == len(spec["volumes"]) == 3
            and volumes["gateway-ca"]
            == {
                "name": "gateway-ca",
                "configMap": {"name": ca_configmap, "defaultMode": 292, "items": [{"key": "ca.crt", "path": "ca.crt"}]},
            }
            and projection
            == [
                {
                    "serviceAccountToken": {
                        "audience": BOOTSTRAP_AUDIENCE,
                        "expirationSeconds": 600,
                        "path": "token",
                    }
                }
            ]
            and volumes["sandbox-identity"]["projected"].get("defaultMode") == 292
            and volumes["scratch"] == {"name": "scratch", "emptyDir": {"sizeLimit": "512Mi"}}
            and len(mounts) == len(worker["volumeMounts"]) == 3
            and mounts["gateway-ca"] == {"name": "gateway-ca", "mountPath": "/var/run/adp-chat-ca", "readOnly": True}
            and mounts["sandbox-identity"]
            == {
                "name": "sandbox-identity",
                "mountPath": "/var/run/adp-model",
                "readOnly": True,
            }
            and mounts["scratch"]
            in (
                {"name": "scratch", "mountPath": "/tmp"},
                {"name": "scratch", "mountPath": "/tmp", "readOnly": False},
            )
            and pod_security.get("runAsNonRoot") is True
            and pod_security.get("runAsUser") == 10001
            and pod_security.get("fsGroup") == 10001
            and pod_security.get("seccompProfile") == {"type": "RuntimeDefault"}
            and worker.get("image", "").endswith("@" + image_digest)
            and worker.get("imagePullPolicy") == "IfNotPresent"
            and worker.get("command") == ["/app/chat-sandbox-entrypoint"]
            and not worker.get("args")
            and not worker.get("envFrom")
            and worker.get("workingDir") == "/tmp"
            and len(env) == len(expected_env)
            and all(entry == {"name": key, "value": value} for entry in env for key, value in expected_env.items() if entry.get("name") == key)
            and {entry.get("name") for entry in env} == set(expected_env)
            and security.get("runAsNonRoot") is True
            and security.get("runAsUser") == 10001
            and security.get("readOnlyRootFilesystem") is True
            and security.get("allowPrivilegeEscalation") is False
            and security.get("privileged") in (None, False)
            and security.get("procMount") in (None, "Default")
            and not worker.get("volumeDevices")
            and not worker.get("ports")
            and security.get("capabilities") == {"drop": ["ALL"]}
            and security.get("seccompProfile") == {"type": "RuntimeDefault"}
        )
    except (KeyError, IndexError, TypeError, AttributeError, ValueError):
        return False


class KubernetesWorkloadVerifier:
    def __init__(
        self,
        *,
        client: httpx.Client,
        image_digests: frozenset[str],
        namespace: str = "adp-agents",
        service_account: str = "agent-scaledjob-sa",
        container_name: str = "agent-worker",
        authority_flag: str = "ADP_AGENT_AUTHORITY_ENABLED",
        gateway_token_path: Path = _SA_DIRECTORY / "token",
        chat_sandbox: bool = False,
    ) -> None:
        if not image_digests or any(not _DIGEST.fullmatch(d) for d in image_digests):
            raise WorkloadRefusedError("approved worker image digests are required")
        if not _NAME.fullmatch(namespace) or not _NAME.fullmatch(service_account):
            raise WorkloadRefusedError("invalid workload configuration")
        self._client = client
        self._digests = image_digests
        self._namespace = namespace
        self._service_account = service_account
        if not _NAME.fullmatch(container_name) or authority_flag not in {
            "ADP_AGENT_AUTHORITY_ENABLED",
            "ADP_CHAT_MODEL_POLICY_ENABLED",
            "ADP_TASK_API_WORKER_ENABLED",
        }:
            raise WorkloadRefusedError("invalid workload configuration")
        self._container_name = container_name
        self._authority_flag = authority_flag
        self._gateway_token_path = gateway_token_path
        self._chat_sandbox = chat_sandbox

    @property
    def exit_retention(self):
        from src.agentauth.exit_retention import PodExitRetention

        return PodExitRetention(
            client=self._client,
            namespace=self._namespace,
            service_account=self._service_account,
            token_path=self._gateway_token_path,
        )

    @classmethod
    def in_cluster(cls, *, chat: bool = False, task_api: bool = False, chat_sandbox: bool = False) -> KubernetesWorkloadVerifier:
        # Fixed service DNS and the mounted cluster CA; neither comes from a
        # request. Do not inherit HTTP proxy settings for this credential path.
        if sum((chat, task_api, chat_sandbox)) > 1:
            raise WorkloadRefusedError("ambiguous workload configuration")
        if chat_sandbox:
            digest_env = "ADP_CHAT_SANDBOX_IMAGE_DIGESTS"
            service_account = "adp-chat-sandbox"
        elif task_api:
            digest_env = "ADP_TASK_WORKER_IMAGE_DIGESTS"
            service_account = os.environ.get("ADP_TASK_WORKER_SERVICE_ACCOUNT", "agent-scaledjob-sa")
        elif chat:
            digest_env = "ADP_CHAT_WORKER_IMAGE_DIGESTS"
            service_account = os.environ.get("ADP_CHAT_WORKER_SERVICE_ACCOUNT", "adp-agent")
        else:
            digest_env = "AGENT_WORKER_IMAGE_DIGESTS"
            service_account = os.environ.get("AGENT_WORKER_SERVICE_ACCOUNT", "agent-authority-worker-sa")
        context = ssl.create_default_context(cafile=str(_SA_DIRECTORY / "ca.crt"))
        return cls(
            client=httpx.Client(
                base_url="https://kubernetes.default.svc",
                verify=context,
                timeout=5.0,
                follow_redirects=False,
                trust_env=False,
            ),
            image_digests=frozenset(filter(None, os.environ.get(digest_env, "").split(","))),
            namespace=(
                os.environ.get("ADP_TASK_WORKER_NAMESPACE", "adp-agents")
                if task_api
                else os.environ.get("ADP_CHAT_WORKER_NAMESPACE", "adp-gateway-agents")
                if chat or chat_sandbox
                else os.environ.get("AGENT_WORKER_NAMESPACE", "adp-agents")
            ),
            service_account=service_account,
            container_name="chat-agent" if chat or chat_sandbox else "agent-worker",
            chat_sandbox=chat_sandbox,
            authority_flag=(
                "ADP_TASK_API_WORKER_ENABLED"
                if task_api
                else "ADP_CHAT_MODEL_POLICY_ENABLED"
                if chat or chat_sandbox
                else "ADP_AGENT_AUTHORITY_ENABLED"
            ),
        )

    def verify(self, token: str) -> VerifiedPod:
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            raise WorkloadRefusedError("workload refused")
        try:
            # Reread the gateway's projected token too: Kubernetes rotates it
            # while this service is running.
            gateway_token = self._gateway_token_path.read_text().strip()
            if not gateway_token:
                raise WorkloadUnavailableError("workload verifier unavailable")
            headers = {"Authorization": f"Bearer {gateway_token}"}
            review = self._client.post(
                "/apis/authentication.k8s.io/v1/tokenreviews",
                headers=headers,
                json={
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "TokenReview",
                    "spec": {"token": token, "audiences": [BOOTSTRAP_AUDIENCE]},
                },
            )
            review.raise_for_status()
            status = review.json()["status"]
            user = status["user"]
            if (
                status.get("authenticated") is not True
                or not isinstance(status.get("audiences"), list)
                or BOOTSTRAP_AUDIENCE not in status.get("audiences", [])
                or user.get("username") != f"system:serviceaccount:{self._namespace}:{self._service_account}"
            ):
                raise WorkloadRefusedError("workload refused")
            extra = user["extra"]
            names = extra["authentication.kubernetes.io/pod-name"]
            uids = extra["authentication.kubernetes.io/pod-uid"]
            if not isinstance(names, list) or len(names) != 1 or not isinstance(uids, list) or len(uids) != 1:
                raise WorkloadRefusedError("workload refused")
            name, uid = names[0], uids[0]
            if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(uid, str) or not uid:
                raise WorkloadRefusedError("workload refused")
            return self.verify_bound(name=name, uid=uid)
        except WorkloadRefusedError:
            raise
        except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            # Do not expose an HTTP exception or request body: TokenReview
            # contains the worker credential, and Authorization contains ours.
            raise WorkloadUnavailableError("workload verifier unavailable") from None

    def verify_bound(self, *, name: str, uid: str) -> VerifiedPod:
        """Recheck a previously TokenReview-bound pod from protected metadata."""
        if not isinstance(name, str) or not _NAME.fullmatch(name) or not isinstance(uid, str) or not uid:
            raise WorkloadRefusedError("workload refused")
        try:
            headers = {"Authorization": f"Bearer {self._gateway_token_path.read_text().strip()}"}
            response = self._client.get(f"/api/v1/namespaces/{self._namespace}/pods/{name}", headers=headers)
            response.raise_for_status()
            pod = response.json()
            metadata, spec, pod_status = pod["metadata"], pod["spec"], pod["status"]
            containers = [c for c in pod_status.get("containerStatuses", []) if c.get("name") == self._container_name]
            worker_specs = [c for c in spec.get("containers", []) if c.get("name") == self._container_name]
            enabled = [e for e in worker_specs[0].get("env", []) if e.get("name") == self._authority_flag] if len(worker_specs) == 1 else []
            if (
                metadata.get("uid") != uid
                or metadata.get("name") != name
                or metadata.get("namespace") != self._namespace
                or metadata.get("deletionTimestamp")
                or spec.get("serviceAccountName") != self._service_account
                or pod_status.get("phase") != "Running"
                or len(containers) != 1
                or len(worker_specs) != 1
                or (not self._chat_sandbox and (worker_specs[0].get("command") or worker_specs[0].get("args")))
                or (
                    self._chat_sandbox
                    and not _approved_chat_sandbox(
                        metadata,
                        spec,
                        worker_specs[0],
                        containers[0].get("imageID", "").rsplit("@", 1)[-1],
                    )
                )
                or enabled != [{"name": self._authority_flag, "value": "true"}]
                or "running" not in containers[0].get("state", {})
                or containers[0].get("imageID", "").rsplit("@", 1)[-1] not in self._digests
                or not pod_status.get("podIP")
            ):
                raise WorkloadRefusedError("workload refused")
            return VerifiedPod(
                uid,
                name,
                self._namespace,
                self._service_account,
                pod_status["podIP"],
                self._deadline(pod, headers),
                containers[0]["imageID"].rsplit("@", 1)[-1],
                metadata.get("labels", {}).get("adp.io/run-hash") if self._chat_sandbox else None,
                metadata.get("labels", {}).get("adp.io/session-hash") if self._chat_sandbox else None,
            )
        except WorkloadRefusedError:
            raise
        except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise WorkloadUnavailableError("workload verifier unavailable") from None

    def _deadline(self, pod: dict, headers: dict) -> str | None:
        """Conservative absolute lifetime from Kubernetes, including Job retries.

        A Job's activeDeadlineSeconds starts before registration and before a
        replacement pod. Creation time is an earlier bound than status.startTime
        and remains conservative across suspension/resumption. Missing lifecycle
        evidence disables pause; it does not disable unrelated worker services.
        """
        try:
            bounds = []

            def add_bound(resource):
                seconds = resource["spec"].get("activeDeadlineSeconds")
                if type(seconds) is not int or seconds <= 0:
                    return
                created = datetime.fromisoformat(resource["metadata"]["creationTimestamp"].replace("Z", "+00:00"))
                if created.tzinfo is None or created > datetime.now(UTC):
                    raise ValueError("invalid lifecycle timestamp")
                bounds.append(created + timedelta(seconds=seconds))

            add_bound(pod)
            owners = [owner for owner in pod["metadata"].get("ownerReferences", []) if owner.get("controller") is True]
            if owners:
                if len(owners) != 1:
                    return None
                owner = owners[0]
                name = owner.get("name", "")
                if owner.get("kind") != "Job" or owner.get("apiVersion") != "batch/v1" or not _NAME.fullmatch(name) or not owner.get("uid"):
                    return None
                response = self._client.get(f"/apis/batch/v1/namespaces/{self._namespace}/jobs/{name}", headers=headers)
                response.raise_for_status()
                job = response.json()
                metadata = job["metadata"]
                if metadata.get("uid") != owner["uid"] or metadata.get("name") != name or metadata.get("namespace") != self._namespace:
                    return None
                add_bound(job)
            return min(bounds).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ") if bounds else None
        except (httpx.HTTPError, KeyError, ValueError, TypeError, AttributeError, OverflowError):
            return None

    @property
    def observation_scope(self) -> dict:
        return {"api_server": str(self._client.base_url), "namespace": self._namespace, "service_account": self._service_account}

    def is_absent(self, *, name: str) -> bool:
        """Confirm deletion only after the caller durably recorded UID-bound exit."""
        if not _NAME.fullmatch(name):
            return False
        try:
            response = self._client.get(
                f"/api/v1/namespaces/{self._namespace}/pods/{name}",
                headers={"Authorization": f"Bearer {self._gateway_token_path.read_text().strip()}"},
            )
            if response.status_code != 404:
                return False
            status = response.json()
            return (
                status.get("kind") == "Status"
                and status.get("reason") == "NotFound"
                and status.get("details", {}).get("name") == name
                and status.get("details", {}).get("kind") == "pods"
            )
        except (OSError, httpx.HTTPError, ValueError, TypeError, AttributeError):
            return False

    def has_exited(self, *, name: str, uid: str, image_digest: str | None = None) -> bool:
        """Positive container-exit evidence for a previously verified workload.

        A timeout, missing pod or reused name cannot prove the original worker
        stopped. Lease expiry is deliberately absent from this decision.
        """
        if self._chat_sandbox and not image_digest:
            return False
        details = self._exit_details(name=name, uid=uid)
        return details is not None and (not self._chat_sandbox or details[0] == image_digest)

    def exited_sandbox_image(self, *, name: str, uid: str, run_hash: str) -> str | None:
        if not self._chat_sandbox:
            return None
        details = self._exit_details(name=name, uid=uid)
        return details[0] if details is not None and details[1] == run_hash else None

    def approved_sandbox_image(self, image_digest: str) -> bool:
        return self._chat_sandbox and image_digest in self._digests

    def find_exited_sandbox(self, *, name: str, run_hash: str, image_digest: str) -> str | None:
        if not self.approved_sandbox_image(image_digest):
            return None
        details = self._exit_details(name=name, uid=None)
        return details[2] if details is not None and details[:2] == (image_digest, run_hash) else None

    def find_reserved_sandbox(self, *, name: str, run_hash: str, image_digest: str, session_hash: str) -> VerifiedPod | None:
        if not _NAME.fullmatch(name) or not self.approved_sandbox_image(image_digest):
            raise WorkloadRefusedError("chat reservation refused")
        try:
            response = self._client.get(
                f"/api/v1/namespaces/{self._namespace}/pods/{name}",
                headers={"Authorization": f"Bearer {self._gateway_token_path.read_text().strip()}"},
            )
            if response.status_code == 404:
                status = response.json()
                if (
                    status.get("kind") == "Status"
                    and status.get("reason") == "NotFound"
                    and status.get("details", {}).get("name") == name
                    and status.get("details", {}).get("kind") == "pods"
                ):
                    return None
                raise WorkloadUnavailableError("chat reservation observation unavailable")
            response.raise_for_status()
            document = response.json()
            if document.get("status", {}).get("phase") == "Pending":
                raise WorkloadUnavailableError("chat reserved pod not running")
            pod = self.verify_bound(name=name, uid=document["metadata"]["uid"])
            if (pod.run_hash, pod.session_hash, pod.image_digest) != (run_hash, session_hash, image_digest):
                raise WorkloadRefusedError("chat reserved pod binding changed")
            return pod
        except WorkloadRefusedError:
            raise
        except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise WorkloadUnavailableError("chat reservation observation unavailable") from None

    def _exit_details(self, *, name: str, uid: str | None) -> tuple[str, str | None, str] | None:
        if not _NAME.fullmatch(name) or (uid is not None and not uid):
            return None
        try:
            response = self._client.get(
                f"/api/v1/namespaces/{self._namespace}/pods/{name}",
                headers={"Authorization": f"Bearer {self._gateway_token_path.read_text().strip()}"},
            )
            response.raise_for_status()
            pod = response.json()
            metadata, spec, status = pod.get("metadata", {}), pod.get("spec", {}), pod.get("status", {})
            if (uid is not None and metadata.get("uid") != uid) or spec.get("serviceAccountName") != self._service_account:
                return None
            if self._chat_sandbox and (metadata.get("name") != name or metadata.get("namespace") != self._namespace):
                return None
            container_name = "chat-agent" if self._chat_sandbox else "agent-worker"
            workers = [c for c in status.get("containerStatuses", []) if c.get("name") == container_name]
            resolved = workers[0].get("imageID", "").rsplit("@", 1)[-1] if len(workers) == 1 else ""
            if self._chat_sandbox:
                containers = [c for c in spec.get("containers", []) if c.get("name") == container_name]
                if (
                    len(containers) != 1
                    or not re.fullmatch(r"[a-z0-9-]{1,128}", metadata.get("uid", ""))
                    or resolved not in self._digests
                    or not _approved_chat_sandbox(metadata, spec, containers[0], resolved)
                ):
                    return None
            terminated = workers[0].get("state", {}).get("terminated") if len(workers) == 1 else None
            if isinstance(terminated, dict) and type(terminated.get("exitCode")) is int and status.get("phase") in {"Succeeded", "Failed"}:
                return resolved, metadata.get("labels", {}).get("adp.io/run-hash"), metadata.get("uid", "")
            return None
        except (OSError, httpx.HTTPError, ValueError, TypeError, AttributeError):
            return None
