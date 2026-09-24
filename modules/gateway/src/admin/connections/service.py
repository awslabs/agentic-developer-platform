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

import asyncio
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
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

from .bot_identity import seed_bot_identity
from .github_app_provider import get_github_app_provider
from .github_client import GitHubAppClient
from .schemas import (
    AppStatusResponse,
    ConnectionsListResponse,
    ConnectionVerification,
    DeleteConnectionResponse,
    DisconnectAppResponse,
    GitHubConnectionItem,
    InstallStartResponse,
    PlatformVerification,
    RegisterAppStartResponse,
    RotateKeyResponse,
)

logger = logging.getLogger(__name__)

_PROVIDER_GITHUB_INSTALL = "github_install"
_PROVIDER_GITHUB_APP_REGISTER = "github_app_register"
_NONCE_TTL_SECONDS = 900  # 15 minutes

# Roles that may replace the deployment's SHARED GitHub App / webhook / sign-in
# secrets (#5664). Mirrors the claim values `auth_service` maps to `is_admin`, but
# read from the database: the setup callback is a browser redirect with no token,
# so there is no claim available to trust.
_PLATFORM_ADMIN_ROLES: frozenset[str] = frozenset({"platform_admin", "admin"})

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
# Issue #4017: the deployment's expected GitHub App configuration.
#
# SINGLE SOURCE OF TRUTH. These were previously duplicated between
# _build_app_manifest (what we ASK GitHub for) and register_app_manual's inline
# validator (what we CHECK GitHub has) — plus a third copy in
# register-github-app.sh which even carries a "mirrored from _build_app_manifest()"
# comment. A drift checker built on a second copy can disagree with the manifest,
# which would report drift on an App that is exactly what we asked for.
#
# The shell script's copy is out of Python's reach; these two are now unified.
# ---------------------------------------------------------------------------

_EXPECTED_APP_PERMISSIONS: dict[str, str] = {
    "contents": "write",
    "issues": "write",
    "pull_requests": "write",
    "checks": "write",
    "metadata": "read",
}

_EXPECTED_APP_EVENTS: tuple[str, ...] = (
    "issues",
    "issue_comment",
    "pull_request",
    "pull_request_review",
    "pull_request_review_comment",
    "label",
)


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
# Issue #4016: Short-TTL caches for the onboarding verification checks.
#
# These bound the extra Secrets Manager + DynamoDB reads the connections card
# adds. Reuses the 60s TTL pattern above.
#
# CAVEAT: these are per-pod, in-process dicts and the gateway runs replicas: 2,
# so two pods can briefly disagree. That is acceptable for a status tile. It
# must NEVER become a control signal — nothing may gate behaviour on these.
# ---------------------------------------------------------------------------

_VERIFICATION_TTL_SECONDS = 60

# keyed by tenant/org id → (expires_at_monotonic, exists|None)
_tenant_secret_cache: dict[str, tuple[float, bool | None]] = {}
# keyed by (kind, key) → (expires_at_monotonic, present|None)
_identity_row_cache: dict[tuple[str, str], tuple[float, bool | None]] = {}
# platform-wide singleton checks → (expires_at_monotonic, PlatformVerification)
_platform_verification_cache: tuple[float, Any] | None = None


def _verification_cache_get(cache: dict, key: Any) -> tuple[bool, bool | None]:
    """Return (hit, value) for a TTL verification cache."""
    entry = cache.get(key)
    if entry is None:
        return False, None
    expires_at, value = entry
    if time.monotonic() >= expires_at:
        del cache[key]
        return False, None
    return True, value


def _verification_cache_set(cache: dict, key: Any, value: bool | None) -> None:
    cache[key] = (time.monotonic() + _VERIFICATION_TTL_SECONDS, value)


