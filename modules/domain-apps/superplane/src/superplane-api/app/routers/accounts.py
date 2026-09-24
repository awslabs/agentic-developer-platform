"""Account onboarding + vault credential CRUD endpoints — scoped to org_id from JWT."""

import json
import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.cloud_account import CloudAccount
from app.models.credential import (
    ClusterVaultAssignment,
    CredentialAuditLog,
    CredentialRegistry,
)
from app.models.provider_connection import STATUS_DISABLED, ProviderConnection
from app.schemas.account import (
    AccountDeleteResponse,
    AccountListResponse,
    AccountResponse,
    CredentialDeleteResponse,
    CredentialListResponse,
    CredentialResponse,
    RegisterAccountRequest,
    RegisterCredentialRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["accounts"])


# ── Helpers ──


def _account_to_response(acct: CloudAccount) -> AccountResponse:
    """Convert a CloudAccount model to the API response schema."""
    adp_credential_ids = (
        json.loads(acct.adp_credential_ids_json) if acct.adp_credential_ids_json else []
    )
    irsa_role_arns = (
        json.loads(acct.irsa_role_arns_json) if acct.irsa_role_arns_json else []
    )
    return AccountResponse(
        id=acct.id,
        org_id=acct.org_id,
        name=acct.friendly_name,
        provider=acct.provider,
        account_id=acct.account_identifier,
        status=acct.status,
        adp_credential_ids=adp_credential_ids,
        irsa_role_arns=irsa_role_arns,
        created_at=acct.created_at,
        updated_at=acct.updated_at,
    )


def _credential_to_response(cred: CredentialRegistry) -> CredentialResponse:
    """Convert a CredentialRegistry model to the API response schema."""
    return CredentialResponse(
        id=cred.id,
        org_id=cred.org_id,
        name=cred.friendly_name,
        provider=cred.provider,
        credential_type=cred.credential_type,
        adp_credential_id=cred.adp_credential_id,
        status=cred.status,
        created_at=cred.created_at,
        updated_at=cred.updated_at,
    )


# ── Account Endpoints ──


async def _registered_account_for_request(
    db: AsyncSession,
    org_id: uuid.UUID,
    body: RegisterAccountRequest,
) -> CloudAccount | None:
    existing = await db.scalar(
        select(CloudAccount).where(
            CloudAccount.org_id == org_id,
            CloudAccount.account_identifier == body.account_id,
        )
    )
    if existing is None:
        return None
    adp_credential_ids = (
        json.loads(existing.adp_credential_ids_json)
        if existing.adp_credential_ids_json
        else []
    )
    irsa_role_arns = (
        json.loads(existing.irsa_role_arns_json) if existing.irsa_role_arns_json else []
    )
    if (
        existing.status == "Active"
        and existing.provider == body.provider
        and existing.friendly_name == body.name
        and existing.cross_account_role_arn == body.role_arn
        and existing.external_id == body.external_id
        and existing.ingest_role_arn == body.ingest_role_arn
        and adp_credential_ids == body.adp_credential_ids
        and irsa_role_arns == body.irsa_role_arns
    ):
        return existing
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Account is already registered with different connection metadata",
    )


