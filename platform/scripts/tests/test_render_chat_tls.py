import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

spec = importlib.util.spec_from_file_location(
    "render_chat_tls", Path(__file__).parents[1] / "render-chat-tls.py"
)
renderer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(renderer)
NOW = datetime(2026, 10, 6, tzinfo=UTC)


def certificates(*, hostname=renderer.HOST, expires=None):
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "local test issuer")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(issuer)
        .issuer_name(issuer)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(NOW + timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(True, False, False, False, False, True, True, False, False),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
            critical=False,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)]))
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(NOW - timedelta(days=1))
        .not_valid_after(expires or NOW + timedelta(days=14))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False
        )
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, False, False, False, False),
            critical=True,
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    return (
        leaf.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
        ca.public_bytes(serialization.Encoding.PEM),
    )


@pytest.fixture(scope="module")
def material():
    return certificates()


@pytest.fixture
def baseline():
    return {
        "kind": "Deployment",
        "metadata": {"name": "bedrockgateway", "namespace": "adp-gateway"},
        "spec": {
            "template": {
                "spec": {
                    "serviceAccountName": "gateway-service",
                    "securityContext": {
                        "runAsNonRoot": True,
                        "runAsUser": 65532,
                        "fsGroup": 65532,
                    },
                    "containers": [
                        {
                            "name": "bedrockgateway",
                            "image": "registry.example/gateway@sha256:" + "a" * 64,
                            "envFrom": [
                                {"configMapRef": {"name": "bedrockgateway-config"}}
                            ],
                            "env": [{"name": "ADP_CHAT_DATA_ENABLED", "value": "false"}],
                            "securityContext": {
                                "readOnlyRootFilesystem": True,
                                "allowPrivilegeEscalation": False,
                            },
                            "readinessProbe": {
                                "httpGet": {"path": "/health", "port": 8080},
                                "periodSeconds": 10,
                            },
                        }
                    ],
                }
            }
        },
    }


def test_private_endpoint_preserves_identity_and_feature_gates(baseline, material):
    result = renderer.render(baseline, *material, now=NOW)
    by_kind = {item["kind"]: item for item in result["items"]}
    deployment = by_kind["Deployment"]
    assert deployment["metadata"]["name"] != baseline["metadata"]["name"]
    pod = deployment["spec"]["template"]["spec"]
    assert pod["serviceAccountName"] == "gateway-service"
    container = pod["containers"][0]
    assert (
        container["image"]
        == baseline["spec"]["template"]["spec"]["containers"][0]["image"]
    )
    assert container["env"] == [
        {"name": "ADP_CHAT_DATA_ENABLED", "value": "false"},
        {"name": "ADP_CHAT_TLS_ENABLED", "value": "true"},
        {"name": "ADP_CHAT_DATA_URL", "value": f"https://{renderer.HOST}:8443"},
        {"name": "ADP_CHAT_SANDBOX_CA_CONFIGMAP", "value": by_kind["ConfigMap"]["metadata"]["name"]},
    ]
    assert container["command"] == ["python", "-m", "src.agentauth.chat_tls"]
    assert container["readinessProbe"]["httpGet"]["scheme"] == "HTTPS"
    assert by_kind["Service"]["spec"]["type"] == "ClusterIP"
    assert by_kind["ConfigMap"]["immutable"] is True
    assert set(by_kind["ConfigMap"]["data"]) == {"ca.crt"}
    assert set(by_kind["Secret"]["data"]) == {"tls.crt", "tls.key"}
    assert by_kind["Role"]["rules"] == [{"apiGroups": [""], "resources": ["configmaps"],
        "resourceNames": [by_kind["ConfigMap"]["metadata"]["name"]], "verbs": ["get"]}]
    assert by_kind["RoleBinding"]["subjects"] == [{"kind": "ServiceAccount", "name": "adp-chat-supervisor",
        "namespace": "adp-gateway-agents"}]
    ingress = by_kind["NetworkPolicy"]["spec"]["ingress"]
    assert ingress[0]["ports"] == [{"protocol": "TCP", "port": 8443}]
    assert set(ingress[0]["from"][0]) == {"namespaceSelector", "podSelector"}
    assert len(baseline["spec"]["template"]["spec"]["containers"]) == 1
    assert (
        baseline["spec"]["template"]["spec"]["containers"][0]["name"]
        == "bedrockgateway"
    )


def test_refuses_mutable_source_image(baseline, material):
    baseline["spec"]["template"]["spec"]["containers"][0]["image"] = "gateway:latest"
    with pytest.raises(ValueError, match="immutable digest"):
        renderer.render(baseline, *material, now=NOW)


def test_refuses_private_key_or_extra_material_in_public_ca(baseline, material):
    certificate, key, ca = material
    for suffix in (key, certificate, b"unparsed material"):
        with pytest.raises(ValueError, match="public PEM"):
            renderer.render(baseline, certificate, key, ca + suffix, now=NOW)


def test_service_matches_supervisor_exact_transport_contract(baseline, material):
    items = renderer.render(baseline, *material, now=NOW)["items"]
    service = next(item for item in items if item["kind"] == "Service")
    deployment = next(item for item in items if item["kind"] == "Deployment")
    selector = {"app.kubernetes.io/name": "chat-sandbox-gateway"}
    # isDedicatedGatewayService in the supervisor rejects additional selector
    # fields or a differently named port, even when they select the same pods.
    assert service["spec"]["selector"] == selector
    assert service["spec"]["ports"] == [
        {"name": "https", "protocol": "TCP", "port": 8443, "targetPort": 8443}
    ]
    assert deployment["spec"]["template"]["metadata"]["labels"] == selector


def test_refuses_different_target(baseline, material):
    baseline["metadata"]["namespace"] = "other"
    with pytest.raises(ValueError, match="adp-gateway"):
        renderer.render(baseline, *material, now=NOW)


def test_refuses_foreign_key(material):
    cert, _, ca = material
    _, key, _ = certificates()
    with pytest.raises(ValueError, match="does not match"):
        renderer.validate_certificate(cert, key, ca, NOW)


def test_refuses_foreign_issuer(material):
    cert, key, _ = material
    _, _, ca = certificates()
    with pytest.raises(ValueError):
        renderer.validate_certificate(cert, key, ca, NOW)


def test_refuses_wrong_server_name():
    with pytest.raises(ValueError, match="hostname"):
        renderer.validate_certificate(*certificates(hostname="other.svc"), now=NOW)


def test_refuses_extra_trust_roots(material):
    cert, key, ca = material
    with pytest.raises(ValueError, match="Exactly one"):
        renderer.validate_certificate(cert, key, ca + ca, now=NOW)


def test_refuses_short_lived_certificate():
    with pytest.raises(ValueError, match="48 hours"):
        renderer.validate_certificate(
            *certificates(expires=NOW + timedelta(hours=1)), now=NOW
        )
