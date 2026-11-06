"""Short-lived, single-use handoff ticket carried on a top-level navigation to the Crew host.

The ADP frontend cannot set the Crew host's `adp_crew` cookie through a CORS fetch when
the Crew origin is cross-site (for example `*.cloudfront.net`, a public suffix): browsers
drop a `SameSite=Lax` cookie delivered by a cross-site response. The frontend therefore
asks the ADP origin for one of these tickets and then navigates the tab itself to
`https://<crew-origin>/crew/handoff?ticket=...`; the Crew host answers that first-party
request with the cookie and a redirect to the Kiro Crew login link.

A ticket is bound to the ADP identity (tenant, user) and the exact active lease
(target kind, reference and generation), expires after `HANDOFF_TICKET_LIFETIME_SECONDS`
and carries a nonce that the Crew host burns in Redis on first presentation, so a
captured ticket cannot be replayed. The ticket is never written to logs; the request
loggers redact the `ticket` query parameter.
"""

import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import secrets
from urllib.parse import urlsplit

from src.crew.targets import CrewOwner, ResolvedCrewTarget
from src.crew.tickets import _secret
from src.shared.config import get_settings
from src.shared.redis_client import create_redis_client

HANDOFF_TICKET_LIFETIME_SECONDS = 60
HANDOFF_TICKET_MAX_LENGTH = 2048
REDIS_TIMEOUT_SECONDS = 2
_PREFIX = b"adp-crew-handoff-v1:"


class HandoffTicketUnavailableError(Exception):
    """The single-use registry cannot be reached; redemption must fail closed."""


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def mint_handoff_ticket(owner: CrewOwner, target: ResolvedCrewTarget, *, now: int) -> str:
    if target.lease.tenant_id != owner.tenant_id or target.lease.user_id != owner.user_id:
        raise ValueError("Crew target does not belong to owner")
    payload = json.dumps(
        {
            "v": 1,
            "kind": "handoff",
            "tenant": owner.tenant_id,
            "user": owner.user_id,
            "target_kind": target.lease.target_kind,
            "target_ref": target.lease.target_ref,
            "generation": target.lease.generation,
            "nonce": secrets.token_urlsafe(24),
            "issued": now,
            "expires": now + HANDOFF_TICKET_LIFETIME_SECONDS,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    signature = hmac.digest(_secret(), _PREFIX + payload, hashlib.sha256)
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_handoff_ticket(ticket: str, *, now: int) -> dict:
    """Return the claims of a well-formed, correctly signed, unexpired ticket or raise ValueError."""
    if not isinstance(ticket, str) or not ticket or len(ticket) > HANDOFF_TICKET_MAX_LENGTH:
        raise ValueError("Invalid handoff ticket")
    try:
        payload_text, signature_text = ticket.split(".")
        payload = _decode(payload_text)
        signature = _decode(signature_text)
        expected = hmac.digest(_secret(), _PREFIX + payload, hashlib.sha256)
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Invalid handoff ticket")
        claims = json.loads(payload)
        if (
            not isinstance(claims, dict)
            or claims.get("v") != 1
            or claims.get("kind") != "handoff"
            or not isinstance(claims.get("tenant"), str)
            or not claims["tenant"]
            or not isinstance(claims.get("user"), str)
            or not claims["user"]
            or claims.get("target_kind") not in ("eks-pod", "ec2-instance")
            or not isinstance(claims.get("target_ref"), str)
            or not claims["target_ref"]
            or type(claims.get("generation")) is not int
            or not isinstance(claims.get("nonce"), str)
            or not (16 <= len(claims["nonce"]) <= 64)
            or type(claims.get("issued")) is not int
            or type(claims.get("expires")) is not int
            or claims["expires"] - claims["issued"] > HANDOFF_TICKET_LIFETIME_SECONDS
            or claims["issued"] > now + 5
            or claims["expires"] <= now
        ):
            raise ValueError("Invalid handoff ticket")
        return claims
    except (ValueError, UnicodeError, TypeError, binascii.Error) as exc:
        raise ValueError("Invalid handoff ticket") from exc


class HandoffTicketRegistry:
    """Burns ticket nonces so each ticket is redeemable exactly once across gateway replicas."""

    def __init__(self, client, namespace: str):
        self.client = client
        self.namespace = hashlib.sha256(namespace.encode()).hexdigest()[:24]

    async def consume(self, claims: dict, *, now: int) -> bool:
        """Mark the ticket as used; return False when it was already presented."""
        digest = hashlib.sha256(f"{claims['tenant']}\0{claims['user']}\0{claims['nonce']}".encode()).hexdigest()
        key = f"crew-handoff:{self.namespace}:{digest}"
        ttl = max(1, claims["expires"] - now) + HANDOFF_TICKET_LIFETIME_SECONDS
        try:
            async with asyncio.timeout(REDIS_TIMEOUT_SECONDS):
                return bool(await self.client.set(key, "1", nx=True, ex=ttl))
        except Exception as exc:
            raise HandoffTicketUnavailableError from exc

    async def aclose(self) -> None:
        try:
            await self.client.aclose()
        except Exception:
            pass


def handoff_ticket_registry() -> HandoffTicketRegistry:
    settings = get_settings()
    if not settings.redis_url:
        raise HandoffTicketUnavailableError
    try:
        endpoint = urlsplit(settings.redis_url)
        namespace = settings.redis_cache_name or f"{endpoint.hostname}:{endpoint.port or 6379}{endpoint.path or '/0'}"
        return HandoffTicketRegistry(
            create_redis_client(settings.redis_url, socket_timeout=REDIS_TIMEOUT_SECONDS, socket_connect_timeout=REDIS_TIMEOUT_SECONDS),
            namespace,
        )
    except Exception as exc:
        raise HandoffTicketUnavailableError from exc


async def consume_handoff_ticket(claims: dict, *, now: int) -> bool:
    registry = handoff_ticket_registry()
    try:
        return await registry.consume(claims, now=now)
    finally:
        await registry.aclose()
