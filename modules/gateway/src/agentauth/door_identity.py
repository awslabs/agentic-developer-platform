"""Short-lived Door delegation, signed by the gateway's private Ed25519 key.

Only the mediated knowledge service calls this after live run/membership checks.
The Door holds public verification keys, never a secret capable of issuing grants.
"""

import base64
import hashlib
import json
import os
import time

from src.agentauth.envelope import SIGNING_KEY_ID_ENV, EnvelopeError, _signing_key


def sign_door_identity(*, principal: str, tenant_id: str, identity: dict[str, str], method: str, path: str, body: bytes, env=None) -> str:
    source = os.environ if env is None else env
    kid = source.get(SIGNING_KEY_ID_ENV, "")
    if not kid or not principal or not tenant_id:
        raise EnvelopeError("Door signing identity unavailable")
    now = int(time.time())
    payload = {
        "iss": "adp-gateway",
        "aud": "adp-knowledge-door",
        "kid": kid,
        "sub": principal,
        "tenant_id": tenant_id,
        "github_login": identity.get("x-github-login", ""),
        "owner_sub": identity.get("x-owner-sub", ""),
        "method": method,
        "path": path,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "iat": now,
        "exp": now + 30,
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).rstrip(b"=")
    signed = b"adpd1." + encoded
    signature = base64.urlsafe_b64encode(_signing_key(source).sign(signed)).rstrip(b"=")
    return (signed + b"." + signature).decode()
