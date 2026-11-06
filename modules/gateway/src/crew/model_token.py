"""Short-lived, gateway-only signing for an owner-bound EC2 Crew model run."""

import base64
import binascii
import hashlib
import hmac
import json

from fastapi import HTTPException

from src.crew.handoff import active_crew_run
from src.crew.targets import CrewOwner, ResolvedCrewTarget
from src.shared.config import get_settings

MODEL_TOKEN_LIFETIME_SECONDS = 60


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _secret() -> bytes:
    secret = get_settings().token_secret_key
    if not secret:
        raise HTTPException(503, detail={"error": "crew_model_signing_unavailable"})
    return secret.encode()


def _claims(owner: CrewOwner, target: ResolvedCrewTarget, run_id: str) -> dict:
    lease = target.lease
    return {
        "v": 1,
        "tenant": owner.tenant_id,
        "owner": owner.user_id,
        "run": run_id,
        "box": lease.target_ref,
        "instance": target.host_instance_ref,
        "generation": lease.generation,
        "fingerprint": target.host_fingerprint,
    }


async def mint_model_token(owner: CrewOwner, target: ResolvedCrewTarget, *, now: int) -> str:
    lease = target.lease
    if (
        lease.target_kind != "ec2-instance"
        or lease.tenant_id != owner.tenant_id
        or lease.user_id != owner.user_id
        or not target.host_instance_ref
        or not target.host_fingerprint
        or lease.expires_at <= now
    ):
        raise HTTPException(409, detail={"error": "lease_fenced"})
    run_id = await active_crew_run(owner, target, now=now)
    payload = json.dumps(
        {**_claims(owner, target, run_id), "nbf": now, "exp": min(now + MODEL_TOKEN_LIFETIME_SECONDS, lease.expires_at)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    signature = hmac.digest(_secret(), b"adp-crew-host-model-v1:" + payload, hashlib.sha256)
    return f"{_encode(payload)}.{_encode(signature)}"


async def verify_model_token(token: str, target: ResolvedCrewTarget, *, now: int) -> dict:
    if not isinstance(token, str) or len(token) > 2048:
        raise HTTPException(401, detail={"error": "crew_model_credential_denied"})
    try:
        encoded, signed = token.split(".")
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        signature = base64.urlsafe_b64decode(signed + "=" * (-len(signed) % 4))
        if not hmac.compare_digest(
            signature,
            hmac.digest(
                _secret(),
                b"adp-crew-host-model-v1:" + payload,
                hashlib.sha256,
            ),
        ):
            raise ValueError("invalid signature")
        claims = json.loads(payload)
        if (
            not isinstance(claims, dict)
            or set(claims) != {"v", "tenant", "owner", "run", "box", "instance", "generation", "fingerprint", "nbf", "exp"}
            or type(claims["v"]) is not int
            or claims["v"] != 1
            or type(claims["nbf"]) is not int
            or type(claims["exp"]) is not int
            or claims["nbf"] > now
            or claims["exp"] <= now
            or claims["exp"] - claims["nbf"] > MODEL_TOKEN_LIFETIME_SECONDS
            or type(claims["generation"]) is not int
            or not isinstance(claims["run"], str)
            or not claims["run"]
        ):
            raise ValueError("invalid claims")
    except (ValueError, TypeError, KeyError, UnicodeError, binascii.Error):
        raise HTTPException(401, detail={"error": "crew_model_credential_denied"}) from None
    owner = CrewOwner(claims["tenant"], claims["owner"])
    expected = _claims(owner, target, claims["run"])
    if (
        target.lease.target_kind != "ec2-instance"
        or target.lease.tenant_id != owner.tenant_id
        or target.lease.user_id != owner.user_id
        or target.lease.expires_at <= now
        or any(claims[key] != value for key, value in expected.items())
    ):
        raise HTTPException(401, detail={"error": "crew_model_credential_denied"})
    try:
        current = await active_crew_run(owner, target, now=now)
    except HTTPException:
        raise HTTPException(401, detail={"error": "crew_model_credential_denied"}) from None
    if current != claims["run"]:
        raise HTTPException(401, detail={"error": "crew_model_credential_denied"})
    return claims