@router.post(
    "/accounts", response_model=AccountResponse, status_code=status.HTTP_201_CREATED
)
async def register_account(
    body: RegisterAccountRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> AccountResponse:
    """Register a BYOA cloud account.

    ADP resolves an authorized vault reference into the role metadata before this
    endpoint. Identical retries converge on one account record; conflicting
    metadata for the same tenant and account remains a conflict.
    """
    existing = await _registered_account_for_request(db, org_id, body)
    if existing is not None:
        return _account_to_response(existing)

    account = CloudAccount(
        org_id=org_id,
        provider=body.provider,
        account_identifier=body.account_id,
        friendly_name=body.name,
        provisioning_mode="customer_onboarded",
        cross_account_role_arn=body.role_arn,
        external_id=body.external_id,
        ingest_role_arn=body.ingest_role_arn,
        adp_credential_ids_json=json.dumps(body.adp_credential_ids)
        if body.adp_credential_ids
        else None,
        irsa_role_arns_json=json.dumps(body.irsa_role_arns)
        if body.irsa_role_arns
        else None,
        status="Active",
    )
    db.add(account)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await _registered_account_for_request(db, org_id, body)
        if existing is None:
            raise
        return _account_to_response(existing)
    await db.refresh(account)

    logger.info(
        "Registered BYOA account %s (AWS %s) for org %s",
        account.id,
        body.account_id,
        org_id,
    )
    return _account_to_response(account)


@router.get("/accounts", response_model=AccountListResponse)
async def list_accounts(
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> AccountListResponse:
    """List all registered cloud accounts for the organization."""
    result = await db.execute(
        select(CloudAccount)
        .where(CloudAccount.org_id == org_id)
        .order_by(CloudAccount.created_at.desc())
    )
    accounts = result.scalars().all()
    return AccountListResponse(
        accounts=[_account_to_response(a) for a in accounts],
        total=len(accounts),
    )


@router.delete("/accounts/{account_id}", response_model=AccountDeleteResponse)
async def delete_account(
    account_id: uuid.UUID,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> AccountDeleteResponse:
    """Deregister a cloud account.

    This removes the account registration from Superplane — it does NOT
    delete IAM roles or secrets in the user's AWS account.
    """
    result = await db.execute(
        select(CloudAccount).where(
            CloudAccount.id == account_id, CloudAccount.org_id == org_id
        )
    )
    account = result.scalar_one_or_none()

    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Account not found"
        )

    await db.delete(account)
    await db.commit()

    logger.info("Deregistered account %s for org %s", account_id, org_id)
    return AccountDeleteResponse(id=account_id)


# ── Vault Credential Endpoints ──


async def _registered_credential_for_reference(
    db: AsyncSession,
    org_id: uuid.UUID,
    body: RegisterCredentialRequest,
) -> CredentialRegistry | None:
    result = await db.execute(
        select(CredentialRegistry).where(
            CredentialRegistry.org_id == org_id,
            CredentialRegistry.adp_credential_id == body.adp_credential_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing is None:
        return None
    if (
        existing.status == "Active"
        and existing.provider == body.provider
        and existing.friendly_name == body.name
        and existing.credential_type == body.credential_type
    ):
        return existing
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail="Credential reference is already registered with different metadata",
    )


@router.post(
    "/vault/credentials",
    response_model=CredentialResponse,
    status_code=status.HTTP_201_CREATED,
)
async def register_credential(
    body: RegisterCredentialRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> CredentialResponse:
    """Register an ADP credential reference.

    Issue #5046 (U13b): the stored reference is an **ADP credential ID** — an opaque
    handle only the ADP vault can resolve — not a Secrets Manager ARN. The credential
    value never reaches Superplane, and neither does the address of the secret holding it,
    so vault rotation and revocation remain the single control point.

    The route accepts a reference that is well-formed. It does NOT establish that the
    reference resolves: that ADP owns the credential and can read it under the relevant
    account and KMS permissions is verified by the audited vault-owned migration and the
    ADP-side client contract (U7), not here.
    """
    existing = await _registered_credential_for_reference(db, org_id, body)
    if existing is not None:
        return _credential_to_response(existing)

    credential = CredentialRegistry(
        org_id=org_id,
        provider=body.provider,
        friendly_name=body.name,
        credential_type=body.credential_type,
        adp_credential_id=body.adp_credential_id,
        status="Active",
    )
    db.add(credential)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        existing = await _registered_credential_for_reference(db, org_id, body)
        if existing is not None:
            return _credential_to_response(existing)
        raise
    await db.refresh(credential)

    logger.info(
        "Registered credential %s (%s) for org %s", credential.id, body.name, org_id
    )
    return _credential_to_response(credential)


@router.get("/vault/credentials", response_model=CredentialListResponse)
async def list_credentials(
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> CredentialListResponse:
    """List all registered credentials for the organization."""
    result = await db.execute(
        select(CredentialRegistry)
        .where(
            CredentialRegistry.org_id == org_id,
            CredentialRegistry.status != "Deregistered",
        )
        .order_by(CredentialRegistry.created_at.desc())
    )
    credentials = result.scalars().all()
    return CredentialListResponse(
        credentials=[_credential_to_response(c) for c in credentials],
        total=len(credentials),
    )


@router.delete(
    "/vault/credentials/{credential_id}", response_model=CredentialDeleteResponse
)
async def delete_credential(
    credential_id: uuid.UUID,
    request: Request,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> CredentialDeleteResponse:
    """Deregister a credential.

    Retain the reference and audit history as a tombstone. Active connections and
    cluster assignments must be disabled/replaced or detached first. This does not
    revoke the credential in ADP's vault or at the provider.
    """
    result = await db.execute(
        select(CredentialRegistry)
        .where(
            CredentialRegistry.id == credential_id,
            CredentialRegistry.org_id == org_id,
            CredentialRegistry.status != "Deregistered",
        )
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    credential = result.scalar_one_or_none()

    if credential is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Credential not found"
        )

    connection = (
        await db.execute(
            select(ProviderConnection.id)
            .where(
                ProviderConnection.org_id == org_id,
                ProviderConnection.adp_credential_id == credential.adp_credential_id,
                ProviderConnection.status != STATUS_DISABLED,
            )
            .limit(1)
        )
    ).first()
    assignment = (
        await db.execute(
            select(ClusterVaultAssignment.id)
            .where(
                ClusterVaultAssignment.credential_registry_id == credential_id,
            )
            .limit(1)
        )
    ).first()
    if connection or assignment:
        raise HTTPException(
            status_code=409,
            detail="credential is still in use; disable or replace its connections and detach cluster assignments first",
        )
    credential.status = "Deregistered"
    caller = getattr(request.state, "caller", None)
    db.add(
        CredentialAuditLog(
            org_id=org_id,
            credential_registry_id=credential_id,
            accessed_by=caller.principal.subject if caller else None,
            action="Deregistered",
        )
    )
    await db.commit()

    logger.info("Deregistered credential %s for org %s", credential_id, org_id)
    return CredentialDeleteResponse(id=credential_id)
