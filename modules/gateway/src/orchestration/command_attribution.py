"""Verify engine-command attribution before the tick trusts a row (issue #4539).

An `@agent-engine` command reaches this component as a DynamoDB row, never as a
call: the webhook Lambda marks the row `engine_command_status=pending` and the
orchestration tick finds it on the sparse `engine-command-index` on its next wake.
The row is therefore the carrier of *who asked for what, on which plan*.

Before #4539 those authority fields — tenant, installation, repository, issue,
commenter id, author kind, command body — were ordinary mutable attributes. GitHub's
HMAC was verified on the DELIVERY and nothing carried that verification forward, so
anything able to write the row could choose the acting identity and the routing
target of a human approval, and this side had no way to tell a delivered tuple from
an authored one.

This module is the consumer half of the fix. `verify_row` recomputes the signature
over the canonical bytes stored on the row and refuses anything that does not
reproduce, so the tick acts only on tuples that came from a verified delivery
unaltered.

--------------------------------------------------------------------------------
What a valid signature does and does not establish
--------------------------------------------------------------------------------

It establishes exactly one thing: *this tuple came from a GitHub delivery that
passed webhook signature verification, and has not been altered since.*

It establishes **nothing about authorization**. A valid signature is NOT a
substitute for current tenant membership, for `PLAN_APPROVE`, or for the human-only
gate checks — those run afterwards, unchanged, and a valid-but-unauthorized command
keeps the existing uniform, tagless refusal. Nor does it confer freshness: replay
protection comes from the row's own idempotent `pending -> consumed` consume.

Reading a valid signature as "this caller is allowed to do this" would convert an
authentication mechanism into an authorization bypass, which is a strictly worse
defect than the one this module fixes.

--------------------------------------------------------------------------------
Why verification must come FIRST
--------------------------------------------------------------------------------

`verify_row` is called at the very top of `_handle_row`, before the installation is
resolved, before an identity lookup, and before any row field is used for a side
effect. That ordering is the security property, not a performance choice:

* an identity lookup keyed on an attacker-chosen `sender_github_id` in an
  attacker-chosen tenant is itself a probe, and a distinguishable outcome is an
  oracle for which accounts and orgs exist;
* an acknowledgement addressed with an attacker-chosen repository and installation
  would make this component post to a repository of the attacker's choosing using a
  credential it holds — a confused-deputy write, independent of whether the command
  was ultimately applied.

So an unverified row produces no lookup, no decision, no dispatch and no
acknowledgement. It is quarantined and counted.

--------------------------------------------------------------------------------
A separate implementation of the same canonical form
--------------------------------------------------------------------------------

The signer is `modules/agent-factory/webhook-ingress/lambda/common/command_signing.py`,
packaged into a Lambda zip. This runs in the gateway container image. Separate
deploy units cannot import each other — the same constraint that already forces
`ENGINE_COMMAND_STATUS_*` to be re-declared on both sides.

Canonicalization is therefore written twice, and
`contracts/engine-command-envelope/v1/engine-command-envelope.golden.json` is what
keeps the pair honest: both test suites assert byte-for-byte canonical output and
signature equality against the same vectors. Drift breaks CI instead of silently
refusing every real human command in production.

--------------------------------------------------------------------------------
Failing closed
--------------------------------------------------------------------------------

Every refusal path returns a bounded, sanitized reason and never the signature, the
key, or any part of the command body. A missing key, an unknown or stale key id, an
unknown protocol version, a malformed payload, a mismatch between the signed tuple
and the row's mutable copies — all refuse. An un-rotated placeholder secret is
treated as *no key at all*: a confident verdict computed under a value that ships in
the repo would report success, which is worse than no verdict (#4128).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

#: Protocol version this verifier understands. An unknown version REFUSES rather
#: than guessing at a layout — a verifier that guessed would be checking a
#: signature over bytes the signer never produced. Accepting a new version is a
#: deliberate code change here, paired with new vectors in the contract.
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"1"})

#: The only provider this protocol version describes. Signed, so a row shaped like
#: another channel's delivery cannot be accepted as a verified GitHub comment.
PROVIDER_GITHUB = "github"

#: Environment variable naming the Secrets Manager secret holding the keyring.
#: Deliberately the same name the signer reads: one signing key behind two names
#: drifts silently, and the failure mode is every command refused.
SIGNING_KEY_SECRET_ARN_ENV = "ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN"

# --- The canonical form, re-declared -----------------------------------------
#
# MUST match `command_signing.ENVELOPE_FIELDS` in the webhook-ingress Lambda
# exactly — same names, same order, same types. Pinned by the shared golden
# fixture, which both suites read; there is no import path between the two deploy
# units that could enforce it instead.
ENVELOPE_FIELDS: tuple[tuple[str, type], ...] = (
    ("protocol_version", str),
    ("key_id", str),
    ("provider", str),
    ("delivery_id", str),
    ("event_type", str),
    ("event_id", str),
    ("arrived_at", str),
    ("tenant_id", str),
    ("installation_id", str),
    ("repo_id", int),
    ("repo", str),
    ("issue_number", int),
    ("sender_github_id", str),
    ("sender_type", str),
    ("command_body", str),
    ("signed_at", str),
)

ENVELOPE_FIELD_NAMES: tuple[str, ...] = tuple(name for name, _ in ENVELOPE_FIELDS)

#: Upper bound on the stored payload, before any parsing. A hostile row could carry
#: a very large string; refusing on size first means a malformed row cannot cost
#: parse time proportional to what an attacker wrote.
SIGNED_PAYLOAD_MAX_CHARS = 16000

# --- Row attribute names ------------------------------------------------------
# Re-declared from `webhook_events.py` for the same deploy-unit reason as above.
SIGNATURE_ATTR = "engine_command_signature"
KEY_ID_ATTR = "engine_command_signing_key_id"
SIGNED_PAYLOAD_ATTR = "engine_command_signed_payload"
PROTOCOL_VERSION_ATTR = "engine_command_protocol_version"

#: Known un-rotated placeholder values, treated as "no key available" (#4128).
_PLACEHOLDER_KEYS = frozenset(
    {
        "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND",
        "PLACEHOLDER_SET_BY_OPERATOR",
        "PLACEHOLDER",
        "CHANGEME",
    }
)

# --- Refusal reasons ----------------------------------------------------------
#
# A bounded enum, because these become a metric dimension and are written to logs.
# Never derived from row content: an unbounded reason would both blow up metric
# cardinality and put attacker-chosen text into operator surfaces.
REASON_NO_KEY = "no_verification_key"
REASON_MISSING_SIGNATURE = "missing_signature"
REASON_MISSING_KEY_ID = "missing_key_id"
REASON_MISSING_PAYLOAD = "missing_payload"
REASON_UNKNOWN_KEY_ID = "unknown_key_id"
REASON_STALE_KEY_ID = "stale_key_id"
REASON_UNKNOWN_PROTOCOL = "unknown_protocol_version"
REASON_MALFORMED_PAYLOAD = "malformed_payload"
REASON_PAYLOAD_TOO_LARGE = "payload_too_large"
REASON_BAD_SIGNATURE = "invalid_signature"
REASON_ROW_MISMATCH = "row_disagrees_with_signed_tuple"
REASON_WRONG_PROVIDER = "wrong_provider"


@dataclass(frozen=True)
class VerifiedCommand:
    """The signed authority tuple, once the signature has been confirmed.

    The tick reads its authority and routing from THIS, not from the row. That is
    the point: the row's mutable attributes may agree with it (they are checked, and
    a disagreement refuses), but only these values are covered by the signature.

    Frozen, so a later stage cannot quietly rewrite an authority field after it was
    verified.
    """

    tenant_id: str
    installation_id: str
    repo: str
    repo_id: int
    issue_number: int
    sender_github_id: str
    sender_type: str
    command_body: str
    delivery_id: str
    event_id: str
    arrived_at: str
    key_id: str

    @property
    def sender_is_bot(self) -> bool:
        """Author kind, from the SIGNED tuple rather than the row's mutable flag.

        GitHub's own author kind for an App or bot account. The tick uses this to
        stay quiet at a bot's own comment — a noise filter, not an authorization
        boundary (bot identities resolve to MEMBER, which lacks `PLAN_APPROVE`),
        but a noise filter that can now be trusted not to have been flipped.
        """
        return self.sender_type == "Bot"


class AttributionError(Exception):
    """A row's attribution could not be verified.

    Carries a bounded, sanitized `reason` suitable for a metric dimension. Never
    carries the signature, the key, or command content.
    """

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason}{f': {detail}' if detail else ''}")
        self.reason = reason


def _placeholder(value: str) -> bool:
    """True when a secret value is empty or a known un-rotated placeholder."""
    return value.strip() == "" or value.strip() in _PLACEHOLDER_KEYS


def canonical_bytes(envelope: dict[str, Any]) -> bytes:
    """The exact bytes the signer signed for *envelope*.

    A JSON array of `[name, value]` pairs in :data:`ENVELOPE_FIELDS` order, minimal
    separators, `ensure_ascii=False`. An array rather than an object because a JSON
    object has no inherent order: two implementations agreeing on "sorted keys" is a
    convention that survives until someone adds a field, whereas a positional array
    makes the order part of the data.

    Raises :class:`AttributionError` unless the envelope matches the spec exactly —
    so canonical bytes never exist for a tuple that failed validation.
    """
    validate_envelope(envelope)
    pairs = [[name, envelope[name]] for name, _ in ENVELOPE_FIELDS]
    return json.dumps(pairs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def validate_envelope(envelope: dict[str, Any]) -> None:
    """Raise :class:`AttributionError` unless *envelope* matches the spec exactly.

    Identical strictness to the signer, and for the same reason: a verifier that
    tolerated an unknown field, a missing one, or a coerced type would accept a
    tuple the signer could never have produced. In particular no value is coerced —
    `"4539"` in the `issue_number` slot is refused, not read as `4539`.
    """
    if not isinstance(envelope, dict):
        raise AttributionError(REASON_MALFORMED_PAYLOAD, "not an object")

    unknown = sorted(set(envelope) - set(ENVELOPE_FIELD_NAMES))
    if unknown:
        raise AttributionError(REASON_MALFORMED_PAYLOAD, f"unknown field(s): {', '.join(unknown)}")

    for name, expected_type in ENVELOPE_FIELDS:
        if name not in envelope:
            raise AttributionError(REASON_MALFORMED_PAYLOAD, f"missing field: {name}")
        value = envelope[name]
        # `bool` is a subclass of `int`; a boolean in an int slot is a type error.
        if isinstance(value, bool) or not isinstance(value, expected_type):
            raise AttributionError(
                REASON_MALFORMED_PAYLOAD,
                f"field {name} must be {expected_type.__name__}",
            )


def parse_keyring(secret_value: str) -> tuple[str, dict[str, bytes], str | None]:
    """Parse the keyring secret into `(active_key_id, keys, previous_valid_until)`.

    Shape::

        {"active_key_id": "...", "keys": {"<id>": "<secret>"},
         "previous_valid_until": "<ISO-8601 Z>"}

    Raises :class:`AttributionError` when the value is absent, a placeholder, not
    JSON, or names an `active_key_id` with no material. Placeholder-valued
    individual keys are dropped, so a half-rotated secret degrades to "that key id
    is unknown" — which refuses — rather than to a verdict computed under a value
    published in the repo.
    """
    if _placeholder(secret_value):
        raise AttributionError(REASON_NO_KEY, "secret holds an un-rotated placeholder")

    try:
        parsed = json.loads(secret_value)
    except (TypeError, ValueError) as exc:
        raise AttributionError(REASON_NO_KEY, f"secret is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise AttributionError(REASON_NO_KEY, "secret must be a JSON object")

    active_key_id = parsed.get("active_key_id")
    if not isinstance(active_key_id, str) or not active_key_id.strip():
        raise AttributionError(REASON_NO_KEY, "no usable active_key_id")
    active_key_id = active_key_id.strip()

    raw_keys = parsed.get("keys")
    if not isinstance(raw_keys, dict):
        raise AttributionError(REASON_NO_KEY, "no keys object")

    keys: dict[str, bytes] = {}
    for key_id, value in raw_keys.items():
        if not isinstance(key_id, str) or not isinstance(value, str):
            continue
        if _placeholder(value):
            logger.warning(
                "engine command attribution: key id %r holds a placeholder; ignoring",
                key_id,
            )
            continue
        keys[key_id] = value.encode("utf-8")

    if active_key_id not in keys:
        raise AttributionError(REASON_NO_KEY, f"active_key_id {active_key_id!r} has no key material")

    previous_valid_until = parsed.get("previous_valid_until")
    if not isinstance(previous_valid_until, str) or not previous_valid_until.strip():
        previous_valid_until = None

    return active_key_id, keys, previous_valid_until


# Cached per process. A rotation takes effect on the next restart; the overlap
# window below is what covers rows signed under the previous key meanwhile. Failure
# is cached too, so a misconfigured environment does not call Secrets Manager once
# per command and turn a refusal into throttling.
_keyring: tuple[str, dict[str, bytes], str | None] | None = None
_keyring_loaded = False


def _load_keyring() -> tuple[str, dict[str, bytes], str | None]:
    """The verification keyring, or raise :class:`AttributionError`."""
    global _keyring, _keyring_loaded

    if _keyring_loaded:
        if _keyring is None:
            raise AttributionError(REASON_NO_KEY, "no verification key available")
        return _keyring

    _keyring_loaded = True

    secret_arn = (os.environ.get(SIGNING_KEY_SECRET_ARN_ENV) or "").strip()
    if not secret_arn:
        raise AttributionError(REASON_NO_KEY, f"{SIGNING_KEY_SECRET_ARN_ENV} is not set")

    import boto3

    region = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1"
    client = boto3.client("secretsmanager", region_name=region)
    secret_value = client.get_secret_value(SecretId=secret_arn).get("SecretString") or ""

    _keyring = parse_keyring(secret_value)
    logger.info("engine command attribution: keyring loaded, active key id %r", _keyring[0])
    return _keyring


def _select_key(key_id: str, *, now: datetime | None = None) -> bytes:
    """The key material for *key_id*, honouring the bounded rotation overlap.

    The active key is always accepted. A non-active key is accepted only while
    `previous_valid_until` is still in the future — that window is what lets rows
    signed just before a rotation still be consumed, and its expiry is what stops a
    retired key being valid forever. An unknown id refuses outright.
    """
    active_key_id, keys, previous_valid_until = _load_keyring()

    if key_id == active_key_id:
        return keys[key_id]

    if key_id not in keys:
        raise AttributionError(REASON_UNKNOWN_KEY_ID)

    if not previous_valid_until:
        # A non-active key with no declared overlap window is retired. Accepting it
        # would make every past key valid indefinitely, which defeats rotation.
        raise AttributionError(REASON_STALE_KEY_ID, "no overlap window declared")

    try:
        deadline = datetime.fromisoformat(previous_valid_until.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
    except ValueError as exc:
        # An unparseable window is treated as expired, not as unbounded: the
        # fail-closed reading of "we cannot tell whether this key is still valid".
        raise AttributionError(REASON_STALE_KEY_ID, "overlap window is unparseable") from exc

    if (now or datetime.now(UTC)) >= deadline:
        raise AttributionError(REASON_STALE_KEY_ID, "overlap window has expired")

    return keys[key_id]


def verify_row(row: dict[str, Any], *, now: datetime | None = None) -> VerifiedCommand:
    """Verify one marked row's attribution, or raise :class:`AttributionError`.

    MUST be called before any identity or permission lookup, and before any row
    field is used for a side effect — see the module docstring on ordering. This
    function performs no I/O beyond loading the (cached) keyring, so it cannot itself
    become a probe.

    Returns the signed tuple. The caller must read authority and routing from the
    return value rather than from the row.
    """
    signature = str(row.get(SIGNATURE_ATTR) or "").strip()
    key_id = str(row.get(KEY_ID_ATTR) or "").strip()
    payload = row.get(SIGNED_PAYLOAD_ATTR) or ""

    # Presence first, and each absence distinguished, because they have different
    # operational causes: an unsigned row means the publisher could not sign (no key
    # seeded, or the row predates signing), whereas a partial one means a code
    # defect or tampering. Both refuse.
    if not signature:
        raise AttributionError(REASON_MISSING_SIGNATURE)
    if not key_id:
        # Deliberately NOT "fall back to the active key": that would let whoever
        # wrote the row choose which key their forgery is checked against.
        raise AttributionError(REASON_MISSING_KEY_ID)
    if not isinstance(payload, str) or not payload:
        raise AttributionError(REASON_MISSING_PAYLOAD)
    if len(payload) > SIGNED_PAYLOAD_MAX_CHARS:
        # Bounded before parsing, so a hostile row cannot cost parse time
        # proportional to what an attacker chose to write.
        raise AttributionError(REASON_PAYLOAD_TOO_LARGE)

    try:
        pairs = json.loads(payload)
    except (TypeError, ValueError) as exc:
        raise AttributionError(REASON_MALFORMED_PAYLOAD, "not JSON") from exc

    # The canonical form is a list of [name, value] pairs in a FIXED order.
    #
    # The order is checked explicitly rather than normalised away. `canonical_bytes`
    # below re-serialises in `ENVELOPE_FIELDS` order, so a reordered payload would
    # otherwise rebuild to identical bytes and verify — the values would all still be
    # authenticated (so this is not a forgery path), but "field order is part of the
    # data" would quietly stop being true, and the contract's `field_order` would
    # become documentation of something nothing enforces. Refusing keeps the stored
    # payload and the canonical form the same object rather than two shapes that
    # happen to agree.
    if not isinstance(pairs, list):
        raise AttributionError(REASON_MALFORMED_PAYLOAD, "not a pair array")
    envelope: dict[str, Any] = {}
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2 or not isinstance(pair[0], str):
            raise AttributionError(REASON_MALFORMED_PAYLOAD, "malformed pair")
        if pair[0] in envelope:
            # A duplicate name would make "which value was verified" ambiguous, and
            # a verifier resolving it by last-wins while a reader resolves it by
            # first-wins is a forgery path.
            raise AttributionError(REASON_MALFORMED_PAYLOAD, "duplicate field")
        envelope[pair[0]] = pair[1]

    if [name for name, _ in pairs] != list(ENVELOPE_FIELD_NAMES):
        raise AttributionError(REASON_MALFORMED_PAYLOAD, "field order")

    validate_envelope(envelope)

    if envelope["protocol_version"] not in SUPPORTED_PROTOCOL_VERSIONS:
        raise AttributionError(REASON_UNKNOWN_PROTOCOL)
    if envelope["provider"] != PROVIDER_GITHUB:
        raise AttributionError(REASON_WRONG_PROVIDER)
    if envelope["key_id"] != key_id:
        # The row's key id must be the one inside the signed tuple, or an attacker
        # could point verification at a different key than the one committed to.
        raise AttributionError(REASON_ROW_MISMATCH, "key_id")

    key = _select_key(key_id, now=now)

    expected = base64.urlsafe_b64encode(hmac.new(key, canonical_bytes(envelope), hashlib.sha256).digest()).rstrip(b"=").decode("ascii")

    # Constant-time, and BEFORE any use of the tuple. `compare_digest` on the
    # base64 text rather than raw bytes so a malformed stored signature cannot
    # raise during decoding — a decode error would be a distinguishable outcome.
    if not hmac.compare_digest(expected, signature):
        raise AttributionError(REASON_BAD_SIGNATURE)

    verified = VerifiedCommand(
        tenant_id=envelope["tenant_id"],
        installation_id=envelope["installation_id"],
        repo=envelope["repo"],
        repo_id=envelope["repo_id"],
        issue_number=envelope["issue_number"],
        sender_github_id=envelope["sender_github_id"],
        sender_type=envelope["sender_type"],
        command_body=envelope["command_body"],
        delivery_id=envelope["delivery_id"],
        event_id=envelope["event_id"],
        arrived_at=envelope["arrived_at"],
        key_id=key_id,
    )

    _assert_row_agrees(row, verified)
    return verified


def _assert_row_agrees(row: dict[str, Any], verified: VerifiedCommand) -> None:
    """Refuse when the row's mutable copies disagree with the signed tuple.

    The tick reads authority from `verified`, so a disagreement is not itself an
    authority bypass — but it is unambiguous evidence of tampering or of two
    publishers disagreeing, and continuing would mean acting on a row whose visible
    content differs from what was verified. That is precisely the state an operator
    reading the table would be misled by.

    Only the fields that exist on both sides are compared. `event_id` and
    `arrived_at` are the row's keys, so a mismatch means the signature was lifted
    from another row — the forgery this module exists to stop, one indirection out.
    """
    checks: tuple[tuple[str, Any, Any], ...] = (
        ("event_id", str(row.get("event_id") or ""), verified.event_id),
        ("arrived_at", str(row.get("arrived_at") or ""), verified.arrived_at),
        ("tenant_id", str(row.get("tenant_id") or ""), verified.tenant_id),
        ("repo", str(row.get("repo") or ""), verified.repo),
        (
            "engine_command_body",
            str(row.get("engine_command_body") or ""),
            verified.command_body,
        ),
        (
            "engine_command_sender_github_id",
            str(row.get("engine_command_sender_github_id") or ""),
            verified.sender_github_id,
        ),
    )
    for name, row_value, signed_value in checks:
        if row_value != signed_value:
            # The names of the disagreeing FIELD only — never the values, which are
            # attacker-chosen and would land in logs.
            raise AttributionError(REASON_ROW_MISMATCH, name)

    raw_issue = row.get("issue_number")
    try:
        row_issue = int(raw_issue)
    except (TypeError, ValueError):
        raise AttributionError(REASON_ROW_MISMATCH, "issue_number") from None
    if row_issue != verified.issue_number:
        raise AttributionError(REASON_ROW_MISMATCH, "issue_number")

    # The row's installation, when present, must be the signed one. Unlike the
    # fields above this is checked even when the row's copy is EMPTY: an absent
    # installation used to skip the comparison entirely, which meant omitting the
    # attribute was enough to bypass it.
    row_installation = str(row.get("installation_id") or "").strip()
    if row_installation != verified.installation_id.strip():
        raise AttributionError(REASON_ROW_MISMATCH, "installation_id")


def reset_key_cache() -> None:
    """Reset the cached keyring (tests only)."""
    global _keyring, _keyring_loaded
    _keyring = None
    _keyring_loaded = False
