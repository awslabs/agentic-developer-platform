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
from dataclasses import dataclass
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


class KubernetesWorkloadVerifier:
    def __init__(
        self,
        *,
        client: httpx.Client,
        image_digests: frozenset[str],
        namespace: str = "adp-agents",
        service_account: str = "agent-scaledjob-sa",
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
        self._gateway_token_path = gateway_token_path

    @classmethod
    def in_cluster(cls) -> KubernetesWorkloadVerifier:
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
            image_digests=frozenset(filter(None, os.environ.get("AGENT_WORKER_IMAGE_DIGESTS", "").split(","))),
            namespace=os.environ.get("AGENT_WORKER_NAMESPACE", "adp-agents"),
            service_account=os.environ.get("AGENT_WORKER_SERVICE_ACCOUNT", "agent-authority-worker-sa"),
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
            response = self._client.get(f"/api/v1/namespaces/{self._namespace}/pods/{name}", headers=headers)
            response.raise_for_status()
            pod = response.json()
            metadata, spec, pod_status = pod["metadata"], pod["spec"], pod["status"]
            containers = [c for c in pod_status.get("containerStatuses", []) if c.get("name") == "agent-worker"]
            worker_specs = [c for c in spec.get("containers", []) if c.get("name") == "agent-worker"]
            enabled = [e for e in worker_specs[0].get("env", []) if e.get("name") == "ADP_AGENT_AUTHORITY_ENABLED"] if len(worker_specs) == 1 else []
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
                or enabled != [{"name": "ADP_AGENT_AUTHORITY_ENABLED", "value": "true"}]
                or "running" not in containers[0].get("state", {})
                or containers[0].get("imageID", "").rsplit("@", 1)[-1] not in self._digests
                or not pod_status.get("podIP")
            ):
                raise WorkloadRefusedError("workload refused")
            return VerifiedPod(uid, name, self._namespace, self._service_account, pod_status["podIP"])
        except WorkloadRefusedError:
            raise
        except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            # Do not expose an HTTP exception or request body: TokenReview
            # contains the worker credential, and Authorization contains ours.
            raise WorkloadRefusedError("workload verifier unavailable") from None
