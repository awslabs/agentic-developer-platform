"""Small valid-input checks of preserved public native cryptography behavior."""

import datetime
import hashlib
import json
from pathlib import Path

import cryptography
from cryptography import x509
from cryptography.fernet import Fernet
from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from cryptography.x509.oid import NameOID

assert cryptography.__version__ == "46.0.7+adp1"
f = Fernet(Fernet.generate_key())
assert f.decrypt(f.encrypt(b"demo")) == b"demo"
key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
signature = key.sign(b"demo", padding.PKCS1v15(), hashes.SHA256())
key.public_key().verify(signature, b"demo", padding.PKCS1v15(), hashes.SHA256())
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "demo.local")])
now = datetime.datetime.now(datetime.timezone.utc)  # noqa: UP017 - runtime is Python 3.10
cert = (
    x509.CertificateBuilder()
    .subject_name(name)
    .issuer_name(name)
    .public_key(key.public_key())
    .serial_number(1)
    .not_valid_before(now - datetime.timedelta(minutes=1))
    .not_valid_after(now + datetime.timedelta(days=1))
    .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    .sign(key, hashes.SHA256())
)
roundtrip = x509.load_der_x509_certificate(
    cert.public_bytes(serialization.Encoding.DER)
)
assert roundtrip.fingerprint(hashes.SHA256()) == cert.fingerprint(hashes.SHA256())
signed = (
    pkcs7.PKCS7SignatureBuilder()
    .set_data(b"demo")
    .add_signer(cert, key, hashes.SHA256())
    .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
)
assert len(pkcs7.load_der_pkcs7_certificates(signed)) == 1
loaded = {}
for line in Path("/proc/self/maps").read_text().splitlines():
    path = line.split()[-1]
    if path.startswith("/") and (
        "libssl.so" in path or "libcrypto.so" in path or "_rust.abi3.so" in path
    ):
        loaded[path] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
assert any("libcrypto.so.3" in p for p in loaded)
assert any("_rust.abi3.so" in p for p in loaded)
print(
    json.dumps(
        {
            "cryptography": cryptography.__version__,
            "openssl": backend.openssl_version_text(),
            "checks": ["Fernet", "RSA sign/verify", "X509 DER", "PKCS7 signed-data"],
            "loaded_native_files": loaded,
        },
        indent=2,
    )
)
