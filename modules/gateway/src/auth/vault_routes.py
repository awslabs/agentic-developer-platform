"""FastAPI routes for vault credential and identity CRUD, plus magic-link flow.

Issue #135: Vault Phase 2a — Credential + Identity CRUD
Issue #446: Vault Phase 2b — Magic-link identity linking flow

Endpoints:
  GET    /auth/credentials                   — list caller's credentials (metadata only)
  POST   /auth/credentials                   — register a new credential
  PATCH  /auth/credentials/{id}              — update label / expires_at / strict
  DELETE /auth/credentials/{id}              — delete DB row + SM secret
  GET    /auth/identities                    — list caller's linked identities
  DELETE /auth/identities/{id}               — unlink an identity
  POST   /auth/identities/{provider}/link    — issue magic-link for a known signed-in user
  GET    /auth/link/magic                    — magic-link landing page (validate + confirm)

All endpoints require Cognito JWT except GET /auth/link/magic which validates the
token independently (redirects to Cognito if the user is not signed in).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.config import get_settings
from src.shared.database import get_db
from src.shared.models.audit import AuditLog
from src.shared.models.organization import User
from src.shared.models.vault import MagicLinkNonce, UserIdentity
from src.shared.services.secrets_manager import SecretsManagerHelper

from .magic_link import (
    ChannelContextMismatchError,
    NonceAlreadyConsumedError,
    NonceNotFoundError,
    TargetUserMismatchError,
    TokenExpiredError,
    TokenInvalidError,
    consume_nonce,
    issue_token,
    store_nonce,
    verify_token,
)
from .middleware import get_current_user_context
from .vault_schemas import VALID_SCOPES, CredentialCreate, CredentialResponse, CredentialUpdate, IdentityResponse
from .vault_service import (
    CredentialNotFoundError,
    DuplicateCredentialError,
    IdentityNotFoundError,
    InsufficientPrivilegesError,
    InvalidScopeConfigError,
    create_credential,
    delete_credential,
    list_credentials,
    list_identities,
    unlink_identity,
    update_credential,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["vault"])

# Module-level SM helper (real boto3 client); tests replace via dependency injection.
_sm_helper = SecretsManagerHelper()


def get_secrets_manager() -> SecretsManagerHelper:
    """FastAPI dependency that returns the SecretsManagerHelper instance.

    Overridden in tests to inject a mock.
    """
    return _sm_helper


async def _resolve_user_id_in_context(token_context, db: AsyncSession) -> None:
    """Resolve Cognito sub → Postgres users.id and mutate token_context in place.

    TokenContext.user_id holds the Cognito sub for human users (the JWT `sub`
    claim), but user_credentials.user_id is a FK on users.id.  Without this
    resolution, vault_service._visible_credential_filter compares sub vs UUID
    and silently returns zero rows.  Same bug that #567 fixed on the
    aws_connect_routes side — applying it here too.

    Service accounts don't hit this path (their JWT sub IS the users.id).
    If no users row matches (shouldn't happen for registered humans), leave
    the context untouched — the downstream query will return empty, which is
    the safe behavior.
    """
    if token_context.account_type != "human":
        return
    stmt = select(User).where(User.cognito_sub == token_context.user_id)
    result = await db.execute(stmt)
    user = result.scalar_one_or_none()
    if user is not None:
        token_context.user_id = user.id


# ---------------------------------------------------------------------------
# Credential endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/credentials",
    response_model=list[CredentialResponse],
    summary="List credentials visible to the caller",
    description=(
        "Returns credential metadata for all credentials the caller can see. "
        "Secret values and ARNs are never returned. "
        "Optionally filter by scope: user | team | org | domain_app."
    ),
)
async def list_credentials_endpoint(
    scope: str | None = Query(None, description="Filter by scope: user | team | org | domain_app"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[CredentialResponse]:
    if scope is not None and scope not in VALID_SCOPES:
        raise HTTPException(status_code=400, detail={"error": "invalid_scope", "message": f"scope must be one of {VALID_SCOPES}"})

    await _resolve_user_id_in_context(token_context, db)
    creds = await list_credentials(db, token_context, scope_filter=scope)
    return [CredentialResponse.from_model(c) for c in creds]


@router.post(
    "/credentials",
    response_model=CredentialResponse,
    status_code=201,
    summary="Register a new credential",
    description=(
        "Stores the raw value in AWS Secrets Manager and records metadata in the database. "
        "scope_hint defaults to 'user'. Non-user scopes require admin role."
    ),
)
async def create_credential_endpoint(
    data: CredentialCreate,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> CredentialResponse:
    try:
        await _resolve_user_id_in_context(token_context, db)
        cred = await create_credential(data, db, token_context, sm)
        return CredentialResponse.from_model(cred)
    except InsufficientPrivilegesError as exc:
        raise HTTPException(status_code=403, detail={"error": "insufficient_privileges", "message": str(exc)})
    except InvalidScopeConfigError as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_scope_config", "message": str(exc)})
    except DuplicateCredentialError:
        # Service layer already cleaned up the orphaned SM secret (F2) and
        # rolled back the DB session.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "duplicate_credential",
                "message": "A credential with this service and label already exists. Delete it first to re-register.",
            },
        )
    except Exception:
        logger.exception("Unexpected error creating credential for user=%s", token_context.user_id)
        raise HTTPException(status_code=500, detail={"error": "create_failed", "message": "Failed to create credential"})


@router.patch(
    "/credentials/{credential_id}",
    response_model=CredentialResponse,
    summary="Update credential metadata",
    description=("Update label, expires_at, or strict. The value cannot be changed via PATCH — delete and re-register for audit clarity."),
)
async def update_credential_endpoint(
    credential_id: str = Path(..., description="Credential ID"),
    data: CredentialUpdate = ...,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> CredentialResponse:
    try:
        await _resolve_user_id_in_context(token_context, db)
        cred = await update_credential(credential_id, data, db, token_context)
        return CredentialResponse.from_model(cred)
    except CredentialNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Credential not found"})
    except Exception:
        logger.exception("Unexpected error updating credential %s", credential_id)
        raise HTTPException(status_code=500, detail={"error": "update_failed", "message": "Failed to update credential"})


@router.delete(
    "/credentials/{credential_id}",
    status_code=204,
    response_model=None,
    summary="Delete a credential",
    description=(
        "Synchronously deletes the database row and the AWS Secrets Manager secret. Returns 204 on success, 404 if not found or not owned by caller."
    ),
)
async def delete_credential_endpoint(
    credential_id: str = Path(..., description="Credential ID"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
    sm: SecretsManagerHelper = Depends(get_secrets_manager),
) -> None:
    try:
        await _resolve_user_id_in_context(token_context, db)
        await delete_credential(credential_id, db, token_context, sm)
    except CredentialNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Credential not found"})
    except Exception:
        logger.exception("Unexpected error deleting credential %s", credential_id)
        raise HTTPException(status_code=500, detail={"error": "delete_failed", "message": "Failed to delete credential"})


# ---------------------------------------------------------------------------
# Identity endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/identities",
    response_model=list[IdentityResponse],
    summary="List linked identities",
    description="Returns all external identities (Slack, GitHub, etc.) linked to the caller.",
)
async def list_identities_endpoint(
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[IdentityResponse]:
    await _resolve_user_id_in_context(token_context, db)
    identities = await list_identities(db, token_context)
    return [IdentityResponse.from_model(i) for i in identities]


@router.delete(
    "/identities/{identity_id}",
    status_code=204,
    response_model=None,
    summary="Unlink an identity",
    description=("Removes the user_identities row (unlinks the external identity). Returns 204 on success, 404 if not found or not owned by caller."),
)
async def unlink_identity_endpoint(
    identity_id: str = Path(..., description="Identity ID"),
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    try:
        await _resolve_user_id_in_context(token_context, db)
        await unlink_identity(identity_id, db, token_context)
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Identity not found"})
    except Exception:
        logger.exception("Unexpected error unlinking identity %s", identity_id)
        raise HTTPException(status_code=500, detail={"error": "unlink_failed", "message": "Failed to unlink identity"})


# ---------------------------------------------------------------------------
# Magic-link issuance (Cognito-authed user adds a new identity they own)
# ---------------------------------------------------------------------------


class MagicLinkIssueRequest(BaseModel):
    """Body for POST /auth/identities/{provider}/link."""

    provider_user_id: str
    channel_context: str | None = None


class MagicLinkIssueResponse(BaseModel):
    magic_link_url: str


def _get_magic_link_secret() -> str:
    settings = get_settings()
    return settings.magic_link_secret or settings.token_secret_key


def _build_magic_link_url(token: str) -> str:
    settings = get_settings()
    base = settings.gateway_base_url.rstrip("/")
    return f"{base}/auth/link/magic?token={token}"


async def _append_audit(
    db: AsyncSession,
    *,
    event_type: str,
    org_id: str,
    actor_id: str | None,
    details: dict | None,
) -> None:
    log = AuditLog(org_id=org_id, event_type=event_type, actor_id=actor_id, details=details)
    db.add(log)


@router.post(
    "/identities/{provider}/link",
    response_model=MagicLinkIssueResponse,
    status_code=201,
    summary="Issue a magic-link to add a new identity",
    description=(
        "Cognito-authed users call this to obtain a magic-link URL they can share "
        "with their other-channel identity (e.g. a Slack bot DM).  "
        "The token binds to the caller's Cognito user_id so the landing page "
        "will reject a different signed-in user."
    ),
)
async def issue_identity_magic_link(
    provider: str = Path(..., description="Identity provider: slack | github | whatsapp | discord"),
    body: MagicLinkIssueRequest = ...,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> MagicLinkIssueResponse:
    secret = _get_magic_link_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={"error": "not_configured", "message": "Magic-link signing key not configured"},
        )

    result = issue_token(
        provider=provider,
        provider_user_id=body.provider_user_id,
        channel_context=body.channel_context,
        target_user_id=token_context.user_id,
        secret_key=secret,
    )

    await store_nonce(
        jti=result["jti"],
        provider=provider,
        provider_user_id=body.provider_user_id,
        channel_context=body.channel_context,
        target_user_id=token_context.user_id,
        expires_at=result["expires_at"],
        db=db,
    )

    magic_link_url = _build_magic_link_url(result["token"])

    await _append_audit(
        db,
        event_type="magic_link_issued",
        org_id=token_context.org_id,
        actor_id=token_context.user_id,
        details={
            "provider": provider,
            "provider_user_id": body.provider_user_id,
            "channel_context": body.channel_context,
            "jti": result["jti"],
            "source": "user_initiated",
        },
    )
    await db.commit()

    logger.info(
        "Magic-link issued by user=%s provider=%s provider_user_id=%s jti=%s",
        token_context.user_id,
        provider,
        body.provider_user_id,
        result["jti"],
    )
    return MagicLinkIssueResponse(magic_link_url=magic_link_url)


# ---------------------------------------------------------------------------
# Magic-link landing page
# ---------------------------------------------------------------------------


@router.get(
    "/link/magic",
    summary="Magic-link landing page — validate token and confirm identity link",
    description=(
        "Validates the signed token.  If the user is not Cognito-authenticated, "
        "redirects to the Cognito hosted UI (which loops back here after login).  "
        "Returns JSON with confirmation details when authenticated but not yet confirmed.\n\n"
        "POST to this endpoint (with the same ?token= param) to confirm the link."
    ),
    responses={
        200: {"description": "Token valid; returns confirmation details"},
        302: {"description": "Redirect to Cognito for login"},
        400: {"description": "Token expired, invalid, or already consumed"},
        403: {"description": "Logged-in user does not match token target_user_id"},
    },
)
async def magic_link_landing_get(
    request: Request,
    token: str,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Validate the token and return confirmation details.

    The caller is already Cognito-authenticated (FastAPI dependency ensures it).
    If the user is *not* authenticated, the ``get_current_user_context`` dependency
    raises a 401 which the frontend should intercept and redirect to Cognito,
    then loop back to this URL.
    """
    secret = _get_magic_link_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={"error": "not_configured", "message": "Magic-link signing key not configured"},
        )

    try:
        payload = verify_token(token, secret)
    except TokenExpiredError:
        raise HTTPException(status_code=400, detail={"error": "token_expired", "message": "Magic-link token has expired"})
    except TokenInvalidError as exc:
        raise HTTPException(status_code=400, detail={"error": "token_invalid", "message": str(exc)})

    # Verify nonce exists and is not consumed (do not consume yet — just peek)
    jti = payload["jti"]
    stmt = select(MagicLinkNonce).where(MagicLinkNonce.jti == jti)
    result = await db.execute(stmt)
    nonce = result.scalar_one_or_none()

    if nonce is None:
        raise HTTPException(status_code=400, detail={"error": "token_invalid", "message": "Token nonce not found"})
    if nonce.consumed_at is not None:
        raise HTTPException(status_code=400, detail={"error": "token_already_used", "message": "Magic-link has already been used"})
    if nonce.expires_at.replace(tzinfo=UTC) < datetime.now(UTC):
        raise HTTPException(status_code=400, detail={"error": "token_expired", "message": "Magic-link token has expired"})

    # target_user_id check
    if payload.get("target_user_id") and payload["target_user_id"] != token_context.user_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "user_mismatch",
                "message": ("This magic-link was issued for a different user. Please sign in as the correct account to proceed."),
            },
        )

    return {
        "status": "pending_confirmation",
        "provider": payload["provider"],
        "provider_user_id": payload["provider_user_id"],
        "channel_context": payload.get("channel_context"),
        "linking_to_user": token_context.user_id,
        "jti": jti,
    }


