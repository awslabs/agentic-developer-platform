"""FastAPI routes for vault credential and identity CRUD.

Issue #135: Vault Phase 2a — Credential + Identity CRUD

Endpoints:
  GET    /auth/credentials            — list caller's credentials (metadata only)
  POST   /auth/credentials            — register a new credential
  PATCH  /auth/credentials/{id}       — update label / expires_at / strict
  DELETE /auth/credentials/{id}       — delete DB row + SM secret
  GET    /auth/identities             — list caller's linked identities
  DELETE /auth/identities/{id}        — unlink an identity

All endpoints require Cognito JWT.  Non-owned resources return 404 (no enumeration).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession

from src.shared.database import get_db
from src.shared.services.secrets_manager import SecretsManagerHelper

from .middleware import get_current_user_context
from .vault_schemas import VALID_SCOPES, CredentialCreate, CredentialResponse, CredentialUpdate, IdentityResponse
from .vault_service import (
    CredentialNotFoundError,
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
        cred = await create_credential(data, db, token_context, sm)
        return CredentialResponse.from_model(cred)
    except InsufficientPrivilegesError as exc:
        raise HTTPException(status_code=403, detail={"error": "insufficient_privileges", "message": str(exc)})
    except InvalidScopeConfigError as exc:
        raise HTTPException(status_code=422, detail={"error": "invalid_scope_config", "message": str(exc)})
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
        await unlink_identity(identity_id, db, token_context)
    except IdentityNotFoundError:
        raise HTTPException(status_code=404, detail={"error": "not_found", "message": "Identity not found"})
    except Exception:
        logger.exception("Unexpected error unlinking identity %s", identity_id)
        raise HTTPException(status_code=500, detail={"error": "unlink_failed", "message": "Failed to unlink identity"})
