"""Business logic for the connections module.

Issue #465: GitHub App install-start, install-callback, list, and delete.
Issue #2593: Platform-admin GitHub App registration via manifest conversion flow.
Issue #2595: GitHub App lifecycle endpoints (status, rotate-key, disconnect).

Design notes:
- State nonces reuse the existing magic_link_nonces table with provider="github_install".
- No new tables; installation-to-tenant mapping is owned by the admin identity service
  via POST /api/admin/identity/organizations.
- GitHub metadata (account_login, repo count) is fetched live from the GitHub API and
  cached in-process for ~5 minutes to avoid hammering the API on list calls.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.magic_link import (
    NonceAlreadyConsumedError,
    NonceNotFoundError,
    TargetUserMismatchError,
    TokenExpiredError,
    store_nonce,
)
from src.shared.config import get_settings
from src.shared.models.vault import MagicLinkNonce

from .github_app_provider import get_github_app_provider
from .github_client import GitHubAppClient
from .schemas import (
    AppStatusResponse,
    ConnectionsListResponse,
    DeleteConnectionResponse,
    DisconnectAppResponse,
    GitHubConnectionItem,
    InstallStartResponse,
    RegisterAppStartResponse,
    RotateKeyResponse,
)

logger = logging.getLogger(__name__)

_PROVIDER_GITHUB_INSTALL = "github_install"
_PROVIDER_GITHUB_APP_REGISTER = "github_app_register"
_NONCE_TTL_SECONDS = 900  # 15 minutes

# Terraform seeds secrets with this literal placeholder at deploy time
# (modules/agent-factory/webhook-ingress/infra/secrets.tf:39,54).
# It must never be treated as a real App credential.
_PLACEHOLDER_SENTINEL = "PLACEHOLDER_SET_BY_REGISTER_SCRIPT"

# Issue #2746: in-process TTL cache for the public login_enabled read, so the
# unauthenticated /auth/login-options endpoint does not hit Secrets Manager on
# every request. Bounded to ~1 SM read/min/pod. Stores (expires_at_monotonic, value).
_LOGIN_ENABLED_CACHE: tuple[float, bool] | None = None
_LOGIN_ENABLED_TTL_SECONDS = 60


def _is_placeholder(value: str) -> bool:
    """Return True if the value is the deploy-time placeholder, not a real credential."""
    return value.strip() == _PLACEHOLDER_SENTINEL


# ---------------------------------------------------------------------------
# Simple in-process cache for GitHub installation metadata
# (avoids repeated API calls when the user refreshes the connections list)
# ---------------------------------------------------------------------------

_metadata_cache: dict[int, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL_SECONDS = 300  # 5 minutes


def _cache_get(installation_id: int) -> dict[str, Any] | None:
    entry = _metadata_cache.get(installation_id)
    if entry is None:
        return None
    cached_at, data = entry
    if time.monotonic() - cached_at > _CACHE_TTL_SECONDS:
        del _metadata_cache[installation_id]
        return None
    return data


def _cache_set(installation_id: int, data: dict[str, Any]) -> None:
    _metadata_cache[installation_id] = (time.monotonic(), data)


def _cache_invalidate(installation_id: int) -> None:
    _metadata_cache.pop(installation_id, None)


# ---------------------------------------------------------------------------
# Issue #2983: Short-TTL cache for live repository lists from GitHub.
# Separate from the 5-minute metadata cache — repos refresh every 60s so
# the connections card reflects GitHub's current state without hammering the API.
# ---------------------------------------------------------------------------

_repo_list_cache: dict[int, tuple[float, list[str]]] = {}
_REPO_LIST_CACHE_TTL_SECONDS = 60  # 1 minute


def _repo_cache_get(installation_id: int) -> list[str] | None:
    entry = _repo_list_cache.get(installation_id)
    if entry is None:
        return None
    cached_at, data = entry
    if time.monotonic() - cached_at > _REPO_LIST_CACHE_TTL_SECONDS:
        del _repo_list_cache[installation_id]
        return None
    return data


def _repo_cache_set(installation_id: int, data: list[str]) -> None:
    _repo_list_cache[installation_id] = (time.monotonic(), data)


def _repo_cache_invalidate(installation_id: int) -> None:
    _repo_list_cache.pop(installation_id, None)


# ---------------------------------------------------------------------------
# Settings helpers
# ---------------------------------------------------------------------------


def _get_github_app_slug() -> str:
    """Return the GitHub App slug used for the install URL.

    Resolution order (Issue #2594):
      1. Secrets Manager cache (adp/<env>/github-app/adp-agent-platform-meta)
      2. BG_GITHUB_APP_SLUG env var (backward-compatible fallback)

    A blank value means the App identity was never wired; fail loudly rather
    than point the UI at the wrong App.
    """
    provider = get_github_app_provider()
    slug = provider.get_slug()
    if not slug:
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub App not configured. Register via Settings > Connections "
                "or set BG_GITHUB_APP_SLUG (and BG_GITHUB_APP_ID / "
                "BG_GITHUB_APP_PRIVATE_KEY) on the gateway."
            ),
        )
    return slug


def _get_github_app_credentials() -> tuple[str, str]:
    """Return (app_id, private_key_pem) for the platform GitHub App.

    Resolution order (Issue #2594):
      1. Secrets Manager cache (adp/<env>/github-app/adp-agent-platform-{id,key})
      2. BG_GITHUB_APP_ID / BG_GITHUB_APP_PRIVATE_KEY env vars (fallback)
    """
    provider = get_github_app_provider()
    return provider.get_credentials()


# ---------------------------------------------------------------------------
# Service functions
# ---------------------------------------------------------------------------


async def install_start(
    *,
    cognito_sub: str,
    user_id: str,
    db: AsyncSession,
) -> InstallStartResponse:
    """Generate a state nonce and return the GitHub App install URL.

    Args:
        cognito_sub: The Cognito subject claim from the caller's JWT.
        user_id:     The internal users.id for the caller.
        db:          Database session.
    """
    jti = str(uuid.uuid4())
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=_NONCE_TTL_SECONDS)

    await store_nonce(
        jti=jti,
        provider=_PROVIDER_GITHUB_INSTALL,
        provider_user_id=cognito_sub,
        channel_context=None,
        target_user_id=user_id,
        expires_at=expires_at,
        db=db,
    )

    app_slug = _get_github_app_slug()
    install_url = f"https://github.com/apps/{app_slug}/installations/new?state={jti}"

    logger.info(
        "GitHub install-start jti=%s user=%s expires_at=%s",
        jti,
        user_id,
        expires_at.isoformat(),
    )

    return InstallStartResponse(
        install_url=install_url,
        state_token=jti,
        expires_at=expires_at,
    )


async def install_callback(
    *,
    installation_id: int,
    setup_action: str,
    state: str,
    db: AsyncSession,
    github_client: GitHubAppClient | None = None,
) -> dict[str, Any]:
    """Validate state nonce, consume it, and attach the installation to the tenant.

    Identity comes from the **state nonce**, not a bearer token: GitHub redirects
    the operator's browser here as a plain GET with no Authorization header, so
    there is no token to read. The nonce was minted by install-start for a
    specific signed-in user (`target_user_id`), is single-use, and expires in 15
    minutes — so it is the authenticator here. We resolve the caller's user_id
    from the nonce and their org_id from the `users` table.

    Issue #2952: When `state` is empty/missing (public-App install initiated from
    GitHub by a non-ADP user), bypass nonce validation entirely. Resolve the org
    exclusively from the installation metadata via GitHub API. Create the tenant
    shell (upsert only, no user attachment). Return a generic success page.

    Returns a dict with keys:
        success          — bool
        installation_id  — int
        account_login    — str
        account_type     — str
        error_code       — str | None  (set on failure)
        error_message    — str | None
        no_nonce         — bool  (True when state was empty — public-App path)

    Raises:
        ValueError  — nonce validation failure (expired, consumed, not found)
        PermissionError — cross-tenant ownership conflict
    """
    from sqlalchemy import select, update

    from src.shared.models.organization import Organization, User

    # Issue #2952: No-nonce path for public-App installs initiated from GitHub
    # by a non-ADP user. Safe because it only creates resources keyed by the
    # GitHub-verified installation ID and grants no session or access.
    if not state:
        return await _handle_no_nonce_install(
            installation_id=installation_id,
            db=db,
            github_client=github_client,
        )

    # 1. Look up nonce
    stmt = select(MagicLinkNonce).where(
        MagicLinkNonce.jti == state,
        MagicLinkNonce.provider == _PROVIDER_GITHUB_INSTALL,
    )
    result = await db.execute(stmt)
    nonce = result.scalar_one_or_none()

    if nonce is None:
        raise NonceNotFoundError(f"State token not found: {state}")

    now = datetime.now(UTC)
    expires_at = nonce.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < now:
        raise TokenExpiredError("State token has expired")

    if nonce.consumed_at is not None:
        raise NonceAlreadyConsumedError(f"State token already used: {state}")

    # 2. Resolve the caller's org from the nonce (the nonce IS the authenticator
    #    — see the docstring). target_user_id is the internal users.id set at
    #    install-start; provider_user_id is the cognito_sub. The install attaches
    #    to this user's own org (org + personal installs alike).
    user_row = None
    if nonce.target_user_id:
        user_row = await db.get(User, nonce.target_user_id)
    if user_row is None and nonce.provider_user_id:
        user_row = (await db.execute(select(User).where(User.cognito_sub == nonce.provider_user_id))).scalar_one_or_none()
    if user_row is None:
        logger.warning("GitHub install-callback: no users row for nonce jti=%s", state)
        raise TargetUserMismatchError("Could not resolve the user this install link was issued for")
    caller_org_id = user_row.org_id

    # 3. Atomically consume the nonce (WHERE consumed_at IS NULL prevents races)
    consume_stmt = (
        update(MagicLinkNonce)
        .where(MagicLinkNonce.jti == state, MagicLinkNonce.consumed_at.is_(None))
        .values(consumed_at=now)
        .returning(MagicLinkNonce.jti)
    )
    consume_result = await db.execute(consume_stmt)
    consumed_jti = consume_result.scalar_one_or_none()
    if consumed_jti is None:
        # Another concurrent request consumed it first
        raise NonceAlreadyConsumedError(f"State token already used (concurrent): {state}")
    await db.commit()

    logger.info("GitHub install-callback nonce consumed jti=%s installation_id=%d", state, installation_id)

    # 4. Fetch installation metadata from GitHub
    app_id, private_key = _get_github_app_credentials()
    if github_client is None and app_id and private_key:
        github_client = GitHubAppClient(app_id=app_id, private_key_pem=private_key)

    account_login = "unknown"
    account_type = "Organization"
    github_org_id: int | None = None
    repository_selection = "selected"
    repositories: list[str] = []

    if github_client is not None:
        try:
            meta = await github_client.get_installation(installation_id)
            account = meta.get("account", {})
            account_login = account.get("login", "unknown")
            account_type = account.get("type", "Organization")
            github_org_id = account.get("id")
            repository_selection = meta.get("repository_selection", "selected")
            _cache_set(installation_id, meta)
        except Exception as exc:
            logger.warning("Could not fetch GitHub installation metadata: %s", exc)
        # Fetch the actual repo names (informational; never fail the install for it).
        try:
            repositories = await github_client.list_installation_repository_names(installation_id)
        except Exception as exc:
            logger.warning("Could not fetch repositories for installation %d: %s", installation_id, exc)

    # 5. Issue #2952: Resolve the target tenant for org installs.
    #    For account_type == "Organization", look up by github_org_id first;
    #    if found, route the install to that org's tenant instead of caller's.
    #    For unknown orgs (public-App installs), upsert the tenant shell.
    #    Personal installs and pre-existing behavior preserved via caller_org_id.
    resolved_org_id = caller_org_id

    if account_type == "Organization" and github_org_id is not None:
        # Try to find existing org by github_org_id
        org_by_github_id = (await db.execute(select(Organization).where(Organization.github_org_id == str(github_org_id)))).scalar_one_or_none()

        if org_by_github_id is not None:
            resolved_org_id = org_by_github_id.id
            logger.info(
                "install-callback: resolved org by github_org_id=%s → tenant=%s",
                github_org_id,
                resolved_org_id,
            )
        elif os.environ.get("ORG_TENANT_AUTO_CREATE", "false").lower() == "true":
            # Issue #2952 (Rev 4 C): Install-time tenant upsert for unknown orgs.
            # On a public App, orgs install without ever registering.
            upserted_id = await _upsert_org_tenant_shell(
                owner_login=account_login,
                github_org_id=str(github_org_id),
                github_app_id="",
                db=db,
            )
            if upserted_id:
                resolved_org_id = upserted_id
                logger.info(
                    "install-callback: upserted org-tenant shell for unknown org %s → tenant=%s",
                    account_login,
                    resolved_org_id,
                )

    await _attach_org_installation(
        installation_id=installation_id,
        github_org_id=github_org_id,
        github_org_login=account_login,
        caller_org_id=resolved_org_id,
        db=db,
        account_type=account_type,
        repository_selection=repository_selection,
        repositories=repositories,
    )

    # Issue #2085: Seed per-tenant GitHub App secret so that downstream
    # resolve_tenant_app_credentials() never hits a missing-secret error.
    from .tenant_secret import seed_tenant_github_app_secret

    await seed_tenant_github_app_secret(resolved_org_id, installation_id)

    if account_type == "Organization":
        # Issue #719: Populate organizations.github_installation_ids so that
        # future users from this org are matched to this tenant automatically.
        await _append_installation_id_to_org(
            installation_id=installation_id,
            caller_org_id=resolved_org_id,
            db=db,
        )

    # Issue #2950: Write the installation → tenant mapping to DynamoDB
    # identity-index so the webhook-ingress resolver can find it. Without
    # this, the webhook rejects all events as unknown_installation because
    # the DDB lookup misses and Postgres is only consulted as a drift
    # safety-net AFTER a DDB hit.
    # Issue #2952 (E): MUST use the resolved org tenant, not caller_org_id,
    # otherwise webhook routing points at the wrong tenant.
    await _write_installation_identity_index(
        installation_id=installation_id,
        org_id=resolved_org_id,
    )

    return {
        "success": True,
        "installation_id": installation_id,
        "account_login": account_login,
        "account_type": account_type,
        "error_code": None,
        "error_message": None,
    }


async def _handle_no_nonce_install(
    *,
    installation_id: int,
    db: AsyncSession,
    github_client: GitHubAppClient | None = None,
) -> dict[str, Any]:
    """Handle a public-App install with no state/nonce (non-ADP user path).

    Issue #2952 (Rev 4 C): When state is empty or missing on the install
    callback (public-App install initiated from GitHub by a non-ADP user),
    bypass nonce validation entirely. Resolve the org exclusively from the
    installation metadata via GitHub API. Create the tenant shell (upsert
    only, no user attachment, no caller_org_id). Return a generic success.

    This path is safe because it only creates resources keyed by the
    GitHub-verified installation ID and grants no session or access to anyone.
    """
    from sqlalchemy import select

    from src.shared.models.organization import Organization

    # Fetch installation metadata from GitHub
    app_id, private_key = _get_github_app_credentials()
    if github_client is None and app_id and private_key:
        github_client = GitHubAppClient(app_id=app_id, private_key_pem=private_key)

    account_login = "unknown"
    account_type = "Organization"
    github_org_id: int | None = None
    repository_selection = "selected"
    repositories: list[str] = []

    if github_client is not None:
        try:
            meta = await github_client.get_installation(installation_id)
            account = meta.get("account", {})
            account_login = account.get("login", "unknown")
            account_type = account.get("type", "Organization")
            github_org_id = account.get("id")
            repository_selection = meta.get("repository_selection", "selected")
            _cache_set(installation_id, meta)
        except Exception as exc:
            logger.warning("no-nonce install: could not fetch metadata: %s", exc)
        try:
            repositories = await github_client.list_installation_repository_names(installation_id)
        except Exception as exc:
            logger.warning("no-nonce install: could not fetch repos for %d: %s", installation_id, exc)

    # For org installs, resolve or upsert the org tenant
    resolved_org_id: str | None = None

    if account_type == "Organization" and github_org_id is not None:
        # Try to find existing org by github_org_id
        org_by_github_id = (await db.execute(select(Organization).where(Organization.github_org_id == str(github_org_id)))).scalar_one_or_none()

        if org_by_github_id is not None:
            resolved_org_id = org_by_github_id.id
        elif os.environ.get("ORG_TENANT_AUTO_CREATE", "false").lower() == "true":
            # Upsert the tenant shell for this unknown org
            resolved_org_id = await _upsert_org_tenant_shell(
                owner_login=account_login,
                github_org_id=str(github_org_id),
                github_app_id="",
                db=db,
            )

    if resolved_org_id:
        # Attach the install to the resolved org tenant
        await _attach_org_installation(
            installation_id=installation_id,
            github_org_id=github_org_id,
            github_org_login=account_login,
            caller_org_id=resolved_org_id,
            db=db,
            account_type=account_type,
            repository_selection=repository_selection,
            repositories=repositories,
        )

        # Seed per-tenant secret
        from .tenant_secret import seed_tenant_github_app_secret

        await seed_tenant_github_app_secret(resolved_org_id, installation_id)

        if account_type == "Organization":
            await _append_installation_id_to_org(
                installation_id=installation_id,
                caller_org_id=resolved_org_id,
                db=db,
            )

        # DDB write uses the resolved org tenant
        await _write_installation_identity_index(
            installation_id=installation_id,
            org_id=resolved_org_id,
        )

    logger.info(
        "no-nonce install: installation_id=%d account=%s resolved_org=%s",
        installation_id,
        account_login,
        resolved_org_id or "(none)",
    )

    return {
        "success": True,
        "installation_id": installation_id,
        "account_login": account_login,
        "account_type": account_type,
        "error_code": None,
        "error_message": None,
        "no_nonce": True,
    }


def _build_install_metadata(
    *,
    installation_id: int,
    account_login: str,
    account_type: str,
    repository_selection: str = "selected",
    repository_count: int = 0,
    repositories: list[str] | None = None,
) -> dict[str, Any]:
    """Build the metadata dict stored on ChannelTenantMap at install time."""
    repos = repositories or []
    return {
        "installation_id": installation_id,
        "account_login": account_login,
        "account_type": account_type,
        "repository_selection": repository_selection,
        # Keep count consistent with the stored names when we have them.
        "repository_count": len(repos) if repos else repository_count,
        "repositories": repos,
    }


async def _attach_org_installation(
    *,
    installation_id: int,
    github_org_id: int | None,
    github_org_login: str,
    caller_org_id: str,
    db: AsyncSession,
    account_type: str = "Organization",
    repository_selection: str = "selected",
    repositories: list[str] | None = None,
) -> None:
    """Attach a GitHub installation (org or personal) to the caller's ADP tenant.

    Checks for cross-tenant ownership conflicts via the ChannelTenantMap table.
    On conflict, raises PermissionError with a user-actionable message.
    On first install, inserts a ChannelTenantMap row; on re-install, updates the
    metadata. Stores the repo names so the connections UI can list them.
    """
    from sqlalchemy import select

    from src.shared.models.vault import ChannelTenantMap

    repos = repositories or []

    # Scope key: the GitHub account id (numeric) when available, else the login.
    if github_org_id is not None:
        scope_id = str(github_org_id)
    else:
        scope_id = github_org_login

    def _meta() -> dict[str, Any]:
        return _build_install_metadata(
            installation_id=installation_id,
            account_login=github_org_login,
            account_type=account_type,
            repository_selection=repository_selection,
            repositories=repos,
        )

    stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.provider_scope_id == scope_id,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is not None:
        if existing.org_id != caller_org_id:
            raise PermissionError(
                f"GitHub account '{github_org_login}' is already connected to another ADP tenant. Contact support if you believe this is an error."
            )
        # Already mapped to this tenant — update metadata (idempotent re-install)
        existing.install_metadata = _meta()
        await db.commit()
        logger.info(
            "GitHub %s re-installed installation_id=%d tenant=%s (%d repos)",
            github_org_login,
            installation_id,
            caller_org_id,
            len(repos),
        )
        return

    # New mapping — record it with metadata
    mapping = ChannelTenantMap(
        provider="github",
        provider_scope_id=scope_id,
        org_id=caller_org_id,
        install_metadata=_meta(),
    )
    db.add(mapping)
    await db.commit()
    logger.info(
        "GitHub %s (installation_id=%d) attached to tenant %s (%d repos)",
        github_org_login,
        installation_id,
        caller_org_id,
        len(repos),
    )


async def _append_installation_id_to_org(
    *,
    installation_id: int,
    caller_org_id: str,
    db: AsyncSession,
) -> None:
    """Append installation_id to the caller's organization.github_installation_ids.

    Issue #719: Ensures the org's installation list is populated so that the
    onboarding handler can match future users from the same GitHub org.
    Idempotent — does not double-append.
    """
    from src.shared.models.organization import Organization

    org = await db.get(Organization, caller_org_id)
    if org is None:
        logger.warning(
            "Cannot append installation_id=%d: org %s not found",
            installation_id,
            caller_org_id,
        )
        return

    install_id_str = str(installation_id)
    current_ids = org.github_installation_ids or []
    if install_id_str not in current_ids:
        org.github_installation_ids = current_ids + [install_id_str]
        await db.commit()
        logger.info(
            "Appended installation_id=%d to org %s github_installation_ids",
            installation_id,
            caller_org_id,
        )


async def _write_installation_identity_index(
    *,
    installation_id: int,
    org_id: str,
) -> None:
    """Write the installation → tenant mapping to the DynamoDB identity-index.

    Issue #2950: The webhook-ingress identity resolver uses DDB as its primary
    lookup for installation_id → tenant. Without this row, all webhook events
    for the installation are rejected as unknown_installation.

    Best-effort write-through with retry (same pattern as identity/organizations_service).
    Failures are logged but do not propagate — Postgres remains the source of truth.
    """
    from src.admin.identity_index import IdentityIndexClient

    client = IdentityIndexClient()
    success = await client.put_identity(
        identity_type="github_installation_id",
        identity_value=str(installation_id),
        org_id=org_id,
    )
    if success:
        logger.info(
            "identity-index: wrote github_installation_id=%d → org=%s",
            installation_id,
            org_id,
        )
    else:
        logger.warning(
            "identity-index: failed to write github_installation_id=%d → org=%s (webhook routing will fail until backfilled)",
            installation_id,
            org_id,
        )


async def list_connections(
    *,
    caller_org_id: str,
    caller_user_id: str,
    db: AsyncSession,
    github_client: GitHubAppClient | None = None,
    member_tenant_ids: list[str] | None = None,
) -> ConnectionsListResponse:
    """Return all GitHub installations connected to the caller's ADP tenants.

    Queries ChannelTenantMap for this org's GitHub entries. For personal accounts
    within adp-default, scopes to only the caller's own installations to prevent
    cross-user data leakage.

    Issue #2983: Repository lists are fetched LIVE from GitHub (cached 60s) so
    the card always reflects the current state. Stored metadata is used only as
    a fallback when the GitHub API is unavailable.

    Issue #3018: When member_tenant_ids is provided, queries across ALL member
    tenants and tags each connection with tenant_id, tenant_name, is_active_tenant.
    """
    from sqlalchemy import select

    from src.shared.models.organization import Organization
    from src.shared.models.vault import ChannelTenantMap

    from .adp_default import get_adp_default_org_id

    # Determine which tenant IDs to query
    if member_tenant_ids:
        tenant_ids_to_query = member_tenant_ids
    else:
        tenant_ids_to_query = [caller_org_id]

    # Query connections across all relevant tenants
    stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.org_id.in_(tenant_ids_to_query),
    )
    result = await db.execute(stmt)
    mappings = result.scalars().all()

    if not mappings:
        return ConnectionsListResponse(connections=[])

    # For personal accounts in adp-default, filter to this user's installs only.
    # provider_scope_id format for personal: "personal:<github_id>:<adp_user_id>"
    adp_default_id = get_adp_default_org_id()
    mappings = [m for m in mappings if m.org_id != adp_default_id or m.provider_scope_id.endswith(f":{caller_user_id}")]

    # Issue #3018: Pre-fetch tenant names for multi-tenant tagging
    tenant_name_map: dict[str, str] = {}
    if member_tenant_ids:
        org_stmt = select(Organization.id, Organization.name).where(
            Organization.id.in_(tenant_ids_to_query),
        )
        org_rows = (await db.execute(org_stmt)).all()
        tenant_name_map = {row[0]: row[1] for row in org_rows}

    # Issue #2983: Build a GitHub client for live repo reads if not injected.
    if github_client is None:
        app_id, private_key = _get_github_app_credentials()
        if app_id and private_key:
            github_client = GitHubAppClient(app_id=app_id, private_key_pem=private_key)

    connections: list[GitHubConnectionItem] = []
    for mapping in mappings:
        md = mapping.install_metadata or {}
        install_id = int(md.get("installation_id") or 0)

        if install_id == 0:
            # Legacy row without metadata — skip and warn.
            logger.warning(
                "ChannelTenantMap row %s has no installation_id metadata — skipping",
                mapping.id,
            )
            continue

        account_login = md.get("account_login", "(unknown)")
        account_type = md.get("account_type", "Organization")
        repo_selection = md.get("repository_selection", "selected")

        # Issue #2983: Live repo-list read from GitHub with 60s TTL cache.
        # Falls back to stored metadata on failure.
        repositories = await _fetch_live_repos(install_id, github_client)
        if repositories is None:
            # Graceful degradation — use the stored snapshot.
            repositories = md.get("repositories") or []

        repo_count = len(repositories) if repositories else int(md.get("repository_count") or 0)

        configure_url = f"https://github.com/settings/installations/{install_id}"

        # Issue #2983: manage_url deep-links to GitHub's repo management page.
        if account_type == "Organization":
            manage_url = f"https://github.com/organizations/{account_login}/settings/installations/{install_id}"
        else:
            manage_url = f"https://github.com/settings/installations/{install_id}"

        # Issue #3018: Tag with tenant info when in multi-tenant mode
        tenant_id = mapping.org_id if member_tenant_ids else None
        tenant_name = tenant_name_map.get(mapping.org_id) if member_tenant_ids else None
        is_active_tenant = (mapping.org_id == caller_org_id) if member_tenant_ids else None

        connections.append(
            GitHubConnectionItem(
                provider="github",
                installation_id=install_id,
                account_login=account_login,
                account_type=account_type,
                repository_selection=repo_selection,
                repository_count=repo_count,
                repositories=repositories,
                installed_at=mapping.created_at,
                configure_url=configure_url,
                manage_url=manage_url,
                tenant_id=tenant_id,
                tenant_name=tenant_name,
                is_active_tenant=is_active_tenant,
            )
        )

    return ConnectionsListResponse(connections=connections)


async def _fetch_live_repos(
    installation_id: int,
    github_client: GitHubAppClient | None,
) -> list[str] | None:
    """Fetch the live repo list for an installation, with 60s TTL cache.

    Issue #2983: Returns the repo list from GitHub on success, or None on failure
    (caller should fall back to stored snapshot).
    """
    # Check cache first
    cached = _repo_cache_get(installation_id)
    if cached is not None:
        return cached

    if github_client is None:
        return None

    try:
        repos = await github_client.list_installation_repository_names(installation_id)
        _repo_cache_set(installation_id, repos)
        return repos
    except Exception as exc:
        logger.warning(
            "Issue #2983: live repo fetch failed for installation %d, degrading to stored snapshot: %s",
            installation_id,
            exc,
        )
        return None


async def delete_connection(
    *,
    installation_id: int,
    caller_org_id: str,
    db: AsyncSession,
    github_client: GitHubAppClient | None = None,
) -> DeleteConnectionResponse:
    """Revoke a GitHub App installation and remove the tenant mapping.

    Steps:
    1. Verify the caller's ADP tenant owns this installation (via ChannelTenantMap).
    2. Call GitHub API DELETE /app/installations/{id}.
    3. Remove the ChannelTenantMap row.

    Raises:
        PermissionError — installation not owned by caller's tenant
        ValueError      — installation not found
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import select

    from src.shared.models.vault import ChannelTenantMap

    # 1. Verify ownership — find the ChannelTenantMap row for this org that matches
    #    the installation. Since ChannelTenantMap stores GitHub org ID (or login) not
    #    installation_id, we fetch metadata from GitHub first to get the org ID.
    app_id, private_key = _get_github_app_credentials()
    if github_client is None and app_id and private_key:
        github_client = GitHubAppClient(app_id=app_id, private_key_pem=private_key)

    github_scope_id: str | None = None
    if github_client is not None:
        try:
            meta = await github_client.get_installation(installation_id)
            account = meta.get("account", {})
            org_id_github = account.get("id")
            login = account.get("login", "")
            github_scope_id = str(org_id_github) if org_id_github else login
        except Exception as exc:
            logger.warning("Could not fetch installation metadata for delete: %s", exc)

    if github_scope_id is None:
        raise ValueError(f"Installation {installation_id} not found or GitHub API unavailable")

    stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.provider_scope_id == github_scope_id,
    )
    result = await db.execute(stmt)
    mapping = result.scalar_one_or_none()

    if mapping is None:
        raise ValueError(f"Installation {installation_id} is not connected to any ADP tenant")

    if mapping.org_id != caller_org_id:
        raise PermissionError(f"Installation {installation_id} belongs to a different ADP tenant")

    # 2. Revoke on GitHub
    if github_client is not None:
        try:
            await github_client.delete_installation(installation_id)
        except Exception as exc:
            logger.warning("GitHub installation delete API call failed: %s", exc)
            # Continue — we still clean up locally to avoid orphan state

    # 3. Remove local mapping
    del_stmt = sa_delete(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.provider_scope_id == github_scope_id,
        ChannelTenantMap.org_id == caller_org_id,
    )
    await db.execute(del_stmt)
    await db.commit()

    _cache_invalidate(installation_id)

    logger.info(
        "GitHub installation %d disconnected from tenant %s",
        installation_id,
        caller_org_id,
    )

    return DeleteConnectionResponse(deleted=True, installation_id=installation_id)


