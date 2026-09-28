"""UID-fenced temporary Kubernetes grants and admission guards.

The trusted composer supplies a DynamicClient using the verified target's pinned
TLS transport. Grant bodies are the service-compiled plan in AuthorityJournal.
"""

from __future__ import annotations

import hashlib
import json
import base64
from hmac import compare_digest
from pathlib import Path

from .errors import BootstrapRefused

GENERATION_ANNOTATION = "superplane.aws-e/authority-generation"


def _payload(value):
    return value.to_dict() if hasattr(value, "to_dict") else value


def _fields(body):
    return {
        k: v
        for k, v in body.items()
        if k not in {"apiVersion", "kind", "metadata", "status"}
    }


def _digest(body):
    fields = _fields(body)
    labels = body.get("metadata", {}).get("labels", {})
    if labels and body.get("kind") in {
        "Role",
        "ClusterRole",
        "RoleBinding",
        "ClusterRoleBinding",
    }:
        # Labels can opt a ClusterRole into aggregation and change another role's
        # authority. They are part of the pinned grant, even though not in rules.
        fields["metadata_labels"] = labels
    return hashlib.sha256(
        json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class KubeGrants:
    def __init__(self, client, target):
        from .target import VerifiedTarget

        if not isinstance(target, VerifiedTarget):
            raise BootstrapRefused("Kubernetes grants require a verified target")
        self.client = client
        self.target = target
        self._verify_transport()

    def _verify_transport(self):
        """Reject ambient or subsequently retargeted DynamicClient transports."""
        try:
            config = self.client.client.configuration
            expected = base64.b64decode(
                self.target.certificate_authority_data, validate=True
            )
            actual = Path(config.ssl_ca_cert).read_bytes()
            valid = (
                config.host == self.target.endpoint
                and config.verify_ssl is True
                and config.assert_hostname is None
                and config.tls_server_name is None
                and not config.proxy
                and expected
                and compare_digest(actual, expected)
            )
        except Exception as exc:
            raise BootstrapRefused(
                "Kubernetes grant transport cannot be verified"
            ) from exc
        if not valid:
            raise BootstrapRefused(
                "Kubernetes grant transport differs from the verified endpoint or CA"
            )

    def _resource(self, spec):
        self._verify_transport()
        if spec.get("cluster_arn") != self.target.cluster_arn:
            raise BootstrapRefused("Kubernetes grant targets a different cluster")
        body = spec["body"]
        return self.client.resources.get(
            api_version=body["apiVersion"], kind=body["kind"]
        )

    def _args(self, spec):
        metadata = spec["body"]["metadata"]
        args = {"name": metadata["name"]}
        if metadata.get("namespace"):
            args["namespace"] = metadata["namespace"]
        return args

    def _get(self, spec):
        try:
            result = _payload(self._resource(spec).get(**self._args(spec)))
        except Exception as exc:
            if getattr(exc, "status", None) == 404:
                return None
            raise
        if not isinstance(result, dict):
            raise BootstrapRefused("Kubernetes authority read was not answered")
        return result

    def _identity(self, spec, body):
        metadata = body.get("metadata", {})
        planned = spec["body"]
        if (
            not metadata.get("uid")
            or metadata.get("name") != planned["metadata"]["name"]
            or metadata.get("namespace", "") != planned["metadata"].get("namespace", "")
            or body.get("apiVersion") != planned["apiVersion"]
            or body.get("kind") != planned["kind"]
        ):
            raise BootstrapRefused("Kubernetes grant immutable identity differs")
        identity = {
            "uid": metadata["uid"],
            "generation": metadata.get("annotations", {}).get(GENERATION_ANNOTATION),
            "digest": _digest(body),
        }
        if body.get("kind") == "Namespace":
            identity["labels"] = metadata.get("labels", {})
        return identity

    def observe(self, spec):
        body = self._get(spec)
        return None if body is None else self._identity(spec, body)

    def verify(self, spec, identity):
        if identity.get("generation") != spec["generation"] or identity.get(
            "digest"
        ) != _digest(spec["body"]):
            raise BootstrapRefused(
                "Kubernetes grant has different ownership or permissions"
            )

    def create(self, spec):
        body = spec["body"]
        if (
            body.get("metadata", {}).get("annotations", {}).get(GENERATION_ANNOTATION)
            != spec["generation"]
        ):
            raise BootstrapRefused("Kubernetes grant plan has no generation binding")
        args = {}
        if body["metadata"].get("namespace"):
            args["namespace"] = body["metadata"]["namespace"]
        # Create only: AlreadyExists never adopts or rewrites an existing grant.
        result = _payload(self._resource(spec).create(body=body, **args))
        identity = self._identity(spec, result)
        self.verify(spec, identity)
        return identity

    def delete(self, spec, identity):
        observed = self._get(spec)
        if observed is None or self._identity(spec, observed) != identity:
            raise BootstrapRefused("Kubernetes grant changed before revocation")
        self.verify(spec, identity)
        version = observed["metadata"].get("resourceVersion")
        if not version:
            raise BootstrapRefused("Kubernetes grant has no deletion precondition")
        self._resource(spec).delete(
            **self._args(spec),
            body={
                "apiVersion": "v1",
                "kind": "DeleteOptions",
                "preconditions": {"uid": identity["uid"], "resourceVersion": version},
            },
        )
