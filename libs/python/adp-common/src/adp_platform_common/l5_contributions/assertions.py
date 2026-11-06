"""Fixed, audience-specific, short-lived request binding. Public keys only in consumers."""

import base64
import hashlib
import json
import re
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .contract import canonical, storage_key

HEADER = "x-adp-l5-authority"


def _decode(value):
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("invalid assertion encoding")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def sign(claims, *, key, key_id, method, path, body, destination, now=None):
    now = int(time.time() if now is None else now)
    packet = {**claims, "iss": "adp-gateway", "aud": "adp-l5-" + destination,
              "kid": key_id, "method": method, "path": path,
              "body_sha256": hashlib.sha256(body).hexdigest(), "iat": now, "exp": now + 30}
    encoded = base64.urlsafe_b64encode(canonical(packet)).rstrip(b"=")
    message = b"adpl51." + encoded
    return (message + b"." + base64.urlsafe_b64encode(key.sign(message)).rstrip(b"=")).decode()


def verify(token, keys, *, destination, method=None, path=None, body=None, now=None):
    if not isinstance(token, str) or not token or len(token) > 32768:
        raise ValueError("invalid assertion")
    version, encoded, signature = token.split(".")
    if version != "adpl51":
        raise ValueError("invalid assertion version")
    claims = json.loads(_decode(encoded))
    if not isinstance(claims, dict) or claims.get("kid") not in keys:
        raise ValueError("invalid assertion key")
    key = serialization.load_pem_public_key(keys[claims["kid"]].encode())
    if not isinstance(key, Ed25519PublicKey):
        raise ValueError("invalid assertion key type")
    key.verify(_decode(signature), (version + "." + encoded).encode())
    now = time.time() if now is None else now
    if (claims.get("iss") != "adp-gateway" or claims.get("aud") != "adp-l5-" + destination
            or claims.get("destination") != destination
            or type(claims.get("iat")) is not int or type(claims.get("exp")) is not int
            or not 0 < claims["exp"] - claims["iat"] <= 30
            or claims["iat"] > now + 5 or claims["exp"] <= now
            or (method is not None and claims.get("method") != method)
            or (path is not None and claims.get("path") != path)
            or (body is not None and claims.get("body_sha256") != hashlib.sha256(body).hexdigest())):
        raise ValueError("invalid assertion binding")
    if claims.get("storage_key") != storage_key(claims["scope"], claims["result_ref"], destination, "1.0"):
        raise ValueError("invalid semantic key")
    return claims
