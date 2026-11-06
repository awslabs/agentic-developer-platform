"""Kubernetes TokenReview binding for the role-free Crew checkpoint helper."""

import os
import re
import ssl
from dataclasses import dataclass
from pathlib import Path

import httpx

AUDIENCE = "adp-checkpoint-upload"
SERVICE_ACCOUNT = "adp-crew-checkpoint-helper"
NAMESPACE = "adp-gateway-agents"
TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
HEX = re.compile(r"^[0-9a-f]+$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


class CheckpointIdentityRefusedError(Exception):
    pass


class CheckpointIdentityUnavailableError(Exception):
    pass


@dataclass(frozen=True)
class CheckpointHelperIdentity:
    pod_uid: str
    owner_key: str
    checkpoint_hash: str
    image_digest: str
    checkpoint_id: str
    pod_name: str = ""


class CheckpointHelperVerifier:
    audience = AUDIENCE
    service_account = SERVICE_ACCOUNT
    image_digests_env = "ADP_CREW_CHECKPOINT_HELPER_IMAGE_DIGESTS"
    pod_prefix = "chat-checkpoint-"
    helper_label = "adp.io/crew-checkpoint-helper"
    command = "/app/chat-checkpoint-helper"
    read_only_home = True

    def __init__(self, client: httpx.Client, allowed_digests: frozenset[str], token_path: Path = TOKEN_PATH):
        if not allowed_digests or any(not DIGEST.fullmatch(value) for value in allowed_digests):
            raise CheckpointIdentityRefusedError("approved checkpoint image required")
        self.client = client
        self.allowed_digests = allowed_digests
        self.token_path = token_path

    @classmethod
    def in_cluster(cls):
        context = ssl.create_default_context(cafile=str(CA_PATH))
        digests = frozenset(filter(None, os.environ.get(cls.image_digests_env, "").split(",")))
        return cls(
            httpx.Client(base_url="https://kubernetes.default.svc", verify=context, timeout=5.0, follow_redirects=False, trust_env=False), digests
        )

    def verify(self, token: str) -> CheckpointHelperIdentity:
        if not isinstance(token, str) or not 1 <= len(token) <= 8192:
            raise CheckpointIdentityRefusedError("invalid helper token")
        try:
            gateway_token = self.token_path.read_text().strip()
            if not gateway_token:
                raise CheckpointIdentityUnavailableError("checkpoint identity unavailable")
            authorization = {"Authorization": f"Bearer {gateway_token}"}
            review = self.client.post(
                "/apis/authentication.k8s.io/v1/tokenreviews",
                headers=authorization,
                json={
                    "apiVersion": "authentication.k8s.io/v1",
                    "kind": "TokenReview",
                    "spec": {"token": token, "audiences": [self.audience]},
                },
            )
            review.raise_for_status()
            status = review.json()["status"]
            user = status["user"]
            if (
                status.get("authenticated") is not True
                or not isinstance(status.get("audiences"), list)
                or self.audience not in status["audiences"]
                or user.get("username") != f"system:serviceaccount:{NAMESPACE}:{self.service_account}"
            ):
                raise CheckpointIdentityRefusedError("helper identity refused")
            extra = user["extra"]
            names = extra["authentication.kubernetes.io/pod-name"]
            uids = extra["authentication.kubernetes.io/pod-uid"]
            if not isinstance(names, list) or len(names) != 1 or not isinstance(uids, list) or len(uids) != 1:
                raise CheckpointIdentityRefusedError("helper pod binding refused")
            name, uid = names[0], uids[0]
            if (
                not isinstance(name, str)
                or not re.fullmatch(rf"{self.pod_prefix}[a-f0-9]{{12}}-[a-z0-9]{{5}}", name)
                or not isinstance(uid, str)
                or not uid
            ):
                raise CheckpointIdentityRefusedError("helper pod binding refused")
            result = self.client.get(f"/api/v1/namespaces/{NAMESPACE}/pods/{name}", headers=authorization)
            result.raise_for_status()
            pod = result.json()
            metadata, spec, pod_status = pod["metadata"], pod["spec"], pod["status"]
            labels = metadata["labels"]
            annotations = metadata.get("annotations", {})
            owner_key = labels.get("adp.io/owner-key", "")
            checkpoint_hash = annotations.get("adp.io/checkpoint-hash", "")
            session_hash = annotations.get("adp.io/session-hash", "")
            (container,) = spec["containers"]
            (running,) = pod_status["containerStatuses"]
            image = running["imageID"].rsplit("@", 1)[-1]
            volumes = {volume["name"]: volume for volume in spec["volumes"]}
            mounts = {mount["name"]: mount for mount in container["volumeMounts"]}
            projection = volumes["checkpoint-identity"]["projected"]
            identity_token = projection["sources"][0]["serviceAccountToken"]
            expected_env = {
                "ADP_WORKLOAD_TOKEN_FILE": "/var/run/adp-checkpoint/token",
                "ADP_CHAT_DATA_URL": "https://chat-sandbox-gateway.adp-gateway.svc:8443",
                "NODE_EXTRA_CA_CERTS": "/var/run/adp-chat-ca/ca.crt",
                "ADP_OWNER_KEY": owner_key,
                "KIROCREW_HOME": "/mnt/crew",
            }
            actual_env = {item["name"]: item.get("value") for item in container.get("env", [])}
            if not (
                metadata.get("uid") == uid
                and metadata.get("name") == name
                and metadata.get("namespace") == NAMESPACE
                and not metadata.get("deletionTimestamp")
                and labels.get(self.helper_label) == "true"
                and len(owner_key) == 52
                and HEX.fullmatch(owner_key)
                and len(checkpoint_hash) == 64
                and HEX.fullmatch(checkpoint_hash)
                and len(session_hash) == 64
                and HEX.fullmatch(session_hash)
                and name.startswith(f"{self.pod_prefix}{checkpoint_hash[:12]}-")
                and spec.get("serviceAccountName") == self.service_account
                and len(container.get("env", [])) == len(actual_env) == 6
                and isinstance(actual_env.get("ADP_CHECKPOINT_ID"), str)
                and re.fullmatch(r"[a-f0-9-]{36}", actual_env["ADP_CHECKPOINT_ID"])
                and {key: value for key, value in actual_env.items() if key != "ADP_CHECKPOINT_ID"} == expected_env
                and spec.get("automountServiceAccountToken") is False
                and pod_status.get("phase") == "Running"
                and running.get("state", {}).get("running") is not None
                and container.get("command") == [self.command]
                and image in self.allowed_digests
                and container.get("image", "").endswith("@" + image)
                and len(volumes) == len(spec["volumes"]) == 4
                and len(mounts) == len(container["volumeMounts"]) == 4
                and identity_token == {"audience": self.audience, "expirationSeconds": 600, "path": "token"}
                and mounts["checkpoint-identity"] == {"name": "checkpoint-identity", "mountPath": "/var/run/adp-checkpoint", "readOnly": True}
                and volumes["crew-home"]["persistentVolumeClaim"] == {"claimName": "crew-home-" + owner_key, "readOnly": self.read_only_home}
                and mounts["crew-home"] == {"name": "crew-home", "mountPath": "/mnt/crew", "readOnly": self.read_only_home}
                and container["securityContext"].get("readOnlyRootFilesystem") is True
                and container["securityContext"].get("allowPrivilegeEscalation") is False
                and container["securityContext"].get("runAsUser") == 10001
            ):
                raise CheckpointIdentityRefusedError("helper pod refused")
            return CheckpointHelperIdentity(uid, owner_key, checkpoint_hash, image, actual_env["ADP_CHECKPOINT_ID"], name)
        except CheckpointIdentityRefusedError:
            raise
        except CheckpointIdentityUnavailableError:
            raise
        except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise CheckpointIdentityUnavailableError("checkpoint identity unavailable") from None


class RestoreHelperVerifier(CheckpointHelperVerifier):
    audience = "adp-checkpoint-restore"
    service_account = "adp-crew-restore-helper"
    image_digests_env = "ADP_CREW_RESTORE_HELPER_IMAGE_DIGESTS"
    pod_prefix = "chat-restore-"
    helper_label = "adp.io/crew-restore-helper"
    command = "/app/chat-checkpoint-restore"
    read_only_home = False

    def pod_is_gone(self, pod_name: str, pod_uid: str) -> bool:
        if not re.fullmatch(r"chat-restore-[a-f0-9]{12}-[a-z0-9]{5}", pod_name) or not pod_uid:
            raise CheckpointIdentityRefusedError("unknown restore pin owner")
        try:
            token = self.token_path.read_text().strip()
            if not token:
                raise CheckpointIdentityUnavailableError("checkpoint identity unavailable")
            response = self.client.get(f"/api/v1/namespaces/{NAMESPACE}/pods/{pod_name}", headers={"Authorization": f"Bearer {token}"})
            if response.status_code == 404:
                return True
            response.raise_for_status()
            pod = response.json()
            if pod["metadata"]["uid"] != pod_uid:
                return True
            return pod["status"]["phase"] in ("Succeeded", "Failed") and all(
                status.get("state", {}).get("terminated") is not None for status in pod["status"]["containerStatuses"]
            )
        except CheckpointIdentityUnavailableError:
            raise
        except (OSError, httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
            raise CheckpointIdentityUnavailableError("checkpoint identity unavailable") from None
