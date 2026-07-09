"""Register-time repo accessibility validation.

Issue #2087 (#2082 Phase-1 story 4): Determines at registration whether
a repo is public (shared, no credentials needed) or private (requires
the tenant's GitHub App installation + ownership verification).

Validation order (from architect I3):
  1. Public check (unauthenticated GET /repos/{owner}/{repo})
  2. Resolve installation for repo (global App JWT)
  3. Ownership check (installation belongs to caller's tenant)

Returns a result dataclass indicating scope and installation_id.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from src.knowledge.github_app_service import (
    check_membership_for_installation,
    resolve_installation_for_repo,
    verify_installation_ownership,
)

logger = logging.getLogger("bedrockgateway.knowledge.accessibility")

GITHUB_API_BASE = "https://api.github.com"


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class AccessibilityResult:
    """Result of register-time accessibility validation."""

    allowed: bool
    shared: bool = False  # True → public repo, tenant_id=NULL
    installation_id: int | None = None
    # Issue #3266: When membership fallback resolves a different owning tenant,
    # this field carries the org tenant_id the asset should be scoped to.
    tenant_id: str | None = None
    error_message: str | None = None
    error_code: int = 422  # HTTP status code to use on rejection


# ---------------------------------------------------------------------------
# Public-check cache (5-min TTL, keyed by owner/repo)
# ---------------------------------------------------------------------------

_public_cache: dict[str, tuple[bool, float]] = {}
_PUBLIC_CACHE_TTL = 300  # 5 minutes


def _cache_get(key: str) -> bool | None:
    """Get cached public-check result, or None if expired/missing."""
    entry = _public_cache.get(key)
    if entry is None:
        return None
    value, ts = entry
    if time.monotonic() - ts > _PUBLIC_CACHE_TTL:
        del _public_cache[key]
        return None
    return value


def _cache_set(key: str, value: bool) -> None:
    """Store a public-check result with timestamp."""
    _public_cache[key] = (value, time.monotonic())


def clear_public_cache() -> None:
    """Clear the public-check cache (for testing)."""
    _public_cache.clear()


# ---------------------------------------------------------------------------
# Public-check: unauthenticated GET /repos/{owner}/{repo}
# ---------------------------------------------------------------------------


async def check_repo_public(
    owner: str,
    repo: str,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> bool:
    """Check whether a repo is public via unauthenticated GitHub API call.

    Returns True if the repo exists and is public (private=false).
    Returns False if the repo is private, doesn't exist, or request fails.
    Results are cached for 5 minutes.
    """
    cache_key = f"{owner}/{repo}"
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    client = http_client or httpx.AsyncClient(
        base_url=GITHUB_API_BASE,
        headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=10.0,
    )
    try:
        resp = await client.get(f"/repos/{owner}/{repo}")
        if resp.status_code == 200:
            is_public = resp.json().get("private") is False
            _cache_set(cache_key, is_public)
            return is_public
        # 404 or other error → not public (or doesn't exist)
        _cache_set(cache_key, False)
        return False
    except Exception:
        logger.warning(
            "Public-check failed for %s/%s — treating as non-public",
            owner,
            repo,
            exc_info=True,
        )
        return False
    finally:
        if http_client is None:
            await client.aclose()


# ---------------------------------------------------------------------------
# Source-ref parsing: extract owner/repo from GitHub URL
# ---------------------------------------------------------------------------


def parse_github_owner_repo(source_ref: str) -> tuple[str, str] | None:
    """Extract (owner, repo) from a GitHub source_ref URL.

    Handles:
      - https://github.com/owner/repo
      - https://github.com/owner/repo.git
      - git@github.com:owner/repo.git

    Returns None if the source_ref doesn't match a GitHub repo pattern.
    """
    for prefix in ("https://github.com/", "git@github.com:"):
        if source_ref.startswith(prefix):
            path = source_ref[len(prefix) :]
            # Strip trailing .git
            if path.endswith(".git"):
                path = path[:-4]
            # Strip trailing slash
            path = path.rstrip("/")
            parts = path.split("/")
            if len(parts) >= 2 and parts[0] and parts[1]:
                return parts[0], parts[1]
    return None


# ---------------------------------------------------------------------------
# Main validation entry point
# ---------------------------------------------------------------------------


async def validate_repo_accessibility(
    source_ref: str,
    tenant_id: str,
    *,
    db: AsyncSession,
    gateway_db: AsyncSession,
    caller_user_id: str | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> AccessibilityResult:
    """Validate a repo's accessibility at registration time.

    Returns an AccessibilityResult indicating whether to accept (and how to
    scope) or reject the registration.

    Validation order:
      1. Parse owner/repo from source_ref
      2. Public check → accept shared (no installation needed)
      3. Resolve installation → None means App not installed
      4. Ownership check → reject if installation belongs to another tenant
      4b. Membership fallback (Issue #3266) → if ownership check fails, check
          if the caller is a member of any tenant that owns the installation
      5. Accept tenant-scoped with installation_id

    Args:
        source_ref: The GitHub URL (e.g. https://github.com/acme/my-repo)
        tenant_id: The caller's tenant ID (from session JWT)
        db: Agent-context database session (knowledge_assets table)
        gateway_db: Gateway database session for ownership/membership queries
            (users, channel_tenant_map, tenant_memberships). These tables live
            in the bedrockgateway DB, NOT agent_context — passing the wrong
            session causes 500s. Issue #3358.
        caller_user_id: The caller's Cognito sub (for membership fallback)
        http_client: Optional httpx client for testing
    """
    # Step 1: Parse owner/repo
    parsed = parse_github_owner_repo(source_ref)
    if parsed is None:
        return AccessibilityResult(
            allowed=False,
            error_message=f"Cannot parse GitHub owner/repo from source_ref: {source_ref}",
            error_code=400,
        )

    owner, repo = parsed

    # Step 2: Public check
    is_public = await check_repo_public(owner, repo, http_client=http_client)
    if is_public:
        return AccessibilityResult(allowed=True, shared=True)

    # Step 3: Resolve installation for repo
    try:
        installation_id = await resolve_installation_for_repo(owner, repo, http_client=http_client)
    except ValueError as exc:
        # Global App credentials not configured
        logger.warning("Cannot validate repo accessibility: %s", exc)
        return AccessibilityResult(
            allowed=False,
            error_message=(f"GitHub App not configured on this platform. Cannot verify access to {owner}/{repo}."),
            error_code=503,
        )
    except Exception as exc:
        logger.warning(
            "Installation resolution failed for %s/%s: %s",
            owner,
            repo,
            exc,
        )
        return AccessibilityResult(
            allowed=False,
            error_message=(f"Failed to verify GitHub App access to {owner}/{repo}. Please try again later."),
            error_code=502,
        )

    if installation_id is None:
        return AccessibilityResult(
            allowed=False,
            error_message=(f"We don't have access to {owner}/{repo}. Install the ADP App on it via Settings → Connections."),
            error_code=422,
        )

    # Step 4: Ownership check (per-tenant connection model)
    # Issue #3358: ownership/membership queries hit gateway tables (users,
    # channel_tenant_map, tenant_memberships) which live in the bedrockgateway DB.
    owns_installation = await verify_installation_ownership(tenant_id, installation_id, db=gateway_db)
    if owns_installation:
        # Step 5: Accept tenant-scoped
        return AccessibilityResult(
            allowed=True,
            shared=False,
            installation_id=installation_id,
        )

    # Step 4b: Membership fallback (Issue #3266)
    # The caller's personal tenant doesn't own the installation, but they may
    # be a member of an org-tenant that does. Check tenant_memberships.
    if caller_user_id:
        owning_tenant_id = await check_membership_for_installation(caller_user_id, installation_id, db=gateway_db)
        if owning_tenant_id:
            logger.info(
                "Membership fallback: user %s granted access to %s/%s via org tenant %s",
                caller_user_id,
                owner,
                repo,
                owning_tenant_id,
            )
            return AccessibilityResult(
                allowed=True,
                shared=False,
                installation_id=installation_id,
                tenant_id=owning_tenant_id,
            )

    return AccessibilityResult(
        allowed=False,
        error_message=(f"The GitHub App installation for {owner}/{repo} does not belong to your tenant."),
        error_code=403,
    )
