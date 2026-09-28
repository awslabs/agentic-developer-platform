"""The gateway-signed authorization envelope a worker listener verifies (#5028).

## What this adds to the existing listener checks

The listener already checks a per-run bearer token, its expiry (#5024) and the
run generation. Those prove the caller knows a secret the worker itself minted
and wrote to its own DynamoDB row. They do **not** prove the gateway authorized
this particular command — and the worker IAM role can write that row, so a
compromised worker that can read another run's row learns that run's token.

The envelope proves a different thing: *the trusted control service decided that
this caller may perform this action on this run, and here is that decision bound
to this exact request*. The listener verifies it without querying the chain and
without any database access.

## Why Ed25519 and not the existing HMAC

The lineage-marker HMAC key (`marker_signing.py`) is readable by workers — it
has to be, because workers sign their own markers with it. Any authority whose
key a worker holds is an authority a worker can forge. So this is asymmetric:
the control service holds the signing key, workers receive only the public
verification key. `crypto.verify(null, ...)` in Node and
`Ed25519PublicKey.verify` in Python both do Ed25519 with no extra dependency, so
the verification side stays dependency-free on the worker.

`alg` is a fixed field checked against an allowlist of exactly one value before
any signature work happens. It exists so a future rotation to a second algorithm
is explicit, not so a caller can choose.

## What is bound, and what each binding stops

| Binding | Attack it closes |
|---|---|
| ``iss`` / ``aud`` | An envelope minted for a different service or environment being replayed here |
| ``target_run_id`` + ``target_generation`` | Replaying a valid envelope at a *different* run, or at a restarted pod (AC5) |
| ``action`` | Swapping an authorized `pause` into an `abort` |
| ``command_id`` | Detaching the authorization from the journal entry that dedupes it |
| ``body_digest`` | Changing the steer instruction after authorization (AC5) |
| ``grant_id`` + ``revocation_epoch`` | Executing a queued action under authority since revoked (AC6) |
| ``nbf`` / ``exp`` | Indefinite reuse of one authorization |

## What a signature cannot do

It cannot provide instant revocation — a signed statement stays valid for its
window no matter what happens to the grant. Two things bound that honestly:
the window is short (:data:`MAX_ENVELOPE_TTL_SECONDS`), and the gateway
re-checks live authorization on every request rather than letting a client reuse
an envelope. The resulting maximum revocation delay is therefore the envelope
TTL for an *already-forwarded* command, and zero for a new one. A queued action
must re-check ``revocation_epoch`` before it executes, which is why the epoch is
in the envelope at all.

It also cannot prove the gateway's decision was *correct*, and it cannot protect
a fully compromised target worker from its own process. Both are stated in the
design and neither is claimed here.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

ENVELOPE_VERSION = "adpe1"

# Exactly one permitted algorithm. An allowlist rather than "whatever the
# envelope says" — the classic JWT `alg` confusion has no foothold here.
ALLOWED_ALGORITHMS: frozenset[str] = frozenset({"ed25519"})

# The issuer a listener must see. A constant on both sides, not configuration a
# worker could be tricked into widening.
ENVELOPE_ISSUER = "adp-gateway-control"

# The audience is the worker control listener surface.
ENVELOPE_AUDIENCE = "adp-agent-control-listener"

# A model decision is consumed by worker bootstrap, not by the live-control
# listener.  Keeping a distinct audience prevents a valid decision token from
# being replayed as a control authorization (PMM-06).
MODEL_POLICY_AUDIENCE = "adp-agent-model-policy"

# An abort receipt is consumed by the *finalizing supervisor* (a different process
# from the listener) to decide whether a stopped run may be reported as a
# deliberate abort and its queue message deleted — Issue #3963.
#
# A distinct audience for the same reason as PMM-06, and here the replay it
# prevents is the specific attack root's finding 1 names: a control envelope
# authorizing an abort proves an operator *asked*, and it stays valid for its whole
# TTL whether or not the command was ever accepted by the live run. If the receipt
# shared the listener's audience, that issuance envelope would itself satisfy the
# finalizer, and "an operator requested an abort" would be indistinguishable from
# "this run actually accepted and applied one". The gateway mints this audience
# only after live revalidation succeeds AND durable intent is persisted, so
# possession of it is evidence of accepted live delivery rather than of a request.
ABORT_RECEIPT_AUDIENCE = "adp-agent-abort-receipt"

# The action claim on an abort receipt. Distinct from the `abort` control action so
# a receipt can never be replayed into the listener's command path as a fresh
# authorization to abort something.
ABORT_RECEIPT_ACTION = "abort_accepted"

# Signing key secret name (PEM-encoded Ed25519 private key), gateway only.
SIGNING_KEY_ENV = "AGENT_CONTROL_ENVELOPE_SIGNING_KEY"
# Key ID so a listener holding two public keys can pick the right one during
# rotation without trial verification.
SIGNING_KEY_ID_ENV = "AGENT_CONTROL_ENVELOPE_KEY_ID"

# Short forwarding validity. This IS the documented maximum revocation delay for
# an in-flight forwarded command; see the module docstring.
MAX_ENVELOPE_TTL_SECONDS = 30

_MAX_ENVELOPE_BYTES = 8192

_REQUIRED_CLAIMS = (
    "iss",
    "aud",
    "alg",
    "kid",
    "tenant_id",
    "principal",
    "target_run_id",
    "target_generation",
    "action",
    "command_id",
    "body_digest",
    "iat",
    "nbf",
    "exp",
)

# Claims whose JSON type is part of the contract. Checked as a group *before* any
# claim is used for a set membership test, a dict lookup or a string coercion.
#
# This ordering is the fix for a real defect: ``alg`` went straight into
# ``payload["alg"] not in ALLOWED_ALGORITHMS``, so an unsigned envelope carrying
# ``"alg": []`` raised ``TypeError: unhashable type: 'list'`` out of the frozenset
# hash instead of :class:`EnvelopeError`. The caller saw an unhandled exception —
# an internal error rather than the opaque authorization refusal this contract
# promises. The same shape applied to ``kid``, where a non-string value reached
# ``str(...)`` and then a dict lookup.
#
# It is not an authorization bypass in either language: nothing was admitted that
# should have been refused. It is an exception-handling defect, and the reason it
# matters is that a verifier whose refusal path can throw has a refusal path that
# is not uniformly observable — the listener's outer catch turns it into a 500,
# which both leaks that this input was *different* and loses the logged reason.
#
# Types are asserted rather than coerced. ``str(value)`` on attacker-controlled
# JSON is a silent accept: ``{"kid": ["k1"]}`` would coerce to ``"['k1']"``, miss
# the key map and refuse for the wrong reason, and a hostile ``__str__`` has no
# equivalent in JSON but does in the TypeScript port, where object coercion can
# throw. Requiring the declared type keeps both verifiers rejecting the same
# inputs for the same reason.
_STRING_CLAIMS = (
    "iss",
    "aud",
    "alg",
    "kid",
    "tenant_id",
    "principal",
    "target_run_id",
    "action",
    "command_id",
    "body_digest",
    "iat",
    "nbf",
    "exp",
)

_INT_CLAIMS = ("target_generation",)


class EnvelopeError(Exception):
    """An envelope could not be produced or verified."""


def body_digest(raw: bytes) -> str:
    """Digest of the exact request body bytes the caller will send.

    Over raw bytes, not a parsed-and-reserialized object: re-serialization would
    let two different wire bodies produce one digest, which is exactly the
    "changed body, same authorization" case this must catch.
    """
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class ControlEnvelope:
    """A verified authorization envelope."""

    tenant_id: str
    principal: str
    target_run_id: str
    target_generation: int
    action: str
    command_id: str
    body_digest: str
    grant_id: str | None
    revocation_epoch: int | None
    key_id: str
    issued_at: datetime
    not_before: datetime
    expires_at: datetime
    flow_id: str | None = None
    authority_reference_id: str | None = None
    authority_kind: str = "delegated_grant"
    chain_id: str | None = None


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _canonical(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _signing_key(env: dict[str, str] | None = None) -> Ed25519PrivateKey:
    source = env if env is not None else os.environ
    pem = source.get(SIGNING_KEY_ENV, "")
    if not pem:
        raise EnvelopeError("envelope signing key is not configured")
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError) as exc:
        raise EnvelopeError("envelope signing key is unusable") from exc
    if not isinstance(key, Ed25519PrivateKey):
        # A non-Ed25519 key would otherwise fail later with a confusing error, or
        # worse, be used under an algorithm label that does not match it.
        raise EnvelopeError("envelope signing key is not Ed25519")
    return key


def sign_envelope(
    *,
    tenant_id: str,
    principal: str,
    target_run_id: str,
    target_generation: int,
    action: str,
    command_id: str,
    request_body: bytes,
    grant_id: str | None = None,
    revocation_epoch: int | None = None,
    authority_kind: str = "delegated_grant",
    flow_id: str | None = None,
    authority_reference_id: str | None = None,
    audience: str = ENVELOPE_AUDIENCE,
    chain_id: str | None = None,
    ttl_seconds: int = MAX_ENVELOPE_TTL_SECONDS,
    now: datetime | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Sign an envelope. Only the trusted control service may call this.

    Workers never reach this function: they run in a different process with a
    different image, and the signing key is delivered only to the gateway. That
    separation is the whole security property — see the module docstring.
    """
    if authority_kind == "human_session":
        if grant_id is not None or revocation_epoch is not None or authority_reference_id is not None:
            raise EnvelopeError("human authority cannot carry delegated claims")
    elif authority_kind == "delegated_grant":
        if not isinstance(grant_id, str) or not grant_id or type(revocation_epoch) is not int or revocation_epoch < 1:
            raise EnvelopeError("delegated authority requires a grant and revocation epoch")
    else:
        raise EnvelopeError("unknown authority kind")
    source = env if env is not None else os.environ
    key_id = source.get(SIGNING_KEY_ID_ENV, "")
    if not key_id:
        raise EnvelopeError("envelope key id is not configured")

    # Never turn an expired authorization into a fresh one-second proof. The
    # caller checks freshness after ownership reads; the signer independently
    # refuses invalid lifetimes instead of coercing them into authority.
    if type(ttl_seconds) is not int or ttl_seconds < 1:
        raise EnvelopeError("envelope lifetime must be a positive integer")
    issued = (now or datetime.now(UTC)).replace(microsecond=0)
    ttl = min(ttl_seconds, MAX_ENVELOPE_TTL_SECONDS)

    payload: dict[str, object] = {
        "v": ENVELOPE_VERSION,
        "iss": ENVELOPE_ISSUER,
        "aud": audience,
        "alg": "ed25519",
        "kid": key_id,
        "tenant_id": tenant_id,
        "principal": principal,
        "target_run_id": target_run_id,
        "target_generation": int(target_generation),
        "action": action,
        "command_id": command_id,
        "body_digest": body_digest(request_body),
        "authority_kind": authority_kind,
        "iat": _iso(issued),
        "nbf": _iso(issued),
        "exp": _iso(issued + timedelta(seconds=ttl)),
    }
    if authority_kind == "delegated_grant":
        payload["grant_id"] = grant_id
        payload["revocation_epoch"] = revocation_epoch
    if flow_id:
        payload["flow_id"] = flow_id
    if authority_reference_id:
        payload["authority_reference_id"] = authority_reference_id
    if chain_id:
        payload["chain_id"] = chain_id

    body = _canonical(payload)
    signature = _signing_key(env).sign(ENVELOPE_VERSION.encode() + b"." + body)
    return f"{ENVELOPE_VERSION}.{_b64e(body)}.{_b64e(signature)}"


