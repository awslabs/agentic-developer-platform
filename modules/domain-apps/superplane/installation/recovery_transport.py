"""Optional private recovery TLS and separately scoped observation identity."""

import json
import secrets
from datetime import datetime, timedelta, timezone

from .config import LABEL, identity, image, require


def endpoint(env):
    return f"https://superplane-recovery.{env['namespace']}.svc.cluster.local:8443"


def validate(env):
    selected = env.get("recovery_transport")
    if selected is None:
        return
    prefix = f"adp/{env['environment']}/superplane/"
    require(
        selected
        == {
            "tls_secret": prefix + "recovery-tls",
            "credential_secret": prefix + "recovery-credential",
        },
        "recovery_transport must name the installation's two dedicated secrets",
    )


def generate(env):
    """Generate in memory; the CA signing key is never persisted."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    now = datetime.now(timezone.utc)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, identity(env) + " recovery CA")]
    )
    host = f"superplane-recovery.{env['namespace']}.svc.cluster.local"
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca_name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=90))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    credential = "Bearer " + secrets.token_urlsafe(40)
    grant = {
        "submitter_id": identity(env) + "-recovery",
        "credential": credential,
        "signing_key": secrets.token_urlsafe(40),
        "workspaces": [],
        "lease_scopes": [f"controller_recovery/{env['org_id']}"],
    }
    tls = {
        "tls.crt": cert.public_bytes(pem).decode(),
        "tls.key": key.private_bytes(
            pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ).decode(),
        "ca.pem": ca.public_bytes(pem).decode(),
        "submitter": json.dumps(grant),
    }
    return tls, {"authorization": credential, "ca_pem": tls["ca.pem"]}


def validate_material(env, tls, credential):
    """Refuse expired, foreign, mismatched or over-scoped material before writes."""
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.x509.oid import ExtendedKeyUsageOID

    try:
        require(
            set(tls) == {"tls.crt", "tls.key", "ca.pem", "submitter"},
            "Invalid recovery material",
        )
        require(
            set(credential) == {"authorization", "ca_pem"}, "Invalid recovery material"
        )
        require(credential["ca_pem"] == tls["ca.pem"], "Invalid recovery material")
        grant = json.loads(tls["submitter"])
        require(
            set(grant)
            == {
                "submitter_id",
                "credential",
                "signing_key",
                "workspaces",
                "lease_scopes",
            },
            "Invalid recovery material",
        )
        require(
            grant["submitter_id"] == identity(env) + "-recovery",
            "Invalid recovery material",
        )
        require(grant["workspaces"] == [], "Invalid recovery material")
        require(
            grant["lease_scopes"] == [f"controller_recovery/{env['org_id']}"],
            "Invalid recovery material",
        )
        require(
            grant["credential"] == credential["authorization"],
            "Invalid recovery material",
        )
        require(
            grant["credential"].startswith("Bearer ")
            and len(grant["credential"]) >= 48,
            "Invalid recovery material",
        )
        require(
            all(32 <= ord(c) <= 126 for c in grant["credential"]),
            "Invalid recovery material",
        )
        require(
            isinstance(grant["signing_key"], str) and len(grant["signing_key"]) >= 32,
            "Invalid recovery material",
        )
        cert = x509.load_pem_x509_certificate(tls["tls.crt"].encode())
        ca = x509.load_pem_x509_certificate(tls["ca.pem"].encode())
        key = serialization.load_pem_private_key(tls["tls.key"].encode(), password=None)
        require(
            cert.public_key().public_numbers() == key.public_key().public_numbers(),
            "Invalid recovery material",
        )
        require(
            cert.issuer == ca.subject and ca.issuer == ca.subject,
            "Invalid recovery material",
        )
        for value in (cert, ca):
            ca.public_key().verify(
                value.signature,
                value.tbs_certificate_bytes,
                padding.PKCS1v15(),
                value.signature_hash_algorithm,
            )
            require(
                value.not_valid_before_utc <= datetime.now(timezone.utc),
                "Invalid recovery material",
            )
            require(
                value.not_valid_after_utc
                > datetime.now(timezone.utc) + timedelta(days=7),
                "Invalid recovery material",
            )
        require(
            ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca,
            "Invalid recovery material",
        )
        require(
            not cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca,
            "Invalid recovery material",
        )
        require(
            cert.extensions.get_extension_for_class(
                x509.SubjectAlternativeName
            ).value.get_values_for_type(x509.DNSName)
            == [f"superplane-recovery.{env['namespace']}.svc.cluster.local"],
            "Invalid recovery material",
        )
        require(
            ExtendedKeyUsageOID.SERVER_AUTH
            in cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value,
            "Invalid recovery material",
        )
        return grant
    except Exception:
        from .config import Refusal

        raise Refusal(
            "Recovery TLS/credential pair is invalid, expired or differs from the installation"
        ) from None


def load_material(installer):
    selected = installer.env.get("recovery_transport")
    if selected is None:
        return
    values = {}
    for kind, name in selected.items():
        metadata = installer.json(
            installer.aws("secretsmanager", "describe-secret", "--secret-id", name)
        )
        require(
            not metadata.get("DeletedDate")
            and {"Key": "SuperplaneInstallation", "Value": installer.owner}
            in metadata.get("Tags", []),
            "Recovery secret is not owned by the installation",
        )
        version = installer.receipt.get("secret_versions", {}).get("recovery_" + kind)
        extra = ["--version-id", version] if version else []
        result = installer.json(
            installer.aws(
                "secretsmanager", "get-secret-value", "--secret-id", name, *extra
            )
        )
        try:
            values[kind] = json.loads(result["SecretString"])
        except (KeyError, ValueError, TypeError):
            from .config import Refusal

            raise Refusal("Recovery secret is invalid") from None
        installer.secret_versions["recovery_" + kind] = result["VersionId"]
    grant = validate_material(
        installer.env, values["tls_secret"], values["credential_secret"]
    )
    foundations = installer.env.get("lifecycle_foundations")
    if foundations is not None:
        runtime = json.loads(foundations["policy_json"])["tenants"][
            installer.env["org_id"]
        ]["runtime"]
        require(
            runtime.get("management_api_origin") == endpoint(installer.env)
            and runtime.get("management_ca_pem") == values["tls_secret"]["ca.pem"],
            "Lifecycle bootstrap must pin the selected private listener and its CA",
        )
    observation = installer.secret_values["observation"]
    existing = json.loads(observation["submitters"])
    require(
        all(
            g["credential"] != grant["credential"]
            and g["submitter_id"] != grant["submitter_id"]
            for g in existing
        ),
        "Recovery identity must differ from monitor and controller",
    )
    installer.secret_values["recovery_tls"] = values["tls_secret"]
    # Only the owned Kubernetes projection is augmented; the original monitor/
    # controller Secrets Manager document remains unchanged.
    observation["submitters"] = json.dumps([*existing, grant])


def render(env, lock, docs):
    if env.get("recovery_transport") is None:
        return
    ns, owner = env["namespace"], identity(env)
    api = next(
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == "superplane-api"
    )
    api["spec"]["template"]["metadata"].setdefault("annotations", {})[
        "eks.amazonaws.com/skip-containers"
    ] = "recovery-transport"
    pod = api["spec"]["template"]["spec"]
    pod["volumes"].append(
        {
            "name": "recovery-tls",
            "secret": {
                "secretName": "superplane-recovery-tls",
                "defaultMode": 0o440,
                "items": [{"key": k, "path": k} for k in ("tls.crt", "tls.key")],
            },
        }
    )
    probe = {
        "httpGet": {"path": "/health", "port": "recovery-https", "scheme": "HTTPS"},
        "timeoutSeconds": 5,
    }
    pod["containers"].append(
        {
            "name": "recovery-transport",
            "image": image(lock, "superplane-api"),
            "command": [
                "python",
                "-m",
                "uvicorn",
                "app.recovery_transport:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8443",
                "--ssl-certfile",
                "/recovery-tls/tls.crt",
                "--ssl-keyfile",
                "/recovery-tls/tls.key",
                "--no-access-log",
            ],
            "ports": [{"name": "recovery-https", "containerPort": 8443}],
            "volumeMounts": [
                {"name": "recovery-tls", "mountPath": "/recovery-tls", "readOnly": True}
            ],
            "resources": {
                "requests": {"cpu": "50m", "memory": "96Mi"},
                "limits": {"cpu": "500m", "memory": "256Mi"},
            },
            "securityContext": {
                "allowPrivilegeEscalation": False,
                "readOnlyRootFilesystem": True,
                "capabilities": {"drop": ["ALL"]},
            },
            "readinessProbe": probe,
            "livenessProbe": probe,
        }
    )
    labels = {
        LABEL: owner,
        "app.kubernetes.io/part-of": "adp-superplane",
        "adp.aws-e.io/domain-app": "superplane",
    }
    metadata = {"name": "superplane-recovery", "namespace": ns, "labels": labels}
    selector = {"app.kubernetes.io/name": "superplane-api", LABEL: owner}
    docs.append(
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": metadata,
            "spec": {
                "type": "ClusterIP",
                "selector": selector,
                "ports": [
                    {"name": "https", "port": 8443, "targetPort": "recovery-https"}
                ],
            },
        }
    )
    docs.append(
        {
            "apiVersion": "networking.k8s.io/v1",
            "kind": "NetworkPolicy",
            "metadata": metadata,
            "spec": {
                "podSelector": {"matchLabels": selector},
                "policyTypes": ["Ingress"],
                "ingress": [
                    {
                        "from": [
                            {
                                "namespaceSelector": {
                                    "matchLabels": {
                                        "kubernetes.io/metadata.name": env.get(
                                            "gateway_namespace", "adp"
                                        )
                                    }
                                },
                                "podSelector": {
                                    "matchLabels": {"app": "bedrockgateway"}
                                },
                            }
                        ],
                        "ports": [{"protocol": "TCP", "port": 8443}],
                    }
                ],
            },
        }
    )
    if env.get("paid_worker", {}).get("mode") == "native-lifecycle":
        docs[-1]["spec"]["ingress"][0]["from"].append(
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": ns}
                },
                "podSelector": {
                    "matchLabels": {
                        "app.kubernetes.io/name": "superplane-paid-worker",
                        LABEL: owner,
                    }
                },
            }
        )