def _invalidate_verification_cache() -> None:
    """Clear all onboarding-verification caches (Issue #4016).

    Called after register / rotate / disconnect so the card reflects the
    operator's action immediately instead of waiting out the TTL. Mirrors the
    existing ``_invalidate_login_enabled_cache`` precedent.
    """
    global _platform_verification_cache
    _tenant_secret_cache.clear()
    _identity_row_cache.clear()
    _platform_verification_cache = None
    # Issue #4017: the App-config drift read hangs off the same compute path, so
    # it must clear together with the rest — otherwise "Re-validate" appears to
    # do nothing for up to its (longer) TTL.
    _invalidate_app_config_drift_cache()


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
        # Issue #4016: log the dispatch itself. Without this, a no-nonce install
        # was indistinguishable in the logs from a nonce install, so an operator
        # debugging a silent partial had no way to tell which path ran.
        logger.info(
            "event=install_callback_dispatch installation_id=%d setup_action=%s path=no_nonce",
            installation_id,
            setup_action or "(none)",
        )
        return await _handle_no_nonce_install(
            installation_id=installation_id,
            db=db,
            github_client=github_client,
        )

    logger.info(
        "event=install_callback_dispatch installation_id=%d setup_action=%s path=nonce",
        installation_id,
        setup_action or "(none)",
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

    # 2. Resolve the initiator from the nonce's recorded users.id — and ONLY from
    #    that. target_user_id is written by install-start from the authenticated
    #    caller's own session, so it is the authenticated initiator.
    #
    #    Issue #5664 (A10): the `provider_user_id` fallback that used to follow was
    #    removed. `provider_user_id` is a free-text column on a shared nonce table,
    #    and resolving a user (hence `caller_org_id`, hence which tenant OWNS the
    #    installation) from it let the one-time credential nominate its own subject.
    #    `caller_org_id` drives the routing row, the per-tenant App key seed, the
    #    org_admin membership grant and the identity-index row — so a value carried
    #    in the credential could decide all of those. Ownership now comes from the
    #    authenticated initiator or the callback refuses.
    user_row = await db.get(User, nonce.target_user_id) if nonce.target_user_id else None
    if user_row is None:
        logger.warning(
            "event=install_callback_denied jti=%s reason=initiator_unresolved",
            state,
        )
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

    if github_client is None:
        # Issue #4016: without a client the account login/type stay at their
        # "unknown"/"Organization" defaults, github_org_id stays None, and the
        # org-resolution block below is skipped entirely — the install silently
        # lands on the caller's own tenant. That was previously unlogged.
        logger.error(
            "event=install_callback_no_github_client installation_id=%d "
            "outcome=metadata_unavailable detail=app_credentials_missing_install_attaches_to_caller_tenant",
            installation_id,
        )

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
    #
    #    Issue #4072 (#5, CRITICAL) — why this block needs an authorization gate:
    #    this endpoint is unauthenticated by design (GitHub redirects a browser
    #    here), so the nonce validated above is the ONLY authenticator, and it
    #    binds the CALLER. The target tenant, by contrast, was re-derived from
    #    caller-supplied data: installation_id → GitHub account → github_org_id →
    #    matching organizations row. That made "which tenant do I take over?" a
    #    request parameter. Everything downstream of this block — the routing row,
    #    the org_admin membership (#4006), the auto-switch (#3072), the tenant
    #    secret seed (#2085), the identity-index row (#2950) — then landed in the
    #    victim's tenant. _attach_org_installation's own cross-tenant guard could
    #    not catch it: it compares against `caller_org_id`, which by then has
    #    already been overwritten with the victim's tenant id.
    #
    #    Decision D1 option (b) keeps #2952's routing — a real GitHub org install
    #    SHOULD land in the org's shared workspace so co-workers share it — but
    #    makes it conditional on the caller having STANDING in that tenant. See
    #    _caller_has_standing_in_tenant.
    resolved_org_id = caller_org_id

    if account_type == "Organization" and github_org_id is not None:
        # Try to find existing org by github_org_id
        org_by_github_id = (await db.execute(select(Organization).where(Organization.github_org_id == str(github_org_id)))).scalar_one_or_none()

        if org_by_github_id is not None:
            # Issue #4072 (#5): the gate. An install may only be routed INTO a
            # pre-existing tenant by someone who already belongs to it.
            if not await _caller_has_standing_in_tenant(
                user_id=user_row.id,
                caller_org_id=caller_org_id,
                target_tenant_id=org_by_github_id.id,
                db=db,
            ):
                logger.warning(
                    "event=install_callback_cross_tenant_denied installation_id=%d account=%s github_org_id=%s "
                    "caller_user=%s caller_tenant=%s target_tenant=%s reason=no_membership_in_target_tenant",
                    installation_id,
                    account_login,
                    github_org_id,
                    user_row.id,
                    caller_org_id,
                    org_by_github_id.id,
                )
                # PermissionError is the established cross-tenant signal on this
                # path — the route already renders it as `tenant_conflict`
                # (connections/routes.py) rather than a 500. Raised BEFORE any
                # write, so nothing is bound, granted, seeded, or switched.
                raise PermissionError(
                    f"GitHub organization '{account_login}' is already connected to another ADP workspace that you are not a member of. "
                    "Ask an administrator of that workspace to invite you, then re-run the install."
                )

            resolved_org_id = org_by_github_id.id
            logger.info(
                "install-callback: resolved org by github_org_id=%s → tenant=%s",
                github_org_id,
                resolved_org_id,
            )
        elif os.environ.get("ORG_TENANT_AUTO_CREATE", "false").lower() == "true":
            # Issue #2952 (Rev 4 C): Install-time tenant upsert for unknown orgs.
            # On a public App, orgs install without ever registering.
            # Issue #2724: this branch is reached only after nonce validation
            # above (the nonce IS the authenticator), so an authenticated ADP
            # user deliberately drove this install → register_flow (trusted).
            #
            # Issue #4072 (#5): second door into a pre-existing tenant.
            # _upsert_org_tenant_shell is idempotent BY SLUG, so it returns an
            # existing tenant whenever the account login slugifies onto one. That
            # is the same re-point as the branch above reached by a different
            # route, so it needs the same standing gate — otherwise the gate is
            # bypassable by choosing an account whose login collides with the
            # victim tenant's id. A shell this install actually CREATES has no
            # victim, so #2952 first-installer onboarding is unaffected.
            preexisting_shell = await db.get(Organization, _slugify_org_id(account_login))
            if preexisting_shell is not None and not await _caller_has_standing_in_tenant(
                user_id=user_row.id,
                caller_org_id=caller_org_id,
                target_tenant_id=preexisting_shell.id,
                db=db,
            ):
                logger.warning(
                    "event=install_callback_cross_tenant_denied installation_id=%d account=%s github_org_id=%s "
                    "caller_user=%s caller_tenant=%s target_tenant=%s reason=slug_collides_with_foreign_tenant",
                    installation_id,
                    account_login,
                    github_org_id,
                    user_row.id,
                    caller_org_id,
                    preexisting_shell.id,
                )
                raise PermissionError(
                    f"GitHub organization '{account_login}' maps to an existing ADP workspace that you are not a member of. "
                    "Ask an administrator of that workspace to invite you, then re-run the install."
                )

            upserted_id = await _upsert_org_tenant_shell(
                owner_login=account_login,
                github_org_id=str(github_org_id),
                github_app_id="",
                db=db,
                created_via="register_flow",
            )
            if upserted_id:
                resolved_org_id = upserted_id
                logger.info(
                    "install-callback: upserted org-tenant shell for unknown org %s → tenant=%s",
                    account_login,
                    resolved_org_id,
                )
            else:
                # Issue #4016: the upsert returned nothing, so the install falls
                # back to the caller's own tenant instead of the org's. Silent
                # before; it is the wrong-tenant-routing failure mode.
                logger.error(
                    "event=install_callback_upsert_failed installation_id=%d account=%s github_org_id=%s "
                    "outcome=install_attached_to_caller_tenant fallback_tenant=%s",
                    installation_id,
                    account_login,
                    github_org_id,
                    resolved_org_id,
                )
        else:
            # Issue #4016: unknown org and auto-create is off — the install
            # attaches to the caller's personal tenant, not the org's. Operators
            # read this as "installed for my org"; it is not.
            logger.warning(
                "event=install_callback_org_not_onboarded installation_id=%d account=%s github_org_id=%s "
                "reason=org_tenant_auto_create_disabled outcome=install_attached_to_caller_tenant fallback_tenant=%s",
                installation_id,
                account_login,
                github_org_id,
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
        # Issue #3073: Record the installing user so they can manage without admin.
        installed_by_user_id=user_row.id if user_row else None,
    )

    # Issue #2085: Seed per-tenant GitHub App secret so that downstream
    # resolve_tenant_app_credentials() never hits a missing-secret error.
    from .tenant_secret import seed_tenant_github_app_secret

    # Issue #4016: log with installation_id so a seed can be tied back to the
    # install that triggered it when reconstructing a broken onboarding.
    logger.info(
        "event=install_callback_seed_secret installation_id=%d tenant=%s",
        installation_id,
        resolved_org_id,
    )
    await seed_tenant_github_app_secret(resolved_org_id, installation_id)

    # Issue #3072: Track the previously-active tenant for redirect params.
    switched_from: str | None = None

    if account_type == "Organization":
        # Issue #719: Populate organizations.github_installation_ids so that
        # future users from this org are matched to this tenant automatically.
        await _append_installation_id_to_org(
            installation_id=installation_id,
            caller_org_id=resolved_org_id,
            db=db,
        )

        # Issue #3035: Create a tenant_membership for the installing user.
        # The nonce IS the authenticator — user_row was resolved from it above.
        # Only org installs get memberships; personal installs stay on the
        # personal/adp-default path.
        await _create_installer_membership(
            user_row=user_row,
            tenant_id=resolved_org_id,
            github_org_login=account_login,
            db=db,
        )

        # Issue #3072: Auto-switch the installer's active tenant to the
        # newly-installed org so they land IN the workspace. Reuses the
        # same atomic deactivate-all/activate-one pattern from switch_tenant
        # endpoint (#3071). Skips silently if the org is already active
        # (reinstall case). Nonce-path only, org installs only — guards
        # already enforced by the enclosing if-block + user_row presence.
        switched_from = await _auto_switch_active_tenant(
            user_id=user_row.id,
            target_tenant_id=resolved_org_id,
            db=db,
        )

    # Issue #2950: Write the installation → tenant mapping to DynamoDB
    # identity-index so the webhook-ingress resolver can find it. Without
    # this, the webhook rejects all events as unknown_installation because
    # the DDB lookup misses and Postgres is only consulted as a drift
    # safety-net AFTER a DDB hit.
    # Issue #2952 (E): MUST use the resolved org tenant, not caller_org_id,
    # otherwise webhook routing points at the wrong tenant.
    logger.info(
        "event=install_callback_identity_index installation_id=%d tenant=%s",
        installation_id,
        resolved_org_id,
    )
    await _write_installation_identity_index(
        installation_id=installation_id,
        org_id=resolved_org_id,
    )

    # Seed the platform App's own bot identity so the webhook Lambda
    # recognizes its sender (e.g. the agent editing its own status comment)
    # instead of 403'ing as unknown_user. Best-effort — never blocks install.
    app_slug = get_github_app_provider().get_slug()
    if app_slug:
        await seed_bot_identity(
            installation_id=installation_id,
            org_id=resolved_org_id,
            app_slug=app_slug,
            github_client=github_client,
            db=db,
        )

    # Issue #4016: the verification card must reflect the install immediately,
    # not after the 60s TTL — an operator who just installed and clicks through
    # to Settings would otherwise see stale reds for work that just succeeded.
    _invalidate_verification_cache()

    # Issue #3072: Include switch info in the result so the route layer can
    # pass it to the frontend redirect. switched_from is None when no switch
    # occurred (personal install, reinstall while already active, etc.).
    return {
        "success": True,
        "installation_id": installation_id,
        "account_login": account_login,
        "account_type": account_type,
        "error_code": None,
        "error_message": None,
        "switched_from": switched_from if account_type == "Organization" and user_row else None,
    }


async def _caller_has_standing_in_tenant(
    *,
    user_id: str,
    caller_org_id: str,
    target_tenant_id: str,
    db: AsyncSession,
) -> bool:
    """Whether the install-callback caller may route an install INTO a tenant.

    Issue #4072 (#5, CRITICAL) + decision D1 option (b). The install callback is
    unauthenticated by design (GitHub redirects a browser here with no bearer
    token), so the nonce establishes *identity* — but identity alone is not
    authority over the tenant the install would be bound to. This is the missing
    authority half.

    "Standing" is deliberately narrow — only two things count:

    * the target IS the caller's own tenant (``users.org_id``), or
    * the caller already holds a ``tenant_memberships`` row in the target.

    Note what is NOT accepted: the caller's *role*. Someone who is org_admin of
    tenant A has no standing in tenant B, and #4006 makes every installer an
    org_admin of their own tenant — so accepting role would re-open the hole for
    anybody who has ever installed the App anywhere.

    Membership is checked without an ``is_active`` filter on purpose: ``is_active``
    is per-user session state that ``switch_tenant`` flips, so an inactive row is
    still real standing. Requiring ``is_active`` would break a legitimate
    multi-workspace installer whose active workspace happens to be another one —
    which is the #4006 lockout failure mode #4072's blast-radius table warns
    against.
    """
    if target_tenant_id == caller_org_id:
        return True

    # Function-local imports, matching this module's established convention
    # (install_callback imports select/update the same way).
    from sqlalchemy import select

    from src.shared.models.onboarding import TenantMembership

    membership = (
        await db.execute(
            select(TenantMembership.id)
            .where(
                TenantMembership.user_id == user_id,
                TenantMembership.tenant_id == target_tenant_id,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return membership is not None


class SetupAuthorityError(Exception):
    """The principal completing a platform-App setup flow lacks authority.

    Distinct from the nonce errors: the state token was structurally fine, but the
    person it was issued to may not replace the deployment's shared credentials.
    """


async def _assert_platform_setup_authority(
    *,
    nonce: MagicLinkNonce,
    db: AsyncSession,
) -> Any:
    """Re-derive platform-admin authority for a browser-redirect setup callback.

    Issue #5664 (A10, f-32c4047a-643a-45cc-821a-e45ca5586239). ``register_app_callback``
    writes the deployment's shared GitHub App credentials, the webhook signing
    secret and the GitHub sign-in secret — a replacement that is destructive for
    every tenant at once and hands control of the trusted inbound path to whoever
    triggers it. Its only check was possession of a 15-minute state token.

    Why the authority check lives HERE and not as a route dependency: GitHub
    redirects the operator's browser to the callback as a plain GET with no
    Authorization header, so ``get_current_user`` cannot run — adding it would make
    legitimate setup impossible to complete, which is the "gate on the wrong half
    of the flow" failure mode. The start endpoint IS platform-admin gated and
    records its initiator on the nonce, so authority is re-derived from that
    recorded initiator instead:

    * the initiator must still resolve to a real user row, and
    * that user must still hold platform-admin authority **in the database** — not
      via a token claim, because there is no token here to claim anything.

    Re-checking at completion (rather than trusting the start-time check) is what
    makes a revoked admin's in-flight link stop working.

    Returns the initiating user row on success; raises SetupAuthorityError
    otherwise. The caller must run this BEFORE consuming the nonce and before any
    secret write, so a refusal leaves no trace and stays retryable.
    """
    from src.shared.models.organization import User

    # The nonce records BOTH forms of the initiator: target_user_id is users.id,
    # provider_user_id is the cognito_sub. Require the users.id form — the
    # cognito_sub fallback is the same "credential names its own subject" pattern
    # this issue removes from the installation path.
    if not nonce.target_user_id:
        raise SetupAuthorityError("Setup link carries no initiator")

    initiator = await db.get(User, nonce.target_user_id)
    if initiator is None:
        raise SetupAuthorityError("Setup link initiator no longer exists")

    # Platform-admin authority, server-side. `users.role` is the authority for
    # platform admin: `bootstrap_admin.py` and the admin user-update path are its
    # only writers, and `auth_service` derives the `is_admin` claim from exactly
    # these values (auth_service.py:301-306). So this is the same authority the
    # start endpoint enforced via the claim, re-read from the database at
    # completion time — which is what makes a revoked admin's in-flight link stop
    # working.
    #
    # Deliberately NOT falling back to `tenant_memberships.role`: that column only
    # ever carries "member" or "org_admin", and an org admin is a TENANT-level
    # role. Replacing the shared App/webhook/sign-in secrets is platform-level and
    # affects every tenant, so accepting org_admin here would re-open the
    # escalation one rung lower.
    if (initiator.role or "") not in _PLATFORM_ADMIN_ROLES:
        raise SetupAuthorityError("Setup link initiator is not a platform administrator")

    return initiator


def _promotion_allowed_for_provenance(created_via: str | None) -> tuple[bool, str]:
    """Whether an org may be promoted to a vouched-for tenant, by provenance.

    Issue #2724 (slice B, review finding): the gateway's own unauthenticated
    no-nonce install callback both creates the org shell AND performs the two
    promoting side effects — copying the platform App's private key into
    ``adp/<env>/tenants/<org>/github-app`` and writing the routable
    installation → tenant identity-index row. Both ran before this, with no
    webhook and therefore no Lambda gate involved, which left the primary attack
    path open no matter how correct the Lambda side was.

    This is the gateway-side counterpart of ``installation_gate`` in
    webhook-ingress/lambda/common/gateway_client.py, and it mirrors that
    function's asymmetry deliberately:

    ==============================  ========  =========================
    created_via                     promote   reason
    ==============================  ========  =========================
    operator / register_flow        True      trusted_provenance
    install_autocreate              False     self_created_shell
    absent / unrecognised           True      provenance_unavailable
    ==============================  ========  =========================

    **Unknown is not untrusted.** An absent or unrecognised value fails OPEN, for
    the same reason the Lambda gate does: migration 025 stamps every pre-existing
    row ``operator`` via ``server_default``, so the only way to see something else
    is a row written by code newer than this reader — and bricking onboarding on
    it would be the top row of this issue's own blast-radius table.

    Note the split from the *creation* decision. The gateway's
    ``ORG_TENANT_AUTO_CREATE`` still governs whether a shell may be created at
    all, and stays ``"true"`` because the nonce-authenticated path needs it. Only
    the promotion is refused here; the install itself still succeeds, the shell
    is still created, and the UI still works. Whether an ``install_autocreate``
    shell is ever promoted remains the webhook Lambda's single trust decision,
    behind the single flag (per #2724's BINDING one-flag rule) — this function
    only stops the gateway from pre-empting it.
    """
    from src.shared.models.organization import (
        CREATED_VIA_INSTALL_AUTOCREATE,
        TRUSTED_CREATED_VIA,
    )

    if created_via in TRUSTED_CREATED_VIA:
        return True, "trusted_provenance"
    if created_via == CREATED_VIA_INSTALL_AUTOCREATE:
        return False, "self_created_shell"
    return True, "provenance_unavailable"


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

    This path grants no session and no access to anyone, and only creates
    resources keyed by the GitHub-verified installation ID. It is NOT, however,
    authenticated as an ADP caller — so any org shell it creates is stamped
    ``created_via="install_autocreate"`` (Issue #2724). Downstream trust
    decisions must key on that provenance rather than on the row's existence.

    Issue #2724 (slice B, review finding): this handler is itself such a
    downstream decision, and it is the FIRST one — it runs on the attacker's
    browser redirect, before any webhook exists. So it applies the gate to its
    own two promoting side effects (per-tenant App credentials, routable
    identity-index row) rather than leaving them to the webhook Lambda, which on
    this path is never involved at all. See ``_promotion_allowed_for_provenance``.
    """
    from sqlalchemy import select

    from src.shared.models.organization import CREATED_VIA_INSTALL_AUTOCREATE, Organization

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
    # Provenance of the row that actually resolved above — NOT of this request.
    # An operator-onboarded org taking a public-App install is still `operator`;
    # only a row this unauthenticated path had to create itself is untrusted.
    # None means "no row resolved", which never reaches a promotion decision.
    resolved_created_via: str | None = None

    if account_type == "Organization" and github_org_id is not None:
        # Try to find existing org by github_org_id
        org_by_github_id = (await db.execute(select(Organization).where(Organization.github_org_id == str(github_org_id)))).scalar_one_or_none()

        if org_by_github_id is not None:
            resolved_org_id = org_by_github_id.id
            resolved_created_via = org_by_github_id.created_via
            logger.info(
                "event=no_nonce_install_org_resolved installation_id=%d account=%s github_org_id=%s tenant=%s created_via=%s",
                installation_id,
                account_login,
                github_org_id,
                resolved_org_id,
                resolved_created_via,
            )
        elif os.environ.get("ORG_TENANT_AUTO_CREATE", "false").lower() == "true":
            # Upsert the tenant shell for this unknown org.
            #
            # Issue #2724: THIS is the untrusted door. Nothing authenticated the
            # caller — no nonce, no session — so the row is a self-created shell
            # and is stamped install_autocreate. The webhook auto-register gate
            # refuses to treat it as a known tenant unless the deployment has
            # explicitly opted into open onboarding via ORG_TENANT_AUTO_CREATE
            # on the webhook Lambda too.
            resolved_org_id = await _upsert_org_tenant_shell(
                owner_login=account_login,
                github_org_id=str(github_org_id),
                github_app_id="",
                db=db,
                created_via=CREATED_VIA_INSTALL_AUTOCREATE,
            )
            if resolved_org_id:
                resolved_created_via = CREATED_VIA_INSTALL_AUTOCREATE
                logger.info(
                    "event=no_nonce_install_org_upserted installation_id=%d account=%s github_org_id=%s tenant=%s created_via=%s",
                    installation_id,
                    account_login,
                    github_org_id,
                    resolved_org_id,
                    CREATED_VIA_INSTALL_AUTOCREATE,
                )
            else:
                logger.error(
                    "event=no_nonce_install_upsert_failed installation_id=%d account=%s github_org_id=%s "
                    "outcome=nothing_persisted detail=org_tenant_shell_upsert_returned_no_id",
                    installation_id,
                    account_login,
                    github_org_id,
                )
        else:
            logger.warning(
                "event=no_nonce_install_unresolved installation_id=%d account=%s github_org_id=%s "
                "reason=org_tenant_auto_create_disabled outcome=nothing_persisted",
                installation_id,
                account_login,
                github_org_id,
            )
    else:
        logger.warning(
            "event=no_nonce_install_unresolved installation_id=%d account=%s account_type=%s github_org_id=%s "
            "reason=not_an_org_install_or_no_github_org_id outcome=nothing_persisted",
            installation_id,
            account_login,
            account_type,
            github_org_id,
        )

    # Default: nothing resolved means nothing was promoted, so the outcome
    # report below reads "failed" rather than dereferencing an unset flag.
    promote = False

    # Issue #4016 (🔴-3): the guard is `is not None`, not truthiness. An empty
    # string org id is a resolution bug, not "no org" — the old truthy test
    # silently skipped every write for it and still reported success.
    if resolved_org_id is not None and resolved_org_id != "":
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

        # Issue #2724 (slice B): the two side effects below PROMOTE the org from
        # "a row exists" to "a tenant the platform vouches for" — they hand it
        # the platform App's private key and a routable webhook identity. Neither
        # may fire for a shell this unauthenticated path created itself.
        promote, deny_reason = _promotion_allowed_for_provenance(resolved_created_via)

        if promote:
            # Seed per-tenant secret
            from .tenant_secret import seed_tenant_github_app_secret

            logger.info(
                "event=no_nonce_install_seed_secret installation_id=%d tenant=%s created_via=%s",
                installation_id,
                resolved_org_id,
                resolved_created_via,
            )
            await seed_tenant_github_app_secret(resolved_org_id, installation_id)
        else:
            logger.warning(
                "no-nonce install: NOT promoting org=%s (installation_id=%d, created_via=%s, reason=%s) — "
                "no per-tenant GitHub App secret, no identity-index row. Onboard the org via an operator "
                "or the authenticated install flow, or set ORG_TENANT_AUTO_CREATE=true on the webhook "
                "Lambda for a deliberately-open deployment.",
                resolved_org_id,
                installation_id,
                resolved_created_via,
                deny_reason,
            )

        if account_type == "Organization":
            # Deliberately NOT gated: this populates the very column
            # resolve-installation answers from, which is how the webhook gate
            # learns the provenance. Withholding it would make the gate see an
            # authoritative not_found instead — denying for the wrong reason, and
            # breaking open-onboarding deployments that legitimately allow this.
            await _append_installation_id_to_org(
                installation_id=installation_id,
                caller_org_id=resolved_org_id,
                db=db,
            )

        # DDB write uses the resolved org tenant
        if promote:
            logger.info(
                "event=no_nonce_install_identity_index installation_id=%d tenant=%s",
                installation_id,
                resolved_org_id,
            )
            await _write_installation_identity_index(
                installation_id=installation_id,
                org_id=resolved_org_id,
            )

            # Same best-effort bot-identity seed as the nonce path (install_callback) —
            # see its call site for why this matters.
            app_slug = get_github_app_provider().get_slug()
            if app_slug:
                await seed_bot_identity(
                    installation_id=installation_id,
                    org_id=resolved_org_id,
                    app_slug=app_slug,
                    github_client=github_client,
                    db=db,
                )

    # -----------------------------------------------------------------------
    # Issue #4016 (🔴-3): report the OUTCOME, not the fact that we ran.
    #
    # This used to return success=True unconditionally — including when nothing
    # at all had been persisted (no org resolved, or resolution produced a
    # promotion denial). The operator saw "Installation complete", the install
    # existed on GitHub, and the platform knew nothing about it. That asymmetry
    # with the nonce path (which raises on every failure) is the bug.
    # -----------------------------------------------------------------------
    persisted = resolved_org_id is not None and resolved_org_id != ""

    if not persisted:
        logger.error(
            "event=no_nonce_install_failed installation_id=%d account=%s account_type=%s outcome=nothing_persisted error_code=org_not_resolved",
            installation_id,
            account_login,
            account_type,
        )
        return {
            "success": False,
            "installation_id": installation_id,
            "account_login": account_login,
            "account_type": account_type,
            "error_code": "org_not_resolved",
            "error_message": (
                "The GitHub App was installed, but this deployment could not match it to an ADP "
                "workspace, so nothing was recorded. An operator must onboard the organisation "
                "before the installation will do anything."
            ),
            "no_nonce": True,
        }

    if not promote:
        logger.warning(
            "event=no_nonce_install_partial installation_id=%d account=%s tenant=%s created_via=%s "
            "outcome=recorded_but_not_promoted error_code=promotion_denied",
            installation_id,
            account_login,
            resolved_org_id,
            resolved_created_via,
        )
        # NOTE the deliberate difference from the branch above: success stays
        # True. #2724 contracts a promotion refusal as a SUCCESSFUL install that
        # is intentionally not vouched for — the row was written and the UI
        # works. Flipping it to False would turn that designed security posture
        # into an install failure. What #4016 adds is `partial`, so the page can
        # stop claiming the installation is finished when it is not.
        return {
            "success": True,
            "installation_id": installation_id,
            "account_login": account_login,
            "account_type": account_type,
            "error_code": "promotion_denied",
            "error_message": (
                "The installation was recorded, but this deployment does not vouch for the "
                "organisation, so no credentials or webhook routing were provisioned. Webhooks "
                "for this installation will be rejected until an operator onboards it."
            ),
            "no_nonce": True,
            "partial": True,
        }

    logger.info(
        "event=no_nonce_install_complete installation_id=%d account=%s tenant=%s outcome=success",
        installation_id,
        account_login,
        resolved_org_id,
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
    installed_by_user_id: str | None = None,
) -> None:
    """Attach a GitHub installation (org or personal) to the caller's ADP tenant.

    Checks for cross-tenant ownership conflicts via the ChannelTenantMap table.
    On conflict, raises PermissionError with a user-actionable message.
    On first install, inserts a ChannelTenantMap row; on re-install, updates the
    metadata. Stores the repo names so the connections UI can list them.

    Issue #3073: installed_by_user_id records who performed the install so they
    can manage the connection without workspace admin role.
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
        # Issue #4070 (·A0): keep the canonical installation -> tenant column in
        # step with the metadata blob. A re-install can carry a NEW installation
        # id for the same GitHub account (uninstall + reinstall), so this must be
        # assigned, not just backfilled when NULL.
        existing.installation_id = str(installation_id)
        # Issue #3073: On re-install, update installed_by to the new verified installer.
        if installed_by_user_id:
            existing.installed_by_user_id = installed_by_user_id
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
        # Issue #4070 (·A0): the canonical installation -> tenant key. Written
        # here AND by organizations_service so both writers agree on one column
        # with one meaning; provider_scope_id above stays the ACCOUNT scope key.
        installation_id=str(installation_id),
        org_id=caller_org_id,
        install_metadata=_meta(),
        installed_by_user_id=installed_by_user_id,
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


async def _create_installer_membership(
    *,
    user_row: Any,
    tenant_id: str,
    github_org_login: str,
    db: AsyncSession,
) -> None:
    """Create a tenant_membership for the user who installed the GitHub App.

    Issue #3035: The install event itself is sufficient authorization — only a
    repo admin (or org owner) can install an app, and the installing user is
    the authenticated session that initiated the flow.

    Issue #4006: that same contract means the installer IS an org admin, so the
    row carries role='org_admin'. It used to write 'member', which was actively
    harmful once #3998 made tenant_memberships the read-side authority: the row
    *exists*, so it wins over the legacy ORG_ADMIN fallback and the person who
    installed the app could not administer their own org — a live bug independent
    of any feature flag.

    Idempotent: skips if a membership for (user, tenant) already exists (D7
    pattern). Never modifies is_active of existing rows. Sets is_active=True
    on a NEW membership only if the user has no other memberships at all
    (first-membership-active rule). Pre-existing stale 'member' rows written
    before this fix are healed by scripts/audit_org_admin_memberships.py --apply.
    """
    from sqlalchemy import select

    from src.shared.models.onboarding import TenantMembership

    user_id = user_row.id

    # Check for existing membership (idempotent — skip if exists)
    existing_stmt = select(TenantMembership).where(
        TenantMembership.user_id == user_id,
        TenantMembership.tenant_id == tenant_id,
    )
    existing = (await db.execute(existing_stmt)).scalar_one_or_none()
    if existing is not None:
        logger.info(
            "install-callback: membership already exists for user=%s tenant=%s (idempotent skip)",
            user_id,
            tenant_id,
        )
        # Issue #4849: still refresh the projection. Nothing was written here, so
        # this is a pure read of already-committed state — and a reinstall is the
        # one recurring event that can heal a user whose membership predates
        # consistent write-through, or whose projection write previously failed.
        from src.admin.memberships import project_member_org_ids

        await project_member_org_ids(db, user_id=user_id)
        return

    # Determine is_active: only if user has NO memberships at all
    any_membership_stmt = (
        select(TenantMembership.id)
        .where(
            TenantMembership.user_id == user_id,
        )
        .limit(1)
    )
    has_any = (await db.execute(any_membership_stmt)).scalar_one_or_none() is not None
    is_active = not has_any

    membership = TenantMembership(
        user_id=user_id,
        tenant_id=tenant_id,
        role="org_admin",
        is_active=is_active,
        joined_via="app_install",
        github_org_id=github_org_login,
    )
    db.add(membership)
    await db.commit()

    logger.info(
        "install-callback: created tenant_membership user=%s tenant=%s role=org_admin is_active=%s joined_via=app_install",
        user_id,
        tenant_id,
        is_active,
    )

    # Issue #3134: Write-through member_org_ids to DDB identity rows so the
    # webhook Lambda can enforce trigger_policy without a gateway call.
    # Issue #4849: consolidated into admin/memberships.py — see that helper for
    # the wipe-safety, is_active and multi-identity semantics. Runs post-commit
    # (above) by design.
    from src.admin.memberships import project_member_org_ids

    await project_member_org_ids(db, user_id=user_id)


async def _auto_switch_active_tenant(
    *,
    user_id: str,
    target_tenant_id: str,
    db: AsyncSession,
) -> str | None:
    """Switch the installer's active tenant to the target org after install.

    Issue #3072: Reuses the same atomic deactivate-all/activate-one pattern
    from the switch_tenant endpoint (#3071). Returns the previously-active
    tenant_id if a switch occurred, or None if the target was already active
    (reinstall while active) or no active membership existed.

    Guards: caller must already have a membership for target_tenant_id (just
    written by _create_installer_membership). Asserts this via the same
    membership check the switch endpoint does.
    """
    from sqlalchemy import select, update

    from src.shared.models.onboarding import TenantMembership

    # Verify the target membership exists (assert, not assume — same check
    # the switch endpoint performs).
    target_stmt = select(TenantMembership).where(
        TenantMembership.user_id == user_id,
        TenantMembership.tenant_id == target_tenant_id,
    )
    target_membership = (await db.execute(target_stmt)).scalar_one_or_none()
    if target_membership is None:
        logger.warning(
            "auto-switch: no membership for user=%s tenant=%s — skipping",
            user_id,
            target_tenant_id,
        )
        return None

    # Already active — no-op (reinstall while this org is already active)
    if target_membership.is_active:
        logger.info(
            "auto-switch: target tenant=%s already active for user=%s — no-op",
            target_tenant_id,
            user_id,
        )
        return None

    # Find the currently active tenant (the one we're switching FROM)
    active_stmt = select(TenantMembership.tenant_id).where(
        TenantMembership.user_id == user_id,
        TenantMembership.is_active == True,  # noqa: E712
    )
    previous_active_id = (await db.execute(active_stmt)).scalar_one_or_none()

    # Atomically switch: deactivate all → activate target. Single transaction.
    deactivate_stmt = (
        update(TenantMembership)
        .where(
            TenantMembership.user_id == user_id,
            TenantMembership.is_active == True,  # noqa: E712
        )
        .values(is_active=False)
    )
    await db.execute(deactivate_stmt)

    activate_stmt = (
        update(TenantMembership)
        .where(
            TenantMembership.user_id == user_id,
            TenantMembership.tenant_id == target_tenant_id,
        )
        .values(is_active=True)
    )
    await db.execute(activate_stmt)

    # Explicit commit (#3058 lesson — flush is not enough)
    await db.commit()

    logger.info(
        "auto-switch: switched user=%s from tenant=%s to tenant=%s",
        user_id,
        previous_active_id,
        target_tenant_id,
    )
    return previous_active_id


async def _write_installation_identity_index(
    *,
    installation_id: int,
    org_id: str,
    trigger_policy: str | None = None,
    min_author_association: str | None = None,
) -> None:
    """Write the installation → tenant mapping to the DynamoDB identity-index.

    Issue #2950: The webhook-ingress identity resolver uses DDB as its primary
    lookup for installation_id → tenant. Without this row, all webhook events
    for the installation are rejected as unknown_installation.

    Issue #3134: Also writes trigger_policy and min_author_association when
    provided. These are read by the Lambda at trigger time (zero extra reads).

    Issue #3134 fix: Uses UpdateItem (SET semantics) so that trigger_policy and
    min_author_association attrs set by a prior admin action are NOT wiped when
    this function is called without those params (e.g. from install_callback).

    Best-effort write-through with retry (same pattern as identity/organizations_service).
    Failures are logged but do not propagate — Postgres remains the source of truth.
    """
    from src.admin.identity_index import IdentityIndexClient

    client = IdentityIndexClient()
    success = await client.update_installation_identity(
        identity_value=str(installation_id),
        org_id=org_id,
        trigger_policy=trigger_policy,
        min_author_association=min_author_association,
    )
    if success:
        logger.info(
            "identity-index: wrote github_installation_id=%d → org=%s (trigger_policy=%s)",
            installation_id,
            org_id,
            trigger_policy or "default",
        )
    else:
        logger.warning(
            "identity-index: failed to write github_installation_id=%d → org=%s (webhook routing will fail until backfilled)",
            installation_id,
            org_id,
        )

    # Issue #3860: Write reverse row (org_installation/<org> → installation_id)
    # so that resolve_installation_for_tenant() (used by adp-trigger) can resolve
    # the installation_id from the org_id. Without this, UI-installed tenants
    # permanently 422 on agent-to-agent dispatch.
    reverse_success = await client.write_reverse_installation_identity(
        org_id=org_id,
        installation_id=installation_id,
    )
    if not reverse_success:
        logger.warning(
            "identity-index: failed to write reverse row org_installation/%s → %d (adp-trigger will fail until self-healed or backfilled)",
            org_id,
            installation_id,
        )


async def _check_tenant_secret_seeded(org_id: str) -> bool | None:
    """Cached, read-only probe of the per-tenant GitHub App secret (Issue #4016)."""
    hit, cached = _verification_cache_get(_tenant_secret_cache, org_id)
    if hit:
        return cached

    from .tenant_secret import tenant_github_app_secret_exists

    value = await tenant_github_app_secret_exists(org_id)
    _verification_cache_set(_tenant_secret_cache, org_id, value)
    return value


async def _check_identity_rows(installation_id: int, org_id: str | None) -> tuple[bool | None, bool | None]:
    """Cached, read-only probe of the forward + reverse identity-index rows.

    Issue #4016: returns (forward_present, reverse_present), each tri-state.
    A DDB error degrades to None ("could not determine"), not False — an
    unreadable table must not render as a red "webhook routing is broken".

    READ-ONLY. #3860 owns writing/self-healing the reverse row.
    """
    from src.admin.identity_index import IdentityIndexClient

    fwd_key = ("forward", str(installation_id))
    rev_key = ("reverse", org_id or "")

    fwd_hit, fwd_cached = _verification_cache_get(_identity_row_cache, fwd_key)
    rev_hit, rev_cached = _verification_cache_get(_identity_row_cache, rev_key)

    if fwd_hit and (rev_hit or org_id is None):
        return fwd_cached, (rev_cached if org_id is not None else None)

    try:
        client = IdentityIndexClient()
    except Exception as exc:  # noqa: BLE001
        logger.info(
            "verification: could not construct identity-index client installation_id=%d: %s",
            installation_id,
            exc,
        )
        return None, None

    tasks = [client.get_installation_identity(installation_id)]
    if org_id:
        tasks.append(client.get_reverse_installation_identity(org_id))

    results = await asyncio.gather(*tasks, return_exceptions=True)

    def _present(result: Any) -> bool | None:
        if isinstance(result, BaseException):
            return None
        return result is not None

    forward = _present(results[0])
    _verification_cache_set(_identity_row_cache, fwd_key, forward)

    reverse: bool | None = None
    if org_id:
        reverse = _present(results[1])
        _verification_cache_set(_identity_row_cache, rev_key, reverse)

    return forward, reverse


async def _compute_connection_verification(
    *,
    installation_id: int,
    org_id: str | None,
    record_present: bool,
    repositories_live: bool | None = None,
) -> ConnectionVerification:
    """Compute the per-connection verification block (Issue #4016).

    Read-only and fail-soft throughout: every check degrades to None rather than
    raising, because this decorates the primary settings page and must never be
    able to break it.
    """
    secret_task = _check_tenant_secret_seeded(org_id) if org_id else None
    rows_task = _check_identity_rows(installation_id, org_id)

    if secret_task is not None:
        secret_result, rows_result = await asyncio.gather(secret_task, rows_task, return_exceptions=True)
    else:
        secret_result = None
        (rows_result,) = await asyncio.gather(rows_task, return_exceptions=True)

    tenant_secret: bool | None = secret_result if isinstance(secret_result, bool) else None
    if isinstance(rows_result, tuple):
        forward, reverse = rows_result
    else:
        forward, reverse = None, None

    return ConnectionVerification(
        record_present=record_present,
        tenant_secret_seeded=tenant_secret,
        identity_index_row=forward,
        reverse_identity_row=reverse,
        # Issue #5184: passed in by the caller, which is the only place that
        # knows whether the repository list it served came from GitHub.
        repositories_live=repositories_live,
    )


async def _compute_platform_verification() -> PlatformVerification:
    """Compute the admin-scoped platform verification block (Issue #4016).

    Both checks read deployment-global singletons, so the result is cached once
    for the whole pod rather than per connection. Callers MUST only return this
    to a caller who can manage connections (🔴-2).
    """
    global _platform_verification_cache

    now = time.monotonic()
    if _platform_verification_cache is not None and now < _platform_verification_cache[0]:
        return _platform_verification_cache[1]

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")
    webhook_path = f"adp/{env}/webhook-ingress/github-webhook-secret"

    def _read() -> tuple[bool | None, bool | None]:
        import boto3
        from botocore.exceptions import ClientError

        sm = boto3.client("secretsmanager", region_name=region)

        # Reuse the single source of truth for the login check (🔴-2) rather
        # than writing a second implementation.
        try:
            login_ok: bool | None = _check_login_enabled(sm)
        except Exception:  # noqa: BLE001
            login_ok = None

        # The webhook secret needs its VALUE, not just existence: Terraform
        # seeds the secret so it always exists, and the failure mode is that it
        # still holds the placeholder. Existence alone would report green.
        webhook_ok: bool | None
        try:
            raw = (sm.get_secret_value(SecretId=webhook_path).get("SecretString") or "").strip()
            webhook_ok = bool(raw) and raw != "PLACEHOLDER_REPLACE_WITH_ACTUAL_SECRET" and not _is_placeholder(raw)
        except ClientError as exc:
            error_code = exc.response.get("Error", {}).get("Code", "")
            if error_code == "ResourceNotFoundException":
                webhook_ok = False
            else:
                logger.info("verification: could not read %s: %s", webhook_path, exc)
                webhook_ok = None
        except Exception as exc:  # noqa: BLE001
            logger.info("verification: could not read %s: %s", webhook_path, exc)
            webhook_ok = None

        return login_ok, webhook_ok

    try:
        login_credentials, webhook_secret = await asyncio.to_thread(_read)
    except Exception as exc:  # noqa: BLE001
        logger.info("verification: platform checks failed entirely: %s", exc)
        login_credentials, webhook_secret = None, None

    # Issue #4017: App-config drift rides this same compute path, so it inherits
    # the admin gating, the fail-soft contract, and the single invalidation hook.
    # It keeps its own longer minimum interval (GitHub is rate-limited); a cache
    # hit there makes this effectively free.
    try:
        drift = await _compute_app_config_drift()
    except Exception as exc:  # noqa: BLE001
        logger.info("verification: App-config drift checks unavailable: %s", exc)
        drift = {}

    result = PlatformVerification(
        login_credentials=login_credentials,
        webhook_secret=webhook_secret,
        app_webhook_url_matches=drift.get("app_webhook_url_matches"),
        app_permissions_match=drift.get("app_permissions_match"),
        app_events_match=drift.get("app_events_match"),
        expected_callback_url=drift.get("expected_callback_url"),
        app_oauth_settings_url=drift.get("app_oauth_settings_url"),
        app_config_warnings=drift.get("app_config_warnings") or [],
    )
    _platform_verification_cache = (now + _VERIFICATION_TTL_SECONDS, result)
    return result


# ---------------------------------------------------------------------------
# Issue #4017: GitHub App configuration drift.
#
# The App's settings on GitHub can be edited by any admin at any time, and no
# webhook event fires when they are. Three of those settings are readable back
# and therefore diffable; one — the OAuth callback URL — is NOT.
#
# WHAT IS DIFFABLE:
#   webhook URL   → GET /app/hook/config   (NOT on GET /app)
#   permissions   → GET /app
#   events        → GET /app
#
# WHAT IS NOT, AND WHY IT IS HANDLED DIFFERENTLY:
#   The user-authorization callback URL is write-only at manifest creation and
#   thereafter UI-only. ``callback_urls`` appears nowhere in GitHub's REST API,
#   and ``external_url`` on GET /app is the App's *Homepage* URL — diffing it
#   against the broker callback would report drift on every healthy deployment.
#   So the callback is REPORTED (expected value + deep-link for eyeball
#   comparison), never diffed, and real mismatches are detected at login time
#   from GitHub's ``redirect_uri_mismatch`` error (see the broker handler).
#
# Tri-state throughout, matching #4016: True = verified matching,
# False = verified drifted, None = could not determine. A GitHub API failure or
# an unresolvable expected value is None, NEVER False — a red "your App is
# misconfigured" on an App nobody touched sends operators to fix a non-problem.
# ---------------------------------------------------------------------------


@dataclass
class AppConfigCheck:
    """Structured result of comparing the App's GitHub config to what we expect.

    Replaces the prose-only ``warnings: list[str]`` that ``register_app_manual``
    used to emit inline. ``warnings`` is still produced (unchanged wire contract
    for RegisterManualResponse) but is now rendered FROM the structured fields
    rather than being the only output.
    """

    reachable: bool = False
    webhook_url_matches: bool | None = None
    permissions_match: bool | None = None
    events_match: bool | None = None
    app_slug: str = ""
    app_name: str = ""
    actual_webhook_url: str = ""
    actual_permissions: dict[str, str] = field(default_factory=dict)
    actual_events: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _read_ssm_string(param_name: str) -> str:
    """Read an SSM parameter, returning "" unless it yields a real string.

    Issue #4017: the ``isinstance(str)`` guard is load-bearing, not defensive
    noise. These values get stored into a JSON payload, so a non-string here
    (a mocked client, an SSM response shape change, a ``StringList``) would
    raise inside ``json.dumps`` and take down the caller — which for
    ``_store_app_credentials`` means failing a registration over an
    unresolvable *optional* hint. Unresolvable reads as "unknown" instead.
    """
    try:
        import boto3

        region = os.environ.get("AWS_REGION", "us-east-1")
        ssm = boto3.client("ssm", region_name=region)
        value = ssm.get_parameter(Name=param_name)["Parameter"]["Value"]
        return value if isinstance(value, str) else ""
    except Exception as exc:  # noqa: BLE001
        logger.info("Could not read SSM parameter %s: %s", param_name, exc)
        return ""


def _resolve_expected_webhook_url() -> str:
    """Resolve where this deployment's GitHub webhooks must be delivered.

    ``WEBHOOK_URL`` env var, else the SSM parameter Terraform writes
    (``/adp/<env>/webhook-ingress/endpoint``). Returns "" when neither resolves —
    callers treat that as "unknown", never as drift.

    Issue #4017: extracted from register_app_start / register_app_manual, which
    each had their own copy of this lookup.
    """
    webhook_url = os.environ.get("WEBHOOK_URL", "")
    if webhook_url:
        return webhook_url

    return _read_ssm_string(f"/adp/{_get_environment()}/webhook-ingress/endpoint")


def _resolve_expected_oauth_callback_url() -> str:
    """Resolve the OAuth callback URL the broker will send as ``redirect_uri``.

    The broker derives this at runtime from the incoming request context (#2708),
    so this is a RE-DERIVATION of the same value from the same SSM parameter
    (``/adp/<env>/gateway/apigw-invoke-url``) that the manifest build uses. It is
    reported to the operator for comparison against the App's settings page; it
    is deliberately NOT written anywhere the broker reads, because pinning it
    would defeat the runtime derivation that keeps it self-healing (#2708).

    Returns "" when SSM cannot supply it.
    """
    apigw_url = _read_ssm_string(f"/adp/{_get_environment()}/gateway/apigw-invoke-url")
    return f"{apigw_url}/auth/github/callback" if apigw_url else ""


def _resolve_expected_app_config(*, existing: dict[str, Any] | None = None) -> dict[str, Any]:
    """Build the expected-config record to store in the App ``-meta`` secret.

    Issue #4017: the baseline a later drift check reports against, plus the audit
    record of what callback URL we told GitHub to use. Contains NO credentials.

    Best-effort by design: a value we cannot resolve right now falls back to the
    previously recorded one, and failing that is omitted entirely — an absent
    expected value reads as "unknown" downstream, never as drift. Callers run
    this inside a thread (it does SSM I/O).
    """
    prior = existing or {}

    webhook_url = _resolve_expected_webhook_url() or prior.get("expected_webhook_url", "")
    callback_url = _resolve_expected_oauth_callback_url() or prior.get("expected_callback_url", "")

    record: dict[str, Any] = {
        "expected_permissions": dict(_EXPECTED_APP_PERMISSIONS),
        "expected_events": list(_EXPECTED_APP_EVENTS),
    }
    if webhook_url:
        record["expected_webhook_url"] = webhook_url
    if callback_url:
        record["expected_callback_url"] = callback_url
    return record


def diff_app_config(
    *,
    app_slug: str = "",
    app_name: str = "",
    actual_webhook_url: str = "",
    actual_permissions: dict[str, str] | None = None,
    actual_events: list[str] | None = None,
    expected_webhook_url: str = "",
    reachable: bool = True,
) -> AppConfigCheck:
    """Diff an App's live GitHub config against what this deployment expects.

    Issue #4017: the single comparison implementation, extracted from
    ``register_app_manual``'s inline validator so registration and read-time
    drift detection cannot disagree. PURE — no I/O, no writes, no raising.

    The prose in ``warnings`` is byte-identical to what the manual-registration
    flow emitted before this refactor, so ``RegisterManualResponse.warnings``
    keeps its wire contract; the structured tri-state fields are the new output.

    Only a comparison of two KNOWN values can be drift. If either side is
    unresolvable the check is None ("unknown"), never False.
    """
    result = AppConfigCheck(
        reachable=reachable,
        app_slug=app_slug,
        app_name=app_name,
        actual_webhook_url=actual_webhook_url,
        actual_permissions=actual_permissions or {},
        actual_events=list(actual_events or []),
    )

    if not reachable:
        return result

    # --- webhook URL (GET /app/hook/config) --------------------------------
    if expected_webhook_url and actual_webhook_url:
        result.webhook_url_matches = actual_webhook_url == expected_webhook_url
        if not result.webhook_url_matches:
            result.warnings.append(
                f"Webhook URL mismatch: App has '{actual_webhook_url}', "
                f"deployment expects '{expected_webhook_url}'. "
                "Update the App's webhook URL in GitHub Settings to receive events."
            )
    elif expected_webhook_url:
        # Expected side known, actual side not → unknown, with a hint.
        result.warnings.append(f"Could not verify webhook URL from GitHub API response. Ensure the App's webhook points to: {expected_webhook_url}")

    # --- permissions (GET /app) --------------------------------------------
    missing_perms: list[str] = []
    for perm, level in _EXPECTED_APP_PERMISSIONS.items():
        actual = result.actual_permissions.get(perm, "")
        if not actual:
            missing_perms.append(f"{perm}: {level}")
        elif level == "write" and actual == "read":
            missing_perms.append(f"{perm}: needs 'write', has 'read'")
    result.permissions_match = not missing_perms
    if missing_perms:
        result.warnings.append("Missing or insufficient permissions: " + ", ".join(missing_perms) + ". Update in GitHub App Settings → Permissions.")

    # --- events (GET /app) -------------------------------------------------
    missing_events = set(_EXPECTED_APP_EVENTS) - set(result.actual_events)
    result.events_match = not missing_events
    if missing_events:
        result.warnings.append(
            "Missing event subscriptions: " + ", ".join(sorted(missing_events)) + ". Enable in GitHub App Settings → Subscribe to events."
        )

    return result


async def check_app_config(
    *,
    app_id: str,
    pem: str,
    expected_webhook_url: str = "",
) -> AppConfigCheck:
    """Read the App's live config from GitHub and diff the readable fields.

    Issue #4017: two App-JWT calls — ``GET /app`` (permissions, events, slug) and
    ``GET /app/hook/config`` (the webhook URL, which ``GET /app`` does not
    include) — then ``diff_app_config``.

    READ-ONLY: writes nothing to GitHub or to our own storage. NEVER raises — an
    unreachable GitHub yields ``reachable=False`` with all-None checks, because
    this decorates a status surface and must not be able to break it. That is the
    opposite contract to ``register_app_manual``, which deliberately fails loudly
    on the same calls because the operator is waiting on a submit.
    """
    from .github_client import GITHUB_API_BASE, _mint_app_jwt

    if not app_id or not pem:
        return AppConfigCheck()

    try:
        token = _mint_app_jwt(app_id, pem)
    except Exception as exc:  # noqa: BLE001
        logger.info("app-config check: could not mint App JWT: %s", exc)
        return AppConfigCheck()

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{GITHUB_API_BASE}/app", headers=headers)
            if resp.status_code != 200:
                logger.info("app-config check: GET /app returned %d", resp.status_code)
                return AppConfigCheck()

            data = resp.json() or {}

            # Separate endpoint, separate failure mode: a readable GET /app with
            # an unreadable hook config leaves the webhook check unknown while
            # permissions/events stay authoritative.
            actual_webhook_url = ""
            try:
                hook_resp = await client.get(f"{GITHUB_API_BASE}/app/hook/config", headers=headers)
                if hook_resp.status_code == 200:
                    actual_webhook_url = (hook_resp.json() or {}).get("url", "") or ""
            except Exception as hook_exc:  # noqa: BLE001
                logger.debug("app-config check: could not fetch /app/hook/config: %s", hook_exc)
    except Exception as exc:  # noqa: BLE001
        logger.info("app-config check: could not reach GitHub: %s", exc)
        return AppConfigCheck()

    return diff_app_config(
        app_slug=data.get("slug", "") or "",
        app_name=data.get("name", "") or "",
        actual_webhook_url=actual_webhook_url,
        actual_permissions=data.get("permissions", {}) or {},
        actual_events=data.get("events", []) or [],
        expected_webhook_url=expected_webhook_url,
        reachable=True,
    )


# Issue #4017: the App-config drift read is the only check in this module that
# calls a THIRD-PARTY, RATE-LIMITED API, so it gets a longer minimum interval
# than the 60s Secrets-Manager/DynamoDB checks around it — two App-JWT calls per
# pod per interval instead of per minute.
#
# It is still ONE gate on ONE read path, reached only from
# _compute_platform_verification and cleared by the same
# _invalidate_verification_cache() as every other verification cache. #3453 will
# consume GET /app on this same path; it must reuse this marker rather than add
# a second, unsynchronised one.
_APP_CONFIG_DRIFT_TTL_SECONDS = 900  # 15 minutes
_app_config_drift_cache: tuple[float, dict[str, Any]] | None = None


def _invalidate_app_config_drift_cache() -> None:
    global _app_config_drift_cache
    _app_config_drift_cache = None


async def _compute_app_config_drift() -> dict[str, Any]:
    """Compute the App-config drift fields for the platform verification block.

    Returns a dict of PlatformVerification field values. Fail-soft: any failure
    yields all-None checks (rendered amber, "could not determine"), never False.
    """
    global _app_config_drift_cache

    now = time.monotonic()
    if _app_config_drift_cache is not None and now < _app_config_drift_cache[0]:
        return _app_config_drift_cache[1]

    result: dict[str, Any] = {
        "app_webhook_url_matches": None,
        "app_permissions_match": None,
        "app_events_match": None,
        "expected_callback_url": None,
        "app_oauth_settings_url": None,
        "app_config_warnings": [],
    }

    try:
        app_id, pem = await asyncio.to_thread(_get_github_app_credentials)
    except Exception as exc:  # noqa: BLE001
        logger.info("app-config drift: credentials unavailable: %s", exc)
        app_id, pem = "", ""

    if not app_id or not pem:
        # No App registered (or creds unreadable) — nothing to diff. Cache the
        # miss so an unregistered deployment does not retry every request.
        _app_config_drift_cache = (now + _APP_CONFIG_DRIFT_TTL_SECONDS, result)
        return result

    stored = await asyncio.to_thread(_read_expected_app_config)

    # Live resolution wins over the value recorded at registration: the recorded
    # value is an audit record and a fallback, not the truth. If webhook-ingress
    # was redeployed to a new endpoint, deliveries must go to the NEW one, and
    # trusting the stored value would report "ok" on a genuinely broken App.
    expected_webhook_url = await asyncio.to_thread(_resolve_expected_webhook_url)
    if not expected_webhook_url:
        expected_webhook_url = stored.get("expected_webhook_url", "") or ""

    check = await check_app_config(app_id=app_id, pem=pem, expected_webhook_url=expected_webhook_url)

    result["app_webhook_url_matches"] = check.webhook_url_matches
    result["app_permissions_match"] = check.permissions_match
    result["app_events_match"] = check.events_match
    result["app_config_warnings"] = check.warnings

    # Callback URL: reported, never diffed (see the section header).
    expected_callback_url = await asyncio.to_thread(_resolve_expected_oauth_callback_url)
    if not expected_callback_url:
        expected_callback_url = stored.get("expected_callback_url", "") or ""
    result["expected_callback_url"] = expected_callback_url or None

    slug = check.app_slug or stored.get("app_slug", "") or ""
    if slug:
        result["app_oauth_settings_url"] = f"https://github.com/settings/apps/{slug}/oauth"

    _app_config_drift_cache = (now + _APP_CONFIG_DRIFT_TTL_SECONDS, result)
    return result


def _read_expected_app_config() -> dict[str, Any]:
    """Read the expected-config keys recorded in the App ``-meta`` secret.

    Returns {} when the secret is absent, unreadable, or has no expected_* keys —
    which is the normal state for a deployment registered before #4017. Callers
    treat missing values as "unknown", never as drift.
    """
    import json

    import boto3
    from botocore.exceptions import ClientError

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")
    meta_path = f"adp/{env}/github-app/adp-agent-platform-meta"

    try:
        sm = boto3.client("secretsmanager", region_name=region)
        raw = sm.get_secret_value(SecretId=meta_path).get("SecretString", "")
        if not raw:
            return {}
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            return {}
    except (ClientError, json.JSONDecodeError, TypeError) as exc:
        logger.info("app-config drift: could not read %s: %s", meta_path, exc)
        return {}

    # Never return the credential-bearing keys to a caller that only needs
    # expected config.
    return {
        "app_slug": parsed.get("app_slug", ""),
        "expected_callback_url": parsed.get("expected_callback_url", ""),
        "expected_webhook_url": parsed.get("expected_webhook_url", ""),
        "expected_permissions": parsed.get("expected_permissions", {}),
        "expected_events": parsed.get("expected_events", []),
    }


def _record_expected_app_config(*, actor: str) -> bool:
    """Read-modify-write ONLY the ``expected_*`` keys of the App ``-meta`` secret.

    Issue #4017, review §6 — the repair action's entire write surface. This
    follows ``github_app_provider._write_back_slug`` and deliberately NOT
    ``_store_app_credentials``: the latter writes six secrets plus two
    write-throughs (the webhook-ingress secret and the broker OAuth secret), so
    calling it from a "re-validate config" button could clobber live credentials
    with empty strings. Here every pre-existing key is preserved and only the
    expected-config keys are set.

    NEVER touched by this function: the private key, ``client_id`` /
    ``client_secret``, ``webhook_secret``, the broker OAuth secret, the
    webhook-ingress secret, and Lambda environment. In particular the broker's
    ``CALLBACK_URL`` is not written — that would reverse #2708's runtime
    derivation and pin a value that goes stale with no self-heal.

    Logs actor, timestamp, and the before/after of each key it changes.
    Returns True when the record was written. Never raises.
    """
    import json

    import boto3
    from botocore.exceptions import ClientError

    env = _get_environment()
    region = os.environ.get("AWS_REGION", "us-east-1")
    meta_path = f"adp/{env}/github-app/adp-agent-platform-meta"

    try:
        sm = boto3.client("secretsmanager", region_name=region)
    except Exception as exc:  # noqa: BLE001
        logger.warning("revalidate-app: cannot create Secrets Manager client: %s", exc)
        return False

    meta: dict[str, Any] = {}
    try:
        raw = sm.get_secret_value(SecretId=meta_path).get("SecretString", "")
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                meta = parsed
    except (ClientError, json.JSONDecodeError, TypeError) as exc:
        logger.info("revalidate-app: could not read %s (%s); nothing to update", meta_path, exc)
        return False

    if not meta:
        # No App metadata to attach an expected-config record to. Creating the
        # secret here would invent App state the registration flow owns.
        logger.info("revalidate-app: no App metadata at %s; skipping expected-config write", meta_path)
        return False

    expected_config = _resolve_expected_app_config(existing=meta)

    changes: list[str] = []
    for key, new_value in expected_config.items():
        old_value = meta.get(key)
        if old_value != new_value:
            changes.append(f"{key}: {old_value!r} -> {new_value!r}")

    if not changes:
        logger.info(
            "event=app_config_expected_record actor=%s timestamp=%s result=unchanged",
            actor,
            datetime.now(UTC).isoformat(),
        )
        return True

    meta.update(expected_config)
    try:
        sm.put_secret_value(SecretId=meta_path, SecretString=json.dumps(meta))
    except Exception as exc:  # noqa: BLE001
        logger.warning("revalidate-app: could not write expected config to %s: %s", meta_path, exc)
        return False

    # Safe to log: expected_* keys hold URLs, permission names and event names —
    # no credentials. Credential keys are never in `changes` because
    # _resolve_expected_app_config never emits them.
    logger.info(
        "event=app_config_expected_record actor=%s timestamp=%s changed=[%s]",
        actor,
        datetime.now(UTC).isoformat(),
        "; ".join(changes),
    )
    return True


async def revalidate_app_config(*, actor: str) -> dict[str, Any]:
    """Re-check the registered App's configuration against GitHub, on demand.

    Issue #4017: the explicit re-sync/repair action behind
    ``POST /github/app/revalidate``. GitHub fires no event when an admin edits
    App settings, so without this the only signal is the next failure.

    Read-only against GitHub. The only write is the expected-config record via
    ``_record_expected_app_config`` (see its docstring for the constraints).
    Bypasses the drift throttle deliberately — it is an operator-initiated,
    admin-gated action, and a button that returned stale cached results would be
    indistinguishable from a broken one. It clears the caches afterwards so the
    settings page immediately agrees with what the operator just saw.
    """
    app_id, private_key = await asyncio.to_thread(_get_github_app_credentials)
    if not app_id or not private_key:
        return {
            "checked": False,
            "warnings": ["No GitHub App is registered for this deployment, so there is no configuration to validate."],
            "expected_config_recorded": False,
            "message": "No GitHub App registered.",
        }

    pem = _normalize_pem(private_key)
    stored = await asyncio.to_thread(_read_expected_app_config)

    # Live resolution wins; the stored value is a fallback (see
    # _compute_app_config_drift for why that ordering matters).
    expected_webhook_url = await asyncio.to_thread(_resolve_expected_webhook_url)
    if not expected_webhook_url:
        expected_webhook_url = stored.get("expected_webhook_url", "") or ""

    check = await check_app_config(app_id=app_id, pem=pem, expected_webhook_url=expected_webhook_url)

    warnings = list(check.warnings)
    if not check.reachable:
        warnings.append(
            "Could not read the App's configuration from GitHub. The checks below are "
            "unknown, not failed — retry, or verify the App still exists and its "
            "credentials are valid."
        )

    expected_config_recorded = await asyncio.to_thread(_record_expected_app_config, actor=actor)

    expected_callback_url = await asyncio.to_thread(_resolve_expected_oauth_callback_url)
    if not expected_callback_url:
        expected_callback_url = stored.get("expected_callback_url", "") or ""

    slug = check.app_slug or stored.get("app_slug", "") or ""

    # The operator just asked for the truth; make sure the next page load shows
    # it rather than a pre-repair cached verdict.
    _invalidate_verification_cache()

    drifted = [
        name
        for name, state in (("webhook URL", check.webhook_url_matches), ("permissions", check.permissions_match), ("events", check.events_match))
        if state is False
    ]
    if not check.reachable:
        message = "Could not reach GitHub to validate the App configuration."
    elif drifted:
        message = "App configuration has drifted: " + ", ".join(drifted) + "."
    else:
        message = "App configuration matches this deployment."

    logger.info(
        "revalidate-app: actor=%s reachable=%s webhook=%s permissions=%s events=%s recorded=%s",
        actor,
        check.reachable,
        check.webhook_url_matches,
        check.permissions_match,
        check.events_match,
        expected_config_recorded,
    )

    return {
        "checked": check.reachable,
        "app_webhook_url_matches": check.webhook_url_matches,
        "app_permissions_match": check.permissions_match,
        "app_events_match": check.events_match,
        "expected_callback_url": expected_callback_url or None,
        "app_oauth_settings_url": f"https://github.com/settings/apps/{slug}/oauth" if slug else None,
        "warnings": warnings,
        "expected_config_recorded": expected_config_recorded,
        "message": message,
    }


async def _find_orphaned_installations(
    *,
    tenant_ids: list[str],
    known_installation_ids: set[int],
) -> list[tuple[str, int]]:
    """Find installations known to DynamoDB but with no Postgres row (Issue #4016, 🔴-1).

    This is the whole reason verification cannot simply hang off ChannelTenantMap.
    The webhook Lambda's auto-register path writes DynamoDB only — it never
    writes Postgres — so the exact tenant this feature exists to diagnose has no
    ChannelTenantMap row at all, and ``list_connections`` used to return an empty
    list for it. The card would have rendered green on healthy deployments and
    blank on the broken one.

    Returns (tenant_id, installation_id) pairs to surface as synthetic entries.
    Scoped to the caller's own tenants — no cross-org reads.
    """
    from src.admin.identity_index import IdentityIndexClient

    if not tenant_ids:
        return []

    try:
        client = IdentityIndexClient()
    except Exception as exc:  # noqa: BLE001
        logger.info("verification: could not construct identity-index client for orphan scan: %s", exc)
        return []

    results = await asyncio.gather(
        *(client.get_reverse_installation_identity(tid) for tid in tenant_ids),
        return_exceptions=True,
    )

    orphans: list[tuple[str, int]] = []
    for tenant_id, result in zip(tenant_ids, results, strict=True):
        if isinstance(result, BaseException) or not result:
            continue
        raw_id = result.get("installation_id", {}).get("N")
        if not raw_id:
            continue
        try:
            install_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if install_id in known_installation_ids:
            continue
        logger.warning(
            "event=connection_verification issue=orphaned_installation tenant_id=%s installation_id=%d detail=known_to_dynamodb_but_no_postgres_row",
            tenant_id,
            install_id,
        )
        orphans.append((tenant_id, install_id))

    return orphans


async def list_connections(
    *,
    caller_org_id: str,
    caller_user_id: str,
    db: AsyncSession,
    github_client: GitHubAppClient | None = None,
    member_tenant_ids: list[str] | None = None,
    caller_is_admin: bool = False,
    caller_pg_user_id: str | None = None,
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

    Issue #3073: Computes can_manage per connection (admin OR installer).

    Issue #4016: Every returned connection carries a read-only ``verification``
    block, and the response is computed over the UNION of Postgres
    ChannelTenantMap rows and installations known only to DynamoDB — an install
    that never reached the gateway callback used to be invisible here, which is
    precisely the fail-soft this issue exists to close. Admin callers also get a
    ``platform_verification`` block. All checks are read-only: nothing is
    seeded, written, or healed from this path.
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

    # Issue #4016: NO early return on an empty result set. A tenant whose
    # installation was auto-registered by the webhook Lambda has DynamoDB rows
    # and no ChannelTenantMap row at all; returning [] here rendered the settings
    # page blank for exactly the broken case the card must diagnose.

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
    # Issue #5184: installation_id → whether its repository list came from a live
    # GitHub read. Collected here and merged into the verification blocks below,
    # which are computed in one gather after the loop.
    repositories_live_by_install: dict[int, bool] = {}
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
        # Issue #5184: remember WHICH of the two we served. None here means the
        # live read failed, and a snapshot must not be presented as proof of
        # current access to a specific repository.
        repositories_live_by_install[install_id] = repositories is not None
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

        # Issue #3073: Compute can_manage — admin OR the installer.
        can_manage = caller_is_admin or (
            caller_pg_user_id is not None and mapping.installed_by_user_id is not None and caller_pg_user_id == mapping.installed_by_user_id
        )

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
                can_manage=can_manage,
                tenant_id=tenant_id,
                tenant_name=tenant_name,
                is_active_tenant=is_active_tenant,
            )
        )

    # -----------------------------------------------------------------------
    # Issue #4016: union in installations known only to DynamoDB (🔴-1), then
    # decorate every entry with its verification block.
    # -----------------------------------------------------------------------
    known_install_ids = {c.installation_id for c in connections}
    # adp-default is excluded from the orphan scan: its installs are per-USER
    # and the reverse identity row is keyed per-TENANT, so a single row there
    # cannot be attributed to a user and surfacing it would leak another
    # personal-account holder's installation.
    orphans = await _find_orphaned_installations(
        tenant_ids=[t for t in tenant_ids_to_query if t != adp_default_id],
        known_installation_ids=known_install_ids,
    )
    for tenant_id_o, install_id_o in orphans:
        connections.append(
            GitHubConnectionItem(
                provider="github",
                installation_id=install_id_o,
                account_login="(not recorded)",
                account_type="Organization",
                repository_selection="selected",
                repository_count=0,
                repositories=[],
                installed_at=None,
                configure_url=f"https://github.com/settings/installations/{install_id_o}",
                manage_url=f"https://github.com/settings/installations/{install_id_o}",
                # Nothing to manage: with no Postgres row, disconnect has no row
                # to delete. Surfacing it as manageable would offer a button
                # that cannot work.
                can_manage=False,
                tenant_id=tenant_id_o if member_tenant_ids else None,
                tenant_name=tenant_name_map.get(tenant_id_o) if member_tenant_ids else None,
                is_active_tenant=(tenant_id_o == caller_org_id) if member_tenant_ids else None,
            )
        )

    verifications = await asyncio.gather(
        *(
            _compute_connection_verification(
                installation_id=c.installation_id,
                org_id=c.tenant_id or caller_org_id,
                record_present=c.installation_id in known_install_ids,
                # Absent for an orphan entry: no repository read was attempted
                # for it at all, which is None rather than False.
                repositories_live=repositories_live_by_install.get(c.installation_id),
            )
            for c in connections
        ),
        return_exceptions=True,
    )
    for conn, verification in zip(connections, verifications, strict=True):
        if isinstance(verification, ConnectionVerification):
            conn.verification = verification
        else:
            # Fail-soft: an all-unknown block still renders, it just renders amber.
            logger.info(
                "verification: could not compute checks installation_id=%d: %s",
                conn.installation_id,
                verification,
            )
            # Issue #5184: the repository-list provenance is known independently
            # of these checks (it was decided when the list was fetched above),
            # so it survives their failure rather than degrading to unknown.
            conn.verification = ConnectionVerification(
                repositories_live=repositories_live_by_install.get(conn.installation_id),
            )

    # 🔴-2: platform checks read deployment-global singletons, so they go only
    # to callers who can manage connections.
    platform_verification: PlatformVerification | None = None
    if caller_is_admin:
        try:
            platform_verification = await _compute_platform_verification()
        except Exception as exc:  # noqa: BLE001
            logger.info("verification: platform checks unavailable: %s", exc)
            platform_verification = PlatformVerification()

    return ConnectionsListResponse(
        connections=connections,
        platform_verification=platform_verification,
    )


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
    caller_user_id: str | None = None,
    caller_is_admin: bool = True,
) -> DeleteConnectionResponse:
    """Revoke a GitHub App installation and every local record of its authority.

    Steps:
    1. Resolve the owning tenant from ``installation_id`` and authorize the caller.
    2. Revoke at GitHub. Abort, changing nothing, unless it succeeds.
    3. Delete the local authority records, in ONE transaction.
    4. Best-effort: drop the projections and caches that mirror the deleted claims.

    Issue #3073: Non-admin callers are allowed if their Postgres user ID matches
    the connection's installed_by_user_id. This lets the installer manage their
    own connection without role elevation.

    #5664 (A10) rewrote steps 1-4. Three defects, each of which alone left an
    installation's authority intact after a "successful" disconnect:

    **It keyed ownership on the wrong column.** Both the check and the delete
    matched ``provider_scope_id`` — the GitHub ACCOUNT id — which
    ``internal/routes.py`` documents as explicitly NOT the installation key
    ("the installation id now lives in its own column,
    ``channel_tenant_map.installation_id``, which is where uniqueness is
    enforced"). Two consequences: reinstalling an account produced a row whose
    account id matched but whose ``installation_id`` was a different, still-live
    installation, so disconnecting id A deleted the mapping for id B; and rows
    written with ``provider_scope_id == installation_id`` (by
    ``identity/organizations_service.py``) were invisible to the delete entirely.
    Ownership now comes from ``resolve_installation_owner``, the canonical resolver
    that unions both records of ownership and fails closed on a quarantined
    cross-tenant conflict.

    **It deleted one of several records of the same fact.** Only the
    ``ChannelTenantMap`` row went; ``organizations.github_installation_ids``
    survived. That JSON list is what ``internal/routes.py::resolve_installation``
    answers from, which is the oracle the webhook Lambda's auto-register gate
    consults — so the Lambda re-created the DynamoDB routing rows from it on the
    very next webhook. Deleting the projection without clearing the Postgres claim
    it is derived from is self-undoing, which is why order matters here: Postgres
    first, in a transaction, and only then the projections.

    **It reported success it had not achieved.** The GitHub revoke was wrapped in
    ``except Exception: logger.warning(...)`` and execution continued to
    ``deleted=True``. The one step an operator cannot perform locally could fail
    silently. Now it is a precondition: no revoke, no disconnect, nothing changed.

    Idempotent and recoverable by construction. The provider call comes first
    precisely so that recovery is possible — the local claims are the only thing
    that authorizes this operation, so deleting them before the revoke would leave
    a failed attempt with no authority to retry under (the retry resolves
    NOT_FOUND and raises, while the installation stays live at GitHub). Because
    ``delete_installation`` treats 404 as success, a retry after a crash at any
    point finds the provider side already done and completes the local half, and
    every local step is "delete if present".

    ``residual`` marks the honest limit of that. Once the Postgres claims are gone
    the installation no longer resolves, so re-running raises ``NOT_FOUND`` and
    CANNOT retry a failed projection cleanup. That is why the security-critical
    forward routing row gets its own in-line fallback rather than relying on a
    retry, and why anything still listed is reported for operator action instead of
    being described as self-healing. What remains is safe to leave pending:
    Postgres is authoritative, so a surviving projection is a stale cache rather
    than a live grant.

    Note what this does NOT do. It does not delete the per-tenant App secret, the
    bot identity, or the installer's ``org_admin`` membership. Those are shared
    across a tenant's installations, or are records of something that genuinely
    happened, and destroying them here would exceed "disconnect this
    installation". They are listed in the runbook as operator follow-ups.

    Raises:
        PermissionError — installation not owned by caller's tenant, or caller
                          lacks permission (not admin and not installer)
        ValueError      — installation not found, or App credentials unavailable
                          so the provider revoke cannot be attempted
        RuntimeError    — GitHub refused or could not complete the uninstall.
                          Nothing was changed locally; the call is retryable.
    """
    from sqlalchemy import delete as sa_delete
    from sqlalchemy import select

    from src.admin.installations.resolver import OwnerState, resolve_installation_owner
    from src.shared.models.organization import Organization
    from src.shared.models.vault import ChannelTenantMap

    app_id, private_key = _get_github_app_credentials()
    if github_client is None and app_id and private_key:
        github_client = GitHubAppClient(app_id=app_id, private_key_pem=private_key)

    scope_id = str(installation_id)

    # 1. Ownership, from the canonical resolver rather than a hand-rolled lookup.
    #    `attest=False`: this is a REVOCATION. Requiring a network attestation
    #    would make a disconnect impossible exactly when it is most needed — the
    #    App already deleted at GitHub, credentials rotated, or the API down — and
    #    a local claim is sufficient authority to delete a local claim.
    owner, state = await resolve_installation_owner(installation_id, db=db)

    if state is OwnerState.NOT_FOUND:
        raise ValueError(f"Installation {installation_id} is not connected to any ADP tenant")
    if state is OwnerState.AMBIGUOUS:
        # Two tenants claim it and migration 026 deliberately did not pick a
        # winner. Deleting "the" mapping here would resolve that conflict by
        # guessing, and in the caller's favour.
        raise PermissionError(
            f"Installation {installation_id} is claimed by more than one ADP tenant and is quarantined. An operator must resolve the conflict first."
        )

    if state is OwnerState.UNATTESTABLE:
        # The only claim is a tenant's own `github_installation_ids` assertion with
        # no server-written map row behind it. The resolver withholds ownership
        # there because a self-assertion must not GRANT authority — but this
        # operation only ever REMOVES it. Refusing here would make a
        # self-asserted claim permanently undeletable, leaving the tenant listed
        # as an owner with no way to stop being one, which is the opposite of the
        # security property the resolver is protecting. So: a caller may always
        # retract their OWN tenant's assertion, and only their own.
        org_claiming = await db.get(Organization, caller_org_id)
        claimed = [str(i) for i in (org_claiming.github_installation_ids or [])] if org_claiming else []
        if scope_id not in claimed:
            raise PermissionError(f"Installation {installation_id} belongs to a different ADP tenant")
        if not caller_is_admin:
            # No map row exists, so there is no recorded installer to fall back
            # on; admin is the only standing that can retract a tenant-level claim.
            raise PermissionError(
                f"You do not have permission to disconnect installation {installation_id}. "
                "Only workspace admins or the user who installed it can disconnect."
            )
    elif owner is None or owner.tenant_id != caller_org_id:
        raise PermissionError(f"Installation {installation_id} belongs to a different ADP tenant")

    # The map rows this installation owns, keyed on installation_id. Fetched before
    # the delete both for the installer authorization below and because
    # `provider_scope_id` is needed to clear the account-keyed rows that predate
    # the installation_id column (migration 026 backfilled it, but a row written
    # before that backfill can still carry NULL).
    mapped = (
        (
            await db.execute(
                select(ChannelTenantMap).where(
                    ChannelTenantMap.provider == "github",
                    ChannelTenantMap.org_id == caller_org_id,
                    ChannelTenantMap.installation_id == scope_id,
                )
            )
        )
        .scalars()
        .all()
    )

    # Issue #3073: Authorization — workspace admin OR the installer who created
    # this connection. The tenant ownership check above is a hard precondition
    # (unchanged); this is AND-ed on top.
    if not caller_is_admin:
        installers = {m.installed_by_user_id for m in mapped if m.installed_by_user_id}
        if not (caller_user_id is not None and caller_user_id in installers):
            raise PermissionError(
                f"You do not have permission to disconnect installation {installation_id}. "
                "Only workspace admins or the user who installed it can disconnect."
            )

    # 2. Revoke at the provider FIRST, and abort if it does not succeed.
    #
    #    Ordering is the whole of item 3's "retry must be recoverable". The local
    #    claims are the only thing that authorizes this operation, so deleting
    #    them before the provider call destroys the authority a retry would need:
    #    the second attempt resolves NOT_FOUND, raises, and the installation stays
    #    live at GitHub forever with no local record that it was ever ours. That
    #    is the unrecoverable direction. Provider-first inverts it — every failure
    #    leaves the local claims intact, so the operation is simply retryable.
    #
    #    Aborting rather than continuing is also what makes the report honest. The
    #    old code swallowed the failure and returned `deleted=True`; returning
    #    "partially revoked" instead would still leave the caller to reason about
    #    a half-state. Nothing changed, so there is nothing to reconcile.
    #
    #    `delete_installation` already treats 404 as success, which is what makes
    #    a retry after a mid-operation crash idempotent: the second call finds the
    #    installation already gone at GitHub and proceeds to finish the local half.
    #
    #    An operator who needs to cut local routing while GitHub is unreachable is
    #    not blocked: that is `org_connections.detach_github`, which is explicitly
    #    a local-only detach.
    if github_client is None:
        raise ValueError(
            f"Cannot revoke installation {installation_id}: GitHub App credentials are unavailable, "
            "so the installation cannot be uninstalled at GitHub. Nothing has been changed."
        )
    try:
        await github_client.delete_installation(installation_id)
    except Exception as exc:
        logger.warning(
            "event=github_installation_revoke_failed installation_id=%s org=%s error=%s outcome=aborted_nothing_changed",
            scope_id,
            caller_org_id,
            exc,
        )
        raise RuntimeError(
            f"GitHub could not uninstall installation {installation_id} ({exc}). Nothing has been changed — retry the disconnect."
        ) from exc

    # 3. Remove the local authority records, together. Both are independently
    #    sufficient to grant ownership (see `resolve_installation_owner`, which
    #    unions them), so a commit that dropped one and not the other would leave
    #    the installation fully routable while presenting as disconnected.
    org = await db.get(Organization, caller_org_id)
    old_github_ids = [str(i) for i in (org.github_installation_ids or [])] if org else []
    remaining = [i for i in old_github_ids if i != scope_id]
    if org is not None:
        org.github_installation_ids = remaining
        # Per-account, not per-installation: cleared only once nothing is left, or
        # the surviving installations would become UNATTESTABLE and lose routing.
        # Same rule as `org_connections.detach_github`.
        if not remaining:
            org.github_org_id = None
            org.github_app_id = None

    if mapped:
        await db.execute(
            sa_delete(ChannelTenantMap).where(
                ChannelTenantMap.provider == "github",
                ChannelTenantMap.org_id == caller_org_id,
                ChannelTenantMap.installation_id == scope_id,
            )
        )

    await db.commit()

    logger.warning(
        "event=github_installation_disconnected installation_id=%s org=%s map_rows=%d remaining=%d outcome=local_authority_revoked",
        scope_id,
        caller_org_id,
        len(mapped),
        len(remaining),
    )

    # 4. Projections and caches. Best-effort and individually reported: each
    #    mirrors a Postgres claim that is already gone, so a reader that still
    #    sees one is stale rather than authoritative — but a stale routing row is
    #    how a disconnected installation keeps delivering events, so an operator
    #    must be told which cleanup to retry.
    residual = await _revoke_installation_projections(
        installation_id=installation_id,
        org_id=caller_org_id,
        remaining_github_ids=remaining,
        old_github_ids=old_github_ids,
        cognito_client_ids=[str(c) for c in (org.cognito_client_ids or [])] if org else [],
    )

    _cache_invalidate(installation_id)
    _repo_cache_invalidate(installation_id)
    # The verification caches key on the tenant, not the installation, and their
    # own docstring says they clear "after register / rotate / disconnect" — the
    # disconnect half was never wired up, so the connections card kept reporting
    # this installation as seeded and indexed for up to its TTL.
    _invalidate_verification_cache()

    warning = None
    if residual:
        warning = "Access is revoked. Some index cleanups did not complete; re-running this disconnect retries exactly those."

    return DeleteConnectionResponse(
        deleted=True,
        installation_id=installation_id,
        # Unconditionally True: step 2 aborts the whole operation unless GitHub
        # confirmed the uninstall, so reaching here means it succeeded. The field
        # stays in the response because it is the fact a caller needs to know and
        # the guarantee behind it may change; what it must never do is report True
        # on a call that did not revoke, which is the defect this replaced.
        provider_revoked=True,
        residual=residual,
        warning=warning,
    )


async def _revoke_installation_projections(
    *,
    installation_id: int,
    org_id: str,
    remaining_github_ids: list[str],
    old_github_ids: list[str],
    cognito_client_ids: list[str],
) -> list[str]:
    """Drop the DDB projections that mirror a now-deleted installation claim.

    #5664 (A10). Split out of ``delete_connection`` so each cleanup can fail on its
    own and be named in the response, rather than one exception skipping the rest.

    Returns the names of the cleanups that did NOT complete. An empty list means
    every projection is consistent with Postgres.

    Ordering note: this runs strictly AFTER the Postgres claims are committed.
    The webhook Lambda re-derives these rows from the gateway's
    ``resolve_installation`` (which reads ``organizations.github_installation_ids``)
    and writes them back on a miss, so deleting a projection while its source claim
    still exists is undone by the next inbound event.
    """
    residual: list[str] = []
    scope_id = str(installation_id)

    # Forward row: github_installation_id -> org. The webhook hot path reads DDB
    # FIRST, so this is the row that keeps delivering events to the tenant after a
    # disconnect — the one cleanup that is itself a security property rather than
    # mere tidiness. It therefore gets two independent attempts.
    try:
        from src.admin.identity.identity_index_writer import IdentityIndexWriter

        writer = IdentityIndexWriter()
        await writer.sync_org_channels(
            org_id=org_id,
            github_installation_ids=remaining_github_ids,
            cognito_client_ids=cognito_client_ids,
            old_github_installation_ids=old_github_ids,
        )
    except Exception:
        logger.exception(
            "event=github_installation_projection_cleanup_failed installation_id=%s org=%s phase=forward_row",
            scope_id,
            org_id,
        )
        # Fall back to deleting just the revoked row. `sync_org_channels` does a
        # whole-org diff (both channel families, upserts for survivors), so it has
        # many more ways to fail than this single targeted DeleteItem — and the
        # only part that must happen for routing to stop is this one key. Both
        # paths are idempotent, so trying the narrow one after the broad one costs
        # nothing and is not merely a duplicate attempt.
        try:
            from src.admin.identity_index import IdentityIndexClient

            if not await IdentityIndexClient().delete_identity("github_installation_id", scope_id):
                residual.append("identity_index_forward_row")
        except Exception:
            residual.append("identity_index_forward_row")
            logger.exception(
                "event=github_installation_projection_cleanup_failed installation_id=%s org=%s phase=forward_row_fallback",
                scope_id,
                org_id,
            )

    # Reverse row: org -> installation_id, read by `resolve_installation_for_tenant`
    # (adp-trigger, scheduled work). Nothing in the repo deleted this row on any
    # path, so a revoked installation stayed the tenant's chosen credential for
    # outbound dispatch. Touched only when it still names the installation being
    # revoked — a row naming a SURVIVING installation is correct and must be left
    # exactly as it is, including its `auto_registered` flag.
    try:
        from src.admin.identity_index import IdentityIndexClient

        index = IdentityIndexClient()
        existing = await index.get_reverse_installation_identity(org_id)
        current = (existing or {}).get("installation_id", {}).get("N")
        if current is not None and str(current) == scope_id:
            # Delete first, unconditionally, even when a survivor will inherit the
            # row. `write_reverse_installation_identity` refuses to clobber a row
            # that lacks `auto_registered` (it is Postgres-owned) and returns True
            # for that no-op — so repointing in place would report success while
            # leaving the revoked installation as the tenant's dispatch
            # credential. Deleting first makes the subsequent write a create,
            # which that guard permits.
            if await index.delete_identity("org_installation", org_id):
                if remaining_github_ids and not await index.write_reverse_installation_identity(org_id, int(remaining_github_ids[0])):
                    # The revoked id is gone, which is the security-relevant half.
                    # A survivor simply has no reverse row yet; the Lambda's #3860
                    # self-heal re-derives it from the forward row.
                    residual.append("identity_index_reverse_row_repoint")
            else:
                residual.append("identity_index_reverse_row")
    except Exception:
        residual.append("identity_index_reverse_row")
        logger.exception(
            "event=github_installation_projection_cleanup_failed installation_id=%s org=%s phase=reverse_row",
            scope_id,
            org_id,
        )

    if residual:
        logger.warning(
            "event=github_installation_residual_state installation_id=%s org=%s residual=%s",
            scope_id,
            org_id,
            ",".join(residual),
        )
    return residual


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
        # Issue #4017: read from the shared constants so the manifest we ASK for
        # and the drift check that later VERIFIES it can never disagree.
        "default_permissions": dict(_EXPECTED_APP_PERMISSIONS),
        "default_events": list(_EXPECTED_APP_EVENTS),
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

    # 1a. Authority (#5664). Possession of the state token proves only that SOME
    # request started this flow; it is not authority to replace the deployment's
    # shared credentials. Re-derive platform-admin standing from the recorded
    # initiator, BEFORE the nonce is consumed, so a refusal burns nothing and the
    # legitimate admin can still complete the flow.
    await _assert_platform_setup_authority(nonce=nonce, db=db)

    # 1b. Overwrite guard (#5664). `_check_existing_app_secret` previously ran only
    # in register-app-START — a different, earlier request — so by the time this
    # callback wrote secrets the guard had long since passed and was never
    # re-evaluated. `_store_app_credentials` then does create_secret -> on
    # ResourceExistsException -> put_secret_value, an unconditional overwrite of
    # live App credentials, the webhook signing secret and the OAuth secret. Check
    # again here, before any write, so a second registration cannot silently
    # replace a working App and break every tenant's connection.
    existing_app = _check_existing_app_secret()
    if existing_app is not None:
        logger.warning(
            "register-app-callback: refusing to overwrite already-registered App id=%s jti=%s",
            existing_app[0],
            state,
        )
        raise SetupAuthorityError("A GitHub App is already registered for this deployment. Disconnect the existing App before registering a new one.")

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
    # Issue #4016: same reasoning for the onboarding verification checks.
    _invalidate_verification_cache()

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
            # Issue #2724: register-app-callback also runs behind a consumed
            # nonce (see the docstring above), so this is a deliberate,
            # authenticated registration → register_flow (trusted).
            await _upsert_org_tenant_shell(
                owner_login=owner_login,
                github_org_id=owner_id,
                github_app_id=app_id,
                db=db,
                created_via="register_flow",
            )

    # Issue #2952 (D9): Chained onboarding redirect — send the admin directly
    # to GitHub's install page so they can pick repos immediately.
    if app_slug:
        return f"https://github.com/apps/{app_slug}/installations/new"

    # Fallback: return to connections page (should not happen with a valid slug).
    if login_enabled:
        return "/settings/connections?github_app=registered"
    return "/settings/connections?github_app=registered&login_enabled=false"


# ---------------------------------------------------------------------------
# Manual registration (Issue #3360)
# ---------------------------------------------------------------------------


def _normalize_pem(raw: str) -> str:
    """Normalize a PEM private key — handle escaped \\n and whitespace.

    Accepts both:
      - Real newlines (copy-pasted from a .pem file)
      - Escaped \\n literals (from .env files, JSON, or single-line paste)

    Returns the PEM with real newlines and no trailing whitespace.
    """
    # Replace escaped \n (literal two chars) with real newline
    normalized = raw.replace("\\n", "\n")
    # Collapse any \r\n from Windows pastes
    normalized = normalized.replace("\r\n", "\n")
    # Trim trailing whitespace/newlines
    normalized = normalized.strip()
    # Ensure trailing newline (PEM convention)
    if not normalized.endswith("\n"):
        normalized += "\n"
    return normalized


async def register_app_manual(
    *,
    app_id: str,
    private_key: str,
    webhook_secret: str = "",
    client_id: str = "",
    client_secret: str = "",
) -> dict:
    """Import an existing GitHub App by validating credentials and storing them.

    Issue #3360: Manual registration path for admins who already have a GitHub
    App (created outside ADP, or migrating from another deployment).

    Steps:
      1. Normalize the PEM key.
      2. Mint a JWT and call GET /app to validate credentials.
      3. Check deployment configuration (webhook URL, permissions, events).
      4. Store via _store_app_credentials (same as callback path).
      5. Invalidate caches.

    Returns a dict with: registered, app_id, app_slug, app_name, login_enabled, warnings.

    Raises:
        HTTPException(400) — invalid PEM or mismatched app_id/key
    """
    import jwt as pyjwt

    from .github_client import _mint_app_jwt

    warnings: list[str] = []

    # 1. Normalize PEM
    pem = _normalize_pem(private_key)

    # 2. Validate credentials: mint a single JWT and call GET /app
    try:
        token = _mint_app_jwt(app_id, pem)
    except (ValueError, TypeError, pyjwt.exceptions.PyJWTError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid private key: could not encode JWT. {exc}",
        ) from exc

    # Call GET /app to verify the app_id + key pair
    app_slug = ""
    app_name = ""
    app_permissions: dict = {}
    app_events: list[str] = []
    app_webhook_url = ""

    _auth_headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                "https://api.github.com/app",
                headers=_auth_headers,
            )

            if resp.status_code == 401:
                raise HTTPException(
                    status_code=400,
                    detail="App ID and private key don't match. GitHub returned 401 Unauthorized.",
                )
            if resp.status_code != 200:
                raise HTTPException(
                    status_code=400,
                    detail=f"GitHub API returned {resp.status_code} when validating App credentials.",
                )

            data = resp.json()
            app_slug = data.get("slug", "")
            app_name = data.get("name", "")
            app_permissions = data.get("permissions", {})
            app_events = data.get("events", [])

            # Fetch webhook config from the dedicated endpoint (GET /app
            # does NOT include webhook URL — it lives at GET /app/hook/config).
            try:
                hook_resp = await client.get(
                    "https://api.github.com/app/hook/config",
                    headers=_auth_headers,
                )
                if hook_resp.status_code == 200:
                    hook_data = hook_resp.json()
                    app_webhook_url = hook_data.get("url", "")
            except Exception as hook_exc:
                logger.debug("Could not fetch /app/hook/config: %s", hook_exc)

    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach GitHub API to validate App credentials: {exc}",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Failed to validate App credentials against GitHub: {exc}",
        ) from exc

    # 3. Non-blocking configuration verification.
    #
    # Issue #4017: the comparison itself now lives in diff_app_config() so that
    # this flow and the read-time drift check share ONE implementation. A drift
    # checker built on a second copy of the expected config could report drift on
    # an App that is exactly what we asked GitHub for.
    #
    # We pass the data we already fetched above rather than calling
    # check_app_config(): that would mint a second JWT and re-issue both GETs,
    # and its fail-soft contract would swallow the credential errors this flow
    # must raise.
    config_check = diff_app_config(
        app_slug=app_slug,
        app_name=app_name,
        actual_webhook_url=app_webhook_url,
        actual_permissions=app_permissions,
        actual_events=app_events,
        expected_webhook_url=_resolve_expected_webhook_url(),
    )
    warnings.extend(config_check.warnings)

    # 3d. OAuth credentials warning
    if not client_id or not client_secret:
        warnings.append(
            "OAuth credentials (client_id/client_secret) not provided. "
            "'Sign in with GitHub' will not work until these are configured. "
            "Find them in the App's settings under 'Client secrets'."
        )

    # 4. Store credentials via the shared helper
    store_result = await _store_app_credentials(
        app_id=app_id,
        app_slug=app_slug,
        pem=pem,
        client_id=client_id,
        client_secret=client_secret,
        webhook_secret=webhook_secret,
    )

    # login_enabled is true only when OAuth credentials were actually provided
    # AND the store succeeded. _store_app_credentials returns True even when
    # nothing was written (defensive branch), so we must gate on the inputs.
    login_enabled = bool(client_id and client_secret) and store_result

    # 5. Invalidate caches
    get_github_app_provider().invalidate()
    _invalidate_login_enabled_cache()
    # Issue #4016: same reasoning for the onboarding verification checks.
    _invalidate_verification_cache()

    logger.info(
        "register-app-manual: App imported successfully id=%s slug=%s login_enabled=%s warnings=%d",
        app_id,
        app_slug,
        login_enabled,
        len(warnings),
    )

    return {
        "registered": True,
        "app_id": app_id,
        "app_slug": app_slug,
        "app_name": app_name,
        "login_enabled": login_enabled,
        "warnings": warnings,
    }


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

        # Issue #3360: When optional fields (webhook_secret, client_id,
        # client_secret) are empty, merge with existing meta blob rather than
        # overwriting with blanks. This preserves values written by a prior
        # registration or manual setup.
        #
        # Issue #4017: this read is now UNCONDITIONAL (it used to be skipped when
        # all three optional fields were supplied). The expected-config keys added
        # below must survive a full re-registration, and the merge below is
        # unchanged — `x or existing_meta.get(x)` only consults the existing blob
        # when the incoming value is empty, so reading it always cannot alter
        # #3360's behaviour.
        existing_meta: dict[str, str] = {}
        try:
            existing_resp = sm.get_secret_value(SecretId=meta_path)
            existing_raw = existing_resp.get("SecretString", "")
            if existing_raw:
                existing_meta = json.loads(existing_raw)
        except Exception:
            pass  # No existing meta or unreadable — proceed with empty

        # Issue #4017: record the configuration we asked GitHub for, so drift can
        # later be reported against a known baseline and a support engineer can
        # see what the callback URL was supposed to be. These are NOT credentials
        # and are read back by the drift check and the status card.
        #
        # Resolved best-effort: an unresolvable value is simply not recorded
        # (absent ⇒ "unknown" downstream, never "drift"). Existing values are
        # preserved when a re-registration cannot re-resolve them.
        expected_config = _resolve_expected_app_config(existing=existing_meta)

        meta_payload = json.dumps(
            {
                "app_id": app_id,
                "app_slug": app_slug,
                "client_id": client_id or existing_meta.get("client_id", ""),
                "client_secret": client_secret or existing_meta.get("client_secret", ""),
                "webhook_secret": webhook_secret or existing_meta.get("webhook_secret", ""),
                **expected_config,
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
                # Issue #4016: ERROR, not WARNING. The consequence is that every
                # single GitHub delivery for this deployment fails signature
                # validation with a 401 — a total webhook outage that a WARNING
                # buried in a successful registration's log stream. The
                # verification card now also reports it via
                # platform_verification.webhook_secret.
                logger.error(
                    "event=register_app_webhook_secret_write_failed path=%s outcome=webhook_deliveries_will_fail_401 error=%s",
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
    created_via: str = "register_flow",
) -> str | None:
    """Upsert an org-tenant shell: Organization + Tenant + Department + Team.

    Issue #2952 (Rule 1): When a platform admin registers a GitHub App against
    an org, create the tenant structure so members can later auto-join. Also
    called from install_callback for public-App installs by unknown orgs.

    Idempotent: if the org already exists (by slug id), updates github_org_id
    and github_app_id if previously unset and returns the existing id.

    Issue #2724 (slice B): ``created_via`` records WHICH path created the row so
    the webhook auto-register gate can tell a deliberately-onboarded tenant from
    a shell the platform auto-created for whoever clicked Install on a public
    App. Callers on a nonce-authenticated path leave the default
    (``register_flow``); the unauthenticated no-nonce install callback MUST pass
    ``install_autocreate``.

    Provenance is stamped on **create only** — an existing row's provenance is
    never rewritten. An operator-created org that later receives a public-App
    install stays ``operator`` (it was always a real tenant), and an
    ``install_autocreate`` shell is not laundered into a trusted one by a later
    call on an authenticated path.

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
        # Issue #2724: created_via is deliberately NOT touched here — see the
        # docstring. Rewriting it would let a later authenticated call launder an
        # install_autocreate shell into a trusted tenant.
        logger.info(
            "org-tenant-shell: org %s already exists (idempotent), updated=%s created_via=%s (unchanged)",
            tenant_id,
            changed,
            existing.created_via,
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
        created_via=created_via,
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
        "org-tenant-shell: created org=%s github_org_id=%s github_app_id=%s created_via=%s",
        tenant_id,
        github_org_id,
        github_app_id,
        created_via,
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
                # Issue #3789: surface the real botocore error — the cause may be
                # a missing KMS grant (not just a SecretsManager IAM grant), and
                # hardcoding a hypothesis misdirects operators.
                logger.warning(
                    "get_app_status: AccessDenied reading %s — %s",
                    id_path,
                    str(exc),
                )
                raise HTTPException(
                    status_code=503,
                    detail="Unable to determine App registration status — access denied reading secrets. "
                    f"Check gateway role IAM and KMS grants. Error: {exc}",
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
    # Issue #4016: the verification card's checks are now stale.
    _invalidate_verification_cache()

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
    # Issue #4016: the App is gone, so every cached green check is now a lie.
    _invalidate_verification_cache()

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
