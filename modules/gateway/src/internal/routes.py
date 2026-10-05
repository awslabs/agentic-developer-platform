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
    Registered IAM callers authenticate through the verified API edge. Identity
    routing additionally requires operation and tenant capabilities; worker token
    brokers require live run authority. Shared transport keys are never accepted.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response
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
from src.internal.credential_binding_metrics import observe_identity_binding
from src.internal.service_authorization import require_service_operation, service_tenant
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
from src.shared.identity.providers import is_linkable_provider
from src.shared.identity.verification import (
    CHANNEL_PLACEMENT,
    DELIVERY_SHARED_CHANNEL,
    IDENTIFYING_METHODS,
)
from src.shared.models.audit import AuditLog
from src.shared.models.base import new_uuid
from src.shared.models.organization import Organization, User
from src.shared.models.vault import ChannelTenantMap, MagicLinkNonce, UserIdentity  # noqa: F401

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal/v1", tags=["internal"])


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
    # Optional tenant scope (#5664, A10). `user_identities` is unique per tenant,
    # so one external account may legitimately hold rows in several. Supplying the
    # tenant the inbound event is for narrows the lookup to it; omitting it means
    # an account linked in more than one tenant is refused as ambiguous rather
    # than silently resolved to whichever row the database returned first.
    org_id: str | None = None


class ResolveUserResponse(BaseModel):
    user_id: str
    org_id: str
    team_id: str
    is_shadow: bool
    # How the link this answer rests on was established (#5664, A10).
    #
    # A 200 does NOT mean "proven". The lookup filters to IDENTIFYING_METHODS, which
    # deliberately includes the unproven `channel_placement` rows the
    # auto-provision path creates, because this endpoint answers "which platform
    # user is this account" rather than "may this account act". A caller that grants
    # authority MUST read this field and apply `is_proven` to it; inferring proof
    # from the status code is the bug this field exists to prevent.
    #
    # Empty string means a caller is talking to a gateway that predates the field:
    # unknown provenance, which is not proof.
    verification_method: str = ""


class ResolveUserNotFoundResponse(BaseModel):
    magic_link_url: str


class ResolveInstallationRequest(BaseModel):
    """Body for POST /internal/v1/resolve-installation."""

    installation_id: str


