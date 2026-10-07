#!/usr/bin/env python3
"""Render (never apply) the private chat gateway endpoint from an observed gateway.

The output contains a TLS private key. Write only to an operator-owned directory;
never upload it as a CI artifact or commit it. Existing gateway resources are
not modified. A reviewed certificate issuer supplies the three PEM inputs.
"""

import argparse
import base64
import copy
import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID

HOST = "chat-sandbox-gateway.adp-gateway.svc"
NAME = "chat-sandbox-gateway"
GATEWAY_NS = "adp-gateway"
SANDBOX_NS = "adp-gateway-agents"


def validate_certificate(certificate, key, ca, now=None):
    now = now or datetime.now(UTC)
    pem = rb"-----BEGIN CERTIFICATE-----\r?\n[A-Za-z0-9+/=\r\n]+-----END CERTIFICATE-----\r?\n?"
    if not all(re.fullmatch(pem, value) for value in (certificate, ca)):
        raise ValueError("Exactly one public PEM certificate is required per certificate input")
    if (
        len(x509.load_pem_x509_certificates(certificate)) != 1
        or len(x509.load_pem_x509_certificates(ca)) != 1
    ):
        raise ValueError(
            "Exactly one server certificate and one trust root are required"
        )
    leaf = x509.load_pem_x509_certificate(certificate)
    root = x509.load_pem_x509_certificate(ca)
    private_key = serialization.load_pem_private_key(key, password=None)

    def public(item):
        return item.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )

    if public(leaf) != public(private_key):
        raise ValueError("TLS private key does not match certificate")
    try:
        leaf.verify_directly_issued_by(root)
        root.verify_directly_issued_by(root)
    except InvalidSignature as error:
        raise ValueError("TLS certificate signature does not match issuer") from error
    for cert in (leaf, root):
        if (
            cert.not_valid_before_utc > now
            or cert.not_valid_after_utc < now + timedelta(hours=48)
        ):
            raise ValueError("TLS certificate must be valid for at least 48 hours")
    if not root.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("Trust certificate is not a CA")
    if not root.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign:
        raise ValueError("Trust certificate cannot sign certificates")
    if leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca:
        raise ValueError("Server certificate must not be a CA")
    sans = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    if list(sans) != [x509.DNSName(HOST)]:
        raise ValueError(
            "Server certificate must name only the dedicated gateway hostname"
        )
    if (
        ExtendedKeyUsageOID.SERVER_AUTH
        not in leaf.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    ):
        raise ValueError("Server certificate lacks server authentication usage")
    return "chat-sandbox-gateway-ca-" + hashlib.sha256(ca).hexdigest()[:16]