# ---------------------------------------------------------------------------
# GitHub App registration via manifest conversion (Issue #2593)
# ---------------------------------------------------------------------------

_APP_NAME_BASE = "adp-agent-platform"


def _get_environment() -> str:
    """Return the deployment environment (dev/staging/prod)."""
    return os.environ.get("ENVIRONMENT", "dev")


def _check_existing_app_secret() -> tuple[str, str] | None:
    """Check if a GitHub App is already registered in Secrets Manager.

    Returns (app_id, app_slug) if found, None otherwise.
    The secret paths match register-github-app.sh / webhook-ingress/infra/secrets.tf:
        adp/<env>/github-app/adp-agent-platform-id
    """
    import boto3
    from botocore.exceptions import ClientError

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")
    id_path = f"adp/{env}/github-app/adp-agent-platform-id"

    try:
        sm = boto3.client("secretsmanager", region_name=region)
        resp = sm.get_secret_value(SecretId=id_path)
        app_id = resp.get("SecretString", "")
        if app_id and len(app_id) > 0 and not _is_placeholder(app_id):
            # Derive slug from settings — no hardcoded fallback since App names
            # are owner-prefixed and deployment-specific (#2677).
            settings = get_settings()
            app_slug = settings.github_app_slug or _APP_NAME_BASE
            return (app_id, app_slug)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("ResourceNotFoundException", "InvalidRequestException"):
            return None
        logger.warning("Error checking existing app secret: %s", exc)
    except Exception as exc:
        logger.warning("Unexpected error checking existing app secret: %s", exc)
    return None