def verify_envelope(
    token: str,
    *,
    public_keys: dict[str, Ed25519PublicKey | bytes],
    expected_run_id: str,
    expected_generation: int,
    expected_action: str,
    expected_command_id: str,
    request_body: bytes,
    expected_audience: str = ENVELOPE_AUDIENCE,
    expected_chain_id: str | None = None,
    now: datetime | None = None,
) -> ControlEnvelope:
    """Verify an envelope against what the verifier already knows independently.

    The ``expected_*`` arguments are the verifier's own facts — its run ID, its
    generation, the action from the request path, the command ID from the parsed
    body. Passing anything derived from the envelope itself would make the
    binding checks tautological.

    This Python implementation exists so the contract is testable and
    executable in one place; the worker listener implements the same checks in
    TypeScript (``control-envelope.ts``), and a shared vector fixture keeps the
    two honest.
    """
    if not token or len(token) > _MAX_ENVELOPE_BYTES:
        raise EnvelopeError("invalid envelope")

    parts = token.split(".")
    if len(parts) != 3 or parts[0] != ENVELOPE_VERSION:
        raise EnvelopeError("invalid envelope")

    try:
        body = _b64d(parts[1])
        signature = _b64d(parts[2])
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError) as exc:
        raise EnvelopeError("invalid envelope") from exc

    if not isinstance(payload, dict) or payload.get("v") != ENVELOPE_VERSION:
        raise EnvelopeError("invalid envelope")
    for claim in _REQUIRED_CLAIMS:
        if payload.get(claim) in (None, ""):
            raise EnvelopeError("invalid envelope")

    # Claim types, before any claim is hashed, looked up or coerced. See
    # ``_STRING_CLAIMS`` for why this precedes the checks below rather than
    # relying on each of them to be type-safe on its own.
    for claim in _STRING_CLAIMS:
        if not isinstance(payload[claim], str):
            raise EnvelopeError("invalid envelope")
    for claim in _INT_CLAIMS:
        value = payload[claim]
        # ``bool`` is an ``int`` subclass, so ``True`` would otherwise pass as
        # generation 1.
        if isinstance(value, bool) or not isinstance(value, int):
            raise EnvelopeError("invalid envelope")

    # Missing kind is the original delegated wire format, never human authority.
    # The kind and conditional claims are covered by the signature below.
    authority_kind = payload.get("authority_kind", "delegated_grant")
    if authority_kind == "human_session":
        if any(claim in payload for claim in ("grant_id", "revocation_epoch", "authority_reference_id")):
            raise EnvelopeError("invalid envelope")
    elif authority_kind == "delegated_grant":
        if not isinstance(payload.get("grant_id"), str) or not payload["grant_id"]:
            raise EnvelopeError("invalid envelope")
        if type(payload.get("revocation_epoch")) is not int or payload["revocation_epoch"] < 1:
            raise EnvelopeError("invalid envelope")
    else:
        raise EnvelopeError("invalid envelope")

    # Algorithm and key selection happen BEFORE signature verification, and the
    # algorithm is checked against the allowlist rather than used to dispatch.
    if payload["alg"] not in ALLOWED_ALGORITHMS:
        raise EnvelopeError("unsupported envelope algorithm")
    if payload["iss"] != ENVELOPE_ISSUER:
        raise EnvelopeError("untrusted envelope issuer")
    if payload["aud"] != expected_audience:
        raise EnvelopeError("envelope audience mismatch")

    key_id = payload["kid"]
    raw_key = public_keys.get(key_id)
    if raw_key is None:
        raise EnvelopeError("unknown envelope key id")
    key = Ed25519PublicKey.from_public_bytes(raw_key) if isinstance(raw_key, bytes) else raw_key

    try:
        key.verify(signature, ENVELOPE_VERSION.encode() + b"." + body)
    except InvalidSignature as exc:
        raise EnvelopeError("invalid envelope signature") from exc

    # Binding checks. Each compares the envelope's claim to the verifier's own
    # independently known value.
    if payload["target_run_id"] != expected_run_id:
        raise EnvelopeError("envelope target mismatch")
    claimed_generation = payload["target_generation"]
    if claimed_generation != expected_generation:
        raise EnvelopeError("envelope generation mismatch")
    if payload["action"] != expected_action:
        raise EnvelopeError("envelope action mismatch")
    if payload["command_id"] != expected_command_id:
        raise EnvelopeError("envelope command mismatch")
    if payload["body_digest"] != body_digest(request_body):
        raise EnvelopeError("envelope body mismatch")
    if expected_chain_id is not None and payload.get("chain_id") != expected_chain_id:
        raise EnvelopeError("envelope chain mismatch")

    epoch = payload.get("revocation_epoch")

    issued = _parse_iso(payload["iat"])
    not_before = _parse_iso(payload["nbf"])
    expires = _parse_iso(payload["exp"])
    current = now or datetime.now(UTC)
    if current >= expires:
        raise EnvelopeError("envelope has expired")
    if current < not_before:
        raise EnvelopeError("envelope is not yet valid")
    # An envelope claiming a longer life than the policy permits is rejected
    # rather than truncated. Truncating would accept a signed statement whose
    # signer disagreed with our policy about how long it lives.
    if (expires - not_before).total_seconds() > MAX_ENVELOPE_TTL_SECONDS:
        raise EnvelopeError("envelope validity exceeds policy")

    flow_id = payload.get("flow_id")
    authority_ref = payload.get("authority_reference_id")
    chain_id = payload.get("chain_id")

    return ControlEnvelope(
        tenant_id=payload["tenant_id"],
        principal=payload["principal"],
        target_run_id=payload["target_run_id"],
        target_generation=claimed_generation,
        action=payload["action"],
        command_id=payload["command_id"],
        body_digest=payload["body_digest"],
        grant_id=payload.get("grant_id"),
        revocation_epoch=epoch,
        key_id=key_id,
        issued_at=issued,
        not_before=not_before,
        expires_at=expires,
        flow_id=str(flow_id) if flow_id else None,
        authority_reference_id=str(authority_ref) if authority_ref else None,
        authority_kind=authority_kind,
        chain_id=str(chain_id) if chain_id else None,
    )


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(value: object) -> datetime:
    if not isinstance(value, str):
        raise EnvelopeError("invalid envelope")
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise EnvelopeError("invalid envelope") from exc
