"""Authentication rules for observation submission.

Issue #5043 (U8), EPIC #4910. R11 acceptance 2.

## The hole this closes

Today's heartbeat POST carries `Content-Type` and nothing else, and the receiver
has no auth dependency. So the *only* thing a caller needs in order to write a
cluster's state is that cluster's UUID — a value which appears in logs, in
kubeconfigs, in support tickets and in any API response that lists clusters.
Knowing an identifier is not authority over the thing it identifies, and this
module is where that stops being true by accident.

Submission requires a credential and an HMAC over the exact transmitted UTF-8
JSON bytes. The receiver must verify those bytes before constructing domain objects;
re-encoding parsed JSON is not portable across Go and Python (numbers, Unicode,
escapes and timestamps differ). `canonical_body` is a Python sender convenience,
not a cross-language canonicalization standard. A signature does not prevent replay:
verification also bounds reported_at against the receiver clock. U15 must enforce
idempotency/monotonic updates in its persisted authenticated subject stream.

This module holds no credentials, performs no I/O and mounts no receiver. U15
supplies a credential-bound signing key, verified identity and trusted clock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass, field
from typing import Protocol

from .health import ContractViolation
from .observation import Observation
from .version import VERSION_FIELD, VERSION_HEADER, check_version

# Headers carrying the submitter's identity and body signature. Distinct from the
# `x-adp-*` identity headers used by the MCP tool surface: this is a
# machine-to-machine submission path with a signed body, not a user-facing tool
# call, and reusing the tool-surface headers would invite a receiver to accept a
# tool-surface identity as a submission credential.
AUTH_HEADER = "authorization"
SIGNATURE_HEADER = "x-superplane-signature"
SUBMITTER_HEADER = "x-superplane-submitter"

# Prefix the signature header must carry, so an unprefixed opaque value cannot be
# mistaken for a signature of a different algorithm later.
SIGNATURE_PREFIX = "sha256="


@dataclass(frozen=True)
class Submitter:
    """An authenticated submitter and the workspaces it may speak for.

    `workspaces` is the grant, and it is a set rather than a single value because
    a monitor legitimately watches several workspaces. It is never inferred from
    the payload — see `scoping.py`.
    """

    submitter_id: str
    workspaces: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.submitter_id or not self.submitter_id.strip():
            raise ContractViolation("submitter_id must be a non-empty string")


@dataclass(frozen=True)
class AuthResult:
    """Outcome of authenticating and verifying a submission.

    `reason` is stable and caller-safe: it names which requirement failed and
    never echoes a header value, a token or a signature back. A refusal that
    quoted the credential it rejected would put that credential in the
    submitter's logs.
    """

    authenticated: bool
    submitter: Submitter | None = None
    reason: str = ""
    observation: Observation | None = None


class SubmitterResolver(Protocol):
    """Resolves a credential to an authenticated submitter, or None.

    Returning `None` for an invalid credential rather than raising keeps "not
    authenticated" a normal outcome of the contract rather than an exception path
    a receiver might catch too broadly. The real implementation is U15's.
    """

    def resolve(self, credential: str) -> Submitter | None: ...


def canonical_body(observation: Observation) -> bytes:
    """Serialize a Python sender's body; transmit these same bytes when signing.

    Other languages may serialize differently. The receiver signs raw bytes, so
    it must never normalize JSON, timestamps, Unicode or floating-point numbers.
    """
    return json.dumps(
        observation.to_wire(), sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def compute_signature(observation: Observation | bytes, key: bytes) -> str:
    """Compute the body signature a submitter sends in `SIGNATURE_HEADER`."""
    body = (
        canonical_body(observation)
        if isinstance(observation, Observation)
        else observation
    )
    digest = hmac.new(key, body, hashlib.sha256).hexdigest()
    return f"{SIGNATURE_PREFIX}{digest}"


def verify_signature(
    observation: Observation | bytes, key: bytes, provided: str | None
) -> bool:
    """Constant-time check that `provided` signs this observation's body.

    `hmac.compare_digest` rather than `==` so a rejected signature does not leak
    how many leading bytes were correct through timing. Cheap to do correctly and
    awkward to retrofit.
    """
    if not isinstance(provided, str):
        return False
    candidate = (provided or "").strip()
    if not re.fullmatch(r"sha256=[0-9a-f]{64}", candidate):
        return False
    return hmac.compare_digest(candidate, compute_signature(observation, key))


def _invalid_json_constant(value: str) -> None:
    raise ValueError("non-finite JSON number")


def verify_submission(
    observation: bytes,
    headers: dict[str, str],
    resolver: SubmitterResolver,
    signing_key: bytes,
    *,
    now: datetime | None = None,
    max_age: timedelta = timedelta(minutes=5),
    future_skew: timedelta = timedelta(seconds=30),
) -> AuthResult:
    """Authenticate bytes, then validate their schema and probe honesty.

    On success, use the returned observation for scoping and persistence. U15
    must not substitute unvalidated JSON; a valid signature is not schema approval.

    Fail-closed at every branch: missing version, missing credential, unresolvable
    credential and missing or wrong signature all refuse. There is no branch that
    authenticates a submission because a header was absent.

    Version is checked *first*, before the credential is even looked at, so a
    submitter sending an unsupported version gets that answer rather than an
    authentication error that would send it looking in the wrong place.
    """
    lowered = {k.lower(): v for k, v in headers.items()}
    if not isinstance(observation, bytes):
        return AuthResult(
            authenticated=False, reason="raw UTF-8 request bytes required"
        )
    body = observation
    try:
        payload = json.loads(
            body.decode("utf-8"), parse_constant=_invalid_json_constant
        )
        if not isinstance(payload, dict):
            raise ValueError("body must be an object")
    except (ValueError, UnicodeDecodeError, TypeError):
        return AuthResult(authenticated=False, reason="invalid JSON body")
    version = check_version(lowered.get(VERSION_HEADER), payload.get(VERSION_FIELD))
    if not version.accepted:
        return AuthResult(authenticated=False, reason=version.reason)

    credential = (lowered.get(AUTH_HEADER) or "").strip()
    if not credential:
        # This is the current endpoint's whole problem, refused in one line: a
        # body naming a cluster UUID, with no identity, gets nowhere.
        return AuthResult(
            authenticated=False, reason="unauthenticated: no credential presented"
        )

    submitter = resolver.resolve(credential)
    if submitter is None:
        # Same reason string as a malformed credential. Distinguishing "unknown
        # submitter" from "bad token" would let the endpoint be used to test
        # whether a given submitter identity exists.
        return AuthResult(
            authenticated=False, reason="unauthenticated: credential not accepted"
        )

    if not verify_signature(body, signing_key, lowered.get(SIGNATURE_HEADER)):
        # Reached only with a valid credential, so this specifically catches a
        # body that does not match what was signed — a swapped or replayed
        # payload from an otherwise legitimate submitter.
        return AuthResult(
            authenticated=False,
            reason="unsigned or invalid body signature",
        )

    clock = now if now is not None else datetime.now(UTC)
    if (
        clock.utcoffset() is None
        or max_age <= timedelta(0)
        or future_skew < timedelta(0)
    ):
        raise ValueError(
            "a trusted aware clock and bounded positive freshness window are required"
        )
    try:
        reported_at = datetime.fromisoformat(payload["reported_at"])
        if reported_at.utcoffset() is None:
            raise ValueError("timestamp must be aware")
    except (KeyError, TypeError, ValueError):
        return AuthResult(authenticated=False, reason="invalid reported_at")
    if not clock - max_age <= reported_at <= clock + future_skew:
        return AuthResult(
            authenticated=False, reason="observation outside freshness window"
        )
    try:
        validated = Observation.from_wire(payload)
    except ContractViolation:
        return AuthResult(authenticated=False, reason="invalid observation body")
    return AuthResult(authenticated=True, submitter=submitter, observation=validated)