def _derive_app_name(*, owner: str | None, app_name: str | None = None) -> str:
    """Derive a unique GitHub App name for the manifest.

    GitHub App names are globally unique across ALL of GitHub. Mirrors the
    approach in register-github-app.sh:302-303 which prefixes the org name.

    Priority:
    1. Explicit app_name from the caller (UI-editable field) — use as-is.
    2. Owner-prefixed: "<owner>-adp-agent-platform" (e.g. "my-org-adp-agent-platform").
    3. Bare base name if no owner context (shouldn't happen in practice).

    Issue #2677: Fixes the hardcoded name collision on 2nd+ deployments.
    """
    if app_name:
        return app_name
    if owner:
        return f"{owner}-{_APP_NAME_BASE}"
    return _APP_NAME_BASE


def _build_app_manifest(
    *,
    webhook_url: str,
    callback_url: str,
    oauth_callback_url: str = "",
    setup_url: str = "",
    app_name: str = _APP_NAME_BASE,
    public: bool = False,
) -> dict[str, Any]:
    """Build the GitHub App manifest per the GitHub App Manifest spec.

    See: https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest

    Args:
        webhook_url: Webhook delivery URL (hook_attributes.url).
        callback_url: Manifest-conversion redirect URL (redirect_url).
        oauth_callback_url: User-authorization OAuth callback URL. When set,
            the App can perform "Sign in with GitHub" directly — no separate
            OAuth App needed (#2607).
        setup_url: Post-install redirect URL (setup_url). When set, GitHub
            redirects the browser here after an install/reconfigure, carrying
            the ?installation_id=&setup_action=&state= params so the
            install-callback can consume the nonce and attach the tenant.
            Without it GitHub leaves the user on github.com and the install
            never lands in the Connections UI (#2823).
        app_name: Globally unique GitHub App name. Defaults to the base name
            but should be owner-prefixed for multi-deployment uniqueness (#2677).
        public: Whether the App is publicly installable. Issue #2952 (D10).
    """
    manifest: dict[str, Any] = {
        "name": app_name,
        "url": f"https://github.com/apps/{app_name}",
        "hook_attributes": {
            "url": webhook_url,
            "active": True,
        },
        "redirect_url": callback_url,
        "public": public,
        "default_permissions": {
            "contents": "write",
            "issues": "write",
            "pull_requests": "write",
            "checks": "write",
            "metadata": "read",
        },
        "default_events": [
            "issues",
            "issue_comment",
            "pull_request",
            "pull_request_review",
            "pull_request_review_comment",
            "label",
        ],
    }

    # Issue #2607: Enable user-authorization OAuth so the App can perform
    # "Sign in with GitHub" (eliminates the separate OAuth App).
    if oauth_callback_url:
        manifest["callback_urls"] = [oauth_callback_url]
        manifest["request_oauth_on_install"] = False

    # Issue #2823: Emit setup_url so GitHub redirects the browser to the
    # install-callback after install/reconfigure. setup_on_update makes GitHub
    # also redirect after a re-configure — the recovery path for already
    # installed Apps.
    if setup_url:
        manifest["setup_url"] = setup_url
        manifest["setup_on_update"] = True

    return manifest