def render(deployment, certificate, key, ca, now=None):
    ca_name = validate_certificate(certificate, key, ca, now)
    if (
        deployment.get("kind") != "Deployment"
        or deployment.get("metadata", {}).get("namespace") != GATEWAY_NS
    ):
        raise ValueError("Expected observed gateway Deployment in adp-gateway")
    if deployment["metadata"].get("name") != "bedrockgateway":
        raise ValueError("Expected bedrockgateway baseline")
    source_spec = deployment["spec"]["template"]["spec"]
    candidates = [
        item for item in source_spec["containers"] if item["name"] == "bedrockgateway"
    ]
    if len(candidates) != 1 or not re.fullmatch(
        r"[a-z0-9][a-z0-9./:_-]*@sha256:[a-f0-9]{64}", candidates[0]["image"]
    ):
        raise ValueError("Gateway image must be an immutable digest")
    if source_spec.get("serviceAccountName") != "gateway-service":
        raise ValueError("Unexpected gateway service account")
    container = copy.deepcopy(candidates[0])
    container.update(
        name=NAME,
        command=["python", "-m", "src.agentauth.chat_tls"],
        ports=[{"name": "https", "containerPort": 8443, "protocol": "TCP"}],
    )
    container.pop("args", None)
    env = [
        item
        for item in container.get("env", [])
        if item["name"] not in {"ADP_CHAT_TLS_ENABLED", "ADP_CHAT_DATA_URL", "ADP_CHAT_SANDBOX_CA_CONFIGMAP"}
    ]
    container["env"] = [*env, {"name": "ADP_CHAT_TLS_ENABLED", "value": "true"},
                        {"name": "ADP_CHAT_DATA_URL", "value": f"https://{HOST}:8443"},
                        {"name": "ADP_CHAT_SANDBOX_CA_CONFIGMAP", "value": ca_name}]
    container.setdefault("volumeMounts", []).append(
        {"name": "chat-tls", "mountPath": "/var/run/adp-chat-tls", "readOnly": True}
    )
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        if probe in container:
            existing = container[probe]
            existing.pop("exec", None)
            existing.pop("tcpSocket", None)
            existing.pop("grpc", None)
            existing["httpGet"] = {
                "path": "/health",
                "port": 8443,
                "scheme": "HTTPS",
                "httpHeaders": [{"name": "Host", "value": f"{HOST}:8443"}],
            }
    spec = {
        field: copy.deepcopy(source_spec[field])
        for field in (
            "serviceAccountName",
            "automountServiceAccountToken",
            "enableServiceLinks",
            "securityContext",
            "volumes",
            "terminationGracePeriodSeconds",
            "nodeSelector",
            "tolerations",
            "imagePullSecrets",
        )
        if field in source_spec
    }
    spec.setdefault("volumes", []).append(
        {"name": "chat-tls", "secret": {"secretName": NAME, "defaultMode": 0o440}}
    )
    spec["containers"] = [container]
    labels = {"app.kubernetes.io/name": NAME}
    items = [
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": ca_name, "namespace": SANDBOX_NS},
            "rules": [{"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [ca_name], "verbs": ["get"]}],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": ca_name, "namespace": SANDBOX_NS},
            "subjects": [{"kind": "ServiceAccount", "name": "adp-chat-supervisor", "namespace": SANDBOX_NS}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": ca_name},
        },
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": NAME, "namespace": GATEWAY_NS},
            "type": "kubernetes.io/tls",
            "data": {
                "tls.crt": base64.b64encode(certificate).decode(),
                "tls.key": base64.b64encode(key).decode(),
            },
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": ca_name, "namespace": SANDBOX_NS},
            "immutable": True,
            "data": {"ca.crt": ca.decode()},
        },
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": NAME, "namespace": GATEWAY_NS},
            "spec": {
                "replicas": 2,
                "strategy": {
                    "type": "RollingUpdate",
                    "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1},
                },
                "selector": {"matchLabels": labels},
                "template": {"metadata": {"labels": labels}, "spec": spec},
            },
        },
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {"name": NAME, "namespace": GATEWAY_NS},
            "spec": {
                "type": "ClusterIP",
                "selector": labels,
                "ports": [
                    {
                        "name": "https",
                        "protocol": "TCP",
                        "port": 8443,
                        "targetPort": 8443,
                    }
                ],
            },
        },
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": {"name": NAME, "namespace": GATEWAY_NS},
            "spec": {
                "podSelector": {"matchLabels": labels},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": SANDBOX_NS
                                    }
                                },
                                "podSelector": {
                                    "matchLabels": {"adp.io/chat-sandbox": "true"}
                                },
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 8443}],
                    }
                ],
            },
        },
    ]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment-json", type=Path, required=True)
    parser.add_argument("--certificate", type=Path, required=True)
    parser.add_argument("--private-key", type=Path, required=True)
    parser.add_argument("--ca", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = render(
        json.loads(args.deployment_json.read_text()),
        args.certificate.read_bytes(),
        args.private_key.read_bytes(),
        args.ca.read_bytes(),
    )
    # O_EXCL and no-follow semantics prevent overwriting an existing manifest or
    # following a destination symlink. Secret material never goes to stdout.
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(result, output, indent=2)
        output.write("\n")
    print("Private manifest rendered; nothing was applied.")


if __name__ == "__main__":
    main()
