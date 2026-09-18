"""Internal service-to-service endpoints.

Issue #446: Vault Phase 2b — Magic-link identity linking flow

Endpoints (IAM-signed; internal only, not exposed to end users):
    POST /internal/v1/issue-magic-link   — ingest Lambda requests a magic-link URL
                                            for an unknown user event
    POST /internal/v1/resolve-user       — resolve provider identity to internal
                                            user_id; auto-provision shadow user if
                                            channel_tenant_map matches
    POST /internal/v1/resolve-installation
                                         — installation_id -> owning ADP tenant
    POST /internal/v1/github-installation-token
                                         — Issue #4272: mint a repo-scoped GitHub
                                            App installation token for an agent
                                            run, so the App private key never
                                            leaves the gateway

Authentication:
    A shared secret (BG_INTERNAL_API_KEY) is expected in the
    ``X-Internal-Api-Key`` header.  In production, rotate via Secrets Manager.
    Full SigV4 verification is a follow-up (tracked separately).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.admin.installations.resolver import (
    InstallationOwnershipError,
    OwnerState,
    assert_installation_owned_by,
)
from src.auth.magic_link import (
    issue_token,
    store_nonce,
)
from src.internal.auth_deps import verify_internal_or_irsa
from src.internal.credential_binding import resolve_installation_binding
from src.knowledge.github_app_service import (
    AGENT_RUN_PERMISSIONS,
    DEFAULT_IDENTITY,
    REVIEW_ACTION_VALUE,
    REVIEW_IDENTITY,
    REVIEW_IDENTITY_PERMISSIONS,
    SUPPORTED_IDENTITIES,
    ReviewerIdentityUnavailableError,
    mint_installation_token_with_expiry,
    resolve_reviewer_app_credentials,
    resolve_tenant_app_credentials,
)
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import new_uuid
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap, MagicLinkNonce, UserIdentity  # noqa: F401

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal"])


# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------


def _verify_internal_key(x_internal_api_key: str | None = Header(default=None)) -> None:
    """Validate the shared internal API key.

    Missing or wrong key → 403 (not 401) so external scanners don't learn that
    the endpoint exists from a WWW-Authenticate header.
    """
    settings = get_settings()
    expected = settings.internal_api_key
    if not expected:
        # Key not configured — reject all calls in a loud way so misconfiguration
        # is obvious in logs rather than silently open.
        logger.error("BG_INTERNAL_API_KEY is not set; all /internal/v1/* calls will be rejected")
        raise HTTPException(status_code=503, detail={"error": "not_configured", "message": "Internal API not configured"})
    if not x_internal_api_key or x_internal_api_key != expected:
        raise HTTPException(status_code=403, detail={"error": "forbidden", "message": "Invalid internal API key"})


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class IssueMagicLinkRequest(BaseModel):
    """Body for POST /internal/v1/issue-magic-link."""

    provider: str
    provider_user_id: str
    channel_context: str | None = None


class IssueMagicLinkResponse(BaseModel):
    magic_link_url: str


class ResolveUserRequest(BaseModel):
    """Body for POST /internal/v1/resolve-user."""

    provider: str
    provider_user_id: str
    channel_context: str | None = None


class ResolveUserResponse(BaseModel):
    user_id: str
    org_id: str
    team_id: str
    is_shadow: bool


class ResolveUserNotFoundResponse(BaseModel):
    magic_link_url: str


class ResolveInstallationRequest(BaseModel):
    """Body for POST /internal/v1/resolve-installation."""

    installation_id: str


class ResolveInstallationResponse(BaseModel):
    tenant_id: str
    # Issue #2724 (slice B): provenance of the owning organization row — which
    # path created it ("operator" | "register_flow" | "install_autocreate").
    # The webhook auto-register gate denies on "install_autocreate" unless the
    # deployment has opted into open onboarding, because that row could have
    # been self-created by the installing party via the unauthenticated
    # no-nonce install callback. Callers that predate this field must treat its
    # absence as "unknown" and fail open, never as "untrusted".
    created_via: str = "operator"


class GithubInstallationTokenRequest(BaseModel):
    """Body for POST /internal/v1/github-installation-token.

    Issue #4272. Note what is NOT here: the caller does not assert its tenant.
    The tenant is resolved server-side from the run's webhook-events row, so a
    prompt-injected worker cannot name a tenant it does not belong to.
    """

    installation_id: int
    repo_owner: str
    repo_name: str
    # The run's invocation id (= event_id in webhook-events). Required in
    # practice: the binding rejects a request without one.
    invocation_id: str | None = None
    purpose: str | None = None
    # Issue #5350: which App identity to mint as. "default" is the authoring
    # identity (every existing caller, unchanged). "review" asks for the distinct
    # reviewer App so a review is not a self-review — GitHub 422s APPROVE and
    # REQUEST_CHANGES from the PR's own author, which made every engine-authored PR
    # unapprovable. An unrecognised value is REJECTED rather than treated as
    # "default": a caller that asks for an identity this gateway does not know must
    # not be handed the authoring identity while believing it got a reviewing one.
    identity: str = DEFAULT_IDENTITY


class GithubInstallationTokenResponse(BaseModel):
    token: str
    # GitHub's own expiry, passed through verbatim. The worker's TokenManager
    # schedules its refresh from this; a locally-guessed "+1h" drifts and the run
    # dies mid-flight.
    expires_at: str
    # The App ID is a PUBLIC identifier, not a credential (only the private key
    # is secret). It is returned because the worker still needs it for two
    # non-secret purposes it previously read from the vault alongside the key: the
    # bot commit identity (`<app_id>+adp-agent[bot]@…`), and GH_APP_ID, which
    # gates whether the JS TokenManager initialises at all. Omitting it would put
    # the 1-hour silent-death path straight back.
    app_id: str
    # Issue #5350: which identity was ACTUALLY used, which is not always the one
    # requested. When "review" is asked for but no reviewer App is configured, the
    # mint falls back to the authoring identity and reports "default" here. That
    # honesty is the whole point: the reviewer reads this field to learn, BEFORE it
    # tries, whether a formal verdict is even possible — and if it is not, to say so
    # explicitly instead of silently downgrading to a comment that sets no verdict.
    identity: str = DEFAULT_IDENTITY


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_magic_link_secret() -> str:
    settings = get_settings()
    return settings.magic_link_secret or settings.token_secret_key


def _build_magic_link_url(token: str) -> str:
    settings = get_settings()
    base = settings.gateway_base_url.rstrip("/")
    return f"{base}/auth/link/magic?token={token}"


async def _write_audit(
    db: AsyncSession,
    *,
    event_type: str,
    org_id: str,
    actor_id: str | None,
    details: dict | None,
) -> None:
    log = AuditLog(
        org_id=org_id,
        event_type=event_type,
        actor_id=actor_id,
        details=details,
    )
    db.add(log)
    # Flush within the same transaction — the caller commits.
    await db.flush()


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/issue-magic-link
# ---------------------------------------------------------------------------


@router.post(
    "/issue-magic-link",
    response_model=IssueMagicLinkResponse,
    status_code=201,
    summary="Issue a magic-link token (Lambda/internal call)",
    description=(
        "Called by ingest Lambdas when they encounter a provider identity with no "
        "matching user_identities row.  Returns a URL the Lambda posts in-channel.  "
        "target_user_id is null — the user picks their Cognito identity on the landing page."
    ),
)
async def issue_magic_link(
    body: IssueMagicLinkRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> IssueMagicLinkResponse:
    secret = _get_magic_link_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={"error": "not_configured", "message": "Magic-link signing key not configured"},
        )

    result = issue_token(
        provider=body.provider,
        provider_user_id=body.provider_user_id,
        channel_context=body.channel_context,
        target_user_id=None,
        secret_key=secret,
    )

    await store_nonce(
        jti=result["jti"],
        provider=body.provider,
        provider_user_id=body.provider_user_id,
        channel_context=body.channel_context,
        target_user_id=None,
        expires_at=result["expires_at"],
        db=db,
    )

    magic_link_url = _build_magic_link_url(result["token"])

    await _write_audit(
        db,
        event_type="magic_link_issued",
        org_id="__internal__",  # no org_id for Lambda-initiated issuance before user is resolved
        actor_id=None,
        details={
            "provider": body.provider,
            "provider_user_id": body.provider_user_id,
            "channel_context": body.channel_context,
            "jti": result["jti"],
            "source": "lambda",
        },
    )
    await db.commit()

    logger.info(
        "Magic-link issued provider=%s provider_user_id=%s jti=%s",
        body.provider,
        body.provider_user_id,
        result["jti"],
    )
    return IssueMagicLinkResponse(magic_link_url=magic_link_url)


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/resolve-user
# ---------------------------------------------------------------------------


@router.post(
    "/resolve-user",
    summary="Resolve a provider identity to an internal user",
    description=(
        "Returns the internal user_id for a (provider, provider_user_id) pair. "
        "If no identity link exists:\n"
        "- channel_tenant_map matches → auto-provision a shadow user (returns user context)\n"
        "- no mapping → returns 404 with magic_link_url for in-channel posting"
    ),
    responses={
        200: {"description": "User resolved"},
        201: {"description": "Shadow user auto-provisioned"},
        404: {"description": "User not found; magic_link_url provided"},
    },
)
async def resolve_user(
    body: ResolveUserRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
):
    # 1. Check user_identities
    stmt = select(UserIdentity).where(
        UserIdentity.provider == body.provider,
        UserIdentity.provider_user_id == body.provider_user_id,
    )
    result = await db.execute(stmt)
    identity = result.scalar_one_or_none()

    if identity is not None:
        # Fetch the user row for org/team info
        user_stmt = select(User).where(User.id == identity.user_id)
        user_result = await db.execute(user_stmt)
        user = user_result.scalar_one_or_none()
        if user is not None:
            return ResolveUserResponse(
                user_id=user.id,
                org_id=user.org_id,
                team_id=user.team_id,
                is_shadow=user.is_shadow,
            )

    # 2. Check channel_tenant_map for auto-provisioning
    # Extract the "scope" part of the provider_user_id.
    # For Slack the convention is "WORKSPACE_ID:USER_ID"; the workspace is the scope.
    # For GitHub the scope is the ACCOUNT scope key — never an installation id.
    # Issue #4070 (·A0): this comment previously said "the org/installation id",
    # documenting the ambiguity as if it were intentional. It is not: the
    # installation id now lives in its own column, channel_tenant_map.installation_id,
    # which is where uniqueness is enforced. Resolve an installation via
    # src.admin.installations.resolver, not by matching provider_scope_id.
    # We use the channel_context as the provider_scope_id when available;
    # fall back to the provider_user_id itself.
    provider_scope_id = body.channel_context or body.provider_user_id

    map_stmt = select(ChannelTenantMap).where(
        ChannelTenantMap.provider == body.provider,
        ChannelTenantMap.provider_scope_id == provider_scope_id,
    )
    map_result = await db.execute(map_stmt)
    tenant_map = map_result.scalar_one_or_none()

    if tenant_map is not None:
        # Auto-provision a shadow user
        shadow = User(
            id=new_uuid(),
            org_id=tenant_map.org_id,
            team_id="",  # shadow users have no team until claimed
            email=f"{body.provider}:{body.provider_user_id}@shadow.adp",
            is_shadow=True,
        )
        db.add(shadow)

        # Create the identity link
        link = UserIdentity(
            org_id=tenant_map.org_id,
            user_id=shadow.id,
            team_id="",
            provider=body.provider,
            provider_user_id=body.provider_user_id,
            provider_username=None,
            verification_method="admin_manual",
        )
        db.add(link)

        await _write_audit(
            db,
            event_type="shadow_user_created",
            org_id=tenant_map.org_id,
            actor_id=None,
            details={
                "provider": body.provider,
                "provider_user_id": body.provider_user_id,
                "shadow_user_id": shadow.id,
                "channel_context": body.channel_context,
            },
        )
        await db.commit()

        logger.info(
            "Shadow user created provider=%s provider_user_id=%s shadow_user=%s org=%s",
            body.provider,
            body.provider_user_id,
            shadow.id,
            tenant_map.org_id,
        )
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=201,
            content={
                "user_id": shadow.id,
                "org_id": shadow.org_id,
                "team_id": shadow.team_id,
                "is_shadow": True,
            },
        )

    # 3. No mapping — issue magic-link
    secret = _get_magic_link_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={"error": "not_configured", "message": "Magic-link signing key not configured"},
        )

    result_token = issue_token(
        provider=body.provider,
        provider_user_id=body.provider_user_id,
        channel_context=body.channel_context,
        target_user_id=None,
        secret_key=secret,
    )
    await store_nonce(
        jti=result_token["jti"],
        provider=body.provider,
        provider_user_id=body.provider_user_id,
        channel_context=body.channel_context,
        target_user_id=None,
        expires_at=result_token["expires_at"],
        db=db,
    )

    magic_link_url = _build_magic_link_url(result_token["token"])

    await _write_audit(
        db,
        event_type="magic_link_issued",
        org_id="__internal__",
        actor_id=None,
        details={
            "provider": body.provider,
            "provider_user_id": body.provider_user_id,
            "channel_context": body.channel_context,
            "jti": result_token["jti"],
            "source": "resolve_user",
        },
    )
    await db.commit()

    logger.info(
        "Resolve-user miss — magic-link issued provider=%s provider_user_id=%s",
        body.provider,
        body.provider_user_id,
    )
    raise HTTPException(
        status_code=404,
        detail={
            "error": "user_not_found",
            "message": "No linked identity found. Share the magic_link_url in-channel.",
            "magic_link_url": magic_link_url,
        },
    )


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/resolve-installation
# ---------------------------------------------------------------------------


@router.post(
    "/resolve-installation",
    response_model=ResolveInstallationResponse,
    summary="Resolve a GitHub App installation to its ADP tenant",
    description=(
        "Issue #2769: Postgres is the single source of truth for the "
        "installation_id → tenant mapping. Returns the ADP tenant (organization) "
        "that owns the given GitHub App installation_id, looked up from "
        "organizations.github_installation_ids. Used by the webhook-ingress "
        "auto-register write-guard (known-tenant check) and the read-time drift "
        "safety-net. Returns 404 when no organization claims the installation."
    ),
    responses={
        200: {"description": "Installation resolved to a tenant"},
        404: {"description": "No organization owns this installation"},
    },
)
async def resolve_installation(
    body: ResolveInstallationRequest,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> ResolveInstallationResponse:
    installation_id = (body.installation_id or "").strip()
    if not installation_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Unknown installation"},
        )

    # Postgres query intent: organizations WHERE :iid = ANY(github_installation_ids)
    # (backed by the GIN index ix_organizations_github_installation_ids on
    # Postgres, migration 005). We fetch candidate orgs and match in Python so
    # the same code path works against the SQLite JSON column used in tests;
    # the org set per account is small (a handful of tenants).
    stmt = select(Organization)
    result = await db.execute(stmt)
    for org in result.scalars().all():
        ids = [str(i) for i in (org.github_installation_ids or [])]
        if installation_id in ids:
            # Issue #2724: return provenance so the webhook gate can distinguish
            # a deliberately-onboarded tenant from a self-created shell. We keep
            # returning 200 for install_autocreate rows rather than 404 — the
            # drift safety-net and adp-trigger resolution legitimately need the
            # mapping; only the auto-register trust decision cares about
            # provenance, and it is made by the caller.
            return ResolveInstallationResponse(
                tenant_id=org.id,
                created_via=org.created_via or "operator",
            )

    logger.info(
        "resolve-installation miss — no org owns installation_id=%s",
        installation_id,
    )
    raise HTTPException(
        status_code=404,
        detail={"error": "not_found", "message": "Unknown installation"},
    )


async def _revalidate_github_binding(request: Request, binding, permissions=None, authorized_action=None) -> None:
    codex_binding = getattr(request.state, "codex_reviewer_binding", None)
    if codex_binding is not None:
        from src.internal.codex_reviewer_identity import verify_codex_reviewer_broker

        await verify_codex_reviewer_broker(request)
        if (
            getattr(request.state, "codex_reviewer_binding", None) != codex_binding
            or request.state.agent_installation_binding != binding
            or getattr(request.state, "agent_authorized_action", None) is not authorized_action
            or (permissions is not None and request.state.agent_github_permissions != permissions)
        ):
            raise HTTPException(404, "not found")
        return
    if getattr(request.state, "agent_broker_grant", None) is None:
        return  # Legacy rollout cohort, not a protected worker.
    from src.agentauth.broker_identity import verify_broker_worker

    await verify_broker_worker(request)
    current = request.state.agent_installation_binding
    if (
        current != binding
        or getattr(request.state, "agent_authorized_action", None) is not authorized_action
        or (permissions is not None and getattr(request.state, "agent_github_permissions", AGENT_RUN_PERMISSIONS) != permissions)
    ):
        raise HTTPException(404, "not found")


def _validate_github_expiry(request: Request, expires_at: str) -> None:
    expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    not_after = getattr(request.state, "agent_github_not_after", None)
    if expiry.tzinfo is None or expiry <= datetime.now(UTC) or (not_after is not None and expiry > not_after):
        raise ValueError("provider token lifetime exceeds accepted grant")


async def _revoke_undelivered_github_token(token: str) -> None:
    """Best effort provider revocation. Never log token/response/exception text."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.delete(
                "https://api.github.com/installation/token",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
            )
        if response.status_code not in (204, 401):
            logger.error("Undelivered GitHub token revocation refused: status=%s", response.status_code)
    except Exception:
        logger.error("Undelivered GitHub token revocation unavailable")


