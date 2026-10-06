"""Accept real JWTs but reject DER public-key/HMAC confusion in both backends."""

import base64
import hashlib
import hmac
import json

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, rsa
from jose import JWTError, jwt
from jose.backends.cryptography_backend import CryptographyHMACKey
from jose.backends.native import HMACKey
from jose.exceptions import JWKError


def segment(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")


def main():
    if not __debug__:
        raise RuntimeError("Acceptance requires assertions")
    keys = [
        rsa.generate_private_key(public_exponent=65537, key_size=2048),
        ec.generate_private_key(ec.SECP256R1()),
        ed25519.Ed25519PrivateKey.generate(),
    ]
    public_keys = [
        key.public_key().public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        for key in keys
    ]
    public_keys.append(
        keys[0]
        .public_key()
        .public_bytes(
            serialization.Encoding.DER,
            serialization.PublicFormat.PKCS1,
        )
    )
    claims = {"sub": "synthetic-der-regression"}
    rejected = 0
    accepted = 0
    for algorithm in ("HS256", "HS384", "HS512"):
        for secret in (
            b"synthetic-symmetric-secret",
            bytes(range(256)),
            b"\x30\x03\x02\x01\x01",
        ):
            token = jwt.encode(claims, secret, algorithm=algorithm)
            assert jwt.decode(token, secret, algorithms=[algorithm]) == claims
            for backend in (HMACKey, CryptographyHMACKey):
                key = backend(secret, algorithm)
                assert key.verify(b"message", key.sign(b"message"))
                assert not key.verify(b"modified", key.sign(b"message"))
            accepted += 1
        symmetric_jwk = {
            "kty": "oct",
            "k": base64.urlsafe_b64encode(b"synthetic-jwk-secret").decode().rstrip("="),
        }
        token = jwt.encode(claims, symmetric_jwk, algorithm=algorithm)
        assert jwt.decode(token, symmetric_jwk, algorithms=[algorithm]) == claims
        accepted += 1
        for public in public_keys:
            message = segment({"alg": algorithm, "typ": "JWT"}) + b"." + segment(claims)
            signature = hmac.new(
                public, message, getattr(hashlib, "sha" + algorithm[2:])
            ).digest()
            forged = (
                message + b"." + base64.urlsafe_b64encode(signature).rstrip(b"=")
            ).decode()
            try:
                jwt.decode(forged, public, algorithms=[algorithm, "RS256", "ES256"])
            except (JWTError, JWKError):
                rejected += 1
            else:
                raise AssertionError("DER public-key forgery accepted")
            for backend in (HMACKey, CryptographyHMACKey):
                try:
                    backend(public, algorithm)
                except JWKError:
                    rejected += 1
                else:
                    raise AssertionError(f"{backend.__name__} accepted DER public key")
    for algorithm, key in zip(("RS256", "ES256"), keys[:2]):
        public = key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        token = jwt.encode(claims, key, algorithm=algorithm)
        assert jwt.decode(token, public, algorithms=[algorithm, "HS256"]) == claims
        accepted += 1
    print(json.dumps({"legitimate_token_cases": accepted, "DER_rejections": rejected}))


if __name__ == "__main__":
    main()
