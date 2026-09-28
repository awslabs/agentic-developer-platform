"""Sign engine-command authority onto the event row (Issue #4539).

An `@agent-engine` command travels from GitHub to the orchestration engine as a
DynamoDB row, not as a call: this Lambda marks the event row
`engine_command_status=pending` and the gateway-side tick picks it up on its next
wake (see `webhook_events.py` and the gateway's `engine_commands.py`).

That makes the ROW the carrier of *who asked for what, on which plan*. Before this
module the row's authority fields — tenant, installation, repository, issue,
commenter id, author kind, command body — were ordinary mutable attributes. GitHub's
HMAC was verified on the DELIVERY, and then nothing carried that verification
forward: anything able to write the row could choose the acting identity and the
routing target of a human approval, and the tick had no way to tell a delivered
tuple from an authored one.

This module closes that gap. Immediately after the GitHub signature check succeeds,
the handler builds a versioned canonical envelope over exactly the fields the tick
uses as authority or routing, signs it with HMAC-SHA256 under a dedicated key, and
stores the signature *and the exact signed fields* alongside the pending marker. The
tick verifies before it resolves an identity or uses any row field for a side effect.

--------------------------------------------------------------------------------
Why the verifier is a separate implementation
--------------------------------------------------------------------------------

The consumer lives in the gateway container image (`command_attribution.py`); this
lives in a Lambda zip. They are separate deploy units and cannot import each other —
the same constraint that already forces `ENGINE_COMMAND_STATUS_*` to be re-declared
on both sides rather than shared. Canonicalization is therefore written twice and
pinned by `contracts/engine-command-envelope/v1/engine-command-envelope.golden.json`,
a golden-vector file BOTH test suites read.
A canonicalization change on one side without the other breaks those tests rather
than silently rejecting every real command in production.

--------------------------------------------------------------------------------
Canonical form
--------------------------------------------------------------------------------

The signed bytes are a UTF-8 JSON **array of `[name, value]` pairs in a fixed order**,
serialised with no insignificant whitespace. An array rather than an object because a
JSON object has no inherent order: two implementations agreeing on "sorted keys" is a
convention that holds until someone adds a field, whereas a positional array makes the
order part of the data. Field names, order and types come from `ENVELOPE_FIELDS` and
nothing else:

* every field is required — a missing one refuses rather than signing a shorter tuple;
* an unknown or duplicated field refuses, so a caller cannot smuggle an unsigned
  attribute past the spec or shadow a signed one;
* types are explicit (`str` / `int`), so `"4539"` and `4539` are different signed
  tuples and neither side can normalise one into the other;
* `ensure_ascii=False` keeps Unicode as UTF-8 bytes rather than `\\uXXXX` escapes, so a
  command body containing non-ASCII text or newlines signs identically on both sides.

--------------------------------------------------------------------------------
The key is dedicated, and deliberately not the marker-signing key
--------------------------------------------------------------------------------

`marker-signing-key` is readable by the agent worker role — `scaledjob-iam.tf` grants
it a purpose-scoped `kms:Decrypt` for exactly that secret so `marker_signing.py` can
sign correlation markers. Reusing it here would hand the ability to mint human
approval attribution to the identity the authority boundary exists to constrain. This
module reads its own secret (`ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN`), and no
worker/source/supervisor identity is granted a read on it.

The secret holds a small JSON keyring so rotation has explicit key ids and a bounded
overlap for events already signed but not yet consumed:

    {
      "active_key_id": "2026-09",
      "keys": {"2026-09": "<random>", "2026-06": "<random>"},
      "previous_valid_until": "2026-09-22T00:00:00Z"
    }

The signer only ever uses `active_key_id`. The verifier accepts the active key
always, and a non-active key only while `previous_valid_until` is in the future —
unknown or stale key ids fail closed on both sides. An un-rotated placeholder value is
treated as *no key at all*, for the reason `marker_verify.py` documents under #4128: a
confident signature computed under a secret that ships in the repo is strictly worse
than no signature, because it reports success.

--------------------------------------------------------------------------------
Failure is visible, never silent, and never blocking
--------------------------------------------------------------------------------

Signing failure does not block the webhook response and does not drop the audit row.
The handler writes the row with the marker and WITHOUT a signature; the tick refuses
it, quarantines it and emits a sanitised refusal metric. That is what makes an
environment whose key was never seeded diagnosable — every command refuses and the
metric says why — instead of appearing to work.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

#: Envelope protocol version. Bumped only for a breaking canonicalization or
#: field-set change, and carried on the row so a verifier that does not know a
#: version refuses rather than guessing at the layout.
ENVELOPE_VERSION = "1"

#: Environment variable naming the Secrets Manager secret holding the keyring.
#: Deliberately the same name the tick reads, because one signing key behind two
#: names drifts silently.
SIGNING_KEY_SECRET_ARN_ENV = "ENGINE_COMMAND_SIGNING_KEY_SECRET_ARN"

#: The signed field set, in canonical order, with the type each value must have.
#:
#: THIS IS THE AUTHORITY AND ROUTING TUPLE. Every field the tick reads to decide
#: *who* acted, *which tenant* they acted in, or *where* a reply goes must appear
#: here — a field the tick trusts but the signature does not cover is exactly the
#: forgeable-attribution bug this module exists to remove. Adding a field is a
#: protocol change: bump `ENVELOPE_VERSION` and add a vector.
#:
#: `repo_id` accompanies `repo` because a repository can be renamed; the numeric id
#: cannot. `sender_github_id` is numeric for the same reason logins are excluded
#: everywhere else on this path — a renamed account must not inherit another user's
#: approvals. `sender_type` is GitHub's own author kind, so the tick can decide
#: "a bot did not ask for anything" from a signed value rather than a mutable flag.
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

#: The only provider this protocol version describes. Present in the signed tuple so
#: a verifier cannot be handed a GitLab- or EventBridge-shaped row and accept it as a
#: GitHub comment; present as a constant so the handler cannot pass anything else.
PROVIDER_GITHUB = "github"

#: Upper bound on the signed command body, matching the row's own bound
#: (`webhook_events.ENGINE_COMMAND_BODY_MAX_CHARS`). Enforced HERE, before signing,
#: rather than by truncating: signing a body the caller did not send would make the
#: signature cover something other than what arrived, which is the one property this
#: whole mechanism rests on. Oversize refuses, the row carries no signature, and the
#: tick quarantines it visibly.
COMMAND_BODY_MAX_CHARS = 4000

#: Upper bound on any single non-body string field. Every one of them is an id, a
#: timestamp or an `owner/name` pair; four kilobytes is far above all of them and far
#: enough below DynamoDB's item limit that a field can never be what makes the row
#: write fail.
FIELD_MAX_CHARS = 4000

#: Known un-rotated placeholder values, treated as "no key available" (#4128).
_PLACEHOLDER_KEYS = frozenset(
    {
        "PLACEHOLDER_GENERATE_WITH_OPENSSL_RAND",
        "PLACEHOLDER_SET_BY_OPERATOR",
        "PLACEHOLDER",
        "CHANGEME",
    }
)


class CommandSigningError(Exception):
    """The envelope could not be signed. Never fatal to the webhook response.

    Raised for a malformed field set, oversize content, a missing or unusable
    keyring. The handler logs it and writes the row unsigned, so the command is
    refused by the tick and counted rather than applied on unverified fields.
    """


def _placeholder(value: str) -> bool:
    """True when a secret value is empty or a known un-rotated placeholder."""
    return value.strip() == "" or value.strip() in _PLACEHOLDER_KEYS


def validate_envelope(envelope: dict[str, Any]) -> None:
    """Raise :class:`CommandSigningError` unless *envelope* matches the spec exactly.

    Checked BEFORE any signing so that "the signature covers the whole tuple, and
    only the tuple" is a structural property rather than a convention:

    * unknown field — a caller cannot smuggle an attribute the verifier will not
      check;
    * missing field — a shorter tuple must not be signable, or an attacker who can
      influence which fields are present chooses what the signature commits to;
    * wrong type — `"4539"` and `4539` stay distinct signed tuples;
    * oversize content — see :data:`COMMAND_BODY_MAX_CHARS`.

    Duplicate fields cannot exist in a `dict`; the handler builds the envelope through
    :func:`build_envelope`, whose keyword arguments make a duplicate a syntax error.
    """
    unknown = sorted(set(envelope) - set(ENVELOPE_FIELD_NAMES))
    if unknown:
        raise CommandSigningError(f"unknown envelope field(s): {', '.join(unknown)}")

    for name, expected_type in ENVELOPE_FIELDS:
        if name not in envelope:
            raise CommandSigningError(f"missing envelope field: {name}")
        value = envelope[name]
        # `bool` is a subclass of `int`; a boolean in an int slot is a type error,
        # not a 0/1.
        if isinstance(value, bool) or not isinstance(value, expected_type):
            raise CommandSigningError(
                f"envelope field {name} must be {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )

    if len(envelope["command_body"]) > COMMAND_BODY_MAX_CHARS:
        raise CommandSigningError(
            f"command body is {len(envelope['command_body'])} chars, "
            f"over the {COMMAND_BODY_MAX_CHARS}-char bound"
        )

    for name, expected_type in ENVELOPE_FIELDS:
        if expected_type is str and name != "command_body":
            if len(envelope[name]) > FIELD_MAX_CHARS:
                raise CommandSigningError(
                    f"envelope field {name} is over the {FIELD_MAX_CHARS}-char bound"
                )

    if envelope["protocol_version"] != ENVELOPE_VERSION:
        raise CommandSigningError(
            f"protocol_version must be {ENVELOPE_VERSION!r}, "
            f"got {envelope['protocol_version']!r}"
        )
    if envelope["provider"] != PROVIDER_GITHUB:
        raise CommandSigningError(
            f"provider must be {PROVIDER_GITHUB!r}, got {envelope['provider']!r}"
        )


def canonical_bytes(envelope: dict[str, Any]) -> bytes:
    """The exact bytes signed for *envelope*.

    A JSON array of `[name, value]` pairs in :data:`ENVELOPE_FIELDS` order, minimal
    separators, `ensure_ascii=False` so non-ASCII text is UTF-8 rather than escapes.
    Validated first, so canonical bytes never exist for a tuple that failed the spec.
    """
    validate_envelope(envelope)
    pairs = [[name, envelope[name]] for name, _ in ENVELOPE_FIELDS]
    return json.dumps(pairs, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def compute_signature(key: bytes, envelope: dict[str, Any]) -> str:
    """HMAC-SHA256 over :func:`canonical_bytes`, base64url without padding.

    Unpadded base64url because the value is stored as a DynamoDB string and compared
    with `hmac.compare_digest`; the same encoding `marker_verify.py` uses, so there is
    one signature spelling on this platform rather than two.
    """
    digest = hmac.new(key, canonical_bytes(envelope), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def build_envelope(
    *,
    key_id: str,
    delivery_id: str,
    event_type: str,
    event_id: str,
    arrived_at: str,
    tenant_id: str,
    installation_id: str,
    repo_id: int,
    repo: str,
    issue_number: int,
    sender_github_id: str,
    sender_type: str,
    command_body: str,
    signed_at: str,
) -> dict[str, Any]:
    """Assemble a spec-valid envelope, or raise :class:`CommandSigningError`.

    Keyword-only and fully enumerated on purpose: the caller cannot pass a dict it
    assembled elsewhere, so "every signed field came from a server-side value at this
    call site" is readable at the call site. `protocol_version` and `provider` are not
    parameters — they are properties of this module, not of the delivery.
    """
    envelope = {
        "protocol_version": ENVELOPE_VERSION,
        "key_id": key_id,
        "provider": PROVIDER_GITHUB,
        "delivery_id": delivery_id,
        "event_type": event_type,
        "event_id": event_id,
        "arrived_at": arrived_at,
        "tenant_id": tenant_id,
        "installation_id": installation_id,
        "repo_id": repo_id,
        "repo": repo,
        "issue_number": issue_number,
        "sender_github_id": sender_github_id,
        "sender_type": sender_type,
        "command_body": command_body,
        "signed_at": signed_at,
    }
    validate_envelope(envelope)
    return envelope


def parse_keyring(secret_value: str) -> tuple[str, dict[str, bytes], str | None]:
    """Parse the keyring secret into `(active_key_id, keys, previous_valid_until)`.

    Shape (see the module docstring)::

        {"active_key_id": "...", "keys": {"<id>": "<secret>"},
         "previous_valid_until": "<ISO-8601 Z>"}

    Raises :class:`CommandSigningError` when the value is absent, a placeholder, not
    JSON, or names an `active_key_id` with no key material. Fail-closed by
    construction: there is no code path that returns a usable keyring built from a
    partially understood secret.

    Placeholder-valued individual keys are dropped rather than accepted, so a
    half-rotated secret degrades to "that key id is unknown" (which refuses) rather
    than to a signature computed under a public value.
    """
    if _placeholder(secret_value):
        raise CommandSigningError("signing secret holds an un-rotated placeholder")

    try:
        parsed = json.loads(secret_value)
    except (TypeError, ValueError) as exc:
        raise CommandSigningError(f"signing secret is not JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CommandSigningError("signing secret must be a JSON object")

    active_key_id = parsed.get("active_key_id")
    if not isinstance(active_key_id, str) or not active_key_id.strip():
        raise CommandSigningError("signing secret has no usable active_key_id")
    active_key_id = active_key_id.strip()

    raw_keys = parsed.get("keys")
    if not isinstance(raw_keys, dict):
        raise CommandSigningError("signing secret has no keys object")

    keys: dict[str, bytes] = {}
    for key_id, value in raw_keys.items():
        if not isinstance(key_id, str) or not isinstance(value, str):
            continue
        if _placeholder(value):
            # A placeholder key id is UNKNOWN, not usable. Silently dropping it is
            # what makes a half-rotated secret fail closed on that id.
            logger.warning(
                "engine command signing: key id %r holds a placeholder; ignoring it",
                key_id,
            )
            continue
        keys[key_id] = value.encode("utf-8")

    if active_key_id not in keys:
        raise CommandSigningError(
            f"signing secret's active_key_id {active_key_id!r} has no key material"
        )

    previous_valid_until = parsed.get("previous_valid_until")
    if not isinstance(previous_valid_until, str) or not previous_valid_until.strip():
        previous_valid_until = None

    return active_key_id, keys, previous_valid_until


# Cached per execution environment (cold start), like `secrets.py` and
# `marker_verify.py`. A rotation therefore takes effect on the next cold start; the
# verifier's overlap window is what covers events signed under the old key in the
# meantime.
_active: tuple[str, bytes] | None = None
_loaded = False


def _load_active_key() -> tuple[str, bytes]:
    """The active `(key_id, key)` for signing, or raise :class:`CommandSigningError`."""
    global _active, _loaded

    if _loaded:
        if _active is None:
            raise CommandSigningError("no engine-command signing key is available")
        return _active

    _loaded = True

    secret_arn = (os.environ.get(SIGNING_KEY_SECRET_ARN_ENV) or "").strip()
    if not secret_arn:
        raise CommandSigningError(f"{SIGNING_KEY_SECRET_ARN_ENV} is not set")

    from common.secrets import get_secret

    active_key_id, keys, _ = parse_keyring(get_secret(secret_arn))
    _active = (active_key_id, keys[active_key_id])
    logger.info(
        "engine command signing: active key id %r loaded from %s",
        active_key_id,
        secret_arn,
    )
    return _active


def sign_command(
    *,
    delivery_id: str,
    event_type: str,
    event_id: str,
    arrived_at: str,
    tenant_id: str,
    installation_id: str,
    repo_id: int,
    repo: str,
    issue_number: int,
    sender_github_id: str,
    sender_type: str,
    command_body: str,
    signed_at: str,
) -> tuple[str, str, dict[str, Any]]:
    """Sign one command's authority tuple.

    Returns `(key_id, signature, signed_fields)`. `signed_fields` is the exact
    envelope the signature commits to and is stored on the row beside it, so the
    verifier reconstructs the signed bytes from what was signed rather than from the
    row's other (mutable) attributes — and can then refuse a row whose mutable copies
    disagree with the signed tuple.

    Raises :class:`CommandSigningError` for anything that makes an honest signature
    impossible. The caller must treat that as "write the row unsigned", never as
    "write the row as if it were signed".
    """
    key_id, key = _load_active_key()
    envelope = build_envelope(
        key_id=key_id,
        delivery_id=delivery_id,
        event_type=event_type,
        event_id=event_id,
        arrived_at=arrived_at,
        tenant_id=tenant_id,
        installation_id=installation_id,
        repo_id=repo_id,
        repo=repo,
        issue_number=issue_number,
        sender_github_id=sender_github_id,
        sender_type=sender_type,
        command_body=command_body,
        signed_at=signed_at,
    )
    return key_id, compute_signature(key, envelope), envelope


def reset_key_cache() -> None:
    """Reset the cached active key (tests only)."""
    global _active, _loaded
    _active = None
    _loaded = False
