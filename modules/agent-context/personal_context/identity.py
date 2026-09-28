"""Identity middleware for personal-context operations.

Extracts ``X-Owner-Sub`` and ``X-Tenant-Id`` headers, validates them, and
provides a fail-closed guard: personal-context operations without both headers
receive an HTTP 403. Existing non-personal tools are unaffected when the
headers are absent.

Trust boundary: only pods within the agent-context Kubernetes namespace can
reach the Context MCP Server (NetworkPolicy). Headers are set by the trusted
dispatch layer, not by agent code — mirrors the gateway's
``BG_TRUST_APIGW_HEADERS`` / ``X-Caller-Identity`` pattern.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Header names (canonical lowercase for case-insensitive matching)
HEADER_OWNER_SUB = "x-owner-sub"
HEADER_TENANT_ID = "x-tenant-id"

# UUID v4 regex (accepts any UUID format: 8-4-4-4-12 hex)
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CallerIdentity:
    """Validated caller identity extracted from request headers."""

    owner_sub: str  # Cognito sub (UUID, lowercased)
    tenant_id: str  # Organization/tenant ID


class IdentityError(Exception):
    """Raised when identity validation fails (maps to HTTP 403)."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def is_valid_uuid(value: str) -> bool:
    """Check whether *value* is a well-formed UUID string."""
    return bool(_UUID_RE.match(value.strip()))


def extract_identity(headers: dict[str, str]) -> CallerIdentity | None:
    """Extract and validate caller identity from request headers.

    Returns ``None`` when neither header is present (non-personal request).
    Raises :class:`IdentityError` if headers are partially present or invalid
    (fail-closed).

    Parameters
    ----------
    headers:
        A dict of header-name → value. Names are treated case-insensitively.
    """
    # Normalize header keys to lowercase for case-insensitive lookup
    normalized: dict[str, str] = {k.lower(): v for k, v in headers.items()}

    owner_sub_raw = normalized.get(HEADER_OWNER_SUB, "").strip()
    tenant_id_raw = normalized.get(HEADER_TENANT_ID, "").strip()

    # Neither header present → not a personal-context request (pass-through)
    if not owner_sub_raw and not tenant_id_raw:
        return None

    # Partial headers → fail-closed
    if not owner_sub_raw:
        raise IdentityError("Missing X-Owner-Sub header (required for personal-context operations)")
    if not tenant_id_raw:
        raise IdentityError("Missing X-Tenant-Id header (required for personal-context operations)")

    # Validate owner_sub is a UUID (prevent path traversal)
    if not is_valid_uuid(owner_sub_raw):
        raise IdentityError(f"X-Owner-Sub must be a valid UUID, got: {owner_sub_raw!r}")

    # Validate tenant_id is non-empty (format varies by org)
    if not tenant_id_raw:
        raise IdentityError("X-Tenant-Id must not be empty")

    return CallerIdentity(
        owner_sub=owner_sub_raw.lower(),
        tenant_id=tenant_id_raw,
    )


def require_identity(headers: dict[str, str]) -> CallerIdentity:
    """Extract identity or raise IdentityError (fail-closed for personal-context ops).

    Unlike :func:`extract_identity`, this function always requires both headers
    to be present and valid. Use this for personal-context write/read operations.
    """
    identity = extract_identity(headers)
    if identity is None:
        raise IdentityError(
            "Both X-Owner-Sub and X-Tenant-Id headers are required for personal-context operations"
        )
    return identity
