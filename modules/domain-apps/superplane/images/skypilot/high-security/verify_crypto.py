"""Exercise provider cryptography with the system OpenSSL maintenance build."""

import datetime
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID
import json
import importlib.metadata
import ssl
import subprocess
from cryptography import __version__
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
import cryptography.hazmat.bindings._rust as rust

assert __version__ == "46.0.7+adp1"
assert importlib.metadata.version("cryptography") == __version__
assert backend.openssl_version_text() == ssl.OPENSSL_VERSION
assert backend.openssl_version_text().startswith("OpenSSL 3.5.7 ")
linked = subprocess.check_output(["ldd", rust.__file__], text=True)
assert "libssl.so.3" in linked and "libcrypto.so.3" in linked
message = b"provider authentication offline fixture"
for key, algorithm in [
    (rsa.generate_private_key(public_exponent=65537, key_size=2048), "rsa"),
    (ec.generate_private_key(ec.SECP256R1()), "ec"),
]:
    args = (
        (padding.PKCS1v15(), hashes.SHA256())
        if algorithm == "rsa"
        else (ec.ECDSA(hashes.SHA256()),)
    )
    signature = key.sign(message, *args)
    key.public_key().verify(signature, message, *args)
    try:
        key.public_key().verify(signature, message + b"changed", *args)
    except InvalidSignature:
        pass
    else:
        raise AssertionError("modified signature payload accepted")
f = Fernet(Fernet.generate_key())
token = f.encrypt(message)
assert f.decrypt(token) == message
try:
    f.decrypt(token[:-2] + b"xx")
except InvalidToken:
    pass
else:
    raise AssertionError("modified Fernet token accepted")
print(
    json.dumps(
        {
            "cryptography": __version__,
            "openssl": backend.openssl_version_text(),
            "dynamic_linkage": linked,
            "RSA_ECDSA_Fernet": "valid and invalid cases passed",
        }
    )
)

# Upstream GHSA-g6cj-pr64-35w5 regression: each attacker-controlled key
# failure must follow the same decryption path and expose the same error.
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "offline")])
now = datetime.datetime.now(datetime.timezone.utc)
cert = (
    x509.CertificateBuilder()
    .subject_name(name)
    .issuer_name(name)
    .public_key(key.public_key())
    .serial_number(1)
    .not_valid_before(now - datetime.timedelta(days=1))
    .not_valid_after(now + datetime.timedelta(days=1))
    .sign(key, hashes.SHA256())
)
der = (
    pkcs7.PKCS7EnvelopeBuilder()
    .set_data(message)
    .add_recipient(cert)
    .encrypt(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
)
assert pkcs7.pkcs7_decrypt_der(der, cert, key, []) == message
marker = b"\x04\x82" + (key.key_size // 8).to_bytes(2, "big")
assert der.count(marker) == 1
start = der.index(marker) + len(marker)
errors = []
for wrapped in [
    b"\x00" * 256,
    *[key.public_key().encrypt(b"A" * n, padding.PKCS1v15()) for n in (15, 17, 32, 16)],
]:
    altered = der[:start] + wrapped + der[start + 256 :]
    try:
        pkcs7.pkcs7_decrypt_der(altered, cert, key, [])
    except ValueError as exc:
        errors.append(str(exc))
    else:
        raise AssertionError("invalid encrypted key accepted")
assert len(set(errors)) == 1, errors
print(
    json.dumps(
        {
            "PKCS7": "valid decrypt and five equal-error rejection cases passed",
            "errors": errors,
        }
    )
)
