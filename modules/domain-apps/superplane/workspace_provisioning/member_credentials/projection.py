"""CAS publication to pre-installed, UID-pinned credential Secrets.

Only the selected workspace key and its ownership annotation are changed. A
conflict is surfaced to the journalled reconciler; blindly retrying stale intent
could overwrite another rotation. Transport exceptions never expose token bodies.
"""

import base64
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import json

from superplane_bootstrap.errors import BootstrapRefused
from superplane_bootstrap.kube_grants import KubeGrants

from .binding import CredentialBinding, IssuedCredential, canonical, uid


@dataclass(frozen=True)
class ProjectionReceipt:
    metadata_json: str
    content_digest: str
    secret_uid: str
    resource_version: str

    @property
    def marker(self):
        return canonical(
            {"binding": json.loads(self.metadata_json), "sha256": self.content_digest}
        )


def pointer(value):
    return value.replace("~", "~0").replace("/", "~1")


class SecretProjector:
    def __init__(
        self,
        grants,
        authorize,
        *,
        namespace,
        namespace_uid,
        secret_name,
        secret_uid,
        scope,
        now=lambda: datetime.now(UTC),
    ):
        if not isinstance(grants, KubeGrants) or not callable(authorize):
            raise BootstrapRefused(
                "projection requires verified control-plane transport and authority"
            )
        import re

        if (
            scope not in {"reader", "mutator"}
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", namespace)
            or not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", secret_name)
        ):
            raise BootstrapRefused("projection Secret target is invalid")
        self.grants, self.authorize = grants, authorize
        self.namespace, self.namespace_uid = namespace, uid(namespace_uid)
        self.secret_name, self.secret_uid = secret_name, uid(secret_uid)
        self.scope, self.now = scope, now

    def _verify(self, binding, action):
        if (
            binding.scope != self.scope
            or binding.membership.org_id != self.grants.target.org_id
        ):
            raise BootstrapRefused(
                "projection organization or credential scope differs"
            )
        self.authorize(binding, action)
        self.grants._verify_transport()

    def _read(self, binding, action):
        self._verify(binding, action)
        namespace = self.grants.client.resources.get(
            api_version="v1", kind="Namespace"
        ).get(name=self.namespace)
        namespace = namespace.to_dict() if hasattr(namespace, "to_dict") else namespace
        if (
            namespace.get("metadata", {}).get("uid") != self.namespace_uid
            or namespace.get("metadata", {}).get("deletionTimestamp")
            or namespace.get("status", {}).get("phase") != "Active"
        ):
            raise BootstrapRefused("projection namespace changed or is terminating")
        self._verify(binding, action)
        resource = self.grants.client.resources.get(api_version="v1", kind="Secret")
        try:
            body = resource.get(name=self.secret_name, namespace=self.namespace)
            body = body.to_dict() if hasattr(body, "to_dict") else body
        except Exception:
            raise BootstrapRefused("credential projection Secret unavailable") from None
        if (
            body.get("metadata", {}).get("uid") != self.secret_uid
            or not body["metadata"].get("resourceVersion")
            or body["metadata"].get("deletionTimestamp")
            or body.get("immutable") is True
            or body.get("type", "Opaque") != "Opaque"
        ):
            raise BootstrapRefused("credential projection Secret identity changed")
        self._verify(binding, action)
        return resource, body

    @staticmethod
    def _names(binding):
        workspace = binding.membership.workspace_id
        return workspace + ".kubeconfig", "superplane.aws-e/credential-" + workspace

    @staticmethod
    def _owned(body, key, annotation, receipt):
        encoded = (body.get("data") or {}).get(key)
        marker = (body.get("metadata", {}).get("annotations") or {}).get(annotation)
        if receipt is None:
            return encoded is None and marker is None
        if not isinstance(encoded, str) or marker != receipt.marker:
            return False
        try:
            raw = base64.b64decode(encoded, validate=True)
        except ValueError:
            return False
        return hashlib.sha256(raw).hexdigest() == receipt.content_digest

    def _validate_receipt(self, binding, receipt, *, previous=False):
        try:
            if receipt.secret_uid != self.secret_uid or not receipt.resource_version:
                raise ValueError()
            metadata = json.loads(receipt.metadata_json)
            expected = binding.metadata(
                metadata["service_account_uid"],
                datetime.fromisoformat(metadata["expires_at"]),
            )
            if previous:
                expected["revision"] = metadata["revision"]
                if (
                    type(metadata["revision"]) is not int
                    or not 1 <= metadata["revision"] < binding.revision
                ):
                    raise ValueError()
            if metadata != expected:
                raise ValueError()
        except (ValueError, TypeError, KeyError, AttributeError):
            raise BootstrapRefused(
                "projection receipt belongs to another membership or revision"
            ) from None

    def _patch(self, binding, action, resource, body, changes):
        self._verify(binding, action)
        patch = [
            {"op": "test", "path": "/metadata/uid", "value": self.secret_uid},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": body["metadata"]["resourceVersion"],
            },
            *changes,
        ]
        try:
            resource.patch(
                name=self.secret_name,
                namespace=self.namespace,
                body=patch,
                content_type="application/json-patch+json",
            )
        except Exception:
            raise BootstrapRefused(
                "credential projection CAS failed; reconcile the recorded intent"
            ) from None
        self._verify(binding, action)

    def publish(
        self, credential: IssuedCredential, *, certificate_authority_data, previous=None
    ):
        binding = credential.binding
        self._verify(binding, "project")
        if credential.expires_at <= self.now():
            raise BootstrapRefused("expired member credential cannot be projected")
        raw = credential.kubeconfig(certificate_authority_data).encode()
        receipt = ProjectionReceipt(
            canonical(credential.metadata),
            hashlib.sha256(raw).hexdigest(),
            self.secret_uid,
            "",
        )
        if previous is not None:
            self._validate_receipt(binding, previous, previous=True)
        resource, body = self._read(binding, "project")
        key, annotation = self._names(binding)
        if self._owned(body, key, annotation, receipt):
            return ProjectionReceipt(
                receipt.metadata_json,
                receipt.content_digest,
                self.secret_uid,
                body["metadata"]["resourceVersion"],
            )  # Retry after an uncertain write response.
        if not self._owned(body, key, annotation, previous):
            raise BootstrapRefused(
                "credential projection belongs to another revision or owner"
            )
        changes = []
        if "data" not in body or body["data"] is None:
            changes.append({"op": "add", "path": "/data", "value": {}})
        if not body["metadata"].get("annotations"):
            changes.append({"op": "add", "path": "/metadata/annotations", "value": {}})
        changes.extend(
            [
                {
                    "op": "add",
                    "path": "/data/" + pointer(key),
                    "value": base64.b64encode(raw).decode(),
                },
                {
                    "op": "add",
                    "path": "/metadata/annotations/" + pointer(annotation),
                    "value": receipt.marker,
                },
            ]
        )
        self._patch(binding, "project", resource, body, changes)
        _, observed = self._read(binding, "project")
        if not self._owned(observed, key, annotation, receipt):
            raise BootstrapRefused(
                "credential projection changed before acknowledgement"
            )
        return ProjectionReceipt(
            receipt.metadata_json,
            receipt.content_digest,
            self.secret_uid,
            observed["metadata"]["resourceVersion"],
        )

    def remove(self, binding: CredentialBinding, receipt: ProjectionReceipt):
        self._validate_receipt(binding, receipt)
        resource, body = self._read(binding, "unproject")
        key, annotation = self._names(binding)
        if self._owned(body, key, annotation, None):
            return
        if not self._owned(body, key, annotation, receipt):
            raise BootstrapRefused(
                "refusing to remove another credential projection revision"
            )
        self._patch(
            binding,
            "unproject",
            resource,
            body,
            [
                {"op": "remove", "path": "/data/" + pointer(key)},
                {
                    "op": "remove",
                    "path": "/metadata/annotations/" + pointer(annotation),
                },
            ],
        )
        _, observed = self._read(binding, "unproject")
        if not self._owned(observed, key, annotation, None):
            raise BootstrapRefused("credential projection removal is not yet observed")