async def register_app_start(
    *,
    owner_type: str,
    org: str | None,
    app_name: str | None = None,
    visibility: str = "private",
    cognito_sub: str,
    user_id: str,
    db: AsyncSession,
) -> RegisterAppStartResponse:
    """Generate a manifest and state nonce for the GitHub App manifest conversion flow.

    Issue #2593: Platform-admin endpoint to register the deployment's GitHub App
    via GitHub's manifest conversion flow, replacing manual register-github-app.sh.

    First checks Secrets Manager for an existing App (prevents duplicate Apps).
    If an App is already registered, returns status='already_registered'.

    Args:
        owner_type: 'user' or 'org' — where to create the App on GitHub.
        org:        GitHub org login (required when owner_type='org').
        app_name:   Optional custom App name. When omitted, defaults to
                    '<owner>-adp-agent-platform' for global uniqueness (#2677).
        cognito_sub: Caller's Cognito subject (for nonce).
        user_id:    Caller's internal user ID (for nonce).
        db:         Database session.
    """
    # Check for already-registered App
    existing = _check_existing_app_secret()
    if existing is not None:
        app_id, app_slug = existing
        logger.info(
            "register-app-start: App already registered (id=%s, slug=%s)",
            app_id,
            app_slug,
        )
        return RegisterAppStartResponse(
            status="already_registered",
            app_slug=app_slug,
            app_id=app_id,
        )

    # Validate owner_type
    if owner_type not in ("user", "org"):
        raise HTTPException(
            status_code=400,
            detail="owner_type must be 'user' or 'org'",
        )
    if owner_type == "org" and not org:
        raise HTTPException(
            status_code=400,
            detail="org is required when owner_type='org'",
        )

    # Issue #2677: Derive a globally unique App name.
    # GitHub App names are unique across ALL of GitHub. Mirror the CLI script
    # (register-github-app.sh:302-303) which uses org-prefixed names.
    owner = org if owner_type == "org" else None
    resolved_app_name = _derive_app_name(owner=owner, app_name=app_name)

    # Build the POST URL based on owner_type
    if owner_type == "user":
        post_url = "https://github.com/settings/apps/new"
    else:
        post_url = f"https://github.com/organizations/{org}/settings/apps/new"

    # Determine the webhook URL from SSM or env
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    if not webhook_url:
        # Try SSM parameter — matches Terraform-created param name in
        # modules/agent-factory/webhook-ingress/infra/outputs.tf
        try:
            import boto3

            env = _get_environment()
            region = os.environ.get("AWS_REGION", "us-east-1")
            ssm = boto3.client("ssm", region_name=region)
            param = ssm.get_parameter(Name=f"/adp/{env}/webhook-ingress/endpoint")
            webhook_url = param["Parameter"]["Value"]
        except Exception as exc:
            logger.warning("Could not resolve webhook URL from SSM: %s", exc)
            webhook_url = ""

    # Issue #2674: fail fast with a clear error instead of building a manifest
    # with a blank hook_attributes.url that GitHub rejects opaquely.
    if not webhook_url:
        raise HTTPException(
            status_code=422,
            detail=(
                "Webhook endpoint not configured. Deploy webhook-ingress first "
                "(Terraform creates SSM /adp/<env>/webhook-ingress/endpoint), "
                "or set the WEBHOOK_URL environment variable."
            ),
        )

    # Build callback URL — the gateway endpoint that handles the code exchange
    settings = get_settings()
    base_url = settings.gateway_base_url or ""
    callback_url = f"{base_url}/api/admin/connections/github/app/register-callback"

    # Issue #2823: Post-install redirect. GitHub sends the browser here after an
    # install/reconfigure with ?installation_id=&setup_action=&state=<jti>, so
    # install-callback can consume the nonce and attach the tenant. Same base as
    # callback_url — do not invent a second base-URL source.
    setup_url = f"{base_url}/api/admin/connections/github/install-callback"

    # Issue #2607: Resolve the OAuth callback URL for the broker's login flow.
    # The broker sits behind API Gateway at /auth/github/callback. Same SSM
    # parameter that wire-github-app.sh uses.
    oauth_callback_url = ""
    try:
        import boto3

        env = _get_environment()
        region = os.environ.get("AWS_REGION", "us-east-1")
        ssm = boto3.client("ssm", region_name=region)
        param = ssm.get_parameter(Name=f"/adp/{env}/gateway/apigw-invoke-url")
        apigw_url = param["Parameter"]["Value"]
        if apigw_url:
            oauth_callback_url = f"{apigw_url}/auth/github/callback"
    except Exception as exc:
        logger.warning("Could not resolve OAuth callback URL from SSM: %s", exc)

    # Build the manifest
    # Issue #2952 (D10): visibility controls the App's public field.
    is_public = visibility == "public"
    manifest = _build_app_manifest(
        webhook_url=webhook_url,
        callback_url=callback_url,
        oauth_callback_url=oauth_callback_url,
        setup_url=setup_url,
        app_name=resolved_app_name,
        public=is_public,
    )

    # Generate state nonce (reuse magic_link_nonces table)
    jti = str(uuid.uuid4())
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=_NONCE_TTL_SECONDS)

    await store_nonce(
        jti=jti,
        provider=_PROVIDER_GITHUB_APP_REGISTER,
        provider_user_id=cognito_sub,
        channel_context=None,
        target_user_id=user_id,
        expires_at=expires_at,
        db=db,
    )

    logger.info(
        "register-app-start: manifest generated jti=%s user=%s owner_type=%s org=%s",
        jti,
        user_id,
        owner_type,
        org or "(personal)",
    )

    return RegisterAppStartResponse(
        status="ready",
        manifest=manifest,
        post_url=post_url,
        state=jti,
        suggested_app_name=resolved_app_name,
    )


