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
from pathlib import Path

import httpx

BOOTSTRAP_AUDIENCE = "adp-agent-bootstrap"
WORKLOAD_HEADER = "X-Adp-Workload-Token"
_SA_DIRECTORY = Path("/var/run/secrets/kubernetes.io/serviceaccount")
_NAME = re.compile(r"[a-z0-9](?:[-a-z0-9.]{0,251}[a-z0-9])?\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class WorkloadRefusedError(Exception):
    """The request has no verified, approved workload identity."""


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
    ) -> None:
        if not image_digests or any(not _DIGEST.fullmatch(d) for d in image_digests):
            raise WorkloadRefusedError("approved worker image digests are required")
        if not _NAME.fullmatch(namespace) or not _NAME.fullmatch(service_account):
            raise WorkloadRefusedError("invalid workload configuration")
        self._client = client
        self._digests = image_digests
        self._namespace = namespace
        self._service_account = service_account
        if not _NAME.fullmatch(container_name) or authority_flag not in {"ADP_AGENT_AUTHORITY_ENABLED", "ADP_CHAT_MODEL_POLICY_ENABLED"}:
            raise WorkloadRefusedError("invalid workload configuration")
        self._container_name = container_name
        self._authority_flag = authority_flag
        self._gateway_token_path = gateway_token_path

    @property
    def exit_retention(self):
        from src.agentauth.exit_retention import PodExitRetention

        return PodExitRetention(
            client=self._client, namespace=self._namespace,
            service_account=self._service_account, token_path=self._gateway_token_path,
        )

    @classmethod
    def in_cluster(cls, *, chat: bool = False) -> KubernetesWorkloadVerifier:
        # Fixed service DNS and the mounted cluster CA; neither comes from a
        # request. Do not inherit HTTP proxy settings for this credential path.
        context = ssl.create_default_context(cafile=str(_SA_DIRECTORY / "ca.crt"))
        return cls(
            client=httpx.Client(
                base_url="https://kubernetes.default.svc",
                verify=context,
                timeout=5.0,
                follow_redirects=False,
                trust_env=False,
            ),
            image_digests=frozenset(
                filter(None, os.environ.get("ADP_CHAT_WORKER_IMAGE_DIGESTS" if chat else "AGENT_WORKER_IMAGE_DIGESTS", "").split(","))
            ),
            namespace=os.environ.get("ADP_CHAT_WORKER_NAMESPACE", "adp-gateway-agents")
            if chat
            else os.environ.get("AGENT_WORKER_NAMESPACE", "adp-agents"),
            service_account=os.environ.get("ADP_CHAT_WORKER_SERVICE_ACCOUNT", "adp-agent")
            if chat
            else os.environ.get("AGENT_WORKER_SERVICE_ACCOUNT", "agent-authority-worker-sa"),
            container_name="chat-agent" if chat else "agent-worker",
            authority_flag="ADP_CHAT_MODEL_POLICY_ENABLED" if chat else "ADP_AGENT_AUTHORITY_ENABLED",
        )

    def verify(self, token: str) -> VerifiedPod:
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            raise WorkloadRefusedError("workload refused")
        try:
            # Reread the gateway's projected token too: Kubernetes rotates it
            # while this service is running.
            gateway_token = self._gateway_token_path.read_text().strip()
            if not gateway_token:
                raise WorkloadRefusedError("workload verifier unavailable")
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
            raise WorkloadRefusedError("workload verifier unavailable") from None

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
                or worker_specs[0].get("command")
                or worker_specs[0].get("args")
                or enabled != [{"name": self._authority_flag, "value": "true"}]
                or "running" not in containers[0].get("state", {})
                or containers[0].get("imageID", "").rsplit("@", 1)[-1] not in self._digests
                or not pod_status.get("podIP")
            ):
                raise WorkloadRefusedError("workload refused")
            return VerifiedPod(uid, name, self._namespace, self._service_account, pod_status["podIP"], self._deadline(pod, headers))
        except WorkloadRefusedError:
            raise
        except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise WorkloadRefusedError("workload verifier unavailable") from None

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

    def has_exited(self, *, name: str, uid: str) -> bool:
        """Positive container-exit evidence for a previously verified workload.

        A timeout, missing pod or reused name cannot prove the original worker
        stopped. Lease expiry is deliberately absent from this decision.
        """
        if not _NAME.fullmatch(name) or not uid:
            return False
        try:
            response = self._client.get(
                f"/api/v1/namespaces/{self._namespace}/pods/{name}",
                headers={"Authorization": f"Bearer {self._gateway_token_path.read_text().strip()}"},
            )
            response.raise_for_status()
            pod = response.json()
            if pod.get("metadata", {}).get("uid") != uid or pod.get("spec", {}).get("serviceAccountName") != self._service_account:
                return False
            workers = [c for c in pod.get("status", {}).get("containerStatuses", []) if c.get("name") == "agent-worker"]
            return len(workers) == 1 and "terminated" in workers[0].get("state", {}) and pod.get("status", {}).get("phase") in {"Succeeded", "Failed"}
        except (OSError, httpx.HTTPError, ValueError, TypeError, AttributeError):
            return False