class ResolveInstallationResponse(BaseModel):
    revocation_checked: bool = True
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
    """Resolve the magic-link signing key — and only that key.

    Issue #5656 (A05): see src/auth/vault_routes.py._get_magic_link_secret. Both
    resolvers had the same `or token_secret_key` fallback and both had to lose it,
    or the internal issuing path would keep minting identity-linking tokens under
    the session key while the operator-facing path refused.
    """
    return get_settings().magic_link_secret


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
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> IssueMagicLinkResponse:
    principal = require_service_operation(request, "internal:identity:link")
    # Unscoped pre-login linking is reserved for trusted ingress.
    if "internal:cross-tenant" not in principal.credential_scopes:
        raise HTTPException(403, "pre-login linking requires ingress authority")
    # Closed provider allowlist, checked before any state is written (#5664, A10).
    #
    # This is the OTHER writer into the shared `magic_link_nonces` table, and it
    # took `provider` from the request body with no validation at all. The internal
    # plane is authenticated, but that only means the caller is an ADP Lambda — it
    # does not make an arbitrary namespace safe to mint into, and the
    # `github_app_register` namespace is the sole authenticator on the callback that
    # overwrites the deployment's shared GitHub App credentials. A compromised or
    # simply buggy ingest caller must not be able to reach it, so both nonce
    # minters now enforce the same allowlist.
    if not is_linkable_provider(body.provider):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "unsupported_provider",
                "message": "That identity provider is not supported for linking.",
            },
        )

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
        # Recorded honestly (#5664, A10): the ingest caller posts this link back
        # into the conversation the triggering message arrived in, which for a
        # public channel or an issue thread is readable by everyone there. That is
        # channel access, not proof of account ownership, so confirming a link
        # delivered this way yields an UNPROVEN identity row. Mislabelling it
        # `provider_dm` here is exactly the escalation this column prevents.
        delivery_method=DELIVERY_SHARED_CHANNEL,
    )

    magic_link_url = _build_magic_link_url(result["token"])

    await _write_audit(
        db,
        event_type="magic_link_issued",
        org_id="__internal__",  # no org_id for Lambda-initiated issuance before user is resolved
        actor_id=request.state.token_context.user_id,
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
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
):
    body.org_id = service_tenant(request, body.org_id, "internal:identity:resolve")
    # 1. Check user_identities — trust-aware, and tenant-scoped when the caller
    #    supplies a tenant (#5664, A10).
    #
    # Two defects were combined here. The lookup accepted ANY verification method,
    # so a row a user had simply asserted about themselves resolved exactly like
    # one the provider confirmed — and this endpoint's answer decides which
    # platform user an inbound event acts as. And `scalar_one_or_none()` raises
    # MultipleResultsFound on legitimate data: the unique index is per tenant
    # (provider, provider_user_id, org_id), so one external account may hold rows
    # in several tenants. That surfaced as an unhandled 500.
    #
    # Ambiguity is now an explicit refusal. Picking any one row would be guessing
    # which tenant an event belongs to, and the safe answer to "which of these
    # is it?" is to decline rather than to choose.
    #
    # The filter is IDENTIFYING_METHODS, not PROVEN_METHODS. This endpoint answers
    # "which platform user is this account", which an auto-provisioned channel
    # placement legitimately answers even though it proves nothing — and filtering
    # it out would make every subsequent resolve miss, re-provisioning a shadow user
    # and re-issuing a magic link on every inbound message. The authority decision
    # is made by the CALLER from `verification_method` in the response, which is
    # reported truthfully below; it is not implied by having resolved at all.
    stmt = select(UserIdentity).where(
        UserIdentity.provider == body.provider,
        UserIdentity.provider_user_id == body.provider_user_id,
        UserIdentity.verification_method.in_(IDENTIFYING_METHODS),
    )
    if body.org_id:
        stmt = stmt.where(UserIdentity.org_id == body.org_id)

    candidates = (await db.execute(stmt)).scalars().all()

    if len(candidates) > 1:
        logger.warning(
            "Ambiguous identity resolution provider=%s provider_user_id=%s org_id=%r matches=%d",
            body.provider,
            body.provider_user_id,
            body.org_id,
            len(candidates),
        )
        raise HTTPException(
            status_code=409,
            detail={
                "error": "ambiguous_identity",
                "message": ("This provider identity resolves to more than one tenant. Supply org_id to disambiguate."),
            },
        )

    identity = candidates[0] if candidates else None

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
                verification_method=identity.verification_method or "",
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
        if body.org_id and tenant_map.org_id != body.org_id:
            raise HTTPException(
                status_code=403,
                detail={"error": "channel_tenant_mismatch", "message": "Channel does not belong to the requested organization."},
            )
        # Auto-provision a shadow user
        shadow = User(
            id=new_uuid(),
            org_id=tenant_map.org_id,
            team_id="",  # shadow users have no team until claimed
            email=f"{body.provider}:{body.provider_user_id}@shadow.adp",
            is_shadow=True,
        )
        db.add(shadow)

        # Create the identity link.
        #
        # #5664 (A10): this is `channel_placement`, an UNPROVEN method. It used to
        # say `admin_manual`, which is in PROVEN_METHODS, and that was the second
        # half of the finding: an administrator mapped the workspace to the tenant
        # via channel_tenant_map — an accountable act, but a fact about the
        # WORKSPACE. The account id itself arrived in this request's body and
        # nobody verified it. Labelling that as an administrator's assertion about
        # a specific account manufactured proof out of a routing decision.
        #
        # The previous slice left the mislabel in place because relabelling it
        # unproven would have made every subsequent resolve miss the trust filter
        # and re-issue a magic link forever. That is now handled at the seam rather
        # than by mislabelling: resolution filters on IDENTIFYING_METHODS (which
        # includes this value, so the row keeps routing) while everything that
        # mints authority asks `is_proven` (which refuses it). The flow is
        # preserved; only the unearned authority claim is withdrawn.
        #
        # `verified_at` stays NULL — nothing was verified.
        link = UserIdentity(
            org_id=tenant_map.org_id,
            user_id=shadow.id,
            team_id="",
            provider=body.provider,
            provider_user_id=body.provider_user_id,
            provider_username=None,
            verification_method=CHANNEL_PLACEMENT,
        )
        db.add(link)

        await _write_audit(
            db,
            event_type="shadow_user_created",
            org_id=tenant_map.org_id,
            actor_id=request.state.token_context.user_id,
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
                # Stated for the same reason as the 200 path (#5664, A10): the
                # reader should read provenance, not infer it from the status code.
                # The value matches the row written just above.
                "verification_method": link.verification_method or "",
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
        # Same in-channel delivery as /issue-magic-link above, so the same honest
        # label. See the note there.
        delivery_method=DELIVERY_SHARED_CHANNEL,
    )

    magic_link_url = _build_magic_link_url(result_token["token"])

    await _write_audit(
        db,
        event_type="magic_link_issued",
        org_id="__internal__",
        actor_id=request.state.token_context.user_id,
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
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: None = Depends(verify_internal_or_irsa),
) -> ResolveInstallationResponse:
    require_service_operation(request, "internal:installation:resolve")
    from src.admin.installations.resolver import OwnerState, resolve_installation_owner

    installation_id = (body.installation_id or "").strip()
    if not installation_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "not_found", "message": "Unknown installation"},
        )

    if installation_id.isdecimal():
        owner, state = await resolve_installation_owner(int(installation_id), db=db)
        if state is OwnerState.REVOKED:
            raise HTTPException(status_code=410, detail={"error": "installation_revoked"})
        if state is not OwnerState.RESOLVED or owner is None:
            raise HTTPException(status_code=404, detail={"error": "not_found", "message": "No proven installation owner"})
        org = await db.get(Organization, owner.tenant_id)
        if org is not None:
            service_tenant(request, org.id, "internal:installation:resolve")
            return ResolveInstallationResponse(tenant_id=org.id, created_via=org.created_via or "operator")

    # Retain nonnumeric legacy identifiers for compatibility; real provider IDs
    # above always use canonical ownership and durable revocation.
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
            service_tenant(request, org.id, "internal:installation:resolve")
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
    if getattr(request.state, "shared_review_identity", False):
        from src.agentauth.shared_review_identity import verify_shared_review_worker

        await verify_shared_review_worker(request)
    elif getattr(request.state, "agent_broker_grant", None) is not None:
        from src.agentauth.broker_identity import verify_broker_worker

        await verify_broker_worker(request)
    else:
        return  # Legacy rollout cohort, not a protected worker.
    current = request.state.agent_installation_binding
    if (
        current != binding
        or getattr(request.state, "agent_authorized_action", None) is not authorized_action
        or (permissions is not None and getattr(request.state, "agent_github_permissions", AGENT_RUN_PERMISSIONS) != permissions)
    ):
        raise HTTPException(404, "not found")