async def register_app_callback(
    *,
    code: str,
    state: str,
    db: AsyncSession,
) -> str:
    """Exchange the GitHub manifest conversion code for App credentials and store them.

    Issue #2593: After the admin submits the manifest on GitHub and GitHub
    redirects back with a `code`, this function:
    1. Validates the state nonce (CSRF protection).
    2. POSTs to GitHub's /app-manifests/{code}/conversions endpoint.
    3. Stores the returned credentials in Secrets Manager at the shared paths.

    Returns the frontend redirect URL on success.

    Raises:
        NonceNotFoundError — state not in DB
        TokenExpiredError  — state expired
        NonceAlreadyConsumedError — state already used
        HTTPException      — GitHub API failure or storage failure
    """
    from sqlalchemy import select, update

    # 1. Validate + consume state nonce
    stmt = select(MagicLinkNonce).where(
        MagicLinkNonce.jti == state,
        MagicLinkNonce.provider == _PROVIDER_GITHUB_APP_REGISTER,
    )
    result = await db.execute(stmt)
    nonce = result.scalar_one_or_none()

    if nonce is None:
        raise NonceNotFoundError(f"State token not found: {state}")

    now = datetime.now(UTC)
    expires_at = nonce.expires_at
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < now:
        raise TokenExpiredError("State token has expired")

    if nonce.consumed_at is not None:
        raise NonceAlreadyConsumedError(f"State token already used: {state}")

    # Atomically consume (prevents races)
    consume_stmt = (
        update(MagicLinkNonce)
        .where(MagicLinkNonce.jti == state, MagicLinkNonce.consumed_at.is_(None))
        .values(consumed_at=now)
        .returning(MagicLinkNonce.jti)
    )
    consume_result = await db.execute(consume_stmt)
    consumed_jti = consume_result.scalar_one_or_none()
    if consumed_jti is None:
        raise NonceAlreadyConsumedError(f"State token already used (concurrent): {state}")
    await db.commit()

    logger.info("register-app-callback: nonce consumed jti=%s", state)

    # 2. Exchange code for App credentials via GitHub API
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"https://api.github.com/app-manifests/{code}/conversions",
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    if resp.status_code != 201:
        logger.error(
            "register-app-callback: GitHub conversions API returned %d: %s",
            resp.status_code,
            resp.text[:500],
        )
        raise HTTPException(
            status_code=502,
            detail="GitHub App manifest conversion failed. The code may have expired.",
        )

    data = resp.json()
    app_id = str(data.get("id", ""))
    app_slug = data.get("slug", "")
    pem = data.get("pem", "")
    client_id = data.get("client_id", "")
    client_secret = data.get("client_secret", "")
    webhook_secret = data.get("webhook_secret", "")

    if not app_id or not pem:
        logger.error("register-app-callback: missing id or pem in GitHub response")
        raise HTTPException(
            status_code=502,
            detail="GitHub returned incomplete App credentials.",
        )

    # 3. Store credentials in Secrets Manager at the shared paths.
    # Issue #2708: the OAuth write-through result tells us whether "Sign in with
    # GitHub" is actually wired. The broker derives its client_id/callback at
    # runtime (no more Lambda-env mutation), so this secret write is the only
    # login side effect the register flow has.
    login_enabled = await _store_app_credentials(
        app_id=app_id,
        app_slug=app_slug,
        pem=pem,
        client_id=client_id,
        client_secret=client_secret,
        webhook_secret=webhook_secret,
    )

    # Issue #2594: Invalidate the cached provider so subsequent requests use
    # the freshly-stored credentials without a pod restart.
    get_github_app_provider().invalidate()

    # Issue #2746: invalidate the public login_enabled cache so the login page's
    # "Sign in with GitHub" button flips to enabled promptly after registration
    # instead of staying disabled until the TTL expires.
    _invalidate_login_enabled_cache()

    logger.info(
        "register-app-callback: App registered successfully id=%s slug=%s login_enabled=%s",
        app_id,
        app_slug,
        login_enabled,
    )

    # Issue #2952 (Rule 1): If the App was registered against a GitHub org,
    # upsert an org-tenant shell (Organization + Tenant + Department + Team)
    # so members can later auto-join it. Feature-flagged by ORG_TENANT_AUTO_CREATE.
    owner = data.get("owner", {})
    if owner.get("type") == "Organization" and os.environ.get("ORG_TENANT_AUTO_CREATE", "false").lower() == "true":
        owner_login = owner.get("login", "")
        owner_id = str(owner.get("id", ""))
        if owner_login:
            await _upsert_org_tenant_shell(
                owner_login=owner_login,
                github_org_id=owner_id,
                github_app_id=app_id,
                db=db,
            )

    # Issue #2952 (D9): Chained onboarding redirect — send the admin directly
    # to GitHub's install page so they can pick repos immediately.
    if app_slug:
        return f"https://github.com/apps/{app_slug}/installations/new"

    # Fallback: return to connections page (should not happen with a valid slug).
    if login_enabled:
        return "/settings/connections?github_app=registered"
    return "/settings/connections?github_app=registered&login_enabled=false"


