"""Stable, domain-separated mTLS identity shared by gateway replicas."""

import hashlib
import hmac
import os
import ssl
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from src.shared.config import get_settings


@dataclass(frozen=True)
class GatewayClientIdentity:
    certificate: str
    fingerprint: str
    certificate_file: str
    private_key_file: str


def gateway_client_identity() -> GatewayClientIdentity:
    configured_certificate = os.environ.get("ADP_CREW_HOST_CLIENT_CERT_FILE")
    configured_key = os.environ.get("ADP_CREW_HOST_CLIENT_KEY_FILE")
    if bool(configured_certificate) != bool(configured_key):
        raise ValueError("incomplete Crew gateway client identity")
    if not configured_certificate:
        return _derived_identity(get_settings().token_secret_key.encode())
    certificate_file, private_key_file = configured_certificate, configured_key
    public = Path(certificate_file).read_bytes()
    parsed = x509.load_pem_x509_certificate(public)
    ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT).load_cert_chain(certificate_file, private_key_file)
    return GatewayClientIdentity(public.decode("ascii"), parsed.fingerprint(hashes.SHA256()).hex(), certificate_file, private_key_file)


@lru_cache(maxsize=2)
def _derived_identity(secret: bytes) -> GatewayClientIdentity:
    if len(secret) < 32:
        raise ValueError("gateway signing material unavailable")
    seed = hmac.digest(secret, b"adp-crew-gateway-mtls-client-v1", hashlib.sha256)
    private = Ed25519PrivateKey.from_private_bytes(seed)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "adp-crew-gateway-client")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private.public_key())
        .serial_number(int.from_bytes(hmac.digest(secret, b"adp-crew-gateway-mtls-serial-v1", hashlib.sha256)[:20], "big") >> 1)
        .not_valid_before(datetime(2025, 1, 1, tzinfo=UTC))
        .not_valid_after(datetime(2035, 1, 1, tzinfo=UTC))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(private, algorithm=None)
    )
    public = certificate.public_bytes(serialization.Encoding.PEM)
    key = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    directory = Path(tempfile.mkdtemp(prefix="adp-crew-client-"))
    certificate_file = str(directory / "client.pem")
    private_key_file = str(directory / "client.key")
    for path, content in ((certificate_file, public), (private_key_file, key)):
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(content)
    return GatewayClientIdentity(public.decode("ascii"), certificate.fingerprint(hashes.SHA256()).hex(), certificate_file, private_key_file)
