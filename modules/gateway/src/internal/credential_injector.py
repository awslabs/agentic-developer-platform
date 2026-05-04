"""Credential injection — maps credential_type to HTTP request headers.

Issue #136: Vault Phase 3 — service-to-service endpoints + delivery paths

Pure functions; no I/O.  Tests can import this without any DB or AWS setup.

Supported types and their injection strategy:
    bearer       → Authorization: Bearer <token>
    oauth_token  → Authorization: Bearer <token>  (alias)
    api_key      → Authorization: ApiKey <token>
    basic_auth   → Authorization: Basic base64(value)
                   value may be "user:pass" (colon-joined) or a raw token.

File-oriented types (ssh_key, certificate, config_file) cannot be injected
into HTTP headers — they must go through the materialize path instead.
"""

from __future__ import annotations

import base64

# Types that are delivered as files, not header values.
FILE_CREDENTIAL_TYPES: frozenset[str] = frozenset({"ssh_key", "certificate", "config_file"})

# All supported types (for validation).
SUPPORTED_CREDENTIAL_TYPES: frozenset[str] = frozenset({"bearer", "oauth_token", "api_key", "basic_auth"} | FILE_CREDENTIAL_TYPES)


class UnsupportedCredentialTypeError(ValueError):
    """credential_type is not known or cannot be injected into HTTP headers."""


def inject_credential(
    credential_type: str,
    value: str,
    headers: dict[str, str],
) -> dict[str, str]:
    """Return a *new* headers dict with the credential injected.

    Parameters
    ----------
    credential_type:
        One of: bearer, oauth_token, api_key, basic_auth.
        File-oriented types (ssh_key, certificate, config_file) raise
        ``UnsupportedCredentialTypeError`` — use the materialize path instead.
    value:
        The raw credential value fetched from Secrets Manager.
    headers:
        Existing request headers.  The original dict is NOT mutated.

    Returns
    -------
    dict[str, str]
        A new headers dict with the auth header added/replaced.

    Raises
    ------
    UnsupportedCredentialTypeError
        For file-oriented types or unknown types.
    """
    if credential_type in FILE_CREDENTIAL_TYPES:
        raise UnsupportedCredentialTypeError(
            f"credential_type={credential_type!r} is file-oriented and cannot be injected "
            "into HTTP headers. Use the /credential-materialize endpoint instead."
        )

    result = dict(headers)

    if credential_type in ("bearer", "oauth_token"):
        result["Authorization"] = f"Bearer {value}"
    elif credential_type == "api_key":
        result["Authorization"] = f"ApiKey {value}"
    elif credential_type == "basic_auth":
        encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
        result["Authorization"] = f"Basic {encoded}"
    else:
        raise UnsupportedCredentialTypeError(
            f"Unknown credential_type={credential_type!r}. Supported header types: bearer, oauth_token, api_key, basic_auth."
        )

    return result
