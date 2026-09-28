"""Inbound refusal of secret material, and the ARN rule that goes with it.

Issue #5047 (U7), EPIC #4910. R7 acceptances 1 and 4.

## Why refusal rather than scrubbing

There is already a scrubber in this repo: the tool surface's
`tools/superplane-mcp/superplane_mcp/redaction.py` rewrites secrets on the way
*out*, so no tool result carries one. This module is the opposite direction and a
different decision. Here a payload that contains secret material is **refused**,
not cleaned and accepted.

The distinction is the whole of R7 acceptance 1. If the domain API scrubbed a
value and carried on, the caller would get a success and would reasonably believe
the value had been stored — so the credential would exist in the caller's mind and
nowhere else, or worse, the caller would retry through some other path that did
persist it. A refusal tells the submitter the thing they did was wrong. And it
keeps the invariant checkable: the domain API's storage can be asserted to contain
no secret-shaped field, because no request that carried one ever succeeded.

The two modules are therefore not duplicates and neither replaces the other: one
guarantees nothing leaves, this one guarantees nothing arrives. Only the second
keeps secret material out of the domain's own store, which is what "secret values
reach only ADP's vault endpoint" requires.

## Why ARNs are treated as secret material

R7 acceptance 4 names the value **and** the ARN, and it is worth being explicit
about why an ARN is not merely an identifier. A Secrets Manager ARN names the
account, the region and the secret. Anyone holding it needs only a credential with
`secretsmanager:GetSecretValue` to complete the read — so publishing the ARN turns
every over-broad IAM policy in the account into a disclosure. It is also
unrecoverable in the same way a value is: once the string is in a model transcript
and a log aggregator, rotating the secret does not retract it, and the ARN
typically survives rotation unchanged, so the leaked pointer stays valid.

So a credential **reference** in this contract is the vault's own opaque
credential id, never an ARN. `looks_like_arn()` exists so that rule is enforced at
construction rather than described in a docstring.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import unquote

from .health import ContractViolation

# Key substrings that name secret material regardless of the value's shape. A
# provider token can be indistinguishable from a request id, so the key name is
# the only available signal for that case.
#
# Kept deliberately close to the outbound scrubber's list so the two directions do
# not disagree about what a secret is; the divergences are the entries below that
# matter for *inbound* provider onboarding specifically (`secret_access_key`,
# `passphrase`, `session_token`).
_SECRET_KEY_PARTS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
    "passphrase",
    "token",
    "credential",
    "private_key",
    "privatekey",
    "api_key",
    "apikey",
    "access_key",
    "accesskey",
    "session_key",
    "authorization",
    "auth_header",
    "client_secret",
)

# Exact key names that contain a flagged substring but carry no secret. Matched
# exactly, never as substrings: a substring allowance for "token_budget" would
# also admit "token_budget_api_key", whose value has no recognizable shape and
# would sail through the value rules below.

# Key names that are secret material only as an *exact* match, never as a
# substring. `value` is here and not in `_SECRET_KEY_PARTS` because it is the field
# name ADP's vault itself uses for the raw secret (`CredentialCreate.value` in
# `modules/gateway/src/auth/vault_schemas.py`) — so it is exactly what a caller who
# confused the vault's API for the domain's would send, and refusing it is the
# central case of acceptance 1.
#
# It cannot be a substring rule: this contract reports quota and capacity, so
# `observed_value`, `quota_value` and `price_value` are legitimate field names, and
# a substring match would refuse the payloads the contract exists to carry.
_SECRET_EXACT_KEY_NAMES: frozenset[str] = frozenset(
    {
        "value",
        "raw_value",
        "secret_value",
        "credential_value",
        "plaintext",
    }
)

_ALLOWED_KEY_NAMES: frozenset[str] = frozenset(
    {
        # Quota and budget reporting fields (this contract reports quota, so these
        # legitimately appear in the same payloads).
        "token_budget",
        "token_count",
        "tokens_used",
        "max_tokens",
        # The field that names *which* credential, as opposed to its value. This is
        # the reference the contract exists to accept, so it must not be refused
        # for containing "credential".
        "credential_id",
        "credential_type",
        "credential_ref",
        "credential_reference",
    }
)

# Value shapes that are secret material wherever they appear, including under an
# innocuous key. This is the case key-name matching structurally cannot see.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # AWS access key id — AKIA (long-lived) and ASIA (temporary session).
    re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"),
    # PEM private key block of any flavour (RSA, EC, OPENSSH, PGP).
    re.compile(r"-----BEGIN(?: [A-Z]+)* PRIVATE KEY-----"),
    # GitHub tokens: personal, OAuth, user-to-server, server-to-server, refresh.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{16,}"),
    # Slack tokens.
    re.compile(r"xox[abposr]-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{10,}"),
    # A Bearer credential embedded in a header-ish string.
    re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE),
    # Generic provider API keys, e.g. `sk-...` style.
    re.compile(r"(?<![A-Za-z0-9])sk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{20,}"),
    re.compile(r"sk-(?:[A-Za-z0-9]+-)*[A-Za-z0-9]{30,}"),
)

# Any AWS ARN. Not narrowed to `secretsmanager`, because a KMS key ARN or an IAM
# role ARN in a domain payload is the same class of pointer-into-the-account
# disclosure and there is no reason this contract needs to carry one.
_ARN_PATTERN = re.compile(r"arn:[a-z0-9._-]*:[a-z0-9._-]+:[a-z0-9._-]*:", re.IGNORECASE)


def _decoded_forms(value: str) -> tuple[str, ...]:
    """Inspect ordinary transport escapes and whitespace laundering, without
    rewriting accepted metadata. Limit decoding work on attacker-supplied text.
    """
    forms = [value]
    for layer in range(9):
        decoded = html.unescape(unquote(forms[-1]))
        decoded = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), decoded)
        if decoded == forms[-1]:
            break
        if layer == 8:
            return ()  # Excessively nested escapes are refused, not left opaque.
        forms.append(decoded)
    invisible = (
        r"[\s\x00-\x1f\x7f-\x9f\u00ad\u200b-\u200f\u2028-\u202e\u2060-\u2064\ufeff]+"
    )
    return tuple(forms + [re.sub(invisible, "", form) for form in forms])


def looks_like_arn(value: Any) -> bool:
    """True when a string is (or embeds) an AWS ARN.

    Used both by the payload guard below and by `CredentialReference`, which
    refuses to be constructed from an ARN — the reference must be the vault's own
    opaque credential id.
    """
    if not isinstance(value, str):
        return False
    return any(_ARN_PATTERN.search(form) for form in _decoded_forms(value))


def key_names_secret(key: str) -> bool:
    """True when a mapping key names secret material."""
    lowered = key.lower()
    if lowered in _ALLOWED_KEY_NAMES:
        return False
    if lowered in _SECRET_EXACT_KEY_NAMES:
        return True
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def value_is_secret_shaped(value: Any) -> bool:
    """True when a scalar's shape is a known secret shape, or is an ARN."""
    if not isinstance(value, str):
        return False
    if looks_like_arn(value):
        return True
    forms = _decoded_forms(value)
    return not forms or any(
        pattern.search(form) for form in forms for pattern in _SECRET_VALUE_PATTERNS
    )


