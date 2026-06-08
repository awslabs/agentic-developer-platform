"""Business logic for the connections module.

Issue #465: GitHub App install-start, install-callback, list, and delete.

Design notes:
- State nonces reuse the existing magic_link_nonces table with provider="github_install".
- No new tables; installation-to-tenant mapping is owned by the admin identity service
  via POST /api/admin/identity/organizations.
- GitHub metadata (account_login, repo count) is fetched live from the GitHub API and
  cached in-process for ~5 minutes to avoid hammering the API on list calls.
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

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
)

logger = logging.getLogger(__name__)

_PROVIDER_GITHUB_INSTALL = "github_install"
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
    caller_user_id: str,
    caller_org_id: str,
    db: AsyncSession,
    github_client: GitHubAppClient | None = None,
) -> dict[str, Any]:
    """Validate state nonce, consume it, and attach the installation to the caller's tenant.

    Returns a dict with keys:
        success          — bool
        installation_id  — int
        account_login    — str
        account_type     — str
        error_code       — str | None  (set on failure)
        error_message    — str | None

    Raises:
        ValueError  — nonce validation failure (expired, consumed, wrong user)
        PermissionError — cross-tenant ownership conflict
    """
    from sqlalchemy import select, update

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

    # 2. Cross-user replay protection
    if nonce.target_user_id is not None and nonce.target_user_id != caller_user_id:
        logger.warning(
            "GitHub install-callback cross-user replay attempt jti=%s target=%s caller=%s",
            state,
            nonce.target_user_id,
            caller_user_id,
        )
        raise TargetUserMismatchError("State token was issued for a different user")

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

    if github_client is not None:
        try:
            meta = await github_client.get_installation(installation_id)
            account = meta.get("account", {})
            account_login = account.get("login", "unknown")
            account_type = account.get("type", "Organization")
            github_org_id = account.get("id")
            _cache_set(installation_id, meta)
        except Exception as exc:
            logger.warning("Could not fetch GitHub installation metadata: %s", exc)

    # 5. Branch: Organization vs User (personal) account
    if account_type == "Organization":
        # Delegate org creation/attachment to the admin identity service.
        # We call the service layer directly to stay within the same transaction boundary.
        # Cross-tenant check: if the org is already claimed by a different ADP tenant, reject.
        try:
            await _attach_org_installation(
                installation_id=installation_id,
                github_org_id=github_org_id,
                github_org_login=account_login,
                caller_org_id=caller_org_id,
                db=db,
            )
        except PermissionError:
            raise

        # Issue #719: Populate organizations.github_installation_ids so that
        # future users from this org are matched to this tenant automatically.
        await _append_installation_id_to_org(
            installation_id=installation_id,
            caller_org_id=caller_org_id,
            db=db,
        )
    else:
        # Personal account — attach to adp-default free-tier tenant (issue #466)
        from .adp_default import attach_to_adp_default

        await attach_to_adp_default(
            installation_id=installation_id,
            account_login=account_login,
            github_account_id=github_org_id,
            caller_user_id=caller_user_id,
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
) -> dict[str, Any]:
    """Build the metadata dict stored on ChannelTenantMap at install time."""
    return {
        "installation_id": installation_id,
        "account_login": account_login,
        "account_type": account_type,
        "repository_selection": repository_selection,
        "repository_count": repository_count,
    }


async def _attach_org_installation(
    *,
    installation_id: int,
    github_org_id: int | None,
    github_org_login: str,
    caller_org_id: str,
    db: AsyncSession,
) -> None:
    """Attach a GitHub org installation to the caller's ADP tenant.

    Checks for cross-tenant ownership conflicts via the ChannelTenantMap table.
    On conflict, raises PermissionError with a user-actionable message.
    On first install, upserts a ChannelTenantMap row.
    """
    from sqlalchemy import select

    from src.shared.models.vault import ChannelTenantMap

    # Check if this GitHub org is already mapped to an ADP tenant
    if github_org_id is not None:
        scope_id = str(github_org_id)
    else:
        scope_id = github_org_login

    stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == "github",
        ChannelTenantMap.provider_scope_id == scope_id,
    )
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()

    if existing is not None:
        if existing.org_id != caller_org_id:
            raise PermissionError(
                f"GitHub org '{github_org_login}' is already connected to another ADP tenant. Contact support if you believe this is an error."
            )
        # Already mapped to this tenant — update metadata (idempotent re-install)
        existing.install_metadata = _build_install_metadata(
            installation_id=installation_id,
            account_login=github_org_login,
            account_type="Organization",
        )
        await db.commit()
        logger.info(
            "GitHub org %s re-installed installation_id=%d tenant=%s",
            github_org_login,
            installation_id,
            caller_org_id,
        )
        return

    # New mapping — record it with metadata
    mapping = ChannelTenantMap(
        provider="github",
        provider_scope_id=scope_id,
        org_id=caller_org_id,
        install_metadata=_build_install_metadata(
            installation_id=installation_id,
            account_login=github_org_login,
            account_type="Organization",
        ),
    )
    db.add(mapping)
    await db.commit()
    logger.info(
        "GitHub org %s (installation_id=%d) attached to tenant %s",
        github_org_login,
        installation_id,
        caller_org_id,
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
        repo_count = int(md.get("repository_count") or 0)

        configure_url = f"https://github.com/settings/installations/{install_id}"

        connections.append(
            GitHubConnectionItem(
                provider="github",
                installation_id=install_id,
                account_login=account_login,
                account_type=account_type,
                repository_selection=repo_selection,
                repository_count=repo_count,
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
