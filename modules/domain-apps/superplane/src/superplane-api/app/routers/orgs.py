"""Organization endpoints — current org details, settings, and SSO configuration."""

import logging
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_session
from app.middleware.auth import get_current_org
from app.models.organization import Organization
from app.schemas.org import (
    VALID_CLOUDS,
    VALID_SSO_PROVIDERS,
    VALID_SSO_TYPES,
    OrgResponse,
    OrgUpdateRequest,
    OrgUpdateResponse,
    SSOConfigRequest,
    SSOConfigResponse,
    SSODisableRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/orgs", tags=["organizations"])


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_org(org_id: uuid.UUID, db: AsyncSession) -> Organization:
    """Fetch the organization or raise 404."""
    result = await db.execute(select(Organization).where(Organization.id == org_id))
    org = result.scalar_one_or_none()
    if org is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Organization not found",
        )
    return org


# ---------------------------------------------------------------------------
# GET /orgs/current
# ---------------------------------------------------------------------------


@router.get("/current", response_model=OrgResponse)
async def get_current_organization(
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> OrgResponse:
    """Return details of the currently authenticated organization.

    The org_id is extracted from the JWT token.
    """
    org = await _get_org(org_id, db)

    return OrgResponse(
        id=org.id,
        name=org.name,
        billing_plan=org.billing_plan,
        quotas_json=org.quotas_json,
        billing_email=org.billing_email,
        allowed_clouds=org.allowed_clouds,
        default_quotas=org.default_quotas,
        sso_enabled=org.sso_enabled,
        created_at=org.created_at,
    )


# ---------------------------------------------------------------------------
# PATCH /orgs/current — update org settings
# ---------------------------------------------------------------------------


@router.patch("/current", response_model=OrgUpdateResponse)
async def update_org_settings(
    body: OrgUpdateRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> OrgUpdateResponse:
    """Update organization settings (name, billing email, allowed clouds, quotas).

    Only provided (non-null) fields are updated. Requires org admin role.
    """
    org = await _get_org(org_id, db)

    # Validate allowed_clouds values
    if body.allowed_clouds is not None:
        invalid = set(body.allowed_clouds) - VALID_CLOUDS
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid cloud providers: {', '.join(sorted(invalid))}. "
                f"Valid options: {', '.join(sorted(VALID_CLOUDS))}",
            )

    # Apply updates (only non-None fields)
    if body.name is not None:
        org.name = body.name
    if body.billing_email is not None:
        org.billing_email = body.billing_email
    if body.allowed_clouds is not None:
        org.allowed_clouds = body.allowed_clouds
    if body.default_quotas is not None:
        org.default_quotas = body.default_quotas
    if body.billing_plan is not None:
        org.billing_plan = body.billing_plan

    await db.commit()
    await db.refresh(org)

    logger.info("Org %s settings updated by admin", org_id)

    return OrgUpdateResponse(
        id=org.id,
        name=org.name,
        billing_plan=org.billing_plan,
        billing_email=org.billing_email,
        allowed_clouds=org.allowed_clouds,
        default_quotas=org.default_quotas,
        sso_enabled=org.sso_enabled,
    )


# ---------------------------------------------------------------------------
# GET /orgs/current/sso — get SSO configuration
# ---------------------------------------------------------------------------


@router.get("/current/sso", response_model=SSOConfigResponse)
async def get_sso_config(
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> SSOConfigResponse:
    """Get the current SSO configuration for the organization."""
    org = await _get_org(org_id, db)

    # Try to fetch live Cognito IdP status (non-blocking)
    cognito_identifier = None
    if org.sso_enabled and org.sso_provider:
        try:
            from app.services.sso import SSOService

            sso_svc = SSOService()
            provider_info = sso_svc.get_provider(org_id)
            if provider_info:
                cognito_identifier = provider_info.get("ProviderName")
        except Exception:
            logger.warning("Could not fetch Cognito IdP status for org %s", org_id)

    return SSOConfigResponse(
        sso_provider=org.sso_provider,
        sso_provider_type=org.sso_provider_type,
        sso_provider_name=org.sso_provider_name,
        sso_metadata_url=org.sso_metadata_url,
        sso_enabled=org.sso_enabled,
        cognito_idp_identifier=cognito_identifier,
    )


# ---------------------------------------------------------------------------
# PATCH /orgs/current/sso — configure SSO provider
# ---------------------------------------------------------------------------


@router.patch("/current/sso", response_model=SSOConfigResponse)
async def configure_sso(
    body: SSOConfigRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> SSOConfigResponse:
    """Configure or update SSO for the organization.

    Registers a SAML or OIDC identity provider in Cognito user pool.
    Team members will then be able to login via corporate IdP.
    """
    # Validate provider
    if body.sso_provider not in VALID_SSO_PROVIDERS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid SSO provider '{body.sso_provider}'. "
            f"Valid options: {', '.join(sorted(VALID_SSO_PROVIDERS))}",
        )

    if body.sso_provider_type not in VALID_SSO_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid SSO type '{body.sso_provider_type}'. "
            f"Valid options: {', '.join(sorted(VALID_SSO_TYPES))}",
        )

    org = await _get_org(org_id, db)
    display_name = body.provider_name or body.sso_provider

    # Register identity provider with Cognito
    cognito_identifier = None
    try:
        from app.services.sso import SSOService

        sso_svc = SSOService()

        if body.sso_provider_type == "SAML":
            cognito_identifier = sso_svc.create_or_update_saml_provider(
                org_id=org_id,
                metadata_url=body.metadata_url,
                display_name=display_name,
            )
        else:
            cognito_identifier = sso_svc.create_or_update_oidc_provider(
                org_id=org_id,
                issuer_url=body.metadata_url,
                display_name=display_name,
            )
    except Exception as e:
        logger.error("SSO configuration failed for org %s: %s", org_id, e)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to configure identity provider with Cognito. Check server logs for details.",
        )

    # Persist SSO config to database
    org.sso_provider = body.sso_provider
    org.sso_provider_type = body.sso_provider_type
    org.sso_provider_name = display_name
    org.sso_metadata_url = body.metadata_url
    org.sso_enabled = body.enable

    await db.commit()
    await db.refresh(org)

    logger.info(
        "SSO configured for org %s: provider=%s type=%s enabled=%s",
        org_id,
        body.sso_provider,
        body.sso_provider_type,
        body.enable,
    )

    return SSOConfigResponse(
        sso_provider=org.sso_provider,
        sso_provider_type=org.sso_provider_type,
        sso_provider_name=org.sso_provider_name,
        sso_metadata_url=org.sso_metadata_url,
        sso_enabled=org.sso_enabled,
        cognito_idp_identifier=cognito_identifier,
    )