_TOKEN_ROUTE = "github-installation-token"


def _assert_token_repo_binding(binding, requested_repo: str) -> None:
    """Refuse a mint for a repository the run was not assigned (#5663, A09).

    The installation binding already proves "this run's originating webhook named
    installation X for tenant T", and Postgres proves T owns X. Neither proves the
    run was assigned the *repository* being requested. Within one installation that
    gap is a real escalation: a run legitimately dispatched to ``org/service-a`` can
    ask for, and receive, a write-capable token listing ``org/service-b``.

    Why this lives here rather than in ``verify_broker_worker``, which already
    performs an equivalent compare (``broker_identity.py``: ``execution.get("repo")
    != {"S": repo}``): that function runs only when the caller presents a run
    credential, is marked ``requires_run_identity``, or ``AGENT_AUTHORITY_ENABLED``
    is true (``auth_deps.py``). That flag is false in live environments, so on the
    default path the compare never happens. #5663's acceptance is explicit that the
    repository binding must hold "with the authority feature flag absent or set to
    its old default" — so the check has to sit on the path every caller takes. When
    the protected path DID run, this is a cheap second assertion of the same fact,
    not a substitute for it.

    Both outcomes below are counted through ``observe_identity_binding`` so the
    decision is measurable per route, but the counter never decides the outcome.

    ABSENT SERVER EVIDENCE IS A REFUSAL, NOT AN ALLOWANCE (#5663 review). An
    earlier revision of this function allowed a mint when the run's row carried no
    ``repo`` at all, gated on ``enforce_unbound_repo_token_denial`` (default
    false), on the theory that EventBridge/scheduled dispatch legitimately produces
    repo-less runs. That preserved the exact escalation this check exists to close:
    with no bound repository, the *caller's* ``repo_owner``/``repo_name`` became the
    only input deciding which repository got a write-capable token. "The server has
    no evidence for this claim" cannot mean "the claim is granted", whatever a
    metric records alongside it.

    The scheduled-dispatch concern turned out not to describe any caller that can
    actually reach this route. Every mint path derives the repository from the SAME
    envelope field that produced the row's ``repo`` attribute:

    * ``spawn_persona`` passes one ``repo`` value to both ``_build_envelope``
      (``source_ref.repo``) and ``log_event`` (the ``repo`` attribute). They cannot
      disagree, so a run with a usable ``source_ref.repo`` has a row with ``repo``.
    * ``agent-worker-image/entrypoint.py`` calls ``repo.split("/", 1)`` at parse
      time, BEFORE any mint, and ``parse_envelope`` requires ``source_ref.repo`` to
      be present. An empty value raises ``ValueError`` there, so a repo-less run
      fails during bootstrap and never reaches a mint.
    * ``agent/src/token-refresh.ts`` refuses broker mode without ``config.owner``,
      and both TypeScript and Python clients send the run's own envelope values.

    So the repo-less-but-minting run is unreachable: the only callers that arrive
    here with no bound repository are ones whose claim cannot be checked at all.
    Refusing them costs no legitimate traffic and is what makes the binding hold on
    the default configuration, which the issue's acceptance requires explicitly.

    If a future producer does need a repo-less scheduled run to mint, the fix is to
    give it server-owned repository evidence (record ``repo`` on its event row, or
    carry an authorization for the repository it may act on) — not to reopen the
    caller-declared path. Recorded in the PR as a rollout note.
    """
    bound_repo = getattr(binding, "repo", None)

    if not bound_repo:
        observe_identity_binding(route=_TOKEN_ROUTE, outcome="denied", enforced=True)
        logger.warning(
            "github-installation-token DENIED — run has no bound repository tenant=%s installation=%s requested=%s",
            binding.tenant_id,
            binding.installation_id,
            requested_repo,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "repo_binding_failed",
                "message": "This invocation is not bound to a repository.",
            },
        )

    # Case-insensitive: GitHub treats owner/repo case-insensitively, and the row's
    # casing comes from the webhook payload while the request's comes from the
    # worker's env. A case difference is not an authorization difference, and
    # refusing on it would be a self-inflicted outage rather than a control.
    if bound_repo.casefold() != requested_repo.casefold():
        # Unconditional. There is no legitimate caller that asks for a repository
        # other than its own run's, so there is no compatibility window to stage and
        # nothing for a flag to protect — a switch that can turn this off is just a
        # way to reintroduce the escalation. (#5663 review: "a metric does not
        # establish the run's authority for the requested repository".)
        observe_identity_binding(route=_TOKEN_ROUTE, outcome="denied", enforced=True)
        logger.warning(
            "github-installation-token DENIED — repo mismatch tenant=%s installation=%s bound=%s requested=%s",
            binding.tenant_id,
            binding.installation_id,
            bound_repo,
            requested_repo,
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "repo_binding_mismatch",
                "message": "Requested repository does not match this invocation's repository.",
            },
        )

    observe_identity_binding(route=_TOKEN_ROUTE, outcome="allowed", enforced=True)


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
    # Issue #5350: reject an identity this gateway does not implement. Checked before
    # any authz work so a typo cannot be answered with a usable authoring token.
    if body.identity not in SUPPORTED_IDENTITIES:
        raise HTTPException(
            status_code=400,
            detail={"error": "unknown_identity", "message": f"Unsupported identity; expected one of {sorted(SUPPORTED_IDENTITIES)}"},
        )

    if body.identity == REVIEW_IDENTITY and request.headers.get("X-Adp-Report-Credential"):
        from src.agentauth.shared_review_identity import verify_shared_review_worker

        await verify_shared_review_worker(request)

    # Layer 1 — bind the run to the installation its originating webhook carried.
    # Installation binding is unconditional; missing authenticated state fails closed.
    binding = getattr(request.state, "agent_installation_binding", None)
    if binding is None:
        raise HTTPException(403, "authenticated run installation binding required")

    # Reassert the repository bound by broker or shared-review authentication.
    # Missing repository evidence is denied before provider minting.
    _assert_token_repo_binding(binding, f"{body.repo_owner}/{body.repo_name}")

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
        minted_permissions = {key: "read" if key == "contents" else value for key, value in permissions.items() if key in REVIEW_IDENTITY_PERMISSIONS}
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
