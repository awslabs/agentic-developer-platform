"""Signed, short-lived dashboard binding for one owner and exact Crew target."""

import base64
import binascii
import hashlib
import hmac
import json

from fastapi import HTTPException

from src.crew.targets import CrewOwner, ResolvedCrewTarget
from src.shared.config import get_settings

CREW_TICKET_LIFETIME_SECONDS = 20 * 60 * 60


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _secret() -> bytes:
    secret = get_settings().token_secret_key
    if not secret:
        raise HTTPException(503, detail={"error": "crew_signing_unavailable"})
    return secret.encode("utf-8")


def mint_crew_ticket(owner: CrewOwner, target: ResolvedCrewTarget, *, now: int) -> str:
    if target.lease.tenant_id != owner.tenant_id or target.lease.user_id != owner.user_id:
        raise ValueError("Crew target does not belong to owner")
    payload = json.dumps(
        {
            "v": 2,
            "target_kind": target.lease.target_kind,
            "target_ref": target.lease.target_ref,
            "tenant": owner.tenant_id,
            "user": owner.user_id,
            "generation": target.lease.generation,
            "expires": now + CREW_TICKET_LIFETIME_SECONDS,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.digest(_secret(), b"adp-crew-v2:" + payload, hashlib.sha256)
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_crew_ticket(ticket: str) -> dict:
    if len(ticket) > 2048:
        raise ValueError("Invalid dashboard binding")
    try:
        payload_text, signature_text = ticket.split(".")
        payload = base64.urlsafe_b64decode(payload_text + "=" * (-len(payload_text) % 4))
        signature = base64.urlsafe_b64decode(signature_text + "=" * (-len(signature_text) % 4))
        expected = hmac.digest(_secret(), b"adp-crew-v2:" + payload, hashlib.sha256)
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid dashboard binding")
        claims = json.loads(payload)
        if (
            not isinstance(claims, dict)
            or claims.get("v") != 2
            or claims.get("target_kind") not in ("eks-pod", "ec2-instance")
            or not isinstance(claims.get("target_ref"), str)
            or not claims["target_ref"]
            or not isinstance(claims.get("tenant"), str)
            or not isinstance(claims.get("user"), str)
            or type(claims.get("generation")) is not int
            or type(claims.get("expires")) is not int
        ):
            raise ValueError("Invalid dashboard binding")
        return claims
    except (ValueError, UnicodeError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid dashboard binding") from exc
