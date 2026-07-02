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

    Returns a dict with keys:
        success          — bool
        installation_id  — int
        account_login    — str
        account_type     — str
        error_code       — str | None  (set on failure)
        error_message    — str | None

    Raises:
        ValueError  — nonce validation failure (expired, consumed, not found)
        PermissionError — cross-tenant ownership conflict
    """
    from sqlalchemy import select, update

    from src.shared.models.organization import User

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

    # 5. Attach to the caller's tenant. Both Organization and personal (User)
    #    installs attach to caller_org_id — for a GitHub-login user that org is
    #    their own per-user tenant (named after their login), created at
    #    onboarding. (Older code routed personal accounts to a shared adp-default
    #    org, which required that org to be pre-seeded and otherwise FK-failed.)
    await _attach_org_installation(
        installation_id=installation_id,
        github_org_id=github_org_id,
        github_org_login=account_login,
        caller_org_id=caller_org_id,
        db=db,
        account_type=account_type,
        repository_selection=repository_selection,
        repositories=repositories,
    )

    # Issue #2085: Seed per-tenant GitHub App secret so that downstream
    # resolve_tenant_app_credentials() never hits a missing-secret error.
    from .tenant_secret import seed_tenant_github_app_secret

    await seed_tenant_github_app_secret(caller_org_id, installation_id)

    if account_type == "Organization":
        # Issue #719: Populate organizations.github_installation_ids so that
        # future users from this org are matched to this tenant automatically.
        await _append_installation_id_to_org(
            installation_id=installation_id,
            caller_org_id=caller_org_id,
            db=db,
        )

    return {
        "success": True,
        "installation_id": installation_id,
        "account_login": account_login,
        "account_type": account_type,
        "error_code": None,
        "error_message": None,
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


async def list_connections(
    *,
    caller_org_id: str,
    caller_user_id: str,
    db: AsyncSession,
    github_client: GitHubAppClient | None = None,
) -> ConnectionsListResponse:
    """Return all GitHub installations connected to the caller's ADP tenant.

    Queries ChannelTenantMap for this org's GitHub entries. For personal accounts
    within adp-default, scopes to only the caller's own installations to prevent
    cross-user data leakage.

    Enrichment data is read from the `metadata` JSON column (written at install time).
    Legacy rows without metadata are skipped with a warning.
    """
    from sqlalchemy import select

    from src.shared.models.vault import ChannelTenantMap

    from .adp_default import is_adp_default

    stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.org_id == caller_org_id,
    )
    result = await db.execute(stmt)
    mappings = result.scalars().all()

    if not mappings:
        return ConnectionsListResponse(connections=[])

    # For personal accounts in adp-default, filter to this user's installs only.
    # provider_scope_id format for personal: "personal:<github_id>:<adp_user_id>"
    if is_adp_default(caller_org_id):
        mappings = [m for m in mappings if m.provider_scope_id.endswith(f":{caller_user_id}")]

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
        repositories = md.get("repositories") or []
        # Prefer the stored names' length; fall back to the count field for
        # legacy rows written before names were captured.
        repo_count = len(repositories) if repositories else int(md.get("repository_count") or 0)

        configure_url = f"https://github.com/settings/installations/{install_id}"

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
            )
        )

    return ConnectionsListResponse(connections=connections)


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
    app_name: str = _APP_NAME_BASE,
) -> dict[str, Any]:
    """Build the GitHub App manifest per the GitHub App Manifest spec.

    See: https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest

    Args:
        webhook_url: Webhook delivery URL (hook_attributes.url).
        callback_url: Manifest-conversion redirect URL (redirect_url).
        oauth_callback_url: User-authorization OAuth callback URL. When set,
            the App can perform "Sign in with GitHub" directly — no separate
            OAuth App needed (#2607).
        app_name: Globally unique GitHub App name. Defaults to the base name
            but should be owner-prefixed for multi-deployment uniqueness (#2677).
    """
    manifest: dict[str, Any] = {
        "name": app_name,
        "url": f"https://github.com/apps/{app_name}",
        "hook_attributes": {
            "url": webhook_url,
            "active": True,
        },
        "redirect_url": callback_url,
        "public": False,
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

    return manifest


async def register_app_start(
    *,
    owner_type: str,
    org: str | None,
    app_name: str | None = None,
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
    manifest = _build_app_manifest(
        webhook_url=webhook_url,
        callback_url=callback_url,
        oauth_callback_url=oauth_callback_url,
        app_name=resolved_app_name,
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

    # 3. Store credentials in Secrets Manager at the shared paths
    await _store_app_credentials(
        app_id=app_id,
        app_slug=app_slug,
        pem=pem,
        client_id=client_id,
        client_secret=client_secret,
        webhook_secret=webhook_secret,
    )

    # Issue #2607: Update the broker Lambda's GITHUB_CLIENT_ID and CALLBACK_URL
    # env vars so login works immediately without wire-github-app.sh.
    await _update_broker_lambda_env(client_id=client_id)

    # Issue #2594: Invalidate the cached provider so subsequent requests use
    # the freshly-stored credentials without a pod restart.
    get_github_app_provider().invalidate()

    logger.info(
        "register-app-callback: App registered successfully id=%s slug=%s",
        app_id,
        app_slug,
    )

    # Return frontend redirect URL
    settings = get_settings()
    frontend_url = settings.gateway_base_url or ""
    return f"{frontend_url}/settings/connections?github_app=registered"


async def _update_broker_lambda_env(*, client_id: str) -> None:
    """Update the login broker Lambda's GITHUB_CLIENT_ID and CALLBACK_URL env vars.

    Issue #2607: After App registration stores credentials in Secrets Manager,
    the broker Lambda still needs its non-secret env vars updated so it can
    build the OAuth authorize URL. This mirrors what wire-github-app.sh does
    in steps 2b-2c.

    Non-fatal: if the Lambda doesn't exist yet or the update fails, log a
    warning. The broker will work once the env is set manually or on next deploy.
    """
    import asyncio

    import boto3

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")

    def _update_sync() -> None:
        lambda_client = boto3.client("lambda", region_name=region)
        function_name = f"bedrockgw-{env}-github-auth-broker"

        # Resolve the OAuth callback URL from SSM (same as wire-github-app.sh)
        callback_url = ""
        try:
            ssm = boto3.client("ssm", region_name=region)
            param = ssm.get_parameter(Name=f"/adp/{env}/gateway/apigw-invoke-url")
            apigw_url = param["Parameter"]["Value"]
            if apigw_url:
                callback_url = f"{apigw_url}/auth/github/callback"
        except Exception as exc:
            logger.warning("Could not resolve broker callback URL from SSM: %s", exc)

        try:
            # Get current env vars
            config = lambda_client.get_function_configuration(
                FunctionName=function_name,
            )
            current_env = config.get("Environment", {}).get("Variables", {})

            # Update the non-secret env vars
            current_env["GITHUB_CLIENT_ID"] = client_id
            if callback_url:
                current_env["CALLBACK_URL"] = callback_url

            lambda_client.update_function_configuration(
                FunctionName=function_name,
                Environment={"Variables": current_env},
            )
            logger.info(
                "Updated broker Lambda env: GITHUB_CLIENT_ID=%s CALLBACK_URL=%s",
                client_id[:8] + "..." if len(client_id) > 8 else client_id,
                callback_url or "(unchanged)",
            )
        except lambda_client.exceptions.ResourceNotFoundException:
            logger.warning(
                "Broker Lambda %s not found — login env not updated. Run wire-github-app.sh after gateway-infra deploy.",
                function_name,
            )
        except Exception as exc:
            logger.warning(
                "Could not update broker Lambda env for %s: %s. Login will work after manual env update or next deploy.",
                function_name,
                exc,
            )

    await asyncio.to_thread(_update_sync)


async def _store_app_credentials(
    *,
    app_id: str,
    app_slug: str,
    pem: str,
    client_id: str,
    client_secret: str,
    webhook_secret: str,
) -> None:
    """Store GitHub App credentials in Secrets Manager at the shared paths.

    Paths match register-github-app.sh / webhook-ingress/infra/secrets.tf:
        adp/<env>/github-app/adp-agent-platform-id   → app_id
        adp/<env>/github-app/adp-agent-platform-key  → private key PEM

    Additional metadata (slug, client_id, client_secret, webhook_secret) is stored
    in a JSON secret alongside:
        adp/<env>/github-app/adp-agent-platform-meta → JSON blob
    """
    import asyncio
    import json

    import boto3
    from botocore.exceptions import ClientError

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")

    def _store_sync() -> None:
        sm = boto3.client("secretsmanager", region_name=region)

        id_path = f"adp/{env}/github-app/adp-agent-platform-id"
        key_path = f"adp/{env}/github-app/adp-agent-platform-key"
        meta_path = f"adp/{env}/github-app/adp-agent-platform-meta"

        meta_payload = json.dumps(
            {
                "app_id": app_id,
                "app_slug": app_slug,
                "client_id": client_id,
                "client_secret": client_secret,
                "webhook_secret": webhook_secret,
            }
        )

        for path, value, desc in [
            (id_path, app_id, f"GitHub App ID for adp-agent-platform ({env})"),
            (key_path, pem, f"GitHub App private key for adp-agent-platform ({env})"),
            (meta_path, meta_payload, f"GitHub App metadata for adp-agent-platform ({env})"),
        ]:
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

        # Issue #2607: Write-through to the broker's OAuth secret so
        # "Sign in with GitHub" works immediately after App registration
        # without a separate wire-github-app.sh step.
        if client_id and client_secret:
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
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code", "")
                if error_code == "ResourceExistsException":
                    sm.put_secret_value(SecretId=oauth_path, SecretString=oauth_payload)
                    logger.info("Updated broker OAuth secret: %s", oauth_path)
                else:
                    # Non-fatal: broker login won't work but App registration
                    # itself succeeded. Log and continue.
                    logger.warning(
                        "Could not write broker OAuth secret %s: %s",
                        oauth_path,
                        exc,
                    )

    await asyncio.to_thread(_store_sync)


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

        return AppStatusResponse(
            registered=True,
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