# ---------------------------------------------------------------------------
# Endpoint: POST /internal/v1/github-installation-token
# ---------------------------------------------------------------------------


@router.post(
    "/github-installation-token",
    response_model=GithubInstallationTokenResponse,
    summary="Mint a repo-scoped GitHub App installation token for an agent run",
    description=(
        "Issue #4272: the GitHub-token gatekeeper. The platform GitHub App "
        "private key stays server-side; an agent run calls this to obtain a "
        "short-lived installation token scoped to its OWN org and, within that, "
        "to the single repo it was assigned. Previously the key itself was "
        "exported into the agent subprocess, so a prompt-injected run could mint "
        "tokens for every org that had installed the App.\n\n"
        "Two independent authz layers gate the mint: the run's invocation is "
        "bound to an installation via the webhook-events registry (fail-closed), "
        "and that installation's ownership by the bound tenant is confirmed "
        "against Postgres. The caller never asserts its own tenant."
    ),
    responses={
        200: {"description": "Token minted"},
        403: {"description": "Run is not bound to, or its tenant does not own, this installation"},
        409: {"description": "Installation belongs to a different tenant"},
        502: {"description": "GitHub rejected the mint"},
    },
)
async def github_installation_token(
    body: GithubInstallationTokenRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> GithubInstallationTokenResponse:
    settings = get_settings()

    # Issue #5350: reject an identity this gateway does not implement. Checked before
    # any authz work so a typo cannot be answered with a usable authoring token.
    if body.identity not in SUPPORTED_IDENTITIES:
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_identity", "message": f"Unsupported identity; expected one of {sorted(SUPPORTED_IDENTITIES)}"},
        )

    # Layer 1 — bind the run to the installation its originating webhook carried.
    # Fail-closed, and deliberately NOT gated on ENFORCE_CREDENTIAL_BINDING:
    # that flag is false on at least one live environment, so a control behind it
    # shadows instead of enforcing.
    binding = getattr(request.state, "agent_installation_binding", None)
    if binding is None:
        binding = await asyncio.to_thread(
            resolve_installation_binding,
            invocation_id=body.invocation_id,
            requested_installation_id=body.installation_id,
            settings=settings,
        )

    # Layer 2 — the authoritative ownership check. Layer 1 proves "this run's
    # webhook said installation X for tenant T"; this proves T really owns X.
    # attest=False: this is a hot per-run read, not a write that BINDS an
    # installation to a tenant, and the resolver already fails closed on
    # NOT_FOUND / AMBIGUOUS.
    try:
        await assert_installation_owned_by(
            binding.tenant_id,
            binding.installation_id,
            db=db,
        )
    except InstallationOwnershipError as exc:
        await _write_audit(
            db,
            event_type="github_installation_token_denied",
            org_id=binding.tenant_id,
            actor_id=None,
            details={
                "reason": "ownership_check_failed",
                "state": str(exc.state),
                "installation_id": binding.installation_id,
                "repo": f"{body.repo_owner}/{body.repo_name}",
                "invocation_id": body.invocation_id,
            },
        )
        await db.commit()
        logger.warning(
            "github-installation-token DENIED — ownership state=%s tenant=%s installation=%s",
            exc.state,
            binding.tenant_id,
            binding.installation_id,
        )
        # A cleanly-resolved installation owned by someone else is a genuine
        # cross-tenant conflict (409); everything else is an unproven claim (403).
        status_code = 409 if exc.state is OwnerState.RESOLVED else 403
        raise HTTPException(
            status_code=status_code,
            detail={"error": "installation_not_owned", "message": "Installation ownership could not be verified"},
        ) from exc

    # Mint. The key is read here, inside the gateway, and never leaves it.
    #
    # Issue #5350: the reviewer App requires BOTH conditions, and they check
    # different things.
    #
    # The authorized ACTION is the AUTHORITY, resolved server-side from engine-written
    # execution state: a developer cannot turn itself into a reviewer by changing JSON,
    # so an unauthorized ask is refused below with 403. It is compared by string value
    # rather than by importing `Action`: `src/internal/` must not import
    # `src.orchestration` at all (see tests/orchestration/test_internal_plane_guard.py —
    # agent pods can call every internal route, so promotion state must stay
    # unreachable from this plane). `Action` is a `StrEnum`, so the value comparison is
    # exact.
    #
    # `body.identity == REVIEW_IDENTITY` is the REQUEST, and it is required as well
    # because a review run mints more than once. Its bootstrap mint (entrypoint.py,
    # which sends no `identity`) is what clones the repo and drives the check run, and
    # it needs the authoring App's broader grant — `issues: write` and `checks: read`
    # among them. Routing that mint to a review-only App would ask GitHub for
    # permissions that App was never granted, which GitHub refuses outright, so the
    # run would die at startup instead of reviewing anything. Selecting on the action
    # alone therefore breaks the very runs this change exists to enable.
    #
    # `mint_installation_id` diverges from `binding.installation_id` for the reviewer
    # identity because a second App has its own installation on the org; both remain
    # gated by the two authz layers above, which have already proven this tenant owns
    # the bound installation.
    granted_identity = DEFAULT_IDENTITY
    mint_installation_id = binding.installation_id
    authorized_action = getattr(request.state, "agent_authorized_action", None)
    authorized_action_value = getattr(authorized_action, "value", authorized_action)
    action_is_review = authorized_action_value == REVIEW_ACTION_VALUE
    if body.identity == REVIEW_IDENTITY and not action_is_review:
        raise HTTPException(
            status_code=403,
            detail={"error": "review_identity_not_authorized", "message": "Reviewer identity requires an authorized review assignment"},
        )
    wants_reviewer_identity = body.identity == REVIEW_IDENTITY and action_is_review
    try:
        if wants_reviewer_identity:
            try:
                app_id, private_key, mint_installation_id = await resolve_reviewer_app_credentials(binding.tenant_id)
                granted_identity = REVIEW_IDENTITY
            except ReviewerIdentityUnavailableError:
                # Expected until the second App is registered. Fall back to the
                # authoring identity and SAY SO in the response, so the caller
                # reports a pending human approval instead of silently downgrading
                # to a comment that sets no reviewDecision.
                logger.info(
                    "github-installation-token: reviewer identity unavailable for tenant=%s; falling back to authoring identity",
                    binding.tenant_id,
                )
                app_id, private_key = await resolve_tenant_app_credentials(binding.tenant_id)
                mint_installation_id = binding.installation_id
        else:
            app_id, private_key = await resolve_tenant_app_credentials(binding.tenant_id)
    except ValueError as exc:
        logger.warning(
            "github-installation-token: no App credentials for tenant=%s identity=%s: %s",
            binding.tenant_id,
            body.identity,
            type(exc).__name__,
        )
        raise HTTPException(
            status_code=502,
            detail={"error": "app_credentials_unavailable", "message": "GitHub App credentials unavailable"},
        ) from exc

    # Least-privilege: this run's one repo, and only the verbs an agent run needs.
    # GitHub already scopes the token to one org (one installation = one org);
    # narrowing repo + permissions on top means a hijacked run's live token can
    # touch only the repo it was working on.
    await _revalidate_github_binding(request, binding, authorized_action=authorized_action)
    permissions = dict(getattr(request.state, "agent_github_permissions", AGENT_RUN_PERMISSIONS))
    # #5350: narrow what the REVIEWER mint asks GitHub for, while still revalidating
    # against the run's authorized set below. The run's authorized permissions are
    # computed for the AUTHORING App and include `issues: write` and `checks: read`,
    # which a review-only App deliberately does not hold; GitHub refuses a token
    # request naming a permission its App was never granted, so asking unchanged would
    # turn a correctly configured reviewer App into a failed mint. Narrowing also keeps
    # the reviewer token least-privilege — it can record a verdict and nothing else.
    #
    # `minted_permissions` is deliberately separate from `permissions`: the revalidation
    # guard compares against what the policy authorized, so substituting the narrowed
    # set there would make every reviewer mint fail its own re-check with a 404.
    minted_permissions = permissions
    if granted_identity == REVIEW_IDENTITY:
        minted_permissions = {key: value for key, value in permissions.items() if key in REVIEW_IDENTITY_PERMISSIONS}
    token = None
    try:
        token, expires_at = await mint_installation_token_with_expiry(
            app_id,
            private_key,
            mint_installation_id,
            repositories=[body.repo_name],
            permissions=minted_permissions,
        )
        await _revalidate_github_binding(request, binding, permissions, authorized_action)
        _validate_github_expiry(request, expires_at)
    except Exception as exc:
        if token:
            await _revoke_undelivered_github_token(token)
        await _write_audit(
            db,
            event_type="github_installation_token_denied",
            org_id=binding.tenant_id,
            actor_id=None,
            details={
                "reason": "mint_failed",
                "installation_id": binding.installation_id,
                "repo": f"{body.repo_owner}/{body.repo_name}",
                "invocation_id": body.invocation_id,
                "error_type": type(exc).__name__,
            },
        )
        await db.commit()
        logger.warning(
            "github-installation-token: mint failed tenant=%s installation=%s: %s",
            binding.tenant_id,
            binding.installation_id,
            type(exc).__name__,
        )
        if isinstance(exc, HTTPException):
            raise
        raise HTTPException(
            status_code=502,
            detail={"error": "mint_failed", "message": "GitHub rejected the installation token request."},
        ) from exc

    try:
        # Audit every mint, mirroring credential-raw-read. Without this an operator
        # cannot answer "which run got a token for which org" after the fact — which
        # is the whole point of moving the mint server-side.
        await _write_audit(
            db,
            event_type="github_installation_token_minted",
            org_id=binding.tenant_id,
            actor_id=None,
            details={
                "installation_id": mint_installation_id,
                "repo": f"{body.repo_owner}/{body.repo_name}",
                "repositories": [body.repo_name],
                # The permissions actually minted, which for the reviewer identity are
                # narrower than the run's authorized set. Auditing the authorized set
                # here would overstate what the delivered token can do.
                "permissions": minted_permissions,
                "invocation_id": body.invocation_id,
                "expires_at": expires_at,
                "purpose": body.purpose,
                # #5350: record the identity asked for AND the one granted. When they
                # differ, the audit trail carries the reason an engine-authored PR
                # could not be formally approved by that run.
                "identity_requested": body.identity,
                "identity_granted": granted_identity,
                "authorized_action": authorized_action.value if authorized_action is not None else None,
            },
        )
        await db.commit()

        await _revalidate_github_binding(request, binding, permissions, authorized_action)
        _validate_github_expiry(request, expires_at)
    except Exception:
        await _revoke_undelivered_github_token(token)
        raise

    logger.info(
        "github-installation-token minted tenant=%s installation=%s repo=%s/%s expires_at=%s identity=%s",
        binding.tenant_id,
        mint_installation_id,
        body.repo_owner,
        body.repo_name,
        expires_at,
        granted_identity,
    )
    response.headers["Cache-Control"] = "no-store"
    return GithubInstallationTokenResponse(token=token, expires_at=expires_at, app_id=str(app_id), identity=granted_identity)