@router.post(
    "/link/magic",
    summary="Confirm magic-link identity linking",
    status_code=201,
    description=(
        "Consumes the nonce and writes the user_identities row.  "
        "Returns 201 on success with the new identity record.  "
        "Replays (nonce already consumed) return 400.  "
        "Wrong signed-in user returns 403."
    ),
    responses={
        201: {"description": "Identity linked successfully"},
        400: {"description": "Token expired, invalid, or already used"},
        403: {"description": "Logged-in user does not match token target"},
        409: {"description": "Provider identity already linked to another user"},
    },
)
async def magic_link_landing_post(
    token: str,
    token_context=Depends(get_current_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Confirm the link: consume the nonce and write the user_identities row."""
    secret = _get_magic_link_secret()
    if not secret:
        raise HTTPException(
            status_code=503,
            detail={"error": "not_configured", "message": "Magic-link signing key not configured"},
        )

    try:
        payload = verify_token(token, secret)
    except TokenExpiredError:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "token_expired", "token_preview": token[:20]},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "token_expired", "message": "Magic-link token has expired"})
    except TokenInvalidError as exc:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "token_invalid", "error": str(exc)},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "token_invalid", "message": str(exc)})

    jti = payload["jti"]
    provider = payload["provider"]
    provider_user_id = payload["provider_user_id"]
    channel_context = payload.get("channel_context")

    try:
        await consume_nonce(
            jti=jti,
            channel_context=channel_context,
            consuming_user_id=token_context.user_id,
            db=db,
        )
    except TokenExpiredError:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "nonce_expired", "jti": jti},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "token_expired", "message": "Magic-link token has expired"})
    except NonceAlreadyConsumedError:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "replay", "jti": jti},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "token_already_used", "message": "Magic-link has already been used"})
    except NonceNotFoundError:
        raise HTTPException(status_code=400, detail={"error": "token_invalid", "message": "Token nonce not found"})
    except ChannelContextMismatchError as exc:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={"reason": "channel_context_mismatch", "jti": jti, "error": str(exc)},
        )
        await db.commit()
        raise HTTPException(status_code=400, detail={"error": "channel_context_mismatch", "message": str(exc)})
    except TargetUserMismatchError as exc:
        await _append_audit(
            db,
            event_type="magic_link_failed",
            org_id=token_context.org_id,
            actor_id=token_context.user_id,
            details={
                "reason": "target_user_mismatch",
                "jti": jti,
                "target": payload.get("target_user_id"),
                "consumer": token_context.user_id,
            },
        )
        await db.commit()
        raise HTTPException(status_code=403, detail={"error": "user_mismatch", "message": str(exc)})

    # Fetch the user row to get team_id (required by UserIdentity)
    user_stmt = select(User).where(User.id == token_context.user_id)
    user_result = await db.execute(user_stmt)
    user = user_result.scalar_one_or_none()
    team_id = user.team_id if user else ""

    # Write user_identities row — 409 on duplicate (UNIQUE constraint)
    identity = UserIdentity(
        org_id=token_context.org_id,
        user_id=token_context.user_id,
        team_id=team_id,
        provider=provider,
        provider_user_id=provider_user_id,
        provider_username=None,
        verification_method="magic_link",
        verified_at=datetime.now(UTC),
    )
    db.add(identity)

    await _append_audit(
        db,
        event_type="magic_link_consumed",
        org_id=token_context.org_id,
        actor_id=token_context.user_id,
        details={
            "jti": jti,
            "provider": provider,
            "provider_user_id": provider_user_id,
            "channel_context": channel_context,
        },
    )
    await _append_audit(
        db,
        event_type="identity_linked",
        org_id=token_context.org_id,
        actor_id=token_context.user_id,
        details={
            "provider": provider,
            "provider_user_id": provider_user_id,
            "verification_method": "magic_link",
        },
    )

    try:
        await db.commit()
        await db.refresh(identity)
    except Exception as exc:
        # UNIQUE constraint violation — provider identity already linked
        logger.warning("Identity already linked provider=%s provider_user_id=%s: %s", provider, provider_user_id, exc)
        raise HTTPException(
            status_code=409,
            detail={
                "error": "identity_already_linked",
                "message": f"Provider identity {provider}:{provider_user_id} is already linked to a user.",
            },
        )

    logger.info(
        "Identity linked via magic-link user=%s provider=%s provider_user_id=%s",
        token_context.user_id,
        provider,
        provider_user_id,
    )
    return {
        "status": "linked",
        "identity_id": identity.id,
        "provider": provider,
        "provider_user_id": provider_user_id,
        "verification_method": "magic_link",
        "verified_at": identity.verified_at.isoformat() if identity.verified_at else None,
    }