def redact_secret_spans(text: str, *, placeholder: str) -> str:
    """Replace each secret-shaped span in `text`, leaving the rest intact.

    Issue #5053 (U7b). Lives here rather than in `emission.py` because it must match
    on exactly the patterns `value_is_secret_shaped` matches on; two copies of that
    judgement would eventually disagree, and the direction they disagree in is a
    span this function fails to redact.

    Span-level, not value-level, and the distinction matters: use this ONLY where the
    non-matching text is known-safe prose, such as a validator's own refusal message.
    For a value of unknown provenance withhold the whole thing — any part of it could
    be the secret. `emission.scrub` is that tool; this one is not a substitute.
    """
    if not isinstance(text, str):
        return text
    redacted = _ARN_PATTERN.sub(placeholder, text)
    for pattern in _SECRET_VALUE_PATTERNS:
        redacted = pattern.sub(placeholder, redacted)
    # Mixed raw and encoded material must not leave the encoded half recoverable.
    if value_is_secret_shaped(redacted):
        return placeholder
    return redacted


def find_secret_material(payload: Any, *, _path: str = "") -> str | None:
    """Return a path describing the first piece of secret material found, or None.

    The return value names *where* the offending field was, never what it held —
    so the refusal this feeds can tell a submitter which field to remove without
    the refusal itself becoming the leak. Recurses through mappings and sequences
    so a value nested inside a provider blob is reached rather than skipped.
    """
    if isinstance(payload, Mapping):
        for key, item in payload.items():
            if value_is_secret_shaped(key):
                return f"{_path}.<redacted-key>" if _path else "<redacted-key>"
            where = f"{_path}.{key}" if _path else str(key)
            if isinstance(key, str) and key_names_secret(key):
                # A secret-named key is refused even when its value is empty or
                # None. Accepting `{"secret": null}` would mean the shape of a
                # payload that carries secrets is a legal shape, and the next
                # caller fills it in.
                return where
            found = find_secret_material(item, _path=where)
            if found is not None:
                return found
        return None
    if isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            found = find_secret_material(item, _path=f"{_path}[{index}]")
            if found is not None:
                return found
        return None
    if value_is_secret_shaped(payload):
        return _path or "<value>"
    return None


def assert_no_secret_material(payload: Any, *, what: str = "payload") -> None:
    """Raise `ContractViolation` when `payload` carries secret material.

    This is the guard behind R7 acceptance 1: the domain API accepts a credential
    reference and refuses a payload containing a value. Raising the same
    `ContractViolation` every other guard in this package raises means a receiver
    catching that one type catches this too.

    The message names the field path and never the offending content.
    """
    found = find_secret_material(payload)
    if found is not None:
        raise ContractViolation(
            f"{what} carries secret material at {found!r}: register the secret with "
            "ADP's vault endpoint and submit the credential reference it returns"
        )
