"""The secret-redaction boundary every tool result passes through.

R9 acceptance 4: no tool result contains a provider secret. The cost of getting
this wrong is unrecoverable — a model-visible result is copied into the model's
context, the run transcript and the log sink, so a leaked provider key is
disclosed in three places before anyone notices.

The defence is therefore positioned as a boundary rather than as care taken in
each handler: `server.dispatch_tool()` passes *every* result through
`redact()` on the way out. A handler cannot opt out, and a handler that starts
returning a new credential-bearing field is scrubbed without being revisited.

Two independent rules run, because either alone has a known bypass:

1. **Key-name matching** catches a secret whose value shape is unremarkable —
   an opaque provider token indistinguishable from a request id.
2. **Value-shape matching** catches a secret under an innocuous key, which is
   the case key-name matching cannot see (`{"note": "AKIA..."}`).

Redaction is not reversible and no original is retained: this module returns a
placeholder so a caller can tell a field was withheld without learning anything
about what it held.
"""

from __future__ import annotations

import re
from typing import Any

# Replacement written in place of any withheld value. Deliberately uniform: a
# per-type placeholder would leak the kind of credential that was present.
PLACEHOLDER = "[REDACTED]"

# Key substrings that mark a value as a secret regardless of its shape.
_SECRET_KEY_PARTS: tuple[str, ...] = (
    "secret",
    "password",
    "passwd",
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

# Exact quota field names that are safe despite containing "token". Matching
# substrings would also allow keys such as "token_budget_api_key" to leak an
# opaque credential whose value has no recognizable secret shape.
_ALLOWED_KEY_NAMES: tuple[str, ...] = (
    "token_budget",
    "token_count",
    "tokens_used",
    "tokens_per_second",
    "max_tokens",
)

# Value shapes that are provider secrets wherever they appear.
_SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    # AWS access key id — AKIA (long-lived) and ASIA (temporary session).
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    # PEM private key block of any flavour (RSA, EC, OPENSSH, PGP).
    re.compile(r"-----BEGIN(?: [A-Z]+)* PRIVATE KEY-----"),
    # GitHub tokens: personal, OAuth, user-to-server, server-to-server, refresh.
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{16,}\b"),
    # Slack tokens.
    re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}\b"),
    # A Bearer credential embedded in a header-ish string.
    re.compile(r"\bBearer\s+[A-Za-z0-9\-._~+/]{16,}=*", re.IGNORECASE),
    # Generic private-cloud API keys, e.g. `sk-...` style provider keys.
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
)


def _key_marks_secret(key: str) -> bool:
    """True when a mapping key names a secret."""
    lowered = key.lower()
    if lowered in _ALLOWED_KEY_NAMES:
        return False
    return any(part in lowered for part in _SECRET_KEY_PARTS)


def value_contains_secret(value: Any) -> bool:
    """True when a scalar's *shape* is a known provider-secret shape."""
    if not isinstance(value, str):
        return False
    return any(pattern.search(value) for pattern in _SECRET_VALUE_PATTERNS)


def redact(value: Any) -> Any:
    """Return `value` with every secret replaced by `PLACEHOLDER`.

    Recurses through dicts, lists and tuples so a secret nested inside a
    provider response is reached. Non-string scalars pass through untouched;
    they cannot carry a secret shape and rewriting them would corrupt the
    quota numbers this surface exists to report.
    """
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and _key_marks_secret(key):
                out[key] = PLACEHOLDER
            else:
                out[key] = redact(item)
        return out
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if value_contains_secret(value):
        return PLACEHOLDER
    return value


def contains_secret(value: Any) -> bool:
    """True when any secret survives anywhere in `value`.

    Used by the tests as an independent check on `redact()` — asserting the
    output of a scrubber with the scrubber itself would be circular, so this
    walks the structure and reports rather than rewriting.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str) and _key_marks_secret(key) and item != PLACEHOLDER:
                return True
            if contains_secret(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(contains_secret(item) for item in value)
    return value_contains_secret(value)
