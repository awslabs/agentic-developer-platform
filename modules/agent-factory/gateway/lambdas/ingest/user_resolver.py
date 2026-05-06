"""
User identity resolver — calls the gateway's /internal/v1/resolve-user endpoint.

Vault Phase 5 (#138): replaces ad-hoc identity logic with centralized resolution.
Feature-gated behind ENABLE_USER_IDENTITIES env var.

On success: returns resolved user_id, org_id, team_id.
On 404: returns magic_link_url for in-channel posting.
"""

import json
import logging
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Feature flag — when False, resolver is bypassed entirely.
ENABLE_USER_IDENTITIES = os.environ.get("ENABLE_USER_IDENTITIES", "").lower() in (
    "1",
    "true",
    "yes",
)

# Gateway internal endpoint base URL (e.g. http://bedrockgateway.adp-gateway:8080)
RESOLVER_BASE_URL = os.environ.get("RESOLVER_BASE_URL", "")

# Shared secret for internal API authentication
RESOLVER_API_KEY = os.environ.get("BG_INTERNAL_API_KEY", "")

# Cache TTL in seconds
_CACHE_TTL_SECONDS = 300  # 5 minutes


@dataclass
class ResolvedUser:
    """Successful resolution result."""

    user_id: str
    org_id: str
    team_id: str
    is_shadow: bool = False


@dataclass
class UnresolvedUser:
    """404 result — user not linked, magic link available."""

    magic_link_url: str


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------

# Key: (provider, provider_user_id) -> (result, expires_at)
_cache: dict[tuple[str, str], tuple[ResolvedUser | UnresolvedUser, float]] = {}


def _cache_get(provider: str, provider_user_id: str) -> ResolvedUser | UnresolvedUser | None:
    """Retrieve from cache if not expired."""
    key = (provider, provider_user_id)
    entry = _cache.get(key)
    if entry is None:
        return None
    result, expires_at = entry
    if time.time() >= expires_at:
        del _cache[key]
        return None
    return result


def _cache_set(provider: str, provider_user_id: str, result: ResolvedUser | UnresolvedUser) -> None:
    """Store result in cache with TTL."""
    key = (provider, provider_user_id)
    _cache[key] = (result, time.time() + _CACHE_TTL_SECONDS)


def cache_clear() -> None:
    """Clear the resolver cache (useful for testing)."""
    _cache.clear()


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def resolve_user(
    provider: str,
    provider_user_id: str,
    channel_context: str | None = None,
) -> ResolvedUser | UnresolvedUser | None:
    """Resolve a provider identity to an internal user.

    Returns:
        ResolvedUser on success (200/201 from gateway).
        UnresolvedUser on 404 (magic_link_url provided).
        None on error or when feature is disabled.
    """
    if not ENABLE_USER_IDENTITIES:
        return None

    if not RESOLVER_BASE_URL:
        logger.warning("RESOLVER_BASE_URL not configured; skipping identity resolution")
        return None

    if not provider or not provider_user_id:
        return None

    # Check cache first
    cached = _cache_get(provider, provider_user_id)
    if cached is not None:
        return cached

    # Call the gateway endpoint
    url = f"{RESOLVER_BASE_URL.rstrip('/')}/internal/v1/resolve-user"
    payload: dict[str, Any] = {
        "provider": provider,
        "provider_user_id": provider_user_id,
    }
    if channel_context:
        payload["channel_context"] = channel_context

    headers = {
        "Content-Type": "application/json",
    }
    if RESOLVER_API_KEY:
        headers["X-Internal-Api-Key"] = RESOLVER_API_KEY

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = json.loads(resp.read())
            result = ResolvedUser(
                user_id=body["user_id"],
                org_id=body.get("org_id", ""),
                team_id=body.get("team_id", ""),
                is_shadow=body.get("is_shadow", False),
            )
            _cache_set(provider, provider_user_id, result)
            return result

    except urllib.error.HTTPError as e:
        if e.code == 404:
            try:
                error_body = json.loads(e.read())
                magic_link_url = error_body.get("magic_link_url", "")
            except Exception:
                magic_link_url = ""
            result_404 = UnresolvedUser(magic_link_url=magic_link_url)
            # Cache 404s too — prevents spamming the user with links on every message
            _cache_set(provider, provider_user_id, result_404)
            return result_404
        logger.error("resolve-user returned HTTP %d: %s", e.code, e.reason)
        return None

    except Exception as e:
        logger.error("resolve-user call failed: %s", e)
        return None