async def _store_app_credentials(
    *,
    app_id: str,
    app_slug: str,
    pem: str,
    client_id: str,
    client_secret: str,
    webhook_secret: str,
) -> bool:
    """Store GitHub App credentials in Secrets Manager at the shared paths.

    Paths match register-github-app.sh / webhook-ingress/infra/secrets.tf:
        adp/<env>/github-app/adp-agent-platform-id   → app_id
        adp/<env>/github-app/adp-agent-platform-key  → private key PEM

    Additional metadata (slug, client_id, client_secret, webhook_secret) is stored
    in a JSON secret alongside:
        adp/<env>/github-app/adp-agent-platform-meta → JSON blob

    Returns:
        Whether the broker OAuth write-through succeeded — i.e. whether "Sign in
        with GitHub" is now wired (Issue #2708). True when there was nothing to
        write (no client_id/secret) OR the write landed; False when the write was
        attempted but failed (e.g. AccessDenied). The App-creds writes themselves
        still raise on failure — only the login write-through is soft-failed.
    """
    import asyncio
    import json

    import boto3
    from botocore.exceptions import ClientError

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")

    def _store_sync() -> bool:
        sm = boto3.client("secretsmanager", region_name=region)

        # Legacy singleton paths (backward-compatible — existing reads unchanged)
        id_path = f"adp/{env}/github-app/adp-agent-platform-id"
        key_path = f"adp/{env}/github-app/adp-agent-platform-key"
        meta_path = f"adp/{env}/github-app/adp-agent-platform-meta"

        # Issue #2952 (D11): Per-app naming for new secrets (registry seed).
        # Existing secret reads use the singleton names above; only NEW writes
        # additionally go to per-app paths.
        per_app_id_path = f"adp/{env}/github-app/{app_slug}-id" if app_slug else None
        per_app_key_path = f"adp/{env}/github-app/{app_slug}-key" if app_slug else None
        per_app_meta_path = f"adp/{env}/github-app/{app_slug}-meta" if app_slug else None

        meta_payload = json.dumps(
            {
                "app_id": app_id,
                "app_slug": app_slug,
                "client_id": client_id,
                "client_secret": client_secret,
                "webhook_secret": webhook_secret,
            }
        )

        # Write to both legacy singleton and per-app paths
        paths_to_write = [
            (id_path, app_id, f"GitHub App ID for adp-agent-platform ({env})"),
            (key_path, pem, f"GitHub App private key for adp-agent-platform ({env})"),
            (meta_path, meta_payload, f"GitHub App metadata for adp-agent-platform ({env})"),
        ]
        if per_app_id_path:
            paths_to_write.append((per_app_id_path, app_id, f"GitHub App ID for {app_slug} ({env})"))
        if per_app_key_path:
            paths_to_write.append((per_app_key_path, pem, f"GitHub App private key for {app_slug} ({env})"))
        if per_app_meta_path:
            paths_to_write.append((per_app_meta_path, meta_payload, f"GitHub App metadata for {app_slug} ({env})"))

        for path, value, desc in paths_to_write:
            try:
                sm.create_secret(
                    Name=path,
                    Description=desc,
                    SecretString=value,
                    Tags=[
                        {"Key": "ManagedBy", "Value": "adp-gateway-register"},
                        {"Key": "AppSlug", "Value": app_slug},
                    ],
                )
                logger.info("Created secret: %s", path)
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code == "ResourceExistsException":
                    # Update existing secret
                    sm.put_secret_value(SecretId=path, SecretString=value)
                    logger.info("Updated existing secret: %s", path)
                else:
                    logger.error("Failed to store secret %s: %s", path, exc)
                    raise

        # Issue #2824: Write-through to the webhook-ingress secret so that
        # webhooks from a UI-registered App pass HMAC validation. The Lambda
        # validates signatures against adp/<env>/webhook-ingress/github-webhook-secret
        # (WEBHOOK_SECRET_ARN), which Terraform seeds with a placeholder and never
        # updates (ignore_changes = [secret_string]). Without this write the meta
        # secret holds the real webhook_secret but the ingress secret keeps the
        # placeholder — so every delivery fails with 401 invalid_signature.
        #
        # Terraform owns the secret's existence, so we put_secret_value directly
        # (no create fallback). A ResourceNotFound / any ClientError is a soft-fail
        # warning — the webhook path is dead until wired, but App registration and
        # login still succeed (same soft-fail contract as the OAuth write-through).
        if webhook_secret:
            ingress_secret_path = f"adp/{env}/webhook-ingress/github-webhook-secret"
            try:
                sm.put_secret_value(SecretId=ingress_secret_path, SecretString=webhook_secret)
                logger.info("Wrote webhook secret to ingress path: %s", ingress_secret_path)
            except ClientError as exc:
                logger.warning(
                    "Could not write webhook-ingress secret %s (webhook path not wired): %s",
                    ingress_secret_path,
                    exc,
                )

        # Issue #2607/#2708: Write-through to the broker's OAuth secret so
        # "Sign in with GitHub" works immediately after App registration
        # without a separate wire-github-app.sh step. The broker reads
        # client_id + client_secret from this secret at runtime (#2708).
        if not (client_id and client_secret):
            # Nothing to wire — treat as "no login side effect required".
            # (GitHub always returns client_id/secret from the manifest
            # conversion, so this is a defensive branch.)
            return True

        oauth_path = f"adp/{env}/cognito/github-oauth-credentials"
        oauth_payload = json.dumps({"client_id": client_id, "client_secret": client_secret})
        try:
            sm.create_secret(
                Name=oauth_path,
                Description=f"GitHub OAuth credentials for login broker ({env})",
                SecretString=oauth_payload,
                Tags=[
                    {"Key": "ManagedBy", "Value": "adp-gateway-register"},
                    {"Key": "AppSlug", "Value": app_slug},
                ],
            )
            logger.info("Created broker OAuth secret: %s", oauth_path)
            return True
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceExistsException":
                sm.put_secret_value(SecretId=oauth_path, SecretString=oauth_payload)
                logger.info("Updated broker OAuth secret: %s", oauth_path)
                return True
            # Non-fatal for App registration, but login is NOT wired. Issue
            # #2708: surface this to the caller instead of swallowing so the
            # UI can warn the operator rather than report silent success.
            logger.warning(
                "Could not write broker OAuth secret %s (login not wired): %s",
                oauth_path,
                exc,
            )
            return False

    return await asyncio.to_thread(_store_sync)


