"""Signed authority for one ingestion asset and attempt (#5663).

The gateway commits the asset, explicit tenant (including shared NULL), attempt
UUID and token digest before publishing. Callbacks require the current attempt
on a nonterminal asset; reindex and terminal transitions invalidate older grants.
``adpk2`` is an HMAC domain separate from run credentials.

Seven-day expiry accommodates queue retention. Row state and attempt matching
additionally limit replay. Signing is mandatory for dispatch; unsigned and adpk1
callbacks are refused.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# Version tag AND MAC domain separator. See the module docstring: this prefix is
# what keeps a run credential from verifying as an ingestion grant under the
# shared key.
GRANT_VERSION = "adpk2"

# Same env indirection as the run credential, so dev and prod cannot share a key
# by accident and no new secret has to be provisioned for this control.
GRANT_KEY_ENV = "AGENT_RUN_CREDENTIAL_KEY"

# Must outlive SQS retention (4 days) plus the longest ingestion run (1 hour).
MAX_GRANT_TTL_SECONDS = 7 * 24 * 60 * 60

# Tolerance on not_before only. An expired grant is expired; a grace period on
# expiry is how a superseded grant keeps working for one more window.
_NBF_SKEW_SECONDS = 30

# Bound on token size before any parsing. A grant is a few hundred bytes.
_MAX_TOKEN_BYTES = 4096

# Absence of any of these is a verification failure, never a default. Note that
# "tenant_id" is required to be PRESENT and may be null — see the docstring.
_REQUIRED_CLAIMS = ("asset_id", "tenant_id", "attempt_id", "issued_at", "not_before", "expires_at")


class GrantError(Exception):
    """A grant could not be verified.

    One exception type for every failure, and the message never says which check
    failed. A verifier that distinguishes "bad MAC" from "expired" tells a forger
    whether their payload at least had the right shape.
    """


@dataclass(frozen=True)
class IngestionCallbackGrant:
    """A verified authority to write status for exactly one knowledge asset.

    Frozen because this object *is* the authorization input. Code able to
    reassign ``asset_id`` after verification would have rebuilt the forgery this
    module removes. The only way to obtain one is :func:`verify_ingestion_grant`.
    """

    asset_id: str
    attempt_id: str
    # The tenant recorded at dispatch. None means the asset is shared scope, which
    # is a positive fact carried by the grant, not a missing value.
    tenant_id: str | None
    issued_at: datetime
    not_before: datetime
    expires_at: datetime

    @property
    def is_shared_scope(self) -> bool:
        """True when the grant authorizes a shared (tenant-less) asset."""
        return self.tenant_id is None


def _key(env: dict[str, str] | None = None) -> bytes:
    source = env if env is not None else os.environ
    raw = source.get(GRANT_KEY_ENV, "")
    if not raw:
        raise GrantError("ingestion callback grant key is not configured")
    return raw.encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _canonical(payload: dict) -> bytes:
    """Deterministic bytes for the MAC: sorted keys, no whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _mac(body: bytes, env: dict[str, str] | None) -> bytes:
    return hmac.new(_key(env), GRANT_VERSION.encode() + b"." + body, hashlib.sha256).digest()


