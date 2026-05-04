"""Internal service-to-service endpoints.

Issue #446: Vault Phase 2b — Magic-link identity linking flow

Endpoints (IAM-signed; internal only, not exposed to end users):
    POST /internal/v1/issue-magic-link   — ingest Lambda requests a magic-link URL
                                            for an unknown user event
    POST /internal/v1/resolve-user       — resolve provider identity to internal
                                            user_id; auto-provision shadow user if
                                            channel_tenant_map matches

Authentication:
    A shared secret (BG_INTERNAL_API_KEY) is expected in the
    ``X-Internal-Api-Key`` header.  In production, rotate via Secrets Manager.
    Full SigV4 verification is a follow-up (tracked separately).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.auth.magic_link import (
    issue_token,
    store_nonce,
)
from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.base import new_uuid
from src.shared.models.organization import User
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
    _: None = Depends(_verify_internal_key),
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
    _: None = Depends(_verify_internal_key),
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
    # For GitHub the scope is just the org/installation id provided by the Lambda.
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