# ---------------------------------------------------------------------------
# Org-tenant shell upsert (Issue #2952)
# ---------------------------------------------------------------------------


def _slugify_org_id(login: str) -> str:
    """Slugify a GitHub login into a safe tenant/org ID.

    Matches the pattern in onboarding/handler.py:_slugify_tenant_id —
    lowercase, alphanumeric + hyphens, no leading/trailing hyphen, trim to 64.
    """
    import re

    s = login.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    s = s.strip("-")
    if len(s) > 64:
        s = s[:64].rstrip("-")
    return s


async def _upsert_org_tenant_shell(
    *,
    owner_login: str,
    github_org_id: str,
    github_app_id: str,
    db: AsyncSession,
) -> str | None:
    """Upsert an org-tenant shell: Organization + Tenant + Department + Team.

    Issue #2952 (Rule 1): When a platform admin registers a GitHub App against
    an org, create the tenant structure so members can later auto-join. Also
    called from install_callback for public-App installs by unknown orgs.

    Idempotent: if the org already exists (by slug id), updates github_org_id
    and github_app_id if previously unset and returns the existing id.

    Returns the tenant_id on success, None on failure.
    """
    from src.shared.models.base import new_uuid
    from src.shared.models.onboarding import Tenant
    from src.shared.models.organization import Department, Organization, Team

    tenant_id = _slugify_org_id(owner_login)
    if not tenant_id:
        logger.warning("org-tenant-shell: cannot slugify login=%s", owner_login)
        return None

    # Check if org already exists (idempotent)
    existing = await db.get(Organization, tenant_id)
    if existing is not None:
        # Update github_org_id/github_app_id if not already set
        changed = False
        if not existing.github_org_id and github_org_id:
            existing.github_org_id = github_org_id
            changed = True
        if not existing.github_app_id and github_app_id:
            existing.github_app_id = github_app_id
            changed = True
        if changed:
            await db.commit()
        logger.info(
            "org-tenant-shell: org %s already exists (idempotent), updated=%s",
            tenant_id,
            changed,
        )
        return tenant_id

    # Create all rows in a single transaction (pattern from approval.py:202-231)
    dept_id = new_uuid()
    team_id = new_uuid()

    org = Organization(
        id=tenant_id,
        name=owner_login,
        aws_accounts=[],
        role_mappings={},
        settings={},
        github_installation_ids=[],
        cognito_client_ids=[],
        github_org_id=github_org_id,
        github_app_id=github_app_id,
    )
    db.add(org)

    tenant = Tenant(
        id=tenant_id,
        display_name=owner_login,
    )
    db.add(tenant)

    dept = Department(
        id=dept_id,
        org_id=tenant_id,
        name="Default",
    )
    db.add(dept)

    team = Team(
        id=team_id,
        org_id=tenant_id,
        department_id=dept_id,
        name="Default",
    )
    db.add(team)

    await db.commit()
    logger.info(
        "org-tenant-shell: created org=%s github_org_id=%s github_app_id=%s",
        tenant_id,
        github_org_id,
        github_app_id,
    )
    return tenant_id


# ---------------------------------------------------------------------------
# GitHub App lifecycle (Issue #2595)
# ---------------------------------------------------------------------------


def invalidate_app_credentials_cache() -> None:
    """Invalidate the in-process cached App credentials.

    C2 contract (Issue #2594): when a lifecycle mutation (rotate-key, disconnect)
    changes the App secret in Secrets Manager, the gateway must stop using stale
    credentials. This clears any in-process cached state so the next runtime read
    fetches fresh values from Secrets Manager.

    When C2 lands, this function should be replaced by calling C2's
    `invalidate()` which handles cross-pod propagation as well.
    """
    # Clear the installation metadata cache — stale tokens would be minted from
    # the old key if we don't flush.
    _metadata_cache.clear()
    logger.info("App credentials cache invalidated")


def _check_login_enabled(sm: Any) -> bool:
    """Return whether the broker OAuth secret holds a real, non-placeholder client_id.

    Issue #2708: "Sign in with GitHub" is only usable when the broker's OAuth
    secret (adp/<env>/cognito/github-oauth-credentials) has been populated with a
    real client_id — Terraform seeds it with the literal "PLACEHOLDER". A cheap
    read; failures (missing secret, AccessDenied, malformed JSON) all resolve to
    False rather than raising, so status stays informative even when login isn't
    wired. ``sm`` is an already-constructed Secrets Manager client (reused from
    the caller so we don't create a second one).
    """
    import json

    from botocore.exceptions import ClientError

    env = _get_environment()
    oauth_path = f"adp/{env}/cognito/github-oauth-credentials"

    try:
        resp = sm.get_secret_value(SecretId=oauth_path)
        raw = resp.get("SecretString", "")
        if not raw:
            return False
        data = json.loads(raw)
        client_id = (data.get("client_id") or "").strip()
        return bool(client_id) and client_id != "PLACEHOLDER" and not _is_placeholder(client_id)
    except (ClientError, json.JSONDecodeError, TypeError) as exc:
        logger.info("login_enabled check: could not read %s (login treated as not wired): %s", oauth_path, exc)
        return False


def _invalidate_login_enabled_cache() -> None:
    """Clear the cached login_enabled value (Issue #2746).

    Called after the App is registered so the login page flips to enabled
    promptly instead of waiting out the TTL.
    """
    global _LOGIN_ENABLED_CACHE
    _LOGIN_ENABLED_CACHE = None


async def is_github_login_enabled() -> bool:
    """Public, cached read of the login_enabled signal (Issue #2746).

    Called from the UNAUTHENTICATED /auth/login-options endpoint, so it must be
    cheap and never raise. Reuses the single-source-of-truth check
    ``_check_login_enabled`` and caches the result for 60s to bound Secrets
    Manager reads. On any error, returns the last cached value if one exists,
    else False (fail-closed on the backend so the UI can fail-open safely).
    """
    global _LOGIN_ENABLED_CACHE
    import asyncio

    import boto3

    now = time.monotonic()
    if _LOGIN_ENABLED_CACHE is not None and now < _LOGIN_ENABLED_CACHE[0]:
        return _LOGIN_ENABLED_CACHE[1]

    region = os.environ.get("AWS_REGION", "us-east-1")
    try:

        def _read() -> bool:
            sm = boto3.client("secretsmanager", region_name=region)
            return _check_login_enabled(sm)

        value = await asyncio.to_thread(_read)
    except Exception as exc:
        logger.info("is_github_login_enabled: check failed (%s); serving cached/default value", exc)
        if _LOGIN_ENABLED_CACHE is not None:
            return _LOGIN_ENABLED_CACHE[1]
        return False

    _LOGIN_ENABLED_CACHE = (now + _LOGIN_ENABLED_TTL_SECONDS, value)
    return value


