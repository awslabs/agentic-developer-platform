"""Exercise real Authenticode signing and key-usage rejection without network access."""
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


def run(*args):
    return subprocess.run(args, capture_output=True, text=True, timeout=30)


receipt = json.loads(Path('/opt/adp-security/osslsigncode.json').read_text())
assert receipt['commit'] == 'beec94e308d1a1e03ca17b05fe089d93c6303e90'
assert receipt['archive_sha256'] == '1cd8ad26c9ed34e5b96d0a0bef0309437e5e71227a67560da03c33797371ccbd'
assert hashlib.sha256(Path('/usr/bin/osslsigncode').read_bytes()).hexdigest() == receipt['binary_sha256']

with tempfile.TemporaryDirectory(prefix="authenticode-") as directory:
    root = Path(directory)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key_path = root / "key.pem"
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    unsigned = root / "unsigned.ps1"
    unsigned.write_text('Write-Output "local verification fixture"\n')
    now = datetime.datetime.now(datetime.timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Local fixture")])
    outcomes = {}
    for allowed in (True, False):
        name = "digital-signature" if allowed else "key-encipherment-only"
        cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(days=1))
                .not_valid_after(now + datetime.timedelta(days=1))
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CODE_SIGNING]), critical=False)
                .add_extension(x509.KeyUsage(digital_signature=allowed, content_commitment=False,
                    key_encipherment=not allowed, data_encipherment=False, key_agreement=False,
                    key_cert_sign=False, crl_sign=False, encipher_only=False, decipher_only=False), critical=True)
                .sign(key, hashes.SHA256()))
        cert_path = root / (name + ".pem")
        cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        signed = root / (name + ".ps1")
        result = run("osslsigncode", "sign", "-certs", str(cert_path), "-key", str(key_path),
                     "-in", str(unsigned), "-out", str(signed))
        assert result.returncode == 0, result.stdout + result.stderr
        result = run("osslsigncode", "verify", "-CAfile", str(cert_path), "-in", str(signed))
        if allowed:
            assert result.returncode == 0, result.stdout + result.stderr
        else:
            assert result.returncode != 0, "Verifier accepted a key without signing permission"
            assert "keyUsage does not permit digitalSignature" in result.stdout + result.stderr
        outcomes[name] = "accepted" if allowed else "rejected"
    assert run("osslsigncode", "verify", "-in", str(unsigned)).returncode != 0
    print(json.dumps(outcomes))
