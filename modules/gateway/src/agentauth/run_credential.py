"""The credential that answers "which run is calling?" (#5028).

## The problem this closes

`POST /agent/trigger` authenticates the caller with SigV4 over the shared
worker IAM role, then reads the caller's own identity out of the request body
(``parent_invocation_id``, sourced from ``ADP_MESSAGE_ID`` in the pod
environment). Every worker on the platform assumes the same role, and a worker
can rewrite its own environment. So the two things the gateway uses to identify
a caller are one value shared by all callers and one value the caller chooses.
A worker that follows malicious repository instructions can claim to be any
other run by exporting a different ``ADP_MESSAGE_ID``.

A credential fixes this by moving the invocation ID from *the request* into
*the credential*, and minting the credential somewhere the worker is not.

## Shape

An opaque token ``adpr1.<payload-b64url>.<mac-b64url>`` where the payload is
canonical JSON. Not a JWT: JWTs carry a caller-supplied ``alg`` header, and the
one failure mode this must not have is a caller talking the verifier into a
different algorithm. There is exactly one algorithm here and it is not
negotiable — the version prefix is checked before anything else is parsed.

HMAC-SHA256, symmetric, because minting and verification both happen inside
trusted services (dispatch mints, the gateway verifies) and **no worker ever
holds this key**. Contrast :mod:`src.agentauth.envelope`, which is asymmetric
precisely because a worker does have to verify those.

## What is bound and why

``invocation_id`` + ``attempt`` together, not ``invocation_id`` alone. A retried
or restarted attempt of the same invocation is a different execution with a
different pod; binding the attempt means a credential recovered from attempt 1
cannot be replayed by attempt 2 to act with attempt 1's identity.

``credential_epoch`` is deliberately a *separate* counter from the run
generation used by the control listener. Renewal has to be able to hand a
running worker a fresh credential without disturbing its control generation or
its command journal (AC6); one counter serving both would make every renewal
look like a re-registration and invalidate in-flight commands.

Expiry is short and mandatory. ``not_before`` exists so a credential minted
slightly ahead of pod start does not fail on clock skew at the moment the pod
first calls.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

# Version tag. Part of the MAC input, so a token minted for one version can
# never be reinterpreted under another version's rules even if the payload
# happens to parse.
CREDENTIAL_VERSION = "adpr1"

# Secret name for the minting/verification key. Read via env indirection rather
# than hardcoded so dev/prod cannot share a key by accident.
CREDENTIAL_KEY_ENV = "AGENT_RUN_CREDENTIAL_KEY"

# Maximum lifetime a mint may request. A caller asking for longer gets the cap,
# not an error — the cap is a property of the system, not of the request.
MAX_CREDENTIAL_TTL_SECONDS = 900

# Tolerance applied to ``not_before`` only. Expiry gets no grace: an expired
# credential is expired, and a "small" grace on expiry is how a revoked
# credential keeps working for one more window.
_NBF_SKEW_SECONDS = 30

# Bound on token size before any parsing. A credential is a few hundred bytes;
# anything larger is not a credential and should not reach json.loads.
_MAX_TOKEN_BYTES = 4096

# Fields every payload must carry. Absence is a verification failure, not a
# default — a credential missing its tenant must not verify as tenantless.
_REQUIRED_CLAIMS = (
    "invocation_id",
    "attempt",
    "tenant_id",
    "credential_epoch",
    "issued_at",
    "not_before",
    "expires_at",
)


class CredentialError(Exception):
    """A credential could not be verified.

    One exception type for every failure, and the message never distinguishes
    "bad signature" from "unknown invocation" to the caller. A verifier that
    reports *which* check failed tells an attacker whether their forged payload
    at least had a plausible shape.
    """


@dataclass(frozen=True)
class RunCredential:
    """A verified execution identity for one attempt of one invocation.

    Frozen for the same reason :class:`~src.orchestration.genesis.EngineGenesis`
    is: this object *is* the authorization input. Code that could reassign
    ``invocation_id`` after verification would have rebuilt the forgery this
    module exists to remove.

    There is no public constructor path that skips the MAC. The only way to
    obtain one is :func:`verify_credential`.
    """

    invocation_id: str
    attempt: int
    tenant_id: str
    credential_epoch: int
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    # The flow this execution belongs to, when dispatch knew it. Optional
    # because non-AI-DLC runs have no flow; grant resolution treats a missing
    # flow as "no flow-scoped authority", never as "any flow".
    flow_id: str | None = None
    # The persona the worker was dispatched as. Advisory for audit only —
    # authority comes from the grant, never from this string.
    persona: str | None = None

    @property
    def principal(self) -> str:
        """Stable audit identifier for this execution identity."""
        return f"{self.invocation_id}#{self.attempt}"


def _key(env: dict[str, str] | None = None) -> bytes:
    source = env if env is not None else os.environ
    raw = source.get(CREDENTIAL_KEY_ENV, "")
    if not raw:
        # Fail closed and loudly. A missing key must never degrade to
        # "accept anything" or to "unsigned credentials are fine" — that is the
        # marker-signing fail-open (#4128) repeated on an access-control path.
        raise CredentialError("run credential key is not configured")
    return raw.encode("utf-8")


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _canonical(payload: dict) -> bytes:
    """Deterministic bytes for MAC computation.

    Sorted keys and no whitespace: the MAC must be computed over exactly the
    bytes that will be transmitted and re-derived identically at verification.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def mint_credential(
    *,
    invocation_id: str,
    attempt: int,
    tenant_id: str,
    credential_epoch: int = 1,
    flow_id: str | None = None,
    persona: str | None = None,
    ttl_seconds: int = MAX_CREDENTIAL_TTL_SECONDS,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Mint a credential for one attempt of one invocation.

    **This is not an endpoint and must never become one.** Exchanging a claimed
    run ID plus the shared IAM role for a credential would hand any worker any
    identity it can name — the exact escalation the design forbids. The only
    callers are trusted dispatch paths that already know, from their own state,
    which invocation they are launching; the credential is then delivered to
    that pod alone.
    """
    if not invocation_id:
        raise CredentialError("invocation_id is required")
    if not tenant_id:
        raise CredentialError("tenant_id is required")
    if attempt < 1:
        raise CredentialError("attempt must be >= 1")
    if credential_epoch < 1:
        raise CredentialError("credential_epoch must be >= 1")

    issued = (now or datetime.now(UTC)).replace(microsecond=0)
    ttl = max(1, min(int(ttl_seconds), MAX_CREDENTIAL_TTL_SECONDS))

    payload: dict[str, object] = {
        "v": CREDENTIAL_VERSION,
        "invocation_id": invocation_id,
        "attempt": int(attempt),
        "tenant_id": tenant_id,
        "credential_epoch": int(credential_epoch),
        "issued_at": _iso(issued),
        "not_before": _iso(issued),
        "expires_at": _iso(issued + timedelta(seconds=ttl)),
    }
    if flow_id:
        payload["flow_id"] = flow_id
    if persona:
        payload["persona"] = persona

    body = _canonical(payload)
    mac = hmac.new(_key(env), CREDENTIAL_VERSION.encode() + b"." + body, hashlib.sha256).digest()
    return f"{CREDENTIAL_VERSION}.{_b64e(body)}.{_b64e(mac)}"


def verify_credential(
    token: str,
    *,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> RunCredential:
    """Verify a credential and return the execution identity it asserts.

    Order matters: size, then version, then MAC, then claims, then validity.
    The MAC is checked *before* any claim is read, so an unauthenticated payload
    never influences control flow beyond its own rejection.
    """
    if not token or len(token) > _MAX_TOKEN_BYTES:
        raise CredentialError("invalid run credential")

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != CREDENTIAL_VERSION:
        raise CredentialError("invalid run credential")

    try:
        body = _b64d(parts[1])
        provided_mac = _b64d(parts[2])
    except (ValueError, TypeError) as exc:
        raise CredentialError("invalid run credential") from exc

    expected = hmac.new(_key(env), CREDENTIAL_VERSION.encode() + b"." + body, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, provided_mac):
        raise CredentialError("invalid run credential")

    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise CredentialError("invalid run credential") from exc
    if not isinstance(payload, dict) or payload.get("v") != CREDENTIAL_VERSION:
        raise CredentialError("invalid run credential")

    for claim in _REQUIRED_CLAIMS:
        if payload.get(claim) in (None, ""):
            raise CredentialError("invalid run credential")

    attempt = payload["attempt"]
    epoch = payload["credential_epoch"]
    # `isinstance(True, int)` is True in Python, so booleans are excluded
    # explicitly. A payload with `attempt: true` must not verify as attempt 1.
    if isinstance(attempt, bool) or not isinstance(attempt, int) or attempt < 1:
        raise CredentialError("invalid run credential")
    if isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 1:
        raise CredentialError("invalid run credential")

    issued = _parse_iso(payload["issued_at"])
    not_before = _parse_iso(payload["not_before"])
    expires = _parse_iso(payload["expires_at"])

    current = now or datetime.now(UTC)
    if current >= expires:
        raise CredentialError("run credential has expired")
    if current + timedelta(seconds=_NBF_SKEW_SECONDS) < not_before:
        raise CredentialError("run credential is not yet valid")

    flow_id = payload.get("flow_id")
    persona = payload.get("persona")

    return RunCredential(
        invocation_id=str(payload["invocation_id"]),
        attempt=attempt,
        tenant_id=str(payload["tenant_id"]),
        credential_epoch=epoch,
        issued_at=issued,
        not_before=not_before,
        expires_at=expires,
        flow_id=str(flow_id) if flow_id else None,
        persona=str(persona) if persona else None,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise CredentialError("invalid run credential")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise CredentialError("invalid run credential") from exc
    return parsed.replace(tzinfo=UTC)