async def get_app_status() -> AppStatusResponse:
    """Return the registration status of the deployment's GitHub App.

    Reads from Secrets Manager. Never exposes the private key or client secret.
    """
    import json

    import boto3
    from botocore.exceptions import ClientError

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")

    id_path = f"adp/{env}/github-app/adp-agent-platform-id"
    meta_path = f"adp/{env}/github-app/adp-agent-platform-meta"

    try:
        sm = boto3.client("secretsmanager", region_name=region)

        # Check if the App ID secret exists
        try:
            id_resp = sm.get_secret_value(SecretId=id_path)
            app_id = id_resp.get("SecretString", "")
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code in ("ResourceNotFoundException", "InvalidRequestException"):
                return AppStatusResponse(registered=False)
            if error_code in ("AccessDeniedException", "AccessDenied"):
                logger.warning(
                    "get_app_status: AccessDenied reading %s — IAM policy may be missing adp/*/github-app/* grant on the gateway role",
                    id_path,
                )
                raise HTTPException(
                    status_code=503,
                    detail="Unable to determine App registration status — access denied reading secrets. "
                    "The gateway IAM role may need the adp/*/github-app/* grant.",
                ) from exc
            raise

        if not app_id or _is_placeholder(app_id):
            return AppStatusResponse(registered=False)

        # Read metadata for slug and owner info
        app_slug: str | None = None
        owner_type: str | None = None
        created_at: str | None = None

        try:
            meta_resp = sm.describe_secret(SecretId=meta_path)
            created_at_dt = meta_resp.get("CreatedDate")
            if created_at_dt:
                created_at = created_at_dt.isoformat()
        except ClientError:
            pass

        try:
            meta_val_resp = sm.get_secret_value(SecretId=meta_path)
            meta_str = meta_val_resp.get("SecretString", "")
            if meta_str:
                meta_data = json.loads(meta_str)
                app_slug = meta_data.get("app_slug")
        except (ClientError, json.JSONDecodeError):
            pass

        # Fall back to settings for slug if not in meta
        if not app_slug:
            settings = get_settings()
            app_slug = getattr(settings, "github_app_slug", None) or None

        # Issue #2708: report whether "Sign in with GitHub" is actually wired
        # by checking the broker OAuth secret holds a real client_id.
        login_enabled = _check_login_enabled(sm)

        return AppStatusResponse(
            registered=True,
            install_ready=bool(app_slug),
            login_enabled=login_enabled,
            app_id=app_id,
            app_slug=app_slug,
            owner_type=owner_type,
            created_at=created_at,
        )

    except HTTPException:
        raise
    except Exception as exc:
        logger.error("get_app_status failed: %s", exc)
        raise HTTPException(status_code=500, detail="Failed to retrieve App status") from exc


async def rotate_app_key() -> RotateKeyResponse:
    """Rotate the GitHub App's private key.

    Calls the GitHub API to generate a new private key, stores it in Secrets
    Manager (overwriting the old key), and invalidates the credentials cache so
    subsequent runtime reads use the new key.

    The GitHub API endpoint POST /app/installations is not available for key
    rotation — instead we use POST /app/hook/config (for webhook secret) or the
    private-key-specific endpoint. GitHub's App API does not support programmatic
    key rotation directly; the platform admin must generate a new key via the
    GitHub UI and re-register. However, if the App was created via manifest flow,
    we can guide the admin.

    For now: we attempt to call the GitHub API for key creation. If that endpoint
    is not available, we return guidance to use the manifest flow.
    """
    import asyncio

    import boto3
    from botocore.exceptions import ClientError

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")
    id_path = f"adp/{env}/github-app/adp-agent-platform-id"
    key_path = f"adp/{env}/github-app/adp-agent-platform-key"

    # Verify App is registered
    try:
        sm = boto3.client("secretsmanager", region_name=region)
        id_resp = sm.get_secret_value(SecretId=id_path)
        app_id = id_resp.get("SecretString", "")
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("ResourceNotFoundException", "InvalidRequestException"):
            raise HTTPException(
                status_code=404,
                detail="No GitHub App registered. Register one first.",
            ) from exc
        raise HTTPException(status_code=500, detail="Failed to read App credentials") from exc

    if not app_id or _is_placeholder(app_id):
        raise HTTPException(status_code=404, detail="No GitHub App registered. Register one first.")

    # Read current private key to authenticate as the App
    try:
        key_resp = sm.get_secret_value(SecretId=key_path)
        current_key = key_resp.get("SecretString", "")
    except ClientError as exc:
        raise HTTPException(status_code=500, detail="Failed to read current App private key") from exc

    if not current_key or _is_placeholder(current_key):
        raise HTTPException(status_code=500, detail="Current App private key is empty")

    # Attempt GitHub API key rotation: POST /app/hook/deliveries won't work,
    # but GitHub does support creating a new private key for the app via the API
    # at POST /app/keys (undocumented / restricted). The more reliable path is
    # the direct REST call that the GitHub UI makes.
    client = GitHubAppClient(app_id=app_id, private_key_pem=current_key)
    new_key: str | None = None

    try:
        # GitHub exposes key creation at POST /app/keys (returns new PEM)
        resp = await client._http_client.post(
            "/app/keys",
            headers=client._auth_headers(),
        )
        if resp.status_code == 201:
            data = resp.json()
            new_key = data.get("pem", "")
    except Exception as exc:
        logger.warning("GitHub /app/keys endpoint not available: %s", exc)

    if not new_key:
        # Fallback: GitHub doesn't expose a public key-rotation API for all Apps.
        # Guide the admin to use the GitHub UI.
        raise HTTPException(
            status_code=422,
            detail=(
                "Programmatic key rotation is not available for this App. "
                "Generate a new private key from the GitHub App settings page "
                f"(https://github.com/settings/apps → App ID {app_id} → Private keys → Generate), "
                "then re-register with the new key."
            ),
        )

    # Store the new key in Secrets Manager
    def _store_new_key() -> None:
        sm_client = boto3.client("secretsmanager", region_name=region)
        sm_client.put_secret_value(SecretId=key_path, SecretString=new_key)

    await asyncio.to_thread(_store_new_key)

    # Invalidate cached credentials so runtime picks up the new key
    invalidate_app_credentials_cache()

    logger.info("rotate_app_key: key rotated for app_id=%s", app_id)

    return RotateKeyResponse(
        rotated=True,
        app_id=app_id,
        message="Private key rotated successfully. New key is active immediately.",
    )


async def disconnect_app() -> DisconnectAppResponse:
    """Disconnect (deregister) the GitHub App from this deployment.

    Deletes/blanks the App secrets in Secrets Manager and invalidates the
    credentials cache. Does NOT delete the App on GitHub (that requires a
    manual action in the GitHub UI).

    Existing per-tenant installation_id connections are NOT removed — they
    are surfaced as "will stop working" via the affected_installations count.
    """
    import asyncio

    import boto3
    from botocore.exceptions import ClientError

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")

    id_path = f"adp/{env}/github-app/adp-agent-platform-id"
    key_path = f"adp/{env}/github-app/adp-agent-platform-key"
    meta_path = f"adp/{env}/github-app/adp-agent-platform-meta"

    # Verify App is registered
    try:
        sm = boto3.client("secretsmanager", region_name=region)
        id_resp = sm.get_secret_value(SecretId=id_path)
        app_id = id_resp.get("SecretString", "")
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("ResourceNotFoundException", "InvalidRequestException"):
            raise HTTPException(
                status_code=404,
                detail="No GitHub App registered. Nothing to disconnect.",
            ) from exc
        raise HTTPException(status_code=500, detail="Failed to read App credentials") from exc

    if not app_id or _is_placeholder(app_id):
        raise HTTPException(status_code=404, detail="No GitHub App registered. Nothing to disconnect.")

    # Count affected installations (ChannelTenantMap rows with provider="github")
    affected_count = 0
    try:
        from sqlalchemy import func, select

        from src.shared.database import get_session_factory
        from src.shared.models.vault import ChannelTenantMap

        factory = get_session_factory()
        async with factory() as db:
            stmt = select(func.count()).where(ChannelTenantMap.provider == "github")
            result = await db.execute(stmt)
            affected_count = result.scalar() or 0
    except Exception as exc:
        logger.warning("Could not count affected installations: %s", exc)

    # Delete the App secrets from Secrets Manager
    def _delete_secrets() -> None:
        sm_client = boto3.client("secretsmanager", region_name=region)
        for path in [id_path, key_path, meta_path]:
            try:
                sm_client.delete_secret(
                    SecretId=path,
                    ForceDeleteWithoutRecovery=True,
                )
                logger.info("Deleted secret: %s", path)
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code == "ResourceNotFoundException":
                    logger.info("Secret already absent: %s", path)
                else:
                    logger.error("Failed to delete secret %s: %s", path, exc)
                    raise

    await asyncio.to_thread(_delete_secrets)

    # Invalidate cached credentials
    invalidate_app_credentials_cache()

    logger.info("disconnect_app: app_id=%s disconnected, %d installations affected", app_id, affected_count)

    return DisconnectAppResponse(
        disconnected=True,
        app_id=app_id,
        message=(
            "GitHub App disconnected from this deployment. "
            "The App still exists on GitHub — delete it manually from GitHub Settings if desired. "
            f"{affected_count} tenant installation(s) will stop working until a new App is registered."
        ),
        affected_installations=affected_count,
    )
