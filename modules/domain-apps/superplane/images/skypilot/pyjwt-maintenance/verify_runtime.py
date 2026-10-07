"""Exercise legitimate JWTs and GHSA-ffc3-869f-jxw9 in the application interpreter."""

import base64
import hashlib
import hmac
import importlib.metadata
import json
import sys

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa


def segment(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")


def main():
    if not __debug__:
        raise RuntimeError("Acceptance requires assertions")
    assert sys.prefix == "/usr/local", "Exercise the actual SkyPilot interpreter"
    versions = {
        name: importlib.metadata.version(name) for name in ("PyJWT", "skypilot")
    }
    assert versions == {"PyJWT": "2.14.0", "skypilot": "0.12.3"}
    cases = 0
    for algorithm, key in [
        ("RS256", rsa.generate_private_key(public_exponent=65537, key_size=2048)),
        ("ES256", ec.generate_private_key(ec.SECP256R1())),
    ]:
        original = key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        for pem in (
            original.replace(b"-----END", b"\t-----END"),
            original.replace(b"\n", b"\r"),
            b" ".join(original.splitlines()),
        ):
            serialization.load_pem_public_key(pem)
            claims = {"sub": "synthetic-security-test"}
            valid = jwt.encode(claims, key, algorithm=algorithm)
            assert jwt.decode(valid, pem, algorithms=[algorithm, "HS256"]) == claims
            message = segment({"alg": "HS256", "typ": "JWT"}) + b"." + segment(claims)
            signature = base64.urlsafe_b64encode(
                hmac.new(pem, message, hashlib.sha256).digest()
            ).rstrip(b"=")
            try:
                jwt.decode(
                    (message + b"." + signature).decode(),
                    pem,
                    algorithms=[algorithm, "HS256"],
                )
            except jwt.InvalidKeyError:
                cases += 1
            else:
                raise AssertionError("Formatted public key accepted as an HMAC secret")
    print(
        json.dumps(
            {
                "versions": versions,
                "legitimate_tokens_accepted": cases,
                "forgeries_rejected": cases,
            }
        )
    )


if __name__ == "__main__":
    main()