def mint_ingestion_grant(
    *,
    asset_id: str,
    tenant_id: str | None,
    attempt_id: str | None = None,
    ttl_seconds: int = MAX_GRANT_TTL_SECONDS,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Mint a callback grant for one asset.

    **This is not an endpoint and must never become one.** Exchanging a claimed
    asset id for a grant over the internal plane would hand any caller authority
    over any row it can name — the exact escalation this closes. The only caller
    is :func:`src.knowledge.dispatch.dispatch_ingestion`, which already knows
    from the gateway's own request handling which asset it is publishing and which
    tenant that asset belongs to.

    ``tenant_id`` is passed through unchanged, including ``None``. It is NOT
    defaulted, because "the dispatcher did not know the tenant" and "the asset is
    shared" must not become the same token.
    """
    if not asset_id:
        raise GrantError("asset_id is required")
    if tenant_id is not None and (not isinstance(tenant_id, str) or not tenant_id):
        raise GrantError("explicit tenant or shared scope is required")
    try:
        attempt = str(uuid.UUID(attempt_id)) if attempt_id is not None else str(uuid.uuid4())
    except (ValueError, TypeError, AttributeError) as exc:
        raise GrantError("invalid ingestion attempt") from exc

    issued = (now or datetime.now(UTC)).replace(microsecond=0)
    ttl = max(1, min(int(ttl_seconds), MAX_GRANT_TTL_SECONDS))

    payload: dict[str, object] = {
        "v": GRANT_VERSION,
        "asset_id": str(asset_id),
        "attempt_id": attempt,
        # Always present, possibly null. Required by _REQUIRED_CLAIMS.
        "tenant_id": tenant_id,
        "issued_at": _iso(issued),
        "not_before": _iso(issued),
        "expires_at": _iso(issued + timedelta(seconds=ttl)),
    }

    body = _canonical(payload)
    return f"{GRANT_VERSION}.{_b64e(body)}.{_b64e(_mac(body, env))}"


def try_mint_ingestion_grant(
    *,
    asset_id: str,
    tenant_id: str | None,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> str | None:
    """Legacy optional helper; production dispatch requires successful minting."""
    try:
        return mint_ingestion_grant(asset_id=asset_id, tenant_id=tenant_id, now=now, env=env)
    except GrantError:
        logger.warning(
            "ingestion callback grant not minted for asset=%s: signing key unavailable — "
            "the callback for this asset will carry no server-owned binding",
            asset_id,
        )
        return None


def verify_ingestion_grant(
    token: str,
    *,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> IngestionCallbackGrant:
    """Verify a grant and return the asset authority it carries.

    Order matters: size, then version, then MAC, then claims, then validity. The
    MAC is checked before any claim is read, so an unauthenticated payload never
    influences control flow beyond its own rejection.
    """
    if not token or len(token) > _MAX_TOKEN_BYTES:
        raise GrantError("invalid ingestion callback grant")

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != GRANT_VERSION:
        raise GrantError("invalid ingestion callback grant")

    try:
        body = _b64d(parts[1])
        provided_mac = _b64d(parts[2])
    except (ValueError, TypeError) as exc:
        raise GrantError("invalid ingestion callback grant") from exc

    if not hmac.compare_digest(_mac(body, env), provided_mac):
        raise GrantError("invalid ingestion callback grant")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise GrantError("invalid ingestion callback grant") from exc
    if not isinstance(payload, dict) or payload.get("v") != GRANT_VERSION:
        raise GrantError("invalid ingestion callback grant")

    for claim in _REQUIRED_CLAIMS:
        # Presence, not truthiness: tenant_id is legitimately null and must still
        # be carried explicitly, so a stripped tenant cannot verify as shared.
        if claim not in payload:
            raise GrantError("invalid ingestion callback grant")
    if not payload["asset_id"] or not isinstance(payload["asset_id"], str):
        raise GrantError("invalid ingestion callback grant")

    tenant = payload["tenant_id"]
    if tenant is not None and (not isinstance(tenant, str) or not tenant):
        raise GrantError("invalid ingestion callback grant")

    issued = _parse_iso(payload["issued_at"])
    not_before = _parse_iso(payload["not_before"])
    expires = _parse_iso(payload["expires_at"])

    current = now or datetime.now(UTC)
    if current >= expires:
        raise GrantError("ingestion callback grant has expired")
    if current + timedelta(seconds=_NBF_SKEW_SECONDS) < not_before:
        raise GrantError("ingestion callback grant is not yet valid")

    try:
        attempt_id = str(uuid.UUID(payload["attempt_id"]))
    except (ValueError, TypeError, AttributeError) as exc:
        raise GrantError("invalid ingestion callback grant") from exc
    if not_before < issued or expires <= issued or (expires - issued).total_seconds() > MAX_GRANT_TTL_SECONDS:
        raise GrantError("invalid ingestion callback grant lifetime")

    return IngestionCallbackGrant(
        attempt_id=attempt_id,
        asset_id=payload["asset_id"],
        tenant_id=tenant,
        issued_at=issued,
        not_before=not_before,
        expires_at=expires,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise GrantError("invalid ingestion callback grant")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise GrantError("invalid ingestion callback grant") from exc
    return parsed.replace(tzinfo=UTC)
