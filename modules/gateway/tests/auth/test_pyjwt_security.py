"""GHSA-ffc3-869f-jxw9: formatted public keys must never become HMAC secrets."""

import base64
import hashlib
import hmac
import json

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa


@pytest.fixture(params=["RS256", "ES256"])
def signing_key(request):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048) if request.param == "RS256" else ec.generate_private_key(ec.SECP256R1())
    return request.param, key


@pytest.mark.parametrize("formatting", ["indented-end", "carriage-returns", "single-line"])
def test_formatted_public_key_rejects_hmac_forgery(signing_key, formatting):
    algorithm, key = signing_key
    pem = key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    if formatting == "indented-end":
        pem = pem.replace(b"-----END", b"\t-----END")
    elif formatting == "carriage-returns":
        pem = pem.replace(b"\n", b"\r")
    else:
        pem = b" ".join(pem.splitlines())

    # These are still valid public keys, not malformed-input rejection fixtures.
    serialization.load_pem_public_key(pem)
    claims = {"sub": "synthetic-security-test"}
    valid = jwt.encode(claims, key, algorithm=algorithm)
    assert jwt.decode(valid, pem, algorithms=[algorithm, "HS256"]) == claims

    def segment(value):
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")

    # Sign with the public bytes ourselves: fixed PyJWT also rejects this at encode.
    message = segment({"alg": "HS256", "typ": "JWT"}) + b"." + segment(claims)
    signature = base64.urlsafe_b64encode(hmac.new(pem, message, hashlib.sha256).digest()).rstrip(b"=")
    forged = (message + b"." + signature).decode()
    with pytest.raises(jwt.InvalidKeyError):
        jwt.decode(forged, pem, algorithms=[algorithm, "HS256"])