# ---------------------------------------------------------------------------
# DELETE /orgs/current/sso — disable and remove SSO
# ---------------------------------------------------------------------------


@router.delete("/current/sso", response_model=SSOConfigResponse)
async def disable_sso(
    body: SSODisableRequest,
    org_id: uuid.UUID = Depends(get_current_org),
    db: AsyncSession = Depends(get_session),
) -> SSOConfigResponse:
    """Disable SSO and remove the identity provider from Cognito.

    Requires explicit confirmation (confirm=true).
    """
    if not body.confirm:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Set confirm=true to disable SSO",
        )

    org = await _get_org(org_id, db)

    # Remove from Cognito
    if org.sso_provider:
        try:
            from app.services.sso import SSOService

            sso_svc = SSOService()
            sso_svc.delete_provider(org_id)
        except Exception as e:
            logger.error("Failed to remove Cognito IdP for org %s: %s", org_id, e)
            # Continue to clear DB even if Cognito cleanup fails
            # (admin can re-run or clean up manually)

    # Clear SSO config in database
    org.sso_provider = None
    org.sso_provider_type = None
    org.sso_provider_name = None
    org.sso_metadata_url = None
    org.sso_enabled = False

    await db.commit()
    await db.refresh(org)

    logger.info("SSO disabled for org %s", org_id)

    return SSOConfigResponse(
        sso_provider=None,
        sso_provider_type=None,
        sso_provider_name=None,
        sso_metadata_url=None,
        sso_enabled=False,
        cognito_idp_identifier=None,
    )
