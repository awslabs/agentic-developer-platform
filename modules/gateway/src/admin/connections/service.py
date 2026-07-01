"""Business logic for the connections module.

Issue #465: GitHub App install-start, install-callback, list, and delete.
Issue #2593: Platform-admin GitHub App registration via manifest conversion flow.

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

from .github_client import GitHubAppClient
from .schemas import (
    ConnectionsListResponse,
    DeleteConnectionResponse,
    GitHubConnectionItem,
    InstallStartResponse,
    RegisterAppStartResponse,
)

logger = logging.getLogger(__name__)

_PROVIDER_GITHUB_INSTALL = "github_install"
_PROVIDER_GITHUB_APP_REGISTER = "github_app_register"
_NONCE_TTL_SECONDS = 900  # 15 minutes

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

    Reads the BG_GITHUB_APP_SLUG env var (Settings.github_app_slug). This is
    deployment config — there is NO hardcoded fallback on purpose. A blank value
    means the App identity was never wired into the gateway; returning a guessed
    slug would silently point the UI's install button at the wrong App (the user
    installs App X while the gateway can only authenticate as App Y → the install
    never attaches, and nothing shows in the UI). Fail loudly instead.
    """
    settings = get_settings()
    slug = getattr(settings, "github_app_slug", "") or ""
    if not slug:
        raise HTTPException(
            status_code=503,
            detail=(
                "GitHub App not configured: set BG_GITHUB_APP_SLUG (and "
                "BG_GITHUB_APP_ID / BG_GITHUB_APP_PRIVATE_KEY) on the gateway. "
                "Run register-github-app.sh / wire-github-app.sh for this deployment."
            ),
        )
    return slug


def _get_github_app_credentials() -> tuple[str, str]:
    """Return (app_id, private_key_pem) from settings.

    Reads:
        BG_GITHUB_APP_ID         — numeric App ID
        BG_GITHUB_APP_PRIVATE_KEY — PEM-encoded RSA private key
    """
    settings = get_settings()
    app_id: str = getattr(settings, "github_app_id", "") or ""
    private_key: str = getattr(settings, "github_app_private_key", "") or ""
    return app_id, private_key


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
        if app_id and len(app_id) > 0:
            # Derive slug from settings if available, else from convention
            settings = get_settings()
            app_slug = settings.github_app_slug or "adp-agent-platform"
            return (app_id, app_slug)
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("ResourceNotFoundException", "InvalidRequestException"):
            return None
        logger.warning("Error checking existing app secret: %s", exc)
    except Exception as exc:
        logger.warning("Unexpected error checking existing app secret: %s", exc)
    return None


def _build_app_manifest(
    *,
    webhook_url: str,
    callback_url: str,
) -> dict[str, Any]:
    """Build the GitHub App manifest per the GitHub App Manifest spec.

    See: https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest
    """
    return {
        "name": "adp-agent-platform",
        "url": "https://github.com/apps/adp-agent-platform",
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


async def register_app_start(
    *,
    owner_type: str,
    org: str | None,
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

    # Build the POST URL based on owner_type
    if owner_type == "user":
        post_url = "https://github.com/settings/apps/new"
    else:
        post_url = f"https://github.com/organizations/{org}/settings/apps/new"

    # Determine the webhook URL from SSM or env
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    if not webhook_url:
        # Try SSM parameter (same pattern as register-github-app.sh)
        try:
            import boto3

            env = _get_environment()
            region = os.environ.get("AWS_REGION", "us-east-1")
            ssm = boto3.client("ssm", region_name=region)
            param = ssm.get_parameter(Name=f"/adp/{env}/webhook-ingress/webhook-url")
            webhook_url = param["Parameter"]["Value"]
        except Exception as exc:
            logger.warning("Could not resolve webhook URL from SSM: %s", exc)
            webhook_url = ""

    # Build callback URL — the gateway endpoint that handles the code exchange
    settings = get_settings()
    base_url = settings.gateway_base_url or ""
    callback_url = f"{base_url}/api/admin/connections/github/app/register-callback"

    # Build the manifest
    manifest = _build_app_manifest(
        webhook_url=webhook_url,
        callback_url=callback_url,
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

    logger.info(
        "register-app-callback: App registered successfully id=%s slug=%s",
        app_id,
        app_slug,
    )

    # Return frontend redirect URL
    settings = get_settings()
    frontend_url = settings.gateway_base_url or ""
    return f"{frontend_url}/settings/connections?github_app=registered"


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

    await asyncio.to_thread(_store_sync)
